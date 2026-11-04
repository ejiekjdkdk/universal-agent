"""实际任务、权限和工具存储在模型切换后保持唯一执行。"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from test_model_router import policy, response


AUTH = {"Authorization": "Bearer reader"}
BODY = {"business_id": "notes", "message": "会议纪要", "mode": "openai"}


def call_reply(call_id):
    call = {"id": call_id, "type": "function", "function": {"name": "execute_capability",
            "arguments": json.dumps({"name": "notes_search", "arguments": {"text": "会议纪要"}})}}
    body = response()
    body["choices"][0] = {"index": 0, "message": {"role": "assistant", "tool_calls": [call]}, "finish_reason": "tool_calls"}
    return body


def test_fallback_new_call_id_reuses_completed_execution(tmp_path):
    sent = []
    def handler(request):
        sent.append((request.url.port, json.loads(request.content)))
        if len(sent) == 1:
            return httpx.Response(200, json=call_reply("primary-read"))
        if len(sent) == 2:
            return httpx.Response(429)
        if len(sent) == 3:
            return httpx.Response(200, json=call_reply("secondary-new-id"))
        return httpx.Response(200, json=response("已查到资料 [E1]"))
    app = create_app(database=tmp_path / "agent.db", token="reader",
                     model_settings={"provider_policy": policy(), "transport": httpx.MockTransport(handler)})
    with TestClient(app, base_url="http://localhost") as client:
        result = client.post("/api/v1/agent/chat", headers=AUTH, json=BODY).json()
        assert result.get("status") == "completed", result
        assert [port for port, _ in sent] == [18881, 18881, 18882, 18882]
        assert len(result["tool_calls"]) == 1 and result["evidence"]
        assert app.state.runtime.store._db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1
        events = client.get("/api/v1/agent/tasks/" + result["task_id"] + "/trace", headers=AUTH).json()["events"]
        assert sum(row["event"] == "task_completed" for row in events) == 1
        assert not any(row["event"] == "provider_switched" for row in events)
        reports = app.state.runtime.store._db.execute("SELECT resource FROM audit_logs WHERE action='model.attempt'").fetchall()
        assert len(reports) == 4 and any('"usage_known":false' in row[0] for row in reports)


@pytest.mark.parametrize("change", ["permission", "freshness", "small_window", "data_class"])
def test_fallback_cannot_bypass_authority_or_input_budget(tmp_path, change):
    config = policy()
    if change == "small_window":
        config["profiles"]["secondary"]["context_window_tokens"] = 300
    if change == "data_class":
        config["profiles"]["secondary"]["data_handling_class"] = ["public"]
    sent = []
    def handler(request):
        sent.append(request)
        if len(sent) == 1:
            return httpx.Response(200, json=call_reply("read"))
        if change == "permission":
            app.state.runtime.principals["reader"]["grants"]["notes"]["permissions"].clear()
        if change == "freshness":
            app.state.runtime.read_cache.policies["notes/notes_search"]["freshness"] = "changed"
        return httpx.Response(503)
    settings = {"provider_policy": config, "transport": httpx.MockTransport(handler),
                "p2": {"read_cache": {"tools": {"notes/notes_search": {"ttl_s": 60, "freshness": "v1"}}}}}
    app = create_app(database=tmp_path / "agent.db", token="reader", model_settings=settings)
    with TestClient(app, base_url="http://localhost") as client:
        result = client.post("/api/v1/agent/chat", headers=AUTH, json=BODY).json()
        if change == "permission":
            # 撤权后连任务结果也不可读；终态从测试持有的数据库确认。
            assert result["error"]["code"] == "PERMISSION_DENIED"
            assert app.state.runtime.store._db.execute("SELECT status FROM tasks").fetchone()[0] == "failed"
        else:
            assert result.get("status") == "failed", result
        assert len(sent) == 2 and all(request.url.port == 18881 for request in sent)
        assert app.state.runtime.store._db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1
        if change == "small_window":
            assert result["error"]["code"] == "CONTEXT_BUDGET_EXCEEDED"


def test_policy_only_configuration_is_visible_as_model_ready(tmp_path):
    app = create_app(database=tmp_path / "agent.db", token="reader", model_settings={"provider_policy": policy()})
    with TestClient(app, base_url="http://localhost") as client:
        result = client.get("/api/v1/agent/scenarios", headers=AUTH).json()
        assert result["model_available"] is True
