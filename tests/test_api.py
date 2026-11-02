from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/v1/agent"
AUTH = {"Authorization": "Bearer local-test-identity"}
BODY = {"business_id": "notes", "message": "会议纪要的记录要求"}


@pytest.fixture
def client(tmp_path):
    application = create_app(scenarios=ROOT / "scenarios", database=tmp_path / "test.db",
                             token="local-test-identity", model_settings={})
    with TestClient(application, base_url="http://localhost") as connection:
        yield connection


def chat(client, **changes):
    return client.post(BASE + "/chat", json=BODY | changes, headers=AUTH)


def test_notes_answer_has_evidence_and_replay(client):
    response = chat(client)
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "completed" and run["mode"] == "offline", run
    assert run["evidence"][0]["snippet"] and run["tool_calls"]
    task = BASE + "/tasks/" + run["task_id"]
    trace = client.get(task + "/trace", headers=AUTH).json()["events"]
    assert trace[0]["event"] == "task_started"
    assert sum(e["event"] == "task_completed" for e in trace) == 1
    assert any(e["event"] == "authorization" for e in trace)
    assert any(e["event"] == "tool_result" for e in trace)
    replay = client.get(task + "/events", headers=AUTH | {"Last-Event-ID": str(trace[-2]["id"])})
    assert replay.text.count("event: ") == 1 and "task_completed" in replay.text


def test_reopen_task_after_refresh(client):
    run = chat(client).json()
    assert client.get(BASE + "/tasks", headers=AUTH).json()["tasks"][0]["task_id"] == run["task_id"]
    assert client.get(BASE + "/tasks/" + run["task_id"], headers=AUTH).json()["answer"] == run["answer"]


@pytest.mark.parametrize("field,value", [
    ("user_id", "forged"), ("tenant_id", "other"), ("message", ""), ("message", "x" * 16001),
    ("mode", "shell"), ("response_mode", "unknown"), ("context", {"url": "http://example.test"}),
])
def test_invalid_requests(client, field, value):
    response = chat(client, **{field: value})
    assert response.status_code == 400 and response.json()["error"]["code"] == "INVALID_REQUEST"


def test_idempotent_chat_and_conflict(client):
    headers = AUTH | {"Idempotency-Key": "same-request"}
    first = client.post(BASE + "/chat", json=BODY, headers=headers).json()
    duplicate = client.post(BASE + "/chat", json=BODY, headers=headers).json()
    assert first["task_id"] == duplicate["task_id"]
    conflict = client.post(BASE + "/chat", json=BODY | {"message": "different"}, headers=headers)
    assert conflict.status_code == 409


def test_unknown_scenario(client):
    assert chat(client, business_id="unregistered").status_code == 403


def test_stream_ends_once(client):
    response = chat(client, response_mode="streaming")
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("event: task_completed") == 1
    assert response.text.index("event: step_started") < response.text.index("event: task_completed")


def test_model_requires_configuration(client):
    response = chat(client, mode="openai")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "MODEL_NOT_CONFIGURED"


def test_request_without_token(client):
    assert client.get(BASE + "/tasks").status_code == 401


def test_external_site_cannot_bootstrap(client):
    response = client.post("/local/access", headers={"Origin": "https://hostile.example", "X-Local-UI": "1"})
    assert response.status_code == 403
    assert client.post("/local/access").status_code == 403


def test_remote_host_denied(client):
    assert client.get("/", headers={"Host": "hostile.example"}).status_code in (400, 403)


def test_internal_tools_require_service_auth(client):
    response = client.post("/internal/agent/discover", headers=AUTH, json={"run_context_id": "guessed"})
    assert response.status_code in (401, 403)


def test_empty_catalog_still_serves_workspace(tmp_path):
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    with TestClient(create_app(scenarios=scenarios, database=tmp_path / "db", token="x",
                               model_settings={}), base_url="http://localhost") as client:
        assert client.get("/").status_code == 200
        result = client.get(BASE + "/scenarios", headers={"Authorization": "Bearer x"}).json()
        assert result["scenarios"] == []
