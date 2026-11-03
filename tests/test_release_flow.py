"""通过发布门禁、公开接口和待调度任务验证版本切换。"""
import asyncio
import json
from pathlib import Path
import shutil

from fastapi.testclient import TestClient

from app.main import create_app
from app.worker import PlanWorker

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/v1/agent"
ADMIN = "/api/v1/agent-admin"
OWNER = {"Authorization": "Bearer project-owner"}
PUBLISHER = {"Authorization": "Bearer project-admin"}


def candidate(tmp_path, scene_id, source):
    folder = tmp_path / "candidates" / "vnext" / scene_id
    shutil.copytree(source / scene_id, folder, ignore=shutil.ignore_patterns("__pycache__"))
    manifest = folder / "scenario.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["version"] = "1.1.0"
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return folder


def publish(c, scene_id):
    response = c.post(ADMIN + "/releases", headers=PUBLISHER, json={"business_id": scene_id, "candidate": "vnext"})
    assert response.status_code == 201, response.text
    rid = response.json()["release_id"]
    route = ADMIN + "/releases/" + rid
    assert c.post(route + "/activate", headers=PUBLISHER, json={"canary_percent": 100}).is_success
    return route


def test_deeply_nested_candidate_documents_return_422_and_can_be_corrected(tmp_path, monkeypatch):
    source = ROOT / "scenarios"
    folder = candidate(tmp_path, "notes", source)
    documents = folder / "notes.json"
    valid = documents.read_bytes()
    documents.write_bytes(b"[" * 2000 + b"]" * 2000)
    monkeypatch.setenv("AGENT_ADMIN_TOKEN", "project-admin")
    monkeypatch.setenv("AGENT_RELEASE_CANDIDATES", str(tmp_path / "candidates"))
    app = create_app(database=tmp_path / "db", scenarios=source, token="project-owner")
    with TestClient(app, base_url="http://localhost", raise_server_exceptions=False) as client:
        body = {"business_id": "notes", "candidate": "vnext"}
        snapshots = app.state.snapshots.directory
        original = set(snapshots.iterdir())
        for path in ("/releases/validate", "/releases"):
            response = client.post(ADMIN + path, headers=PUBLISHER, json=body)
            assert response.status_code == 422, response.text
            assert response.json()["error"]["code"] == "RELEASE_INVALID"
            assert set(snapshots.iterdir()) == original
        documents.write_bytes(valid)
        publish(client, "notes")


def test_running_task_stays_pinned_but_emergency_revoke_stops_dispatch(tmp_path, monkeypatch):
    scene = "project_brief"
    source = ROOT / "examples" / "durable" / "scenarios"
    folder = candidate(tmp_path, scene, source)
    notes = folder / "notes.json"
    rows = json.loads(notes.read_text(encoding="utf-8"))
    rows[0]["snippet"] += " VERSION-NEXT"
    notes.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("AGENT_ADMIN_TOKEN", "project-admin")
    monkeypatch.setenv("AGENT_RELEASE_CANDIDATES", str(tmp_path / "candidates"))
    app = create_app(scenarios=source, database=tmp_path / "db", token="project-owner",
                     embedded_worker=False, model_settings={})
    with TestClient(app, base_url="http://localhost") as c:
        worker = PlanWorker(app.state.runtime, app.state.snapshots)
        def submit():
            result = c.post(BASE + "/chat", headers=OWNER, json={"business_id": scene, "message": "简报",
                            "execution_mode": "durable_plan"})
            assert result.status_code == 202, result.text
            return BASE + "/tasks/" + result.json()["task_id"]
        def step():
            claim = worker.store.claim_step("release-flow-test", 10)
            assert claim
            asyncio.run(worker.execute(claim))
        old = submit()
        step()
        route = publish(c, scene)
        new = submit()
        for _ in range(5):
            step()
        assert "VERSION-NEXT" not in c.get(old, headers=OWNER).json()["answer"]
        assert "VERSION-NEXT" in c.get(new, headers=OWNER).json()["answer"]
        assert c.post(route + "/rollback", headers=PUBLISHER, json={}).is_success
        interrupted = submit()
        step()
        assert c.post(ADMIN + "/capabilities/compose_brief/revoke", headers=PUBLISHER,
                      json={"business_id": scene}).is_success
        step()
        step()
        failed = c.get(interrupted, headers=OWNER).json()
        assert failed["business_version"] == "1.0.1"
        assert failed["error"]["code"] == "CAPABILITY_REVOKED"
        assert not any(call["capability_id"] == "compose_brief" for call in failed["tool_calls"])


def test_reactive_project_uses_selected_release_and_retains_its_task_pin(tmp_path, monkeypatch):
    source = ROOT / "scenarios"
    folder = candidate(tmp_path, "notes", source)
    docs = folder / "notes.json"
    notes = json.loads(docs.read_text(encoding="utf-8"))
    notes.append({"source_id":"local-cutover", "title":"cutoverstamp", "version":"1", "snippet":"new release"})
    docs.write_text(json.dumps(notes, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("AGENT_ADMIN_TOKEN", "project-admin")
    monkeypatch.setenv("AGENT_RELEASE_CANDIDATES", str(tmp_path / "candidates"))
    with TestClient(create_app(database=tmp_path / "db", scenarios=source, token="project-owner"),
                    base_url="http://localhost") as c:
        route = publish(c, "notes")
        body = {"business_id": "notes", "message": "cutoverstamp"}
        key = OWNER | {"Idempotency-Key":"pin"}
        new = c.post(BASE + "/chat", headers=key, json=body).json()
        assert new["business_version"] == "1.1.0" and new["evidence"][0]["source_id"] == "local-cutover"
        assert c.post(route + "/rollback", headers=PUBLISHER, json={}).is_success
        replay = c.post(BASE + "/chat", headers=key, json=body).json()
        assert replay["task_id"] == new["task_id"] and replay["snapshot_id"] == new["snapshot_id"]
        stable = c.post(BASE + "/chat", headers=OWNER, json=body).json()
        assert stable["business_version"] == "1.0.1" and not stable["evidence"]
