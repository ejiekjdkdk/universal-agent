"""真实 SQLite 下验证接入、重启、连接竞争、事务回滚和运行时撤权。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import time

import pytest

from app.catalog import Catalog
from app.errors import Fault
from app.releases import Releases
from app.runtime import Runtime
from app.snapshots import Snapshots
from app.store import Store
from app.triggers import TriggerGateway, sign
from app.worker import PlanWorker

ROOT = Path(__file__).resolve().parents[1]
SECRET = "local-test-signing-secret-32-characters"
TOKEN = "local-trigger-actor"


def configuration():
    return {"sources": [{"source_id": "local", "token_env": "TEST_TRIGGER_ACTOR",
                          "signing_key_env": "TEST_TRIGGER_SECRET", "businesses": {
                              "project_brief": {"object_types": [], "object_ids": []}}}], "schedules": []}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_TRIGGER_ACTOR", TOKEN)
    monkeypatch.setenv("TEST_TRIGGER_SECRET", SECRET)
    path = tmp_path / "triggers.json"
    path.write_text(json.dumps(configuration()), encoding="utf8")
    catalog = Catalog(ROOT / "examples" / "durable" / "scenarios")
    actors = {TOKEN: {"tenant_id": "test", "org_id": "local", "user_id": "trigger", "grants": {
        "project_brief": {"permissions": ["brief.read", "knowledge.read"]}}}}
    stores = []

    def connect():
        store = Store(tmp_path / "data.db", recover_reactive=False)
        stores.append(store)
        snapshots = Snapshots(tmp_path / "data.db", catalog)
        runtime = Runtime(catalog, store, actors, {})
        runtime.bind_releases(Releases(store, snapshots), snapshots)
        gateway = TriggerGateway(runtime, snapshots, path)
        runtime.triggers = gateway
        return gateway

    gateway = connect()
    yield gateway, connect, path, actors
    for store in stores:
        try:
            store.close()
        except sqlite3.ProgrammingError:
            pass


def request(event_id="event.1", *, now=None, nonce=None, changes=None):
    now = int(time.time()) if now is None else now
    event = {"event_id": event_id, "business_id": "project_brief", "message": "Weekly progress",
             "context": {}, "issued_at": now, "expires_at": now + 120, "execution_mode": "durable_plan"}
    event.update(changes or {})
    raw = json.dumps(event, ensure_ascii=False).encode()
    return raw, headers(raw, now, nonce or event_id)


def headers(raw, now, nonce="nonce.1"):
    timestamp = str(int(now))
    return {"X-Trigger-Source": "local", "X-Trigger-Timestamp": timestamp, "X-Trigger-Nonce": nonce,
            "X-Trigger-Signature": sign(SECRET, "local", timestamp, nonce, raw)}


def count(gateway, table):
    return gateway.store._db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


def test_restart_duplicate_and_real_worker_reuses_durable_engine(env):
    gateway, connect, _, actors = env
    raw, auth = request()
    first = gateway.ingest(raw, auth)
    assert first["created"]
    gateway.store.close()
    restarted = connect()
    second = restarted.ingest(raw, auth)
    assert second == dict(first, created=False)
    assert count(restarted, "tasks") == count(restarted, "trigger_events") == count(restarted, "plans") == 1
    worker = PlanWorker(restarted.runtime, restarted.snapshots)

    async def finish():
        while claim := restarted.store.claim_step("trigger-test", 10):
            await worker.execute(claim)

    asyncio.run(finish())
    task = restarted.store.task(actors[TOKEN], first["task_id"])
    assert task["status"] == "completed"
    assert task["execution_mode"] == "durable_plan" and task["snapshot_id"]
    assert len(task["tool_calls"]) == 3


def test_competing_sqlite_connections_create_exactly_one_task(env):
    first, connect, _, _ = env
    second = connect()
    raw, auth = request()
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(gateway.ingest, raw, auth) for gateway in (first, second)]
        results = [future.result() for future in futures]
    assert sum(result["created"] for result in results) == 1
    assert len({result["task_id"] for result in results}) == 1
    assert count(first, "tasks") == count(first, "trigger_events") == 1


def test_receipt_failure_rolls_back_task_plan_session_and_nonce(env):
    gateway, _, _, _ = env
    gateway.store._db.execute("""CREATE TRIGGER inject_trigger_crash BEFORE INSERT ON trigger_events
        BEGIN SELECT RAISE(ABORT,'injected receipt failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        gateway.ingest(*request())
    for table in ("tasks", "plans", "sessions", "trigger_events", "trigger_nonces"):
        assert count(gateway, table) == 0
    gateway.store._db.execute("DROP TRIGGER inject_trigger_crash")
    assert gateway.ingest(*request())["created"]


@pytest.mark.parametrize("changes,code", [
    ({"expires_at": 1, "issued_at": 0}, "TRIGGER_EXPIRED"),
    ({"tenant_id": "forged"}, "TRIGGER_INVALID"),
    ({"adapter_url": "https://untrusted.invalid"}, "TRIGGER_INVALID"),
    ({"execution_mode": "reactive"}, "TRIGGER_INVALID"),
    ({"business_id": "other"}, "TRIGGER_SCOPE_DENIED"),
    ({"context": {"object_ids": ["other"]}}, "TRIGGER_SCOPE_DENIED"),
    ({"context": {"object_type": "other"}}, "TRIGGER_SCOPE_DENIED"),
    ({"expires_at": float("nan")}, "TRIGGER_INVALID"),
])
def test_untrusted_payloads_are_rejected_without_task(env, changes, code):
    gateway = env[0]
    with pytest.raises(Fault) as exc:
        gateway.ingest(*request(changes=changes))
    assert exc.value.code == code
    assert count(gateway, "tasks") == 0


@pytest.mark.parametrize("field", ["issued_at", "expires_at"])
@pytest.mark.parametrize("timestamp", [10 ** 400, -(10 ** 400)], ids=["huge-positive", "huge-negative"])
def test_oversized_event_timestamps_are_rejected_and_audited(env, field, timestamp):
    gateway = env[0]
    with pytest.raises(Fault) as exc:
        gateway.ingest(*request(changes={field: timestamp}))
    assert exc.value.code == "TRIGGER_INVALID"
    assert exc.value.status == 422
    for table in ("tasks", "plans", "sessions", "trigger_events", "trigger_nonces"):
        assert count(gateway, table) == 0
    audit = gateway.store._db.execute(
        "SELECT action,decision,code FROM audit_logs ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert tuple(audit) == ("trigger.ingress", "deny", "TRIGGER_INVALID")


def test_signature_freshness_and_exact_bytes_are_checked(env):
    gateway = env[0]
    raw, auth = request()
    with pytest.raises(Fault, match="signature"):
        gateway.ingest(raw + b" ", auth)
    with pytest.raises(Fault, match="signature"):
        gateway.ingest(raw, headers(raw, time.time() - 600))
    gateway.ingest(raw, auth)
    with pytest.raises(Fault) as exc:
        gateway.ingest(raw + b" ", headers(raw + b" ", time.time(), "nonce.2"))
    assert exc.value.code == "TRIGGER_EVENT_CONFLICT"
    with pytest.raises(Fault) as exc:
        gateway.ingest(*request("different-event", nonce="event.1"))
    assert exc.value.code == "TRIGGER_REPLAY"
    assert count(gateway, "tasks") == 1


def test_duplicate_event_consumes_new_nonce_against_reuse_for_other_event(env):
    gateway = env[0]
    raw, auth = request()
    first = gateway.ingest(raw, auth)
    retry_headers = headers(raw, time.time(), "retry-nonce")
    duplicate = gateway.ingest(raw, retry_headers)
    assert duplicate == dict(first, created=False)
    assert gateway.ingest(raw, retry_headers) == duplicate
    with pytest.raises(Fault) as exc:
        gateway.ingest(*request("another-event", nonce="retry-nonce"))
    assert exc.value.code == "TRIGGER_REPLAY"
    assert count(gateway, "tasks") == 1


@pytest.mark.parametrize("limit", ["max_active", "max_per_minute"])
def test_limits_are_persisted_and_duplicate_does_not_consume_capacity(env, limit):
    gateway, connect, path, _ = env
    config = configuration()
    config["sources"][0][limit] = 1
    path.write_text(json.dumps(config), encoding="utf8")
    raw, auth = request()
    gateway.ingest(raw, auth)
    restarted = connect()
    assert not restarted.ingest(raw, auth)["created"]
    with pytest.raises(Fault) as exc:
        restarted.ingest(*request("event.2"))
    assert exc.value.code == "TRIGGER_RATE_LIMITED"


@pytest.mark.parametrize("revocation", ["source", "actor", "permission", "release", "corrupt-config"])
def test_live_revoke_blocks_queued_worker_before_tool_call(env, revocation):
    gateway, _, path, actors = env
    receipt = gateway.ingest(*request())
    if revocation == "source":
        config = configuration()
        config["sources"][0]["enabled"] = False
        path.write_text(json.dumps(config), encoding="utf8")
    elif revocation == "actor":
        actors.clear()
    elif revocation == "permission":
        actors[TOKEN]["grants"]["project_brief"]["permissions"] = []
    elif revocation == "release":
        gateway.runtime.releases.revoke(gateway.store.task_snapshot(receipt["task_id"]), actor=actors[TOKEN])
    else:
        path.write_text("{bad", encoding="utf8")
    worker = PlanWorker(gateway.runtime, gateway.snapshots)

    async def finish():
        while claim := gateway.store.claim_step("revocation-test", 10):
            await worker.execute(claim)

    asyncio.run(finish())
    assert count(gateway, "calls") == 0
    assert gateway.store._task(receipt["task_id"])["status"] == "failed"


def test_schedule_cursor_restart_race_and_no_backlog_flood(env):
    gateway, connect, path, _ = env
    now = int(time.time())
    config = configuration()
    config["schedules"] = [{"schedule_id": "weekly", "source_id": "local", "business_id": "project_brief",
                            "message": "Scheduled summary", "context": {}, "interval_seconds": 60, "start_at": now - 120}]
    path.write_text(json.dumps(config), encoding="utf8")
    other = connect()
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(g.tick, now=now) for g in (gateway, other)]
        results = [future.result() for future in futures]
    assert sum(len(result) for result in results) == 1
    assert count(gateway, "tasks") == 1
    restarted = connect()
    assert restarted.tick(now=now) == []
    assert len(restarted.tick(now=now + 60)) == 1
    assert count(gateway, "tasks") == 2
    assert gateway.store._db.execute("SELECT next_due FROM trigger_schedules").fetchone()[0] == now + 120


def test_schedule_crash_rolls_back_cursor_and_receipt(env):
    gateway, _, path, _ = env
    now = int(time.time())
    config = configuration()
    config["schedules"] = [{"schedule_id": "daily", "source_id": "local", "business_id": "project_brief",
                            "message": "Scheduled summary", "context": {}, "interval_seconds": 60, "start_at": now}]
    path.write_text(json.dumps(config), encoding="utf8")
    gateway.store._db.execute("""CREATE TRIGGER inject_schedule_crash BEFORE UPDATE ON trigger_schedules
        BEGIN SELECT RAISE(ABORT,'injected cursor failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        gateway.tick(now=now)
    assert count(gateway, "tasks") == count(gateway, "trigger_events") == count(gateway, "trigger_schedules") == 0
    gateway.store._db.execute("DROP TRIGGER inject_schedule_crash")
    assert len(gateway.tick(now=now)) == 1


def test_disabled_default_and_duplicate_json(env, monkeypatch):
    gateway = env[0]
    monkeypatch.delenv("AGENT_TRIGGERS_FILE", raising=False)
    disabled = TriggerGateway(gateway.runtime, gateway.snapshots)
    with pytest.raises(Fault) as exc:
        disabled.ingest(*request())
    assert exc.value.code == "TRIGGERS_DISABLED"
    raw, _ = request()
    raw = raw[:-1] + b',"event_id":"duplicate"}'
    with pytest.raises(Fault) as exc:
        gateway.ingest(raw, headers(raw, time.time()))
    assert exc.value.code == "TRIGGER_INVALID"


def test_real_api_signed_ingress_has_separate_auth_and_shared_task_routes(env, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app

    _, _, path, actors = env
    monkeypatch.setenv("AGENT_TRIGGERS_FILE", str(path))
    app = create_app(scenarios=ROOT / "examples" / "durable" / "scenarios", database=path.parent / "api.db",
                     token=TOKEN, principals=actors, embedded_worker=True, model_settings={})
    with TestClient(app, base_url="http://localhost") as client:
        raw, auth = request()
        route = "/internal/agent/triggers/events"
        unsigned = client.post(route, content=raw, headers={"Authorization": "Bearer " + TOKEN})
        assert unsigned.status_code in {401, 403}
        response = client.post(route, content=raw, headers=auth)
        assert response.status_code == 202
        receipt = response.json()
        duplicate = client.post(route, content=raw, headers=auth)
        assert duplicate.status_code == 200 and duplicate.json()["task_id"] == receipt["task_id"]
        location = response.headers["Location"]
        assert client.get(location, headers=auth).status_code == 401
        task = client.get(location, headers={"Authorization": "Bearer " + TOKEN})
        assert task.status_code == 200 and task.json()["execution_mode"] == "durable_plan"
        config = configuration()
        config["sources"][0]["enabled"] = False
        path.write_text(json.dumps(config), encoding="utf8")
        assert client.get(location, headers={"Authorization": "Bearer " + TOKEN}).status_code == 403
        assert client.post(route, content=raw, headers=auth).status_code == 403


def test_source_revoked_during_prepare_rolls_back_admission(env, monkeypatch):
    import app.triggers as module

    gateway, _, path, _ = env
    original = module.prepare_plan

    def revoke_during_prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        config = configuration()
        config["sources"][0]["enabled"] = False
        path.write_text(json.dumps(config), encoding="utf8")
        return result

    monkeypatch.setattr(module, "prepare_plan", revoke_during_prepare)
    with pytest.raises(Fault) as exc:
        gateway.ingest(*request())
    assert exc.value.code == "TRIGGER_SOURCE_DENIED"
    assert count(gateway, "tasks") == count(gateway, "trigger_events") == 0


def test_trigger_pins_real_published_skill_and_revocation_stops_queued_plan(env):
    from app.skills import SkillRegistry

    gateway, _, path, actors = env
    registry = SkillRegistry(path.parent / "skills")
    gateway.runtime.skills = registry
    package = path.parent / "brief-skill"
    package.mkdir()
    (package / "SKILL.md").write_text(
        "---\nname: brief-skill\ndescription: 可信项目简报\n---\n核对进度和风险来源，生成简报。", encoding="utf8")
    registry.import_package(package, "1.0.0")
    tools = ["read_progress", "read_risks", "compose_brief", "retrieve_knowledge"]
    registry.publish("project_brief", "brief-skill", "1.0.0", tools, available_tools=tools)
    first = gateway.ingest(*request("with-skill"))
    context = gateway.store.run(gateway.store.context_id(first["task_id"]))
    assert context["context"]["skill"]["name"] == "brief-skill"
    worker = PlanWorker(gateway.runtime, gateway.snapshots)

    async def finish():
        while claim := gateway.store.claim_step("skill-trigger-test", 10):
            await worker.execute(claim)

    asyncio.run(finish())
    assert gateway.store.task(actors[TOKEN], first["task_id"])["status"] == "completed"
    second = gateway.ingest(*request("queued-with-skill"))
    registry.revoke("project_brief", "brief-skill", "1.0.0")
    asyncio.run(finish())
    assert gateway.store._task(second["task_id"])["status"] == "failed"
    assert count(gateway, "calls") == 3
    with pytest.raises(Fault) as exc:
        gateway.ingest(*request("after-skill-revoke"))
    assert exc.value.code == "SKILL_UNAVAILABLE"


@pytest.mark.parametrize("limit,expected", [(2, "BUDGET_EXCEEDED"), (6, "SKILL_SCHEMA_INVALID")])
def test_trigger_skill_declared_input_and_budget_reject_before_receipt(env, limit, expected):
    from app.skills import SkillRegistry

    gateway, _, path, _ = env
    gateway.runtime.skills = registry = SkillRegistry(path.parent / "skills")
    package = path.parent / "bounded-brief"
    package.mkdir()
    (package / "SKILL.md").write_text(
        "---\nname: bounded-brief\ndescription: 受限简报\n---\n遵循输入结构和已批准预算。", encoding="utf8")
    (package / "schemas").mkdir()
    schema = {"type": "object", "properties": {"message": {"const": "Approved summary"},
                                               "context": {"type": "object"}},
              "required": ["message", "context"], "additionalProperties": False}
    (package / "schemas" / "input.json").write_text(json.dumps(schema), encoding="utf8")
    (package / "skill.manifest.yaml").write_text(
        "version: 1.0.0\ninput_schema_ref: schemas/input.json\nexecution:\n  max_tool_calls: " + str(limit), encoding="utf8")
    registry.import_package(package, "1.0.0")
    tools = ["read_progress", "read_risks", "compose_brief", "retrieve_knowledge"]
    registry.publish("project_brief", "bounded-brief", "1.0.0", tools, available_tools=tools)
    message = "Approved summary" if limit == 2 else "Unapproved summary"
    with pytest.raises(Fault) as exc:
        gateway.ingest(*request("invalid-policy", changes={"message": message}))
    assert exc.value.code == expected
    assert count(gateway, "tasks") == count(gateway, "trigger_events") == count(gateway, "trigger_nonces") == 0
    if limit == 6:
        receipt = gateway.ingest(*request("valid-policy", changes={"message": "Approved summary"}))
        stored = gateway.store._db.execute("SELECT call_limit FROM plans WHERE task_id=?", (receipt["task_id"],)).fetchone()
        assert stored[0] == 6
