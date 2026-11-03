"""专业能力委派复用父任务的权限、预算、证据和幂等。"""
import json
from pathlib import Path
import shutil

from fastapi.testclient import TestClient
import pytest

from app.catalog import Catalog
from app.main import API, create_app


ROOT = Path(__file__).resolve().parents[1]
SCENES = ROOT / "examples/specialists/scenarios"


@pytest.mark.parametrize("business,target", [("reader_agent", "read_notes"), ("counter_agent", "count_text")])
def test_specialists_share_task_calls_and_replay(tmp_path, monkeypatch, business, target):
    monkeypatch.setenv("AGENT_SPECIALISTS", "1")
    app = create_app(scenarios=SCENES, database=tmp_path / "db", token="reader-token")
    with TestClient(app, base_url="http://localhost", headers={"Authorization": "Bearer reader-token"}) as client:
        body = {"business_id": business, "message": "观察记录要求", "mode": "mock"}
        headers = {"Idempotency-Key": "delegate-once"}
        task = client.post(API + "/chat", json=body, headers=headers).json()
        assert task["status"] == "completed", task
        assert len(task["tool_calls"]) == 2
        assert target in {call["capability_id"] for call in task["tool_calls"]}
        assert client.post(API + "/chat", json=body, headers=headers).json()["task_id"] == task["task_id"]
        events = client.get(API + "/tasks/" + task["task_id"] + "/trace").json()["events"]
        assert [event["event"] for event in events].count("agent_delegated") == 1
        assert [event["event"] for event in events].count("agent_returned") == 1
        assert [event["event"] for event in events].count("task_completed") == 1
        if business == "reader_agent":
            assert task["evidence"][0]["source_id"] == "demo-note-01"


def test_specialists_default_off_and_leaf_permission_required(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_SPECIALISTS", raising=False)
    actor = {"tenant_id": "t", "org_id": "o", "user_id": "u",
             "grants": {"counter_agent": {"permissions": ["agent.delegate"]}}}
    for enabled, code in [(False, "ACTION_NOT_ALLOWED"), (True, "PERMISSION_DENIED")]:
        if enabled:
            monkeypatch.setenv("AGENT_SPECIALISTS", "1")
        app = create_app(scenarios=SCENES, database=tmp_path / str(enabled), principals={"reader-token": actor})
        with TestClient(app, base_url="http://localhost", headers={"Authorization": "Bearer reader-token"}) as client:
            task = client.post(API + "/chat", json={"business_id": "counter_agent", "message": "示例", "mode": "mock"}).json()
            assert task["error"]["code"] == code, task
            assert not any(row["capability_id"] == "count_text" for row in task["tool_calls"])


def test_specialist_recursion_rejected_at_registration(tmp_path):
    shutil.copytree(SCENES, tmp_path / "scenes")
    path = tmp_path / "scenes/counter_agent/scenario.json"
    scene = json.loads(path.read_text(encoding="utf-8"))
    scene["tools"][-1]["adapter"]["target"] = "counter"
    path.write_text(json.dumps(scene), encoding="utf-8")
    with pytest.raises(ValueError, match="递归"):
        Catalog(tmp_path / "scenes")
