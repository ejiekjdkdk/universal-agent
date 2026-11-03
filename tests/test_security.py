import asyncio
import hashlib
import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.errors import Fault
from app.main import create_app


API = "/api/v1/agent"
SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"
OWNER = {"Authorization": "Bearer primary-test-user"}
OTHER = {"Authorization": "Bearer other-test-user"}
SERVICE = {"Authorization": "Bearer internal-test-token"}


@pytest.mark.parametrize("tokens", [("",), ("   ",), ("read-only", "privileged")])
def test_embedded_service_rejects_ambiguous_identity_at_startup(tmp_path, tokens):
    principals = {
        token: {"tenant_id": "t", "org_id": "o", "user_id": "u",
                "grants": {"text": {"permissions": ["text.read"] if i == 0 else []}}}
        for i, token in enumerate(tokens)
    }
    app = create_app(database=tmp_path / "db", scenarios=SCENARIOS, principals=principals)
    with pytest.raises(ValueError), TestClient(app):
        pass


def test_invalid_live_principal_update_fails_closed_and_can_be_corrected(tmp_path):
    actor = {"tenant_id": "t", "org_id": "o", "user_id": "u", "grants": {"text": {"permissions": ["text.read"]}}}
    actors = {"owner": actor}
    app = create_app(database=tmp_path / "db", scenarios=SCENARIOS, principals=actors)
    with TestClient(app, base_url="http://localhost") as client:
        actors["second"] = dict(actor, grants={"text": {"permissions": []}})
        response = client.post(API + "/chat", headers={"Authorization": "Bearer second"},
                               json={"business_id": "text", "message": "sample"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "IDENTITY_SOURCE_UNAVAILABLE"
        del actors["second"]
        assert client.get(API + "/scenarios").status_code == 401
        assert client.get(API + "/scenarios", headers={"Authorization": "Bearer owner"}).status_code == 200


@pytest.fixture
def secured_app(tmp_path, monkeypatch, request):
    monkeypatch.setenv("AGENT_SERVICE_TOKEN", "internal-test-token")
    principals = {
        token: {
            "tenant_id": "local", "org_id": org, "user_id": "same-name",
            "grants": {"text": {"permissions": ["text.read", "knowledge.read"],
                                "objects": ["mine"], "resource_types": ["document"]}},
        }
        for token, org in [("primary-test-user", "group-one"), ("other-test-user", "group-two")]
    }
    scenarios = SCENARIOS
    if getattr(request, "param", None) == "object_bound":
        scenarios = tmp_path / "scenarios"
        extension = scenarios / "text"
        extension.mkdir(parents=True)
        shutil.copyfile(SCENARIOS / "text" / "adapter.py", extension / "adapter.py")
        manifest = json.loads((SCENARIOS / "text" / "scenario.json").read_text(encoding="utf-8"))
        del manifest["offline_tool"]
        tool = manifest["tools"][0]
        tool["object_ids_field"] = "ids"
        tool["input_schema"]["properties"]["ids"] = {
            "type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 128}, "maxItems": 64,
        }
        tool["input_schema"]["required"].append("ids")
        (extension / "scenario.json").write_text(json.dumps(manifest), encoding="utf-8")
    app = create_app(scenarios=scenarios, database=tmp_path / "security.sqlite",
                     model_settings={}, principals=principals)
    with TestClient(app, base_url="http://localhost") as client:
        yield client, app.state.runtime, principals["primary-test-user"]


def running_task(runtime, actor, **overrides):
    body = {"business_id": "text", "message": "run", **overrides}
    task, _ = runtime.store.start(actor, body, None, "1.0.0")
    return task, runtime.store.context_id(task["task_id"])


def execute(client, run_id, call_id="first", text="word", headers=SERVICE):
    return client.post("/internal/agent/execute", headers=headers, json={
        "run_context_id": run_id, "capability_id": "text_stats",
        "arguments": {"text": text}, "call_id": call_id,
    })


def test_other_organization_cannot_enumerate_reopen_replay_or_cancel(secured_app):
    client, _, _ = secured_app
    created = client.post(API + "/chat", headers=OWNER, json={"business_id": "text", "message": "demo"})
    assert created.status_code == 200, created.text
    task = created.json()
    path = API + "/tasks/" + task["task_id"]
    assert client.get(API + "/tasks", headers=OTHER).json() == {"tasks": []}
    for resource in (path, path + "/trace", path + "/events", API + "/sessions/" + task["session_id"]):
        assert client.get(resource, headers=OTHER).status_code == 404
    assert client.post(path + "/cancel", headers=OTHER).status_code == 404
    reuse = client.post(API + "/chat", headers=OTHER, json={
        "business_id": "text", "message": "other", "session_id": task["session_id"],
    })
    assert reuse.status_code == 404
    assert client.get(path, headers=OWNER).json()["status"] == "completed"


def test_user_token_cannot_act_as_tool_service_even_with_live_run_id(secured_app):
    client, runtime, actor = secured_app
    task, run_id = running_task(runtime, actor)
    assert execute(client, run_id, headers=OWNER).status_code == 401
    assert runtime.store.task(actor, task["task_id"])["tool_calls"] == []


def test_permission_revoked_after_discovery_before_execution(secured_app):
    client, runtime, actor = secured_app
    task, run_id = running_task(runtime, actor)
    seen = client.post("/internal/agent/discover", headers=SERVICE, json={"run_context_id": run_id})
    assert [entry["name"] for entry in seen.json()["capabilities"]] == ["text_stats"]
    actor["grants"]["text"]["permissions"].remove("text.read")
    denied = execute(client, run_id)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "PERMISSION_DENIED"
    events = runtime.store.events(task["task_id"], 0)
    assert any(e["event"] == "authorization" and e["data"]["allowed"] is False for e in events)
    assert runtime.store.task(actor, task["task_id"])["tool_calls"] == []


@pytest.mark.parametrize("context", [
    {"object_type": "document", "object_ids": ["foreign"]},
    {"object_type": "other_type", "object_ids": ["mine"]},
])
def test_public_session_and_chat_reject_ungranted_objects(secured_app, context):
    client, _, _ = secured_app
    for endpoint, extra in (("sessions", {}), ("chat", {"message": "test"})):
        request = {"business_id": "text", "context": context, **extra}
        response = client.post(API + "/" + endpoint, headers=OWNER, json=request)
        assert response.status_code == 403
    assert client.get(API + "/tasks", headers=OWNER).json()["tasks"] == []


def test_object_revocation_invalidates_live_gateway(secured_app):
    client, runtime, actor = secured_app
    task, run_id = running_task(runtime, actor, context={"object_type": "document", "object_ids": ["mine"]})
    actor["grants"]["text"]["objects"].clear()
    denied = execute(client, run_id)
    assert denied.status_code == 403
    assert runtime.store.task(actor, task["task_id"])["tool_calls"] == []


@pytest.mark.parametrize("secured_app", ["object_bound"], indirect=True)
def test_object_tool_rejects_ids_outside_run_even_if_granted_to_actor(secured_app):
    client, runtime, actor = secured_app
    actor["grants"]["text"]["objects"].append("another")
    task, run_id = running_task(runtime, actor, context={"object_type": "document", "object_ids": ["mine"]})

    def call(ids, call_id):
        return client.post("/internal/agent/execute", headers=SERVICE, json={
            "run_context_id": run_id, "capability_id": "text_stats", "arguments": {"text": "test", "ids": ids},
            "call_id": call_id,
        })

    denied = call(["mine", "another"], "outside")
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "PERMISSION_DENIED"
    assert runtime.store.task(actor, task["task_id"])["tool_calls"] == []
    accepted = call(["mine"], "inside")
    assert accepted.status_code == 200 and accepted.json()["data"]["characters"] == 4


def test_notes_tool_cannot_read_its_bound_documents_without_knowledge_grant(secured_app):
    client, runtime, actor = secured_app
    actor["grants"]["notes"] = {"permissions": ["notes.read"]}
    task, run_id = running_task(runtime, actor, business_id="notes")
    request = {"run_context_id": run_id, "capability_id": "notes_search",
               "arguments": {"text": "会议纪要"}, "call_id": "forbidden"}
    denied = client.post("/internal/agent/execute", json=request, headers=SERVICE)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "PERMISSION_DENIED"
    assert any(e["event"] == "authorization" and e["data"]["resource"] == "retrieve_knowledge"
               and not e["data"]["allowed"] for e in runtime.store.events(task["task_id"], 0))
    actor["grants"]["notes"]["permissions"].append("knowledge.read")
    allowed = client.post("/internal/agent/execute", json={**request, "call_id": "allowed"}, headers=SERVICE)
    assert allowed.status_code == 200 and allowed.json()["evidence"]


@pytest.mark.parametrize("query", ["会议纪要", "unmatched-query-375295"])
def test_idempotent_replay_rechecks_bound_document_permission_even_for_empty_matches(secured_app, query):
    client, runtime, actor = secured_app
    actor["grants"]["notes"] = {"permissions": ["notes.read", "knowledge.read"]}
    task, run_id = running_task(runtime, actor, business_id="notes")
    request = {"run_context_id": run_id, "capability_id": "notes_search",
               "arguments": {"text": query}, "call_id": "same-call"}
    first = client.post("/internal/agent/execute", json=request, headers=SERVICE)
    assert first.status_code == 200, first.text
    assert bool(first.json()["evidence"]) == (query == "会议纪要")
    actor["grants"]["notes"]["permissions"].remove("knowledge.read")
    replay = client.post("/internal/agent/execute", json=request, headers=SERVICE)
    assert replay.status_code == 403 and replay.json()["error"]["code"] == "PERMISSION_DENIED"
    assert len(runtime.store.task(actor, task["task_id"])["tool_calls"]) == 1


def test_revoking_object_hides_completed_results_and_trace_from_owner(secured_app):
    client, _, actor = secured_app
    response = client.post(API + "/chat", headers=OWNER, json={
        "business_id": "text", "message": "analyze", "context": {"object_type": "document", "object_ids": ["mine"]},
    })
    assert response.status_code == 200 and response.json()["status"] == "completed", response.text
    task = response.json()
    actor["grants"]["text"]["objects"].remove("mine")
    path = API + "/tasks/" + task["task_id"]
    for url in (path, path + "/trace", path + "/events"):
        assert client.get(url, headers=OWNER).status_code in (403, 404)
    assert client.get(API + "/tasks", headers=OWNER).json()["tasks"] == []


def test_secret_tool_input_is_represented_by_length_and_digest_in_preview(secured_app):
    client, runtime, actor = secured_app
    task, run_id = running_task(runtime, actor)
    secret = "private-message: a test secret"
    response = execute(client, run_id, text=secret)
    assert response.status_code == 200, response.text
    trace = client.get(API + "/tasks/" + task["task_id"] + "/trace", headers=OWNER)
    assert secret not in trace.text
    recorded = next(e["data"] for e in trace.json()["events"] if e["event"] == "tool_input")
    preview = recorded["arguments"]["text"]
    assert preview["chars"] == len(secret)
    assert hashlib.sha256(secret.encode()).hexdigest().startswith(preview["sha256_prefix"])
    assert len(preview["sha256_prefix"]) >= 8


def test_retryable_read_rate_limit_retries_once_then_records_one_completion(secured_app, monkeypatch):
    client, runtime, actor = secured_app
    task, run_id = running_task(runtime, actor)
    attempts = []
    original = runtime.catalog.execute

    async def rate_limited_once(scenario, name, arguments, *, authorize_retrieval):
        attempts.append(name)
        if len(attempts) == 1:
            raise Fault("RATE_LIMITED", "retry later", 429, True)
        return await original(scenario, name, arguments, authorize_retrieval=authorize_retrieval)

    monkeypatch.setattr(runtime.catalog, "execute", rate_limited_once)
    first = execute(client, run_id)
    assert first.status_code == 200, first.text
    replay = execute(client, run_id)
    assert replay.json()["execution_id"] == first.json()["execution_id"]
    assert attempts == ["text_stats", "text_stats"]
    events = runtime.store.events(task["task_id"], 0)
    assert [e["event"] for e in events].count("step_completed") == 1
    retries = [e["data"] for e in events if e["event"] == "tool_retry"]
    assert len(retries) == 1
    assert retries[0]["error_code"] == "RATE_LIMITED" and retries[0]["delay_s"] > 0


def test_cancelling_inflight_adapter_releases_worker_and_emits_one_terminal(secured_app, monkeypatch):
    client, runtime, _ = secured_app
    entered, stopped = threading.Event(), threading.Event()

    async def slow_adapter(*_, authorize_retrieval):
        entered.set()
        try:
            await asyncio.sleep(10)
        finally:
            stopped.set()

    monkeypatch.setattr(runtime.catalog, "execute", slow_adapter)
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(client.post, API + "/chat", headers=OWNER,
                              json={"business_id": "text", "message": "wait"})
        assert entered.wait(5), "worker never reached the adapter"
        task = client.get(API + "/tasks", headers=OWNER).json()["tasks"][0]
        path = API + "/tasks/" + task["task_id"]
        cancelled = client.post(path + "/cancel", headers=OWNER)
        assert cancelled.status_code == 202 and cancelled.json()["status"] == "cancelled"
        assert stopped.wait(5), "adapter did not receive cancellation"
        assert waiting.result(timeout=5).json()["status"] == "cancelled"
    replay = client.get(path + "/events", headers=OWNER)
    assert replay.text.count("event: task_cancelled\n") == 1
    assert "event: task_completed\n" not in replay.text and "event: task_failed\n" not in replay.text
    assert client.get(path, headers=OWNER).json()["tool_calls"][0]["status"] == "unknown_outcome"


@pytest.mark.parametrize("revoked_field", ["permissions", "objects", "resource_types"])
def test_open_sse_rechecks_access_before_each_buffered_event(secured_app, revoked_field):
    client, runtime, actor = secured_app
    task, _ = running_task(runtime, actor, context={"object_ids": ["mine"], "object_type": "document"})
    task_id = task["task_id"]
    runtime.store.emit(task_id, "authorization", {"allowed": True, "permission": "text.read"})
    runtime.store.emit(task_id, "message_delta", {"delta": "private-answer-marker"})
    runtime.store.finish(task_id, "completed", {"answer": "private-answer-marker"})
    endpoint = next(route.endpoint for route in client.app.routes
                    if getattr(route, "path", "") == API + "/tasks/{task_id}/events")

    async def consume():
        response = await endpoint(task_id, actor=actor, cursor=0)
        stream = response.body_iterator
        try:
            assert "event: task_started" in await anext(stream)
            # 模拟客户端接收首帧后暂停；同一批后续事件仍留在服务端迭代器中。
            actor["grants"]["text"][revoked_field].clear()
            with pytest.raises(Fault) as direct:
                runtime.authorize_task(actor, "text", task_id)
            assert direct.value.code == "PERMISSION_DENIED"
            with pytest.raises(Fault) as streamed:
                await anext(stream)
            assert streamed.value.code == "PERMISSION_DENIED"
        finally:
            await stream.aclose()

    asyncio.run(consume())
