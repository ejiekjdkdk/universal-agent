"""新结果API和内部加密引用必须保留原工具执行、终态与恢复语义。"""
import asyncio
import json
from pathlib import Path
import shutil
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from app.errors import Fault
from app.main import API, create_app
from app.runtime import Gateway


AUTH = {"Authorization": "Bearer reader"}
BODY = {"business_id": "notes", "message": "会议纪要", "mode": "mock"}


def configured_app(tmp_path, monkeypatch):
    monkeypatch.setenv("P2_INTEGRATION_KEY", "42" * 32)
    settings = {"p2": {"results": {"keys": {"local": "P2_INTEGRATION_KEY"}, "active_key": "local", "page_bytes": 512}}}
    return create_app(database=tmp_path / "agent.db", token="reader", model_settings=settings)


def test_old_default_has_no_result_routes(tmp_path):
    app = create_app(database=tmp_path / "agent.db", token="reader", model_settings={})
    assert not any("/results" in path for path in app.openapi()["paths"])


def test_idle_result_store_purges_expired_rows_without_new_calls(tmp_path, monkeypatch):
    app = configured_app(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost", headers=AUTH) as client:
        client.post(API + "/chat", json=BODY)
        store = app.state.runtime.store
        with store._transaction():
            store._db.execute("UPDATE tool_results SET expires_at=?", (time.time() - 1,))
        async def run():
            maintenance = asyncio.create_task(app.state.runtime.result_store.maintain())
            await asyncio.sleep(0.02)
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)
        asyncio.run(run())
        assert store._db.execute("SELECT COUNT(*) FROM tool_results").fetchone()[0] == 0


