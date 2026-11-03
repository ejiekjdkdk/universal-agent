import json
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.observability import install_observability_routes
from app.store import Store


def test_admin_metrics_and_otlp_trace_are_correlated_and_redacted(tmp_path):
    with closing(Store(tmp_path / "telemetry.db")) as store:
        actor = {"tenant_id": "secret-tenant", "org_id": "secret-org", "user_id": "secret-name"}
        t, _ = store.start(actor, {"business_id": "notes", "message": "secret-prompt"}, None, "1.0.0")
        tid = t["task_id"]
        execution_id, _ = store.begin_call(tid, "one", "read_notes", "1.0.0", {"x": "secret-arg"})
        store.complete_call(tid, "one", {"data": {"summary": "secret-tool-output"}})
        store.emit(tid, "approval_requested", {"approval_id": "ap_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                              "step_id": "secret-step", "raw": "secret-proposal",
                                              "status": "secret-outcome", "error_code": "SECRET_CUSTOM_ERROR",
                                              "execution_id": "secret-execution", "capability_id": "secret-tool"})
        for _ in range(145):
            store.emit(tid, "step_queued", {"step_id": "secret-forged"})
        store.audit("tool_permission", "deny", actor, tid, "secret-resource", "PERMISSION_DENIED")
        store.finish(tid, "failed", {}, {"code": "TOOL_TIMEOUT", "message": "secret-failure"})
        app = FastAPI()
        def admin(req: Request):
            if req.headers.get("Authorization") != "Bearer admin":
                raise HTTPException(403)
        install_observability_routes(app, lambda: SimpleNamespace(store=store), admin)
        with TestClient(app) as client:
            base = "/api/v1/agent-admin/observability"
            assert client.get(base + "/metrics").status_code == 403
            assert client.get(base + "/traces/" + tid).status_code == 403
            client.headers["Authorization"] = "Bearer admin"
            metrics = client.get(base + "/metrics").json()
            assert metrics["permission_denial_count"] == 1
            assert metrics["token_cost_per_task"] is None
            assert metrics["task_completion_rate"] == 0
            assert client.get(base + "/alerts").json()["alerts"]
            first = client.get(base + "/traces/" + tid)
            trace = first.json()
            spans = trace["resourceSpans"][0]["scopeSpans"][0]["spans"]
            assert len({s["traceId"] for s in spans}) == 1
            assert all(len(s["traceId"]) == 32 and len(s["spanId"]) == 16 for s in spans)
            assert {s["name"] for s in spans} >= {"agent.request", "agent.approval", "agent.tool"}
            assert spans[0]["status"]["code"] == 2
            assert execution_id in json.dumps(spans) and tid in json.dumps(spans)
            assert "secret-" not in json.dumps([metrics, trace])
            assert "SECRET_CUSTOM_ERROR" not in json.dumps(trace)
            assert first.headers["X-Next-Page"] == "1"
            next_page = client.get(base + "/traces/" + tid + "?page=1")
            next_spans = next_page.json()["resourceSpans"][0]["scopeSpans"][0]["spans"]
            assert len(next_spans) > 1 and "X-Next-Page" not in next_page.headers
            assert {s["spanId"] for s in next_spans[1:]}.isdisjoint(s["spanId"] for s in spans[1:])


def test_app_exports_completed_sse_and_enforces_distinct_admin(tmp_path, monkeypatch):
    from app.main import create_app
    monkeypatch.setenv("AGENT_ADMIN_TOKEN", "local-eval-admin")
    root = Path(__file__).resolve().parents[1]
    app = create_app(scenarios=root / "scenarios", database=tmp_path / "db", token="local-eval-user")
    admin = {"Authorization": "Bearer local-eval-admin"}
    user = {"Authorization": "Bearer local-eval-user"}
    base = "/api/v1/agent-admin/observability"
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post("/api/v1/agent/chat", headers=user,
                               json={"business_id": "text", "message": "secret-customer-text",
                                     "response_mode": "streaming"})
        assert response.status_code == 200 and "task_completed" in response.text
        tid = client.get("/api/v1/agent/tasks", headers=user).json()["tasks"][0]["task_id"]
        assert client.get(base + "/traces/" + tid, headers=user).status_code == 401
        trace = client.get(base + "/traces/" + tid, headers=admin)
        assert trace.status_code == 200 and trace.headers["Cache-Control"] == "no-store"
        assert "secret-customer-text" not in trace.text and "agent.response" in trace.text
        m = client.get(base + "/metrics", headers=admin).json()
        assert m["first_event_sample_count"] == 1 and m["task_completion_rate"] == 1


def test_cost_for_observed_rounds_and_unknown_partial_coverage(tmp_path, monkeypatch):
    import httpx
    from app.main import create_app
    base = "/api/v1/agent-admin/observability"
    admin, user = {"Authorization": "Bearer eval-admin"}, {"Authorization": "Bearer eval-user"}
    monkeypatch.setenv("AGENT_ADMIN_TOKEN", "eval-admin")
    monkeypatch.setenv("AGENT_MODEL_INPUT_USD_PER_MILLION", "2")
    monkeypatch.setenv("AGENT_MODEL_OUTPUT_USD_PER_MILLION", "4")
    counts = {"prompt_tokens": 500, "completion_tokens": 250}
    responses = iter([counts, None, counts])
    def reply(request):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}],
                                        "usage": next(responses)})
    cfg = {"base_url": "https://model.example", "model": "fixture", "api_key": "sensitive-key",
           "transport": httpx.MockTransport(reply)}
    root = Path(__file__).resolve().parents[1]
    app = create_app(scenarios=root/"scenarios", database=tmp_path/"db", token="eval-user", model_settings=cfg)
    with TestClient(app, base_url="http://localhost") as client:
        def turn():
            r = client.post("/api/v1/agent/chat", headers=user, json={"business_id": "text", "mode": "openai",
                                                                     "message": "secret-request"})
            assert r.json()["answer"] == "OK"
            return client.get(base + "/metrics", headers=admin).json(), r.json()["task_id"]
        m, tid = turn()
        assert m["token_usage"] == {"prompt_tokens": 500, "completion_tokens": 250, "total_tokens": 750}
        assert m["estimated_cost_usd"] == .002 and m["token_cost_per_task"] == .002
        assert m["usage_round_coverage"] == 1
        trace = client.get(base + "/traces/" + tid, headers=admin).text
        assert "prompt_tokens" in trace and "secret-request" not in trace and "sensitive-key" not in trace
        partial, _ = turn()
        assert partial["observed_token_usage"] == m["token_usage"] and partial["token_usage"] is None
        assert partial["usage_round_coverage"] == .5 and partial["estimated_cost_usd"] is None
        monkeypatch.delenv("AGENT_MODEL_INPUT_USD_PER_MILLION")
        last, _ = turn()
        assert last["priced_round_count"] == 1 and last["usage_round_count"] == 2
        assert last["observed_estimated_cost_usd"] == .002 and last["token_cost_per_task"] is None
