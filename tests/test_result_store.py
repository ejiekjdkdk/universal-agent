"""验证完整结果的加密、所有权、有效期、分页与持久化边界。"""
import importlib
import json

import pytest

from app.errors import Fault
from app.store import Store


ACTOR = {"tenant_id": "one", "org_id": "org", "user_id": "reader"}
BODY = {"business_id": "notes", "message": "查询", "mode": "mock", "context": {}}
RESULT = {"execution_id": "exec_one", "capability_id": "read_notes", "capability_version": "1.0.0",
          "status": "completed", "data": {"summary": "私有摘要", "details": "原始敏感明细" * 1200}, "evidence": []}
PROVENANCE = {"permission": "notes.read", "arguments": {"text": "查询"}, "snapshot_id": "snapshot-one"}


def module():
    try:
        return importlib.import_module("app.result_store")
    except ModuleNotFoundError:
        pytest.fail("尚未实现加密ResultStore")


@pytest.fixture
def result_store(tmp_path, monkeypatch):
    monkeypatch.setenv("P2_TEST_KEY", "12" * 32)
    store = Store(tmp_path / "agent.db")
    task, _ = store.start(ACTOR, BODY, None, "1.0.0")
    settings = {"keys": {"one": "P2_TEST_KEY"}, "active_key": "one", "page_bytes": 1024}
    results = module().ResultStore(store, settings)
    yield results, store, task["task_id"], settings
    store.close()


def allow(meta, provenance):
    assert provenance == PROVENANCE