def test_completed_task_has_authorized_pages_and_unchanged_response(tmp_path, monkeypatch):
    app = configured_app(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost", headers=AUTH) as client:
        task = client.post(API + "/chat", json=BODY).json()
        assert task["status"] == "completed"
        assert "result_ref" not in task and "results" not in task
        listing = client.get(API + "/tasks/" + task["task_id"] + "/results")
        assert listing.status_code == 200
        items = listing.json()["results"]
        assert len(items) == 1
        ref = items[0]["result_ref"]
        response = client.get(API + "/results/" + ref)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        chunks = []
        cursor = None
        while True:
            page = client.get(API + "/results/" + ref + "/pages", params={"cursor": cursor} if cursor else {}).json()
            chunks.append(page["content"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        result = json.loads("".join(chunks))
        assert result["data"]["summary"] and result["evidence"] == task["evidence"]
        assert result["execution_id"] == task["tool_calls"][0]["execution_id"]
        runtime = app.state.runtime
        with pytest.raises(Fault):
            runtime.store.run(runtime.store.context_id(task["task_id"]))
        assert runtime.store._db.execute("SELECT result FROM calls WHERE task_id=?", (task["task_id"],)).fetchone()[0] is None


@pytest.mark.parametrize("revocation", ["permission", "capability"])
def test_result_read_rechecks_source_after_task_completion(tmp_path, monkeypatch, revocation):
    app = configured_app(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost", headers=AUTH) as client:
        task = client.post(API + "/chat", json=BODY).json()
        listing = client.get(API + "/tasks/" + task["task_id"] + "/results")
        assert listing.status_code == 200
        ref = listing.json()["results"][0]["result_ref"]
        runtime = app.state.runtime
        actor = runtime.principals["reader"]
        if revocation == "permission":
            actor["grants"]["notes"]["permissions"].remove("notes.read")
        else:
            runtime.releases.revoke_capability("notes", "notes_search", actor=actor)
        for suffix in ("", "/pages"):
            denied = client.get(API + "/results/" + ref + suffix)
            assert denied.status_code == 403 and "content" not in denied.json()


def test_foreign_principal_and_deleted_handle_are_denied(tmp_path, monkeypatch):
    app = configured_app(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost", headers=AUTH) as client:
        task = client.post(API + "/chat", json=BODY).json()
        listing = client.get(API + "/tasks/" + task["task_id"] + "/results")
        assert listing.status_code == 200
        ref = listing.json()["results"][0]["result_ref"]
        runtime = app.state.runtime
        runtime.principals["outsider"] = {**runtime.principals["reader"], "user_id": "other"}
        assert client.get(API + "/results/" + ref, headers={"Authorization": "Bearer outsider"}).status_code == 404
        assert client.delete(API + "/results/" + ref).json()["deleted"] is True
        assert client.get(API + "/results/" + ref + "/pages").status_code == 404


def test_original_execute_keeps_data_but_persists_only_encrypted_pointer(tmp_path, monkeypatch):
    app = configured_app(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost"):
        runtime = app.state.runtime
        task, _ = runtime.store.start(runtime.principals["reader"], BODY, None, "1.0.1")
        gateway = Gateway(runtime, runtime.store.context_id(task["task_id"]))
        first = asyncio.run(gateway.execute("notes_search", {"text": "会议纪要"}, "same-call"))
        cached = runtime.store._db.execute("SELECT result FROM calls WHERE task_id=?", (task["task_id"],)).fetchone()[0]
        pointer = json.loads(cached)
        assert set(pointer) == {"_encrypted_result"}
        assert first["data"]["summary"] and "result_ref" not in first
        second = asyncio.run(gateway.execute("notes_search", {"text": "会议纪要"}, "same-call"))
        assert second == first
        with runtime.store._transaction():
            runtime.store._db.execute("DELETE FROM tool_results WHERE result_ref=?", (pointer["_encrypted_result"],))
        with pytest.raises(Fault) as unavailable:
            asyncio.run(gateway.execute("notes_search", {"text": "会议纪要"}, "same-call"))
        assert unavailable.value.code == "RESULT_NOT_FOUND"
        assert runtime.store._db.execute("SELECT COUNT(*) FROM calls WHERE task_id=?", (task["task_id"],)).fetchone()[0] == 1


def test_encrypted_task_results_survive_application_restart(tmp_path, monkeypatch):
    app = configured_app(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost", headers=AUTH) as client:
        task = client.post(API + "/chat", json=BODY).json()
        listing = client.get(API + "/tasks/" + task["task_id"] + "/results")
        assert listing.status_code == 200
        ref = listing.json()["results"][0]["result_ref"]
    reopened = configured_app(tmp_path, monkeypatch)
    with TestClient(reopened, base_url="http://localhost", headers=AUTH) as client:
        assert client.get(API + "/results/" + ref + "/pages").status_code == 200
        assert client.get(API + "/tasks/" + task["task_id"]).json()["tool_calls"] == task["tool_calls"]


def test_large_model_result_uses_pages_without_changing_tool_data(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    folder = tmp_path / "scenarios" / "notes"
    shutil.copytree(root / "scenarios" / "notes", folder)
    manifest = json.loads((folder / "scenario.json").read_text(encoding="utf-8"))
    manifest["tools"][0]["output_schema"]["properties"]["details"] = {"type": "string", "maxLength": 200000}
    (folder / "scenario.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "adapter.py").write_text(
        'def search(arguments, retrieve):\n    return {"data": {"summary": "已读取笔记", "matches": 1, '
        '"details": "x" * 100000}, "evidence": retrieve(arguments["text"], 5)}\n', encoding="utf-8")
    requests = []

    def upstream(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert "read_result_page" in [tool["function"]["name"] for tool in payload["tools"]]
        if len(requests) == 1:
            name, args = "execute_capability", {"name": "notes_search", "arguments": {"text": "会议纪要"}}
        elif len(requests) == 2:
            result = json.loads(payload["messages"][-1]["content"])
            assert "details" not in result["data"] and result["truncated"] is True
            assert result["evidence"] and result["limitations"]
            name, args = "read_result_page", {"ref": result["result_ref"]}
        else:
            page = json.loads(payload["messages"][-1]["content"])
            assert page["encoding"] == "utf-8-json" and page["next_cursor"]
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "已读取笔记 [E1]"}}]})
        call = {"id": "call-" + str(len(requests)), "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "tool_calls": [call]}}]})

    monkeypatch.setenv("P2_INTEGRATION_KEY", "42" * 32)
    settings = {"context_compiler": True, "base_url": "http://localhost", "model": "test", "transport": httpx.MockTransport(upstream),
                "p2": {"results": {"keys": {"local": "P2_INTEGRATION_KEY"}, "active_key": "local", "page_bytes": 512}}}
    app = create_app(scenarios=folder.parent, database=tmp_path / "agent.db", token="reader", model_settings=settings)
    with TestClient(app, base_url="http://localhost", headers=AUTH) as client:
        task = client.post(API + "/chat", json={**BODY, "mode": "openai"}).json()
        assert task["status"] == "completed", task
        assert len(requests) == 3 and len(task["tool_calls"]) == 1
        assert any("摘要" in value for value in task["result"].get("limitations", []))
        ref = client.get(API + "/tasks/" + task["task_id"] + "/results").json()["results"][0]["result_ref"]
        original = app.state.runtime.result_store.load(ref)["result"]
        assert original["data"]["details"] == "x" * 100000


def test_model_page_read_cannot_cross_task_even_for_same_owner(tmp_path, monkeypatch):
    app = configured_app(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost", headers=AUTH) as client:
        finished = client.post(API + "/chat", json=BODY).json()
        ref = client.get(API + "/tasks/" + finished["task_id"] + "/results").json()["results"][0]["result_ref"]
        rt = app.state.runtime
        task, _ = rt.store.start(rt.principals["reader"], BODY, None, "1.0.1")
        gateway = Gateway(rt, rt.store.context_id(task["task_id"]))
        with pytest.raises(Fault) as denied:
            gateway._read_result_page(ref, None)
        assert denied.value.code == "PERMISSION_DENIED"


def test_real_plan_worker_keeps_complete_dependencies_with_result_store(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("P2_INTEGRATION_KEY", "42" * 32)
    settings = {"p2": {"results": {"keys": {"local": "P2_INTEGRATION_KEY"}, "active_key": "local"}}}
    app = create_app(scenarios=root / "examples" / "durable" / "scenarios", database=tmp_path / "plan.db",
                     token="reader", model_settings=settings)
    with TestClient(app, base_url="http://localhost", headers=AUTH) as client:
        response = client.post(API + "/chat", json={"business_id": "project_brief", "message": "汇总进度和风险",
                                                     "execution_mode": "durable_plan", "mode": "offline"})
        assert response.status_code == 202, response.json()
        task_id = response.json()["task_id"]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            task = client.get(API + "/tasks/" + task_id).json()
            if task["status"] not in {"running", "queued"}:
                break
            time.sleep(.03)
        assert task["status"] == "completed", task
        assert len(task["tool_calls"]) == 3
        assert len(client.get(API + "/tasks/" + task_id + "/results").json()["results"]) == 3
        assert task["answer"] and task["evidence"]


def test_structural_limit_precedes_output_schema_validation(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    folder = tmp_path / "scenarios" / "notes"
    shutil.copytree(root / "scenarios" / "notes", folder)
    (folder / "adapter.py").write_text('def search(arguments, retrieve):\n    return {"data": {"summary": [0] * 10001}, "evidence": []}\n', encoding="utf-8")
    monkeypatch.setenv("P2_INTEGRATION_KEY", "42" * 32)
    settings = {"p2": {"results": {"keys": {"local": "P2_INTEGRATION_KEY"}, "active_key": "local"}}}
    app = create_app(scenarios=folder.parent, database=tmp_path / "agent.db", token="reader", model_settings=settings)
    with TestClient(app, base_url="http://localhost", headers=AUTH) as client:
        task = client.post(API + "/chat", json=BODY).json()
        assert task["status"] == "failed"
        assert task["error"]["code"] == "TOOL_OUTPUT_INVALID"
        assert "数组" in task["error"]["message"]
