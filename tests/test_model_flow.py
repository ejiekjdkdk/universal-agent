import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.main import create_app


API = "/api/v1/agent"
HEADERS = {"Authorization": "Bearer local-test-token"}
KEY = "test-upstream-private-key"


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def client(tmp_path, handler):
    settings = {"model": "test-model", "api_key": KEY, "base_url": "https://model.example/v1",
                "transport": httpx.MockTransport(handler)}
    app = create_app(scenarios=Path(__file__).resolve().parents[1] / "scenarios",
                     database=tmp_path / "test.db", token="local-test-token", model_settings=settings)
    return TestClient(app, base_url="http://localhost", headers=HEADERS)


def ask(browser, message, **fields):
    return browser.post(API + "/chat", json={"mode": "openai", "business_id": "notes",
                                             "message": message, **fields})


def test_real_gateway_scope_evidence_trace_and_next_turn(tmp_path):
    first = "会议纪要和周复盘分别要记录什么？"
    answer = "会议纪要记录决定与待办 [E1]。周复盘记录本周事项 [E2]。"
    followup = "把上一条改写成一句话。"
    batch = [
        call("search", "execute_capability", {"name": "notes_search", "arguments": {"text": "会议纪要"}}),
        call("repeat", "retrieve_knowledge", {"query": "会议纪要", "top_k": 1}),
        call("review", "retrieve_knowledge", {"query": "周复盘", "top_k": 1}),
    ]
    replies = iter([{"tool_calls": [call("list", "discover_capabilities", {})]},
                    {"tool_calls": batch, "reasoning_content": "hidden-private-marker"},
                    {"content": "<think>hidden-private-marker</think>\n" + answer},
                    {"content": "已改写。"}])
    rounds = []

    def handler(request):
        rounds.append(json.loads(request.content)["messages"])
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", **next(replies)}}]})

    with client(tmp_path, handler) as browser:
        result = ask(browser, first)
        assert result.is_success, result.text
        run = result.json()
        assert run["status"] == "completed" and run["answer"] == answer, run
        assert "notes_search" in rounds[1][-1]["content"]
        results = [json.loads(msg["content"]) for msg in rounds[2] if msg["role"] == "tool"][-3:]
        assert results[0]["data"]["matches"] == 1
        citations = [r["evidence"][0] for r in results]
        assert [(s["source_id"], s["citation"]) for s in citations] == [
            ("note-meeting", "[E1]"), ("note-meeting", "[E1]"), ("note-review", "[E2]")]
        assert [s["source_id"] for s in run["evidence"]] == ["note-meeting", "note-review"]
        assert run["evidence"][1]["snippet"] == citations[2]["snippet"]
        path = API + "/tasks/" + run["task_id"]
        assert browser.get(path).json()["answer"] == answer
        events = browser.get(path + "/trace").json()["events"]
        names = [event["event"] for event in events]
        assert names.count("model_round") == 3 and "tool_result" in names and "authorization" in names
        assert names[-1] == "task_completed"
        assert all(secret not in json.dumps(events) for secret in (KEY, "hidden-private-marker"))
        assert "hidden-private-marker" not in json.dumps(rounds[2])
        second = ask(browser, followup, session_id=run["session_id"]).json()
        assert second["status"] == "completed", second
        assert rounds[-1][-3:] == [{"role": "user", "content": first},
                                   {"role": "assistant", "content": answer},
                                   {"role": "user", "content": followup}]
        assert second["evidence"] == []


def test_misconfigured_upstream_echoing_credential_cannot_publish_it(tmp_path):
    def echo(request):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                                      "content": request.headers["Authorization"]}}]})

    with client(tmp_path, echo) as browser:
        result = ask(browser, "请回答")
        run = result.json()
        assert run["status"] == "failed" and run["error"]["code"] == "MODEL_INVALID_RESPONSE"
        trace = browser.get(API + "/tasks/" + run["task_id"] + "/trace")
        assert KEY not in result.text and KEY not in trace.text
