"""A paused approval survives a process boundary and uses current roles at dispatch."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from fastapi.testclient import TestClient

from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "examples" / "governance"
API = "/api/v1/agent"


def test_external_worker_rechecks_reviewers_current_file_grant(tmp_path, monkeypatch):
    roles = tmp_path / "roles.json"
    original = json.loads((PROJECT / "principals.json").read_text(encoding="utf-8"))
    roles.write_text(json.dumps(original), encoding="utf-8")
    settings = {"AGENT_DATABASE": str(tmp_path / "state.db"), "AGENT_SCENARIOS": str(PROJECT / "scenarios"),
                "AGENT_PRINCIPALS_FILE": str(roles), "AGENT_MOCK_WRITES": "1", "AGENT_PLAN_WORKER": "external",
                "AGENT_ACCESS_TOKEN": "worker-owner", "AGENT_APPROVER_TOKEN": "worker-approver"}
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    owner = {"Authorization": "Bearer worker-owner"}
    approver = {"Authorization": "Bearer worker-approver"}
    def run_worker():
        return subprocess.Popen([sys.executable, "-m", "app.worker"], cwd=ROOT, env=os.environ.copy())
    def stop(process):
        process.kill()
        process.wait(timeout=10)
    def wait(client, tid, status, worker):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            assert worker.poll() is None, "Worker exited early"
            data = client.get(f"{API}/tasks/{tid}", headers=owner).raise_for_status().json()
            if data["status"] == status:
                return data
            time.sleep(.06)
        raise AssertionError(f"Expected {status}: {data}")

    with TestClient(create_app(model_settings={}), base_url="http://localhost") as first:
        task = first.post(API + "/chat", json={"mode":"offline", "execution_mode":"durable_plan",
            "business_id":"change_requests", "context":{"object_ids":["draft-1"]}, "message":"version one"},
            headers=owner).raise_for_status().json()
        tid = task["task_id"]
        worker = run_worker()
        try:
            wait(first, tid, "waiting_approval", worker)
        finally:
            stop(worker)

    with TestClient(create_app(model_settings={}), base_url="http://localhost") as second:
        pending = second.get(API + "/approvals?status=requested", headers=approver).raise_for_status().json()
        a = pending["approvals"][0]
        target = API + "/approvals/" + a["approval_id"]
        result = second.post(target + "/decision", headers=approver | {"Idempotency-Key":"submit-review"},
            json={"decision":"approve", "proposal_digest":a["proposal_digest"]})
        assert result.is_success, result.text
        for entry in original:
            if entry["user_id"] == "reviewer":
                entry["grants"]["change_requests"]["permissions"] = []
        updated = tmp_path / "roles.next"
        updated.write_text(json.dumps(original), encoding="utf-8")
        updated.replace(roles)
        # The already-running API also sees the revoked role immediately.
        assert second.get(target, headers=approver).status_code in (403,404)
        worker = run_worker()
        try:
            failed = wait(second, tid, "failed", worker)
            assert failed["error"]["code"] in {"PERMISSION_DENIED","APPROVAL_PERMISSION_DENIED"}
            assert not failed["tool_calls"]
            assert second.get(target, headers=owner).json()["receipt_id"] is None
        finally:
            stop(worker)
