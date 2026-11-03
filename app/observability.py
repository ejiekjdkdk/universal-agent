"""Admin-only local metrics and OTLP/HTTP JSON export from persisted lifecycle events."""
from collections import Counter, defaultdict, deque
from datetime import datetime
import hashlib
import math
import re
import time

from fastapi import Depends, HTTPException, Query, Response


PREFIX = "/api/v1/agent-admin/observability"
PAGE_SIZE = 128
OUTCOMES = {"running", "completed", "failed", "cancelled", "partial", "succeeded", "unknown_outcome",
            "waiting_approval", "ready", "leased", "skipped", "blocked", "approved", "rejected", "expired"}
ERRORS = {"TOOL_TIMEOUT", "TASK_TIMEOUT", "PERMISSION_DENIED", "ACTION_NOT_ALLOWED", "RATE_LIMITED",
          "LEASE_EXPIRED", "LEASE_LOST", "STEP_ATTEMPTS_EXHAUSTED", "UPSTREAM_DEPENDENCY_FAILED",
          "UPSTREAM_UNAVAILABLE", "TASK_CANCELLED", "MODEL_TIMEOUT", "MODEL_UPSTREAM_ERROR",
          "MODEL_INVALID_RESPONSE", "MODEL_NOT_CONFIGURED", "MODEL_CONNECTION_ERROR", "INTERNAL_ERROR",
          "SCHEMA_VALIDATION_FAILED", "CAPABILITY_OUTPUT_INVALID", "EXECUTION_BUDGET_EXCEEDED",
          "APPROVAL_EXPIRED", "APPROVAL_REJECTED", "COMMIT_OUTCOME_UNKNOWN", "RELEASE_REVOKED"}
EVENT_SPANS = {
    "planner_started": "agent.plan", "plan_validated": "agent.plan", "provider_selected": "agent.planner",
    "step_leased": "agent.step", "step_completed": "agent.step", "step_failed": "agent.step",
    "step_queued": "agent.step", "step_cancelled": "agent.step", "step_skipped": "agent.step",
    "step_retrying": "agent.step", "authorization": "agent.authorization", "tool_retry": "agent.adapter",
    "approval_required": "agent.approval", "approval_requested": "agent.approval",
    "approval_expired": "agent.approval", "approval_decided": "agent.approval",
    "mock_commit_verified": "agent.approval", "mock_commit_unknown": "agent.approval",
    "task_completed": "agent.response", "task_cancelled": "agent.response", "task_failed": "agent.response",
    "model_usage": "agent.model.usage",
}


def _ns(stamp):
    return int(datetime.fromisoformat(stamp).timestamp() * 1_000_000) * 1000


def _rate(count, total):
    return round(count / total, 6) if total else None


def _p95(values):
    return round(sorted(values)[math.ceil(len(values) * .95) - 1], 3) if values else None


def _usage_metrics(db):
    rows = db.execute("""WITH recent AS (SELECT task_id,mode FROM tasks ORDER BY rowid DESC LIMIT 1000)
        SELECT e.task_id, json_extract(e.payload,'$.round') AS round_no,
          MAX(e.event='model_round') AS requested,
          SUM(e.event='model_usage') AS reports,
          MAX(CASE WHEN e.event='model_usage' AND json_type(e.payload,'$.prompt_tokens')='integer'
              THEN json_extract(e.payload,'$.prompt_tokens') END) AS prompt,
          MAX(CASE WHEN e.event='model_usage' AND json_type(e.payload,'$.completion_tokens')='integer'
              THEN json_extract(e.payload,'$.completion_tokens') END) AS completion,
          MAX(CASE WHEN e.event='model_usage' AND json_type(e.payload,'$.estimated_cost_usd') IN ('integer','real')
              THEN json_extract(e.payload,'$.estimated_cost_usd') END) AS usd
        FROM recent r JOIN events e USING(task_id) WHERE r.mode='openai' AND e.event IN ('model_round','model_usage')
          AND json_type(e.payload,'$.round')='integer' AND json_extract(e.payload,'$.round') BETWEEN 1 AND 12
        GROUP BY e.task_id,round_no HAVING requested=1""").fetchall()
    observed = [r for r in rows if r["reports"] == 1 and all(
        type(r[k]) is int and 0 <= r[k] < 2**31 for k in ("prompt", "completion"))]
    priced = [r for r in observed if r["usd"] is not None and math.isfinite(r["usd"]) and r["usd"] >= 0]
    p, c = sum(r["prompt"] for r in observed), sum(r["completion"] for r in observed)
    totals = {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p+c} if observed else None
    cost = round(math.fsum(r["usd"] for r in priced), 12) if priced else None
    known_cost = cost if len(priced) == len(rows) else None
    count = len({r["task_id"] for r in rows})
    return {"model_task_count": count, "model_round_count": len(rows), "usage_round_count": len(observed),
            "priced_round_count": len(priced), "usage_round_coverage": _rate(len(observed), len(rows)),
            "observed_token_usage": totals, "token_usage": totals if len(observed) == len(rows) else None,
            "observed_estimated_cost_usd": cost, "estimated_cost_usd": known_cost,
            "token_cost_per_task": round(known_cost / count, 12) if known_cost is not None and count else None,
            "cost_basis": "operator_usd_per_million_input_and_output_tokens" if priced else None}


