"""验证模型读取多轮历史后，原始资源类型的撤权会沿派生链生效。"""
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


API = "/api/v1/agent"
SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"


def setup_browser(db, grant, handler):
    actor = {"tenant_id": "t", "org_id": "o", "user_id": "u", "grants": {"text": grant}}
    settings = {"model": "test", "base_url": "https://model.example/v1",
                "api_key": "test-key", "transport": httpx.MockTransport(handler)}
    app = create_app(scenarios=SCENARIOS, database=db, model_settings=settings,
                     principals={"history-test": actor})
    return TestClient(app, base_url="http://localhost", headers={"Authorization": "Bearer history-test"})


@pytest.mark.parametrize("legacy", [False, True])
def test_revoked_history_type_across_restart(tmp_path, legacy):
    db = tmp_path / "history.db"
    grant = {"objects": ["same", "other"], "resource_types": ["document", "note"],
             "permissions": ["text.read"]}
    rounds = []

    def handler(request):
        messages = json.loads(request.content)["messages"]
        rounds.append([item["content"] for item in messages[1:]])
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": messages[-1]["content"]}}]})

    with setup_browser(db, grant, handler) as browser:
        base = {"business_id": "text", "mode": "openai",
                "session_id": browser.post(API + "/sessions", json={"business_id": "text"}).json()["session_id"]}
        plain = {**base, "context": {"object_ids": ["same"]}}
        source = {**plain, "context": {"object_ids": ["same"], "object_type": "document"}, "message": "原始机密"}
        assert browser.post(API + "/chat", json=source).json()["status"] == "completed"
        derived = {**plain, "message": "第一轮派生"}
        first = browser.post(API + "/chat", json=derived).json()
        assert "原始机密" in rounds[-1] and first["status"] == "completed"
        # 六轮窗口里移出原始任务，让第二轮只能通过第一轮继承类型。
        for index in range(5):
            public = browser.post(API + "/chat", json={**plain, "mode": "offline", "message": str(index)}).json()
            assert public["status"] == "completed"
        second_body = {**plain, "message": "第二轮派生"}
        key = {"Idempotency-Key": "second-turn"}
        second = browser.post(API + "/chat", json=second_body, headers=key).json()
        assert "原始机密" not in rounds[-1] and "第一轮派生" in rounds[-1]
        assert second["status"] == "completed"
        separate = browser.post(API + "/chat", json={
            **base, "message": "独立对象", "context": {"object_ids": ["other"]}}).json()

    if legacy:
        # 模拟升级前的数据库，保留真实任务、执行事件、幂等键和结果，删除新增的类型来源表。
        with sqlite3.connect(db) as connection:
            connection.execute("DROP TABLE IF EXISTS task_history_types")

    grant["resource_types"].remove("document")
    with setup_browser(db, grant, handler) as browser:
        for task in (first, second):
            path = API + "/tasks/" + task["task_id"]
            for suffix in ("", "/trace", "/events"):
                assert browser.get(path + suffix).status_code == 403
        assert browser.post(API + "/chat", json=second_body, headers=key).status_code == 403
        visible = {item["task_id"] for item in browser.get(API + "/tasks").json()["tasks"]}
        assert {public["task_id"], separate["task_id"]} <= visible
        assert {first["task_id"], second["task_id"]}.isdisjoint(visible)
        fresh = browser.post(API + "/chat", json={**plain, "message": "新问题"}).json()
        assert fresh["status"] == "completed"
        assert {"原始机密", "第一轮派生", "第二轮派生"}.isdisjoint(rounds[-1])

    # 再次重启时，已知没有继承敏感类型的新回答不能被旧数据回填误伤。
    with setup_browser(db, grant, handler) as browser:
        assert browser.get(API + "/tasks/" + fresh["task_id"]).status_code == 200


def test_revocation_during_derived_response(tmp_path):
    grant = {"objects": ["same"], "resource_types": ["document"], "permissions": ["text.read"]}

    def handler(request):
        messages = json.loads(request.content)["messages"]
        if messages[-1]["content"] == "撤权":
            assert any(item["content"] == "机密" for item in messages)
            grant["resource_types"].clear()
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "机密"}}]})

    with setup_browser(tmp_path / "active.db", grant, handler) as browser:
        sid = browser.post(API + "/sessions", json={"business_id": "text"}).json()["session_id"]
        base = {"business_id": "text", "session_id": sid, "mode": "openai"}
        assert browser.post(API + "/chat", json={**base, "message": "原始输入",
            "context": {"object_ids": ["same"], "object_type": "document"}}).json()["status"] == "completed"
        response = browser.post(API + "/chat", json={**base, "message": "撤权", "context": {"object_ids": ["same"]}})
        assert response.status_code == 403
        grant["resource_types"].append("document")
        task = browser.get(API + "/tasks").json()["tasks"][0]
        assert task["status"] == "failed" and task["answer"] == ""
        path = API + "/tasks/" + task["task_id"] + "/trace"
        assert all(event["event"] != "message_delta" for event in browser.get(path).json()["events"])