def test_encrypted_roundtrip_and_pages_preserve_original(result_store):
    results, store, task_id, _ = result_store
    ref = results.put(task_id, RESULT, PROVENANCE)
    row = store._db.execute("SELECT * FROM tool_results WHERE result_ref=?", (ref,)).fetchone()
    assert "原始敏感明细".encode() not in row["ciphertext"]
    assert "私有摘要" not in row["metadata"] and "查询" not in row["metadata"]
    assert results.load(ref)["result"] == RESULT
    metadata = results.metadata(ref, ACTOR, allow)
    assert metadata["result_ref"] == ref and metadata["summary"] == "私有摘要"
    assert metadata["expires_at"] - metadata["created_at"] == 86400
    cursor = None
    parts = []
    while True:
        page = results.page(ref, cursor, ACTOR, allow)
        assert len(page["content"].encode()) <= 1024
        parts.append(page["content"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert json.loads("".join(parts)) == RESULT


@pytest.mark.parametrize("field", ["tenant_id", "org_id", "user_id"])
def test_foreign_owner_rejected_before_authorization_callback(result_store, field):
    results, _, task_id, _ = result_store
    ref = results.put(task_id, RESULT, PROVENANCE)
    calls = []
    with pytest.raises(Fault) as denied:
        results.page(ref, None, {**ACTOR, field: "outsider"}, lambda *args: calls.append(args))
    assert denied.value.code == "RESULT_NOT_FOUND" and not calls


def test_each_page_and_metadata_rechecks_live_authorization(result_store):
    results, _, task_id, _ = result_store
    ref = results.put(task_id, RESULT, PROVENANCE)
    cursor = results.page(ref, None, ACTOR, allow)["next_cursor"]

    def revoked(*args):
        raise Fault("PERMISSION_DENIED", "权限已撤销", 403)

    for operation in (lambda: results.page(ref, cursor, ACTOR, revoked),
                      lambda: results.metadata(ref, ACTOR, revoked)):
        with pytest.raises(Fault, match="权限已撤销"):
            operation()


@pytest.mark.parametrize("field", ["ciphertext", "metadata"])
def test_corrupted_ciphertext_and_metadata_cannot_be_read(result_store, field):
    results, store, task_id, _ = result_store
    ref = results.put(task_id, RESULT, PROVENANCE)
    row = store._db.execute("SELECT ciphertext,metadata FROM tool_results WHERE result_ref=?", (ref,)).fetchone()
    with store._transaction():
        if field == "ciphertext":
            store._db.execute("UPDATE tool_results SET ciphertext=? WHERE result_ref=?", (b"x" + row[0][1:], ref))
        else:
            metadata = json.loads(row[1])
            metadata["created_at"] -= 1
            store._db.execute("UPDATE tool_results SET metadata=? WHERE result_ref=?", (json.dumps(metadata), ref))
    with pytest.raises(Fault) as invalid:
        results.load(ref)
    assert invalid.value.code == "RESULT_INTEGRITY_ERROR"


def test_row_swap_and_forged_cursor_rejected(result_store):
    results, store, task_id, _ = result_store
    first = results.put(task_id, RESULT, PROVENANCE)
    second = results.put(task_id, {**RESULT, "execution_id": "exec_two"}, PROVENANCE)
    cursor = results.page(first, None, ACTOR, allow)["next_cursor"]
    for forged in (cursor[:-1] + ("x" if cursor[-1] != "x" else "y"), cursor + "x"):
        with pytest.raises(Fault):
            results.page(first, forged, ACTOR, allow)
    with pytest.raises(Fault):
        results.page(second, cursor, ACTOR, allow)
    row = store._db.execute("SELECT nonce,ciphertext FROM tool_results WHERE result_ref=?", (first,)).fetchone()
    with store._transaction():
        store._db.execute("UPDATE tool_results SET nonce=?,ciphertext=? WHERE result_ref=?", (*row, second))
    with pytest.raises(Fault) as invalid:
        results.load(second)
    assert invalid.value.code == "RESULT_INTEGRITY_ERROR"


def test_expired_and_deleted_results_are_unavailable(result_store, monkeypatch):
    results, store, task_id, _ = result_store
    ref = results.put(task_id, RESULT, PROVENANCE)
    now = module().time.time()
    monkeypatch.setattr(module().time, "time", lambda: now + 86401)
    with pytest.raises(Fault) as expired:
        results.metadata(ref, ACTOR, allow)
    assert expired.value.code == "RESULT_EXPIRED"
    assert results.purge() == 1
    monkeypatch.setattr(module().time, "time", lambda: now)
    ref = results.put(task_id, {**RESULT, "execution_id": "exec_new"}, PROVENANCE)
    results.revoke(ref, ACTOR, allow)
    with pytest.raises(Fault):
        results.load(ref)
    assert store._db.execute("SELECT COUNT(*) FROM tool_results").fetchone()[0] == 0


def test_restart_and_rotation_keep_old_key_readable(result_store, monkeypatch):
    results, store, task_id, settings = result_store
    ref = results.put(task_id, RESULT, PROVENANCE)
    monkeypatch.setenv("P2_SECOND_KEY", "34" * 32)
    rotated = {**settings, "keys": {**settings["keys"], "two": "P2_SECOND_KEY"}, "active_key": "two"}
    restarted = module().ResultStore(store, rotated)
    assert restarted.load(ref)["result"] == RESULT
    other = restarted.put(task_id, {**RESULT, "execution_id": "exec_two"}, PROVENANCE)
    assert restarted.load(other)["result"]["execution_id"] == "exec_two"
    monkeypatch.setenv("P2_TEST_KEY", "56" * 32)
    wrong_key = module().ResultStore(store, settings)
    with pytest.raises(Fault) as invalid:
        wrong_key.load(ref)
    assert invalid.value.code == "RESULT_INTEGRITY_ERROR"


def test_reopened_database_preserves_ciphertext_and_ttl(result_store):
    results, store, task_id, settings = result_store
    ref = results.put(task_id, RESULT, PROVENANCE)
    path = store._db.execute("PRAGMA database_list").fetchone()[2]
    store.close()
    reopened = Store(path, recover_reactive=False)
    try:
        restored = module().ResultStore(reopened, settings)
        assert restored.load(ref)["result"] == RESULT
        assert restored.metadata(ref, ACTOR, allow)["result_ref"] == ref
    finally:
        reopened.close()


def test_retention_starts_at_task_completion(result_store, monkeypatch):
    results, _, task_id, _ = result_store
    ref = results.put(task_id, RESULT, PROVENANCE)
    created = results.load(ref)["metadata"]["created_at"]
    monkeypatch.setattr(module().time, "time", lambda: created + 120)
    results.finish_task(task_id)
    record = results.load(ref)
    assert record["metadata"]["expires_at"] == created + 120 + 86400
    assert record["result"] == RESULT


def test_quota_rejection_keeps_existing_result(result_store):
    results, store, task_id, settings = result_store
    ref = results.put(task_id, RESULT, PROVENANCE)
    bounded = module().ResultStore(store, {**settings, "quota_bytes": 1024})
    small = {**RESULT, "execution_id": "another", "data": {"summary": "短结果"}}
    with pytest.raises(Fault) as full:
        bounded.put(task_id, small, PROVENANCE)
    assert full.value.code == "RESULT_QUOTA_EXCEEDED"
    assert results.load(ref)["result"] == RESULT


def test_limits_and_idempotent_storage(result_store):
    results, store, task_id, settings = result_store
    first = results.put(task_id, RESULT, PROVENANCE)
    assert results.put(task_id, RESULT, PROVENANCE) == first
    with pytest.raises(Fault):
        results.put(task_id, {**RESULT, "data": {"summary": "变更"}}, PROVENANCE)
    tiny = module().ResultStore(store, {**settings, "max_bytes": 1024, "quota_bytes": 2048})
    with pytest.raises(Fault):
        tiny.put(task_id, {**RESULT, "execution_id": "too-large"}, PROVENANCE)
    nested = "leaf"
    for _ in range(40):
        nested = {"child": nested}
    with pytest.raises(Fault):
        results.put(task_id, {**RESULT, "execution_id": "deep", "data": nested}, PROVENANCE)


def test_missing_key_rejects_configuration(result_store, monkeypatch):
    _, store, _, settings = result_store
    monkeypatch.delenv("P2_TEST_KEY")
    with pytest.raises(ValueError):
        module().ResultStore(store, settings)


def test_transaction_rollback_does_not_leave_orphan(result_store):
    results, store, task_id, _ = result_store
    with pytest.raises(RuntimeError):
        with store._transaction():
            results.put(task_id, RESULT, PROVENANCE)
            raise RuntimeError("模拟调用完成前失去租约")
    assert store._db.execute("SELECT COUNT(*) FROM tool_results").fetchone()[0] == 0
