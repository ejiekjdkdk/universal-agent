"""Public HTTP checks for the combined local governance features."""
import json
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "examples" / "governance"
API = "/api/v1/agent"
ADMIN = "/api/v1/agent-admin"


def headers(token, key=None):
    out = {"Authorization": "Bearer " + token}
    if key:
        out["Idempotency-Key"] = key
    return out


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_PRINCIPALS_FILE", str(PROJECT / "principals.json"))
    monkeypatch.setenv("AGENT_ACCESS_TOKEN", "desk-owner")
    monkeypatch.setenv("AGENT_APPROVER_TOKEN", "desk-reviewer")
    monkeypatch.setenv("AGENT_ADMIN_TOKEN", "desk-admin")
    monkeypatch.setenv("AGENT_MOCK_WRITES", "1")
    db = tmp_path / "db.sqlite"
    app = create_app(database=db, scenarios=PROJECT / "scenarios", model_settings={})
    with TestClient(app, base_url="http://localhost") as client:
        yield client


def wait_until(read, predicate):
    end = time.monotonic() + 12
    while time.monotonic() < end:
        data = read()
        if predicate(data):
            return data
        time.sleep(.05)
    raise AssertionError(f"Timed out; last observation: {data}")


def test_independent_approval_then_observable_single_mock_commit(service):
    text = "private-memo-not-for-telemetry"
    request = {"business_id": "change_requests", "execution_mode": "durable_plan", "message": text,
               "context": {"object_ids": ["draft-1"]}}
    response = service.post(API + "/chat", json=request, headers=headers("desk-owner", "submit"))
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    task_url = f"{API}/tasks/{task_id}"
    def get_task():
        return service.get(task_url, headers=headers("desk-owner")).raise_for_status().json()
    wait_until(get_task, lambda task: task["status"] == "waiting_approval")
    pending = service.get(API + "/approvals", headers=headers("desk-reviewer")).raise_for_status().json()["approvals"]
    assert len(pending) == 1
    approval = pending[0]
    decision = {"decision": "approve", "proposal_digest": approval["proposal_digest"]}
    url = API + "/approvals/" + approval["approval_id"] + "/decision"
    assert service.post(url, headers=headers("desk-owner", "denied"), json=decision).status_code in (403, 404)
    assert service.post(url, headers=headers("desk-reviewer", "approved"), json=decision).is_success
    assert service.post(url, headers=headers("desk-reviewer", "approved"), json=decision).is_success
    task = wait_until(get_task, lambda task: task["status"] in ("completed", "failed"))
    assert task["status"] == "completed", task
    assert service.get(f"{ADMIN}/observability/traces/{task_id}", headers=headers("desk-owner")).status_code in (401,403)
    spans = service.get(f"{ADMIN}/observability/traces/{task_id}", headers=headers("desk-admin")).raise_for_status()
    assert "resourceSpans" in spans.json()
    assert text not in spans.text and "desk-reviewer" not in spans.text
    trace = service.get(task_url + "/trace", headers=headers("desk-owner")).raise_for_status().json()["events"]
    assert sum(event["event"] == "task_completed" for event in trace) == 1


def test_configured_roles_never_share_admin_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ADMIN_TOKEN", "same")
    with pytest.raises(ValueError):
        with TestClient(create_app(database=tmp_path/"test.db", token="same")):
            pass
