"""通过实际任务与 Gateway 检查编译开关、撤权和输入裁减边界。"""
import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
import httpx

from app.context_compiler import ContextSession
from app.errors import Fault
from app.main import API, create_app


ROOT = Path(__file__).resolve().parents[1]
AUTH = {"Authorization": "Bearer reader"}
BODY = {"business_id": "notes", "message": "会议纪要", "mode": "mock"}


def test_enabled_compiler_runs_before_model_without_new_sse_events(tmp_path):
    app = create_app(database=tmp_path / "agent.db", token="reader", model_settings={"context_compiler": True})
    with TestClient(app, base_url="http://localhost") as client:
        task = client.post(API + "/chat", headers=AUTH, json=BODY).json()
        assert task["status"] == "completed" and task["evidence"]
        runtime = app.state.runtime
        rows = runtime.store._db.execute("SELECT resource FROM audit_logs WHERE task_id=? AND action='context.compile'",
                                         (task["task_id"],)).fetchall()
        assert len(rows) >= 3
        report = json.loads(rows[-1][0])
        assert report["input_budget"] > report["token_estimate"] > 0
        assert "会议纪要" not in rows[-1][0]
        events = client.get(API + "/tasks/" + task["task_id"] + "/trace", headers=AUTH).json()["events"]
        assert not any(event["event"] == "context_compiled" for event in events)
        assert len(task["tool_calls"]) == 1


def test_enabled_compiler_rejects_impossible_budget_before_any_tool(tmp_path):
    settings = {"context_compiler": True, "context_window_tokens": 150, "context_output_reserve": 10,
                "context_safety_margin": 10}
    app = create_app(database=tmp_path / "agent.db", token="reader", model_settings=settings)
    with TestClient(app, base_url="http://localhost") as client:
        task = client.post(API + "/chat", headers=AUTH, json=BODY).json()
        assert task["status"] == "failed" and task["error"]["code"] == "CONTEXT_BUDGET_EXCEEDED"
        assert task["tool_calls"] == []


def test_context_reference_is_task_local_and_rechecks_revocation(tmp_path):
    app = create_app(database=tmp_path / "agent.db", token="reader", model_settings={})
    with TestClient(app, base_url="http://localhost"):
        runtime = app.state.runtime
        actor = runtime.principals["reader"]
        task, _ = runtime.store.start(actor, BODY, None, "1.0.0")
        gateway = runtime.gateway(runtime.store.context_id(task["task_id"]))
        session = ContextSession(gateway, {}, [])
        ref = session.add({"role": "tool", "tool_call_id": "one", "content": "私有结果"}, "tool_result", True)
        assert session.read(ref) == "私有结果"
        other, _ = runtime.store.start(actor, BODY, None, "1.0.0")
        other_session = ContextSession(runtime.gateway(runtime.store.context_id(other["task_id"])), {}, [])
        with pytest.raises(Fault) as unknown:
            other_session.read(ref)
        assert unknown.value.code == "PERMISSION_DENIED"
        actor["grants"].pop("notes")
        with pytest.raises(Fault) as denied:
            session.read(ref)
        assert denied.value.code == "PERMISSION_DENIED"
        with pytest.raises(Fault):
            session.compile()


def test_mock_result_matches_legacy_with_compiler_enabled(tmp_path):
    results = []
    for enabled in (False, True):
        app = create_app(database=tmp_path / str(enabled) / "agent.db", token="reader",
                         model_settings={"context_compiler": enabled})
        with TestClient(app, base_url="http://localhost") as client:
            task = client.post(API + "/chat", headers=AUTH, json=BODY).json()
            results.append({"answer": task["answer"], "evidence": task["evidence"], "status": task["status"],
                            "calls": len(task["tool_calls"])})
    assert results[0] == results[1] and results[1]["status"] == "completed"


@pytest.mark.parametrize("read_original", [False, True])
def test_large_tool_input_is_referenced_without_changing_gateway_result(tmp_path, read_original, monkeypatch):
    from app.runtime import Gateway
    original_execute = Gateway.execute
    actual_results = []
    async def record_result(self, *args, **kwargs):
        result = await original_execute(self, *args, **kwargs)
        actual_results.append(result)
        return result
    monkeypatch.setattr(Gateway, "execute", record_result)
    scenario = tmp_path / "scenarios" / "sample"
    scenario.mkdir(parents=True)
    data = {"summary": "两次观测", "details": "z" * 18000}
    evidence = [{"source_id": "sample-a", "title": "观测", "snippet": "两次观测", "version": "1.0.0",
                 "source_ref": "demo://sample/a", "data_as_of": "2026-11-03T00:00:00Z"},
                {"source_id": "sample-a", "title": "观测", "snippet": "两次观测", "version": "1.0.0",
                 "source_ref": "demo://sample/a", "data_as_of": "2026-11-03T01:00:00Z"}]
    manifest = {"id": "sample", "version": "1.0.0", "name": "测试", "description": "测试", "example": "查看",
                "offline_tool": "read_sample", "tools": [{"name": "read_sample", "description": "读取",
                "permission": "sample.read", "action_level": "read", "adapter": {"type": "python", "handler": "read_sample"},
                "input_schema": {"type": "object", "additionalProperties": False, "properties": {"text": {"type": "string"}}, "required": ["text"]},
                "output_schema": {"type": "object", "additionalProperties": False, "properties": {"summary": {"type": "string"}, "details": {"type": "string"}},
                                  "required": ["summary", "details"]}}]}
    (scenario / "scenario.json").write_text(json.dumps(manifest), encoding="utf-8")
    (scenario / "adapter.py").write_text("def read_sample(arguments, retrieve):\n    return " + repr({"data": data, "evidence": evidence}), encoding="utf-8")
    requests = []

    def upstream(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            name, args = "execute_capability", {"name": "read_sample", "arguments": {"text": "查看"}}
        else:
            result = json.loads(payload["messages"][-1]["content"])
            assert "details" not in result["data"] and result["data"]["summary"] == data["summary"]
            assert result["evidence"][0]["data_as_of"] == evidence[0]["data_as_of"]
            if not read_original:
                return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "两次观测 [E1]"}}]})
            name, args = "read_context_reference", {"ref": result["context_ref"]}
        call = {"id": "call-" + str(len(requests)), "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "tool_calls": [call]}}]})

    settings = {"context_compiler": True, "context_window_tokens": 8000, "context_output_reserve": 512,
                "context_safety_margin": 512, "base_url": "http://localhost", "model": "test",
                "transport": httpx.MockTransport(upstream)}
    app = create_app(scenarios=scenario.parent, database=tmp_path / "agent.db", token="reader", model_settings=settings)
    with TestClient(app, base_url="http://localhost") as client:
        task = client.post(API + "/chat", headers=AUTH, json={"business_id": "sample", "mode": "openai", "message": "查看"}).json()
        assert actual_results[0]["data"] == data
        assert len(task["tool_calls"]) == 1 and len(requests) == 2
        compiled_evidence = json.loads(requests[1]["messages"][-1]["content"])["evidence"]
        assert [entry["citation"] for entry in compiled_evidence] == ["[E1]", "[E2]"]
        if read_original:
            assert task["status"] == "failed" and task["error"]["code"] == "CONTEXT_BUDGET_EXCEEDED"
        else:
            assert task["status"] == "completed" and task["evidence"] == evidence