def _metrics(store, first_event):
    with store._transaction(write=False):
        db = store._db
        usage = _usage_metrics(db)
        tasks = db.execute("""SELECT t.status,t.created_at,t.finished_at,
            CASE WHEN p.task_id IS NULL THEN 'reactive' ELSE 'durable_plan' END AS mode
            FROM tasks t LEFT JOIN plans p USING(task_id) ORDER BY t.rowid DESC LIMIT 1000""").fetchall()
        calls = db.execute("""SELECT count(*) FROM calls WHERE task_id IN
                            (SELECT task_id FROM tasks ORDER BY rowid DESC LIMIT 1000)""").fetchone()[0]
        attempts = db.execute("""SELECT event,count(*) FROM events WHERE task_id IN
            (SELECT task_id FROM tasks ORDER BY rowid DESC LIMIT 1000) AND
            (event IN ('step_leased','step_retrying','step_failed') OR
             (event='step_started' AND json_extract(payload,'$.step_id') IS NULL)) GROUP BY event""").fetchall()
        recoveries = db.execute("""SELECT DISTINCT t.task_id,t.status FROM
            (SELECT task_id,status FROM tasks ORDER BY rowid DESC LIMIT 1000) t
            JOIN events e USING(task_id) WHERE e.event='step_retrying'""").fetchall()
        denials = db.execute("SELECT count(*) FROM audit_logs WHERE decision='deny' AND code='PERMISSION_DENIED'").fetchone()[0]
        waits = db.execute("""SELECT 1000*(decided_at-created) FROM approvals
            WHERE decided_at IS NOT NULL ORDER BY decided_at DESC LIMIT 1000""").fetchall()
    latencies = defaultdict(list)
    for row in tasks:
        if row["finished_at"]:
            latencies[row["mode"]].append((_ns(row["finished_at"]) - _ns(row["created_at"])) / 1e6)
    finished = sum(map(len, latencies.values()))
    statuses = Counter(t["status"] for t in tasks)
    events = Counter(dict(attempts))
    # Each lease expiry that led to a retry and each terminal failed attempt count once.
    return {"version": "1.0", "window": "last_1000_tasks", "task_count": len(tasks),
            "finished_count": finished, "status_counts": dict(statuses),
            "task_completion_rate": _rate(statuses["completed"], finished),
            "task_partial_rate": _rate(statuses["partial"], finished),
            "task_latency_p95_ms": {mode: _p95(latencies[mode]) for mode in ("reactive", "durable_plan")},
            "tool_calls_per_task": _rate(calls, len(tasks)),
            "durable_lease_attempts": events["step_leased"],
            "step_error_rate": _rate(events["step_retrying"] + events["step_failed"],
                                    events["step_leased"] + events["step_started"]),
            "recovery_success_rate": _rate(sum(r["status"] == "completed" for r in recoveries), len(recoveries)),
            "first_event_latency_p95_ms": _p95(list(first_event)), "first_event_sample_count": len(first_event),
            "approval_wait_p95_ms": _p95([r[0] for r in waits]),
            "permission_denial_count": denials, "permission_denial_window": "all_time",
            **usage}


def _alerts(store):
    with store._transaction(write=False):
        db = store._db
        denied = db.execute("""SELECT count(*) FROM audit_logs WHERE decision='deny' AND code='PERMISSION_DENIED'
                 AND julianday(timestamp) >= julianday('now','-15 minutes')""").fetchone()[0]
        unknown = db.execute("SELECT count(*) FROM approvals WHERE commit_status='outcome_unknown'").fetchone()[0]
        pending = db.execute("SELECT count(*) FROM approvals WHERE status='requested'").fetchone()[0]
        unauthorized = db.execute("""SELECT count(*) FROM mock_receipts r LEFT JOIN approvals a
            USING(approval_id) WHERE a.status IS NULL OR a.status!='approved'""").fetchone()[0]
        duplicates = db.execute("""SELECT count(*) FROM (SELECT task_id,step_id FROM mock_receipts
            JOIN approvals USING(approval_id) GROUP BY task_id,step_id HAVING count(*)>1)""").fetchone()[0]
    alerts = []
    if denied:
        alerts.append({"code": "PERMISSION_DENIAL", "count": denied, "window": "15_minutes"})
    if unknown:
        alerts.append({"code": "MOCK_COMMIT_UNKNOWN", "count": unknown, "window": "outstanding"})
    for code, count, window in [("APPROVAL_PENDING", pending, "outstanding"),
                                ("UNAPPROVED_RECEIPT", unauthorized, "all_time"),
                                ("DUPLICATE_MOCK_EFFECT", duplicates, "all_time")]:
        if count:
            alerts.append({"code": code, "count": count, "window": window})
    return {"alerts": alerts}


