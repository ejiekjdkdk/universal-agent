"""加密引用不能改变持久计划的依赖值、租约或已完成调用恢复规则。"""
import json

import pytest

from app.errors import Fault
from app.result_store import ResultStore
from app.store import Store


ACTOR = {"tenant_id": "tenant", "org_id": "org", "user_id": "reader"}
BUDGET = {"max_parallel_steps": 1, "max_steps": 4, "max_tool_calls": 4, "deadline_ms": 60000}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("RECOVERY_RESULT_KEY", "73" * 32)
    store = Store(tmp_path / "agent.db", recover_reactive=False)
    results = ResultStore(store, {"keys": {"local": "RECOVERY_RESULT_KEY"}, "active_key": "local"})
    store._result_store = results
    steps = [{"step_id": "first", "capability_id": "read", "arguments": {"text": "初始"}, "depends_on": [], "required": True},
             {"step_id": "second", "capability_id": "read", "arguments": {}, "depends_on": ["first"], "required": True}]
    body = {"business_id": "notes", "message": "查询", "execution_mode": "durable_plan"}
    plan = {"schema_version": 1, "result_contract": "summary_v1", "steps": steps}
    task, _ = store.start(ACTOR, body, None, "1", plan=plan, snapshot_id="snapshot", budget=BUDGET)
    yield store, results, task["task_id"]
    store.close()


def complete(store, results, claim, text):
    call_id = "plan." + claim["step_id"]
    execution, cached = store.begin_call(claim["task_id"], call_id, "read", "1", {}, lease=claim)
    assert cached is None
    result = {"execution_id": execution, "capability_id": "read", "capability_version": "1",
              "status": "completed", "data": {"summary": text, "details": "仅供依赖读取的明细"}, "evidence": []}
    with store._transaction():
        ref = results.put(claim["task_id"], result, {"snapshot_id": "snapshot"})
        store.complete_call(claim["task_id"], call_id, result, lease=claim)
    return result, ref


def test_step_outputs_are_encrypted_but_dependencies_and_summary_are_complete(setup):
    store, results, task_id = setup
    first = store.claim_step("worker", 10)
    result, _ = complete(store, results, first, "第一步")
    assert store.settle_step(first, result=result)
    raw = store._db.execute("SELECT output FROM plan_steps WHERE task_id=? AND step_id='first'", (task_id,)).fetchone()[0]
    assert set(json.loads(raw)) == {"_encrypted_result"}
    second = store.claim_step("worker", 10)
    assert second["outputs"]["first"] == result
    final, _ = complete(store, results, second, "第二步")
    assert store.settle_step(second, result=final)
    assert store.task(ACTOR, task_id)["answer"] == "第二步"
    assert store.task(ACTOR, task_id)["status"] == "completed"


@pytest.mark.parametrize("failure", ["deleted", "disabled", "corrupt"])
def test_unavailable_predecessor_fails_task_without_reexecuting(setup, failure):
    store, results, task_id = setup
    first = store.claim_step("worker", 10)
    result, ref = complete(store, results, first, "第一步")
    store.settle_step(first, result=result)
    with store._transaction():
        if failure == "deleted":
            store._db.execute("DELETE FROM tool_results WHERE result_ref=?", (ref,))
        elif failure == "corrupt":
            store._db.execute("UPDATE tool_results SET ciphertext=? WHERE result_ref=?", (b"invalid", ref))
        else:
            store._result_store = None
    assert store.claim_step("next-worker", 10) is None
    task = store.task(ACTOR, task_id)
    assert task["status"] == "failed" and task["error"]["code"].startswith("RESULT_")
    assert len(task["tool_calls"]) == 1


def test_completed_call_recovery_requires_original_result_and_lease(setup):
    store, results, task_id = setup
    claim = store.claim_step("worker", 10)
    result, ref = complete(store, results, claim, "已完成")
    execution, cached = store.begin_call(task_id, "plan.first", "read", "1", {}, lease=claim)
    assert execution == result["execution_id"] and cached == result
    with pytest.raises(Fault) as stale:
        store.complete_call(task_id, "plan.first", result, lease={**claim, "token": claim["token"] - 1})
    assert stale.value.code == "LEASE_LOST"
    with store._transaction():
        store._db.execute("DELETE FROM tool_results WHERE result_ref=?", (ref,))
    with pytest.raises(Fault) as unavailable:
        store.begin_call(task_id, "plan.first", "read", "1", {}, lease=claim)
    assert unavailable.value.code == "RESULT_NOT_FOUND"
