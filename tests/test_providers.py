import asyncio
import json

import httpx
import pytest

from app.errors import Fault
from app.providers import run_provider


class Gateway:
    def __init__(self):
        self.events = []
        self.executions = []

    def trace(self, event, data):
        self.events.append((event, data))

    def context(self):
        return {}

    async def discover(self):
        return [{"name": "outline", "description": "Prepare an outline",
                 "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
                 "permission": "read", "action_level": "read"}]

    async def execute(self, name, arguments, call_id):
        self.executions.append((name, arguments, call_id))
        return {"data": {"summary": "Approved outline"}, "evidence": []}

    async def retrieve(self, query, top_k=5):
        return [{"source": "handbook", "text": query}]


def tool(call_id, name, args):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def completion(content=None, calls=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message.update(tool_calls=calls, reasoning_content="hidden thoughts")
    return httpx.Response(200, json={"choices": [{
        "finish_reason": "tool_calls" if calls else "stop", "message": message}]})


def run(gateway, handler, history=None, **settings):
    config = {"base_url": "https://llm.example/v1", "model": "test-model",
              "api_key": "private-token", "transport": httpx.MockTransport(handler), **settings}
    return asyncio.run(run_provider("openai", {}, "my private request", history or [], gateway, config))


def test_revoked_after_tool_returns_stops_next_model_request():
    gateway = Gateway()
    sent = []

    def check_context():
        if gateway.executions:
            raise Fault("PERMISSION_DENIED", "历史资源授权已撤销", 403)

    gateway.context = check_context

    def upstream(request):
        sent.append(request)
        if len(sent) == 1:
            return completion(calls=[tool("a", "execute_capability", {"name": "outline", "arguments": {}})])
        return completion("finished")

    with pytest.raises(Fault, match="历史资源授权已撤销"):
        run(gateway, upstream)
    assert len(sent) == 1


def test_tool_round_trip_uses_scoped_gateway_and_clean_messages():
    gateway = Gateway()
    requests = []

    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer private-token"
        body = json.loads(request.content)
        requests.append(body)
        messages = body["messages"]
        if len(requests) == 1:
            assert {t["function"]["name"] for t in body["tools"]} == {
                "discover_capabilities", "execute_capability", "retrieve_knowledge"}
            assert messages[-2:] == [{"role": "assistant", "content": "previous"},
                                      {"role": "user", "content": "my private request"}]
            return completion(calls=[tool("a", "discover_capabilities", {})])
        if len(requests) == 2:
            assert messages[-1]["tool_call_id"] == "a"
            assert "Prepare an outline" in messages[-1]["content"]
            return completion(calls=[
                tool("b", "execute_capability", {"name": "outline", "arguments": {"text": "draft"}}),
                tool("c", "retrieve_knowledge", {"query": "policy"})])
        assert [m["role"] for m in messages[-3:]] == ["assistant", "tool", "tool"]
        assert [m["tool_call_id"] for m in messages[-2:]] == ["b", "c"]
        assert "Approved outline" in messages[-2]["content"]
        assert "handbook" in messages[-1]["content"]
        assert "hidden thoughts" not in json.dumps(messages)
        return completion("Approved [E1]")

    assert run(gateway, handler, [{"role": "assistant", "content": "previous"}]) == "Approved [E1]"
    assert gateway.executions == [("outline", {"text": "draft"}, "b")]
    assert gateway.events
    traces = json.dumps(gateway.events)
    assert all(word not in traces for word in ("private-token", "my private request", "hidden thoughts"))


@pytest.mark.parametrize("limit", [{"max_rounds": 1}, {"max_calls": 1}])
def test_repeated_tool_calls_hit_budget(limit):
    n = 0

    def repeat(request):
        nonlocal n
        n += 1
        return completion(calls=[tool(str(n), "discover_capabilities", {})])

    with pytest.raises(Fault) as error:
        run(Gateway(), repeat, **limit)
    assert error.value.code == "PROVIDER_LIMIT"
    assert n <= 2


@pytest.mark.parametrize("answer", [
    lambda _: httpx.Response(401, text="private-token"),
    lambda _: completion(calls=[tool("x", "bypass_gateway", {})]),
    lambda _: completion(calls=[tool("x", "execute_capability", {"name": "outline", "arguments": []})]),
    lambda _: completion("Unsupported reference [E4]"),
    lambda _: httpx.Response(200, text="invalid JSON")])
def test_model_failures_are_sanitized_and_do_not_fallback(answer):
    gateway = Gateway()
    with pytest.raises(Fault) as error:
        run(gateway, answer)
    assert "private-token" not in error.value.message
    assert not gateway.executions


@pytest.mark.parametrize("settings", [{"base_url": "http://llm.example"}, {"model": ""},
                                      {"base_url": "https://a:b@llm.example"}, {"api_key": ""}])
def test_invalid_remote_configuration_never_connects(settings):
    def unexpected(_):
        pytest.fail("Invalid configuration reached provider")

    with pytest.raises(Fault):
        run(Gateway(), unexpected, **settings)


def test_offline_executes_configured_tool_and_handles_empty_scenario():
    gateway = Gateway()
    result = asyncio.run(run_provider("offline", {"offline_tool": "outline"},
                                     "test", [], gateway, {}))
    assert result == "Approved outline"
    assert gateway.executions[0][:2] == ("outline", {"text": "test"})
    assert asyncio.run(run_provider("offline", {}, "hello", [], gateway, {}))
    assert len(gateway.executions) == 1


def test_local_service_needs_no_key_and_timeouts_are_retryable():
    def timeout(request):
        assert "authorization" not in request.headers
        raise httpx.ReadTimeout("private error", request=request)

    with pytest.raises(Fault) as error:
        run(Gateway(), timeout, api_key="", base_url="http://127.0.0.1:8080/v1")
    assert error.value.status == 504 and error.value.retryable
    assert "private error" not in error.value.message


def test_valid_citation_cannot_link_to_fabricated_source():
    replies = iter([
        completion(calls=[tool("a", "retrieve_knowledge", {"query": "procedure"})]),
        completion("A source [E1](https://made-up.example)")])
    with pytest.raises(Fault) as error:
        run(Gateway(), lambda _: next(replies))
    assert error.value.code == "MODEL_INVALID_RESPONSE"


@pytest.mark.parametrize("usage,valid", [
    ({"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125, "diagnostics": "private-upstream"}, True),
    (None, False), ({"prompt_tokens": True, "completion_tokens": 3}, False),
    ({"prompt_tokens": 11, "completion_tokens": -1}, False),
    ({"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 14}, False),
])
def test_model_usage_is_numeric_and_optional_without_changing_answer(usage, valid):
    gw = Gateway()
    response = completion("OK").json()
    response["usage"] = usage
    assert run(gw, lambda _: httpx.Response(200, json=response),
               input_usd_per_million="2", output_usd_per_million="4") == "OK"
    recorded = [data for event, data in gw.events if event == "model_usage"]
    if valid:
        assert len(recorded) == 1 and recorded[0]["round"] == 1
        assert recorded[0]["prompt_tokens"] == 100 and recorded[0]["completion_tokens"] == 25
        assert recorded[0]["estimated_cost_usd"] == pytest.approx(.0003)
        assert "private-upstream" not in json.dumps(gw.events)
    else:
        assert recorded == []