def _attr(name, value):
    if isinstance(value, bool):
        typed = {"boolValue": value}
    elif isinstance(value, int):
        typed = {"intValue": str(value)}
    elif isinstance(value, float):
        typed = {"doubleValue": value}
    else:
        typed = {"stringValue": value}
    return {"key": name, "value": typed}


def _id(seed, length):
    return hashlib.sha256(seed.encode()).hexdigest()[:length]


def _safe(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9._:/-]{1,128}", value) else None


def _error(code):
    return code if code in ERRORS else ("OTHER" if code else None)


def _trace(store, tid, settings, page):
    # Select metadata columns only; terminal event result, raw previews and free-form errors
    # are intentionally never projected onto an exported span.
    with store._transaction(write=False):
        row = store._db.execute("""SELECT t.request_id,t.status,t.created_at,t.finished_at,t.mode,
            json_extract(t.error,'$.code') AS error_code,s.business_id,p.snapshot_id
            FROM tasks t JOIN sessions s USING(session_id) LEFT JOIN plans p USING(task_id)
            WHERE t.task_id=?""", (tid,)).fetchone()
        if not row:
            raise HTTPException(404, "Task not found")
        # Stable IDs are joined to their server-side record. An event payload alone
        # cannot export an arbitrary string disguised as a tool, approval or step ID.
        events = store._db.execute("""SELECT e.event_id AS id,e.event,e.timestamp,s.step_id,a.approval_id,
            c.execution_id,COALESCE(c.capability_id,json_extract(s.definition,'$.capability_id')) AS capability_id,
            json_extract(e.payload,'$.status') AS status,json_extract(e.payload,'$.error_code') AS error_code,
            json_extract(e.payload,'$.model') AS model,json_type(e.payload,'$.allowed') AS allowed,
            CASE WHEN json_type(e.payload,'$.prompt_tokens')='integer'
                 THEN json_extract(e.payload,'$.prompt_tokens') END AS prompt_tokens,
            CASE WHEN json_type(e.payload,'$.completion_tokens')='integer'
                 THEN json_extract(e.payload,'$.completion_tokens') END AS completion_tokens,
            CASE WHEN json_type(e.payload,'$.estimated_cost_usd') IN ('real','integer')
                 THEN json_extract(e.payload,'$.estimated_cost_usd') END AS estimated_cost_usd
            FROM events e LEFT JOIN plan_steps s ON s.task_id=e.task_id AND s.step_id=json_extract(e.payload,'$.step_id')
            LEFT JOIN approvals a ON a.task_id=e.task_id AND a.approval_id=json_extract(e.payload,'$.approval_id')
            LEFT JOIN calls c ON c.task_id=e.task_id AND c.execution_id=json_extract(e.payload,'$.execution_id')
            WHERE e.task_id=? ORDER BY e.event_id LIMIT ? OFFSET ?""", (tid, PAGE_SIZE + 1, page * PAGE_SIZE)).fetchall()
        calls = store._db.execute("""SELECT execution_id,call_id,capability_id,capability_version,started,
            status,duration_ms,json_extract(error,'$.code') AS error_code FROM calls WHERE task_id=?
            ORDER BY rowid LIMIT ? OFFSET ?""", (tid, PAGE_SIZE + 1, page * PAGE_SIZE)).fetchall()
    trace_id, root_id = _id(tid, 32), _id(tid + "/root", 16)
    start = _ns(row["created_at"])
    end = _ns(row["finished_at"]) if row["finished_at"] else time.time_ns()
    def span(key, name, begin, finish, fields, failed=False, terminal=True, parent=root_id):
        attrs = [_attr("agent." + k, v) for k, v in fields.items() if v is not None]
        attrs.append(_attr("agent.duration_ms", max(0, (finish - begin) // 1_000_000)))
        data = {"traceId": trace_id, "spanId": _id(tid + "/" + key, 16), "name": name, "kind": 1,
                "startTimeUnixNano": str(begin), "endTimeUnixNano": str(max(begin, finish)),
                "attributes": attrs, "status": {"code": 2 if failed else (1 if terminal else 0)}}
        if parent:
            data["parentSpanId"] = parent
        return data
    root = span("root", "agent.request", start, end, {"request_id": row["request_id"], "task_id": tid,
                "business_id": row["business_id"], "release_id": row["snapshot_id"],
                "planner_id": row["mode"] if row["mode"] in {"offline", "openai"} else None,
                "outcome": row["status"] if row["status"] in OUTCOMES else None, "error_code": _error(row["error_code"])},
                row["status"] == "failed", bool(row["finished_at"]), parent=None)
    root["kind"] = 2
    spans = [root]
    call_steps = {}
    for data in events[:PAGE_SIZE]:
        event, point = data["event"], _ns(data["timestamp"])
        if data["execution_id"] and data["step_id"]:
            call_steps[data["execution_id"]] = _safe(data["step_id"])
        if event not in EVENT_SPANS:
            continue
        fields = {"event": event}
        for k in ("step_id", "approval_id", "execution_id", "capability_id"):
            if _safe(data[k]):
                fields[k] = data[k]
        fields["error_code"] = _error(data["error_code"])
        if data["status"] in OUTCOMES:
            fields["outcome"] = data["status"]
        if event == "authorization" and data["allowed"] in ("true", "false"):
            fields["allowed"] = data["allowed"] == "true"
        if event == "model_usage" and all(type(data[k]) is int and 0 <= data[k] < 2**31
                                          for k in ("prompt_tokens", "completion_tokens")):
            fields.update(prompt_tokens=data["prompt_tokens"], completion_tokens=data["completion_tokens"])
            cost = data["estimated_cost_usd"]
            if cost is not None and math.isfinite(cost) and cost >= 0:
                fields["estimated_cost_usd"] = float(cost)
        model = data["model"]
        if event == "provider_selected" and model and model == settings.get("model") and _safe(model):
            if not settings.get("api_key") or settings["api_key"] not in model:
                fields["model_id"] = "model/" + _id(model, 16)
        spans.append(span("event/" + str(data["id"]), EVENT_SPANS[event], point, point, fields,
                          event in ("step_failed", "task_failed", "mock_commit_unknown") or fields.get("allowed") is False))
    for call in calls[:PAGE_SIZE]:
        begin = int(call["started"] * 1e9)
        finish = begin + call["duration_ms"] * 1_000_000 if call["duration_ms"] is not None else end
        fields = {k: _safe(call[k]) for k in ("execution_id", "capability_id", "capability_version")}
        fields.update(step_id=call_steps.get(call["execution_id"]),
                      outcome=call["status"] if call["status"] in OUTCOMES else None,
                      error_code=_error(call["error_code"]))
        spans.append(span("tool/" + call["execution_id"], "agent.tool", begin, finish, fields,
                          call["status"] in ("failed", "unknown_outcome"), call["duration_ms"] is not None))
    document = {"resourceSpans": [{"resource": {"attributes": [_attr("service.name", "universal-agent")]},
                "scopeSpans": [{"scope": {"name": "local.agent.lifecycle", "version": "1.0"}, "spans": spans}]}]}
    return document, max(len(events), len(calls)) > PAGE_SIZE


class _FirstEvent:
    def __init__(self, app, samples):
        self.app, self.samples = app, samples

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/api/v1/agent/chat":
            return await self.app(scope, receive, send)
        start = time.monotonic()
        streaming = False
        async def measure(message):
            nonlocal streaming
            if message["type"] == "http.response.start":
                streaming = dict(message["headers"]).get(b"content-type", b"").startswith(b"text/event-stream")
            elif streaming and b"event:" in message.get("body", b""):
                self.samples.append((time.monotonic() - start) * 1000)
                streaming = False
            await send(message)
        await self.app(scope, receive, measure)


def install_observability_routes(app, runtime, admin_dependency):
    samples = deque(maxlen=1024)
    app.add_middleware(_FirstEvent, samples=samples)
    guard = [Depends(admin_dependency)]

    @app.get(PREFIX + "/metrics", dependencies=guard)
    def metrics(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return _metrics(runtime().store, samples)

    @app.get(PREFIX + "/alerts", dependencies=guard)
    def alerts(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return _alerts(runtime().store)

    @app.get(PREFIX + "/traces/{task_id}", dependencies=guard)
    def trace(task_id: str, response: Response, page: int = Query(0, ge=0, le=100000)):
        response.headers["Cache-Control"] = "no-store"
        rt = runtime()
        document, more = _trace(rt.store, task_id, getattr(rt, "settings", {}), page)
        if more:
            response.headers["X-Next-Page"] = str(page + 1)
        return document
