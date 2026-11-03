import asyncio
from pathlib import Path
import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.worker import PlanWorker

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "durable" / "scenarios"
API = "/api/v1/agent"
AUTH = {"Authorization": "Bearer local-plan-test"}
BODY = {"business_id": "project_brief", "message": "迭代记录", "execution_mode": "durable_plan"}


def client_for(tmp_path, *, embedded=True, principals=None):
    app = create_app(database=tmp_path / "test.db", scenarios=EXAMPLE, token="local-plan-test",
                     model_settings={}, embedded_worker=embedded, principals=principals)
    return TestClient(app, base_url="http://localhost")


def wait_task(client, task_id):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        task = client.get(f"{API}/tasks/{task_id}", headers=AUTH).json()
        if task["status"] in ("completed", "partial", "failed", "cancelled"):
            return task
        time.sleep(.04)
    raise AssertionError("Plan did not terminate")


def test_plan_api_execution_and_authorized_views(tmp_path):
    with client_for(tmp_path) as c:
        result = c.post(API + "/chat", headers=AUTH, json=BODY)
        assert result.status_code == 202, result.text
        tid = result.json()["task_id"]
        assert result.headers["Location"].endswith("/tasks/" + tid)
        task = wait_task(c, tid)
        assert task["status"] == "completed", task
        assert task["answer"] and task["evidence"] and len(task["tool_calls"]) == 3
        path = f"{API}/tasks/{tid}"
        plan = c.get(path + "/plan", headers=AUTH).json()
        steps = c.get(path + "/steps", headers=AUTH).json()["steps"]
        assert plan["plan_id"] and len(steps) == 3
        assert all(s["status"] == "succeeded" for s in steps)
        assert all("arguments" not in s for s in steps)
        assert c.get(path + "/plan").status_code == 401
        trace = c.get(path + "/trace", headers=AUTH).json()["events"]
        assert sum(e["event"] == "task_completed" for e in trace) == 1
        assert any(e["event"] == "plan_validated" for e in trace)
        last = c.get(path + "/events", headers=AUTH | {"Last-Event-ID": str(trace[-2]["id"])})
        assert last.text.count("event: ") == 1


def test_durable_replay_and_cancel_before_worker(tmp_path):
    with client_for(tmp_path, embedded=False) as c:
        headers = AUTH | {"Idempotency-Key": "one"}
        first = c.post(API + "/chat", headers=headers, json=BODY).json()
        replay = c.post(API + "/chat", headers=headers, json=BODY).json()
        assert first["task_id"] == replay["task_id"]
        assert c.post(API + "/chat", headers=headers, json=BODY | {"message": "different"}).status_code == 409
        path = API + "/tasks/" + first["task_id"]
        assert c.post(path + "/cancel", headers=AUTH).json()["status"] == "cancelled"
        assert not c.get(path, headers=AUTH).json()["tool_calls"]


def test_durable_stream(tmp_path):
    with client_for(tmp_path) as c:
        out = c.post(API + "/chat", headers=AUTH, json=BODY | {"response_mode": "streaming"})
        assert out.status_code == 200
        assert out.text.count("event: task_completed") == 1
        assert "event: plan_validated" in out.text


def test_write_permission_cannot_authorize_unregistered_tools(tmp_path):
    actor = {"tenant_id": "t", "org_id": "o", "user_id": "u", "grants": {
        "project_brief": {"permissions": ["unrelated"], "objects": [], "resource_types": []}}}
    with client_for(tmp_path, principals={"local-plan-test": actor}) as c:
        assert c.post(API + "/chat", headers=AUTH, json=BODY).status_code == 403
        assert c.get(API + "/tasks", headers=AUTH).json()["tasks"] == []


def test_revoked_permission_blocks_planned_read_before_dispatch(tmp_path):
    with client_for(tmp_path, embedded=False) as client:
        rt = client.app.state.runtime
        task = client.post(API + "/chat", headers=AUTH, json=BODY).json()
        claim = rt.store.claim_step("test-worker", 12)
        rt.principals["local-plan-test"]["grants"]["project_brief"]["permissions"].remove("brief.read")
        asyncio.run(PlanWorker(rt, client.app.state.snapshots).execute(claim))
        result = client.get(API + "/tasks/" + task["task_id"], headers=AUTH).json()
        assert result["status"] == "failed" and result["error"]["code"] == "PERMISSION_DENIED"
        assert not result["tool_calls"]
        assert client.get(API + "/tasks/" + task["task_id"] + "/plan", headers=AUTH).status_code == 403
