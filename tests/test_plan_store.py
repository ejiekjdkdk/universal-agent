from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing

import pytest

from app.errors import Fault
from app.store import Store


ACTOR = {"tenant_id": "t", "org_id": "o", "user_id": "u"}
BODY = {"business_id": "general", "message": "analyze", "execution_mode": "durable_plan"}
BUDGET = {"max_parallel_steps": 2, "max_steps": 12, "max_tool_calls": 8, "deadline_ms": 60000}


def step(name, deps=(), required=True):
    return {"step_id": name, "capability_id": "inspect", "arguments": {"text": "internal-input"},
            "depends_on": list(deps), "required": required}


def submit(store, *steps, budget=None, key=None):
    plan = {"schema_version": 1, "steps": list(steps), "result_contract": "summary_v1"}
    return store.start(ACTOR, BODY, key, "1", plan=plan, snapshot_id="release", budget=budget or BUDGET)[0]


def result(text):
    return {"data": {"summary": text}, "evidence": [{"source_id": text}], "status": "completed"}


def fault(code):
    return {"code": code, "message": code, "retryable": False, "details": {}}


@pytest.fixture
def stores(tmp_path):
    with ExitStack() as stack:
        db = tmp_path / "db"
        yield tuple(stack.enter_context(closing(Store(db, recover_reactive=False))) for _ in range(3))


def test_dependency_claims_and_final_summary_are_stable_across_connections(stores):
    api, a, b = stores
    task = submit(api, step("a"), step("b"), step("final", ["a", "b"]))
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda pair: pair[0].claim_step(pair[1], 10), [(a, "W1"), (b, "W2")]))
    assert {c["step_id"] for c in claims} == {"a", "b"}
    assert api.claim_step("third", 10) is None
    for c in reversed(claims):
        assert b.settle_step(c, result=result(c["step_id"]))
    sink = api.claim_step("W3", 10)
    assert sink["outputs"]["a"]["data"]["summary"] == "a"
    assert sink["snapshot_id"] == "release" and sink["max_tool_calls"] == 8
    assert a.settle_step(sink, result=result("finished"))
    assert b.claim_step("sweep", 10) is None
    finished = api.task(ACTOR, task["task_id"])
    assert finished["answer"] == "finished" and finished["execution_mode"] == "durable_plan"
    assert [e["source_id"] for e in finished["evidence"]] == ["a", "b", "finished"]
    view = api.plan_view(task["task_id"])
    assert view["snapshot_id"] == "release" and view["plan_id"]
    assert "internal-input" not in str(view)
    assert [s["status"] for s in view["steps"]] == ["succeeded"] * 3


def test_independent_connections_respect_parallel_and_idempotency_limits(stores):
    api, a, b = stores
    budget = {**BUDGET, "max_parallel_steps": 1}
    with ThreadPoolExecutor(3) as pool:
        tasks = list(pool.map(lambda s: submit(s, step("a"), step("b"), budget=budget, key="repeat"), stores))
    assert len({t["task_id"] for t in tasks}) == 1
    assert api.find_replay(ACTOR, BODY, "repeat")["task_id"] == tasks[0]["task_id"]
    assert a.claim_step("one", 10) and b.claim_step("two", 10) is None


@pytest.mark.parametrize("cache", [False, True])
def test_lost_lease_reclaims_same_execution_and_fences_old_worker(stores, monkeypatch, cache):
    api, a, b = stores
    import time
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    tid = submit(api, step("x"))["task_id"]
    c1 = a.claim_step("a", 1)
    args = (tid, "plan.x", "inspect", "1", {"text": "same"})
    execution, _ = a.begin_call(*args, lease=c1)
    if cache:
        a.complete_call(tid, "plan.x", result("cached"), lease=c1)
    now[0] += 2
    c2 = b.claim_step("b", 10)
    assert c2["token"] > c1["token"] and not a.renew_step(c1, 10)
    assert not a.settle_step(c1, result=result("stale"))
    for op in (lambda: a.complete_call(tid, "plan.x", result("stale"), lease=c1),
               lambda: a.emit(tid, "tool_result", {}, lease=c1), lambda: a.begin_call(*args)):
        with pytest.raises(Fault):
            op()
    recovered_id, cached = b.begin_call(*args, lease=c2)
    assert recovered_id == execution
    assert cached == (result("cached") if cache else None)
    if not cache:
        with pytest.raises(Fault):
            b.begin_call(*args, lease=c2)
        b.complete_call(tid, "plan.x", result("new"), lease=c2)
    assert b.renew_step(c2, 5)
    assert b.settle_step(c2, result=cached or result("new"))
    assert api.claim_step("sweep", 1) is None
    assert sum(e["event"] == "task_completed" for e in api.events(tid)) == 1
    assert len(api.task(ACTOR, tid)["tool_calls"]) == 1


@pytest.mark.parametrize("code,expected", [("UPSTREAM_UNAVAILABLE", "partial"), ("PERMISSION_DENIED", "failed"),
                                          ("SCHEMA_VALIDATION_FAILED", "failed"), ("UPSTREAM_INVALID_RESPONSE", "failed"),
                                          ("UPSTREAM_DEPENDENCY_FAILED", "failed"), ("CAPABILITY_OUTPUT_INVALID", "failed")])
def test_optional_failure_only_yields_qualified_partial(stores, code, expected):
    api, a, b = stores
    tid = submit(api, step("ok"), step("optional", required=False), step("later", ["optional"], False))["task_id"]
    assert a.settle_step(a.claim_step("a", 5), result=result("available"))
    assert b.settle_step(b.claim_step("b", 5), error=fault(code))
    assert api.claim_step("sweep", 5) is None
    t = api.task(ACTOR, tid)
    assert t["status"] == expected
    if expected == "partial":
        assert t["answer"] == "available" and t["result"]["failed_steps"] and t["result"]["missing_outputs"]
        assert t["result"]["limitations"]
    else:
        assert t["error"]["code"] == code and not t["answer"]
    assert api.plan_view(tid)["steps"][-1]["status"] == "skipped"


def test_cancel_revokes_lease_and_prevents_execution(stores):
    api, a, _ = stores
    tid = submit(api, step("x"), step("y", ["x"]))["task_id"]
    c = a.claim_step("worker", 10)
    assert api.cancel(ACTOR, tid)["status"] == "cancelled"
    assert not a.settle_step(c, result=result("late")) and not a.renew_step(c, 10)
    assert all(s["status"] == "cancelled" for s in api.plan_view(tid)["steps"])
    assert a.claim_step("sweep", 10) is None
    assert api.events(tid)[-1]["event"] == "task_cancelled"


def test_deadline_and_attempt_cap_terminate_tasks_without_another_execution(stores, monkeypatch):
    import time
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    api, a, _ = stores
    tid = submit(api, step("x"), budget={**BUDGET, "deadline_ms": 500})["task_id"]
    c = a.claim_step("worker", 20)
    now[0] += 1
    assert not a.settle_step(c, result=result("too late")) and api.claim_step("sweep", 1) is None
    assert api.task(ACTOR, tid)["status"] == "failed"
    tid = submit(api, step("x"))["task_id"]
    for i in range(3):
        assert a.claim_step("worker", 1)
        now[0] += 2
    assert a.claim_step("sweep", 1) is None
    assert api.task(ACTOR, tid)["status"] == "failed"


def test_restart_only_fails_reactive_and_can_resume_plan(tmp_path):
    path = tmp_path / "restart.db"
    with closing(Store(path)) as store:
        reactive, _ = store.start(ACTOR, {k: v for k, v in BODY.items() if k != "execution_mode"}, None, "1")
        durable = submit(store, step("x"))
    with closing(Store(path)) as reopened:
        assert reopened.task(ACTOR, reactive["task_id"])["status"] == "failed"
        with pytest.raises(Fault):
            reopened.plan_view(reactive["task_id"])
        assert reopened.is_durable(durable["task_id"]) and not reopened.is_durable(reactive["task_id"])
        assert reopened.claim_step("worker", 10)["task_id"] == durable["task_id"]


def test_direct_finish_cannot_bypass_plan_leases(stores):
    api, a, _ = stores
    tid = submit(api, step("x"))["task_id"]
    claim = a.claim_step("worker", 5)
    with pytest.raises(Fault) as conflict:
        api.finish(tid, "completed", {"answer": "bypass"})
    assert conflict.value.code == "TASK_STATE_CONFLICT"
    assert a.settle_step(claim, result=result("verified"))
    assert api.task(ACTOR, tid)["answer"] == "verified"


def test_claim_sees_transitive_predecessors_but_not_independent_steps(stores):
    api, worker, _ = stores
    submit(api, step("unrelated"), step("grandparent"), step("parent", ["grandparent"]), step("sink", ["parent"]))
    for expected in ("unrelated", "grandparent", "parent"):
        claim = worker.claim_step("w", 5)
        assert claim["step_id"] == expected
        assert worker.settle_step(claim, result=result(expected))
    claim = worker.claim_step("w", 5)
    assert set(claim["outputs"]) == {"grandparent", "parent"}


@pytest.mark.parametrize("outcome", ["settle", "denied", "expired"])
def test_third_attempt_committed_result_gets_one_fenced_replay_only_lease(stores, monkeypatch, outcome):
    import time
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    api, worker, rescue = stores
    tid = submit(api, step("x"))["task_id"]
    args = (tid, "plan.x", "inspect", "1", {"text": "same"})
    for attempt in range(1, 4):
        c = worker.claim_step("worker", 1)
        execution, cached = worker.begin_call(*args, lease=c)
        assert cached is None
        if attempt == 3:
            worker.complete_call(tid, "plan.x", result("stored"), lease=c)
        now[0] += 2
    c = rescue.claim_step("rescue", 1)
    assert c["attempt_no"] == 3 and c["token"] == 4 and c["recovery_only"]
    if outcome == "settle":
        assert rescue.begin_call(*args, lease=c) == (execution, result("stored"))
        assert rescue.settle_step(c, result=result("stored"))
        assert api.task(ACTOR, tid)["status"] == "completed"
    elif outcome == "denied":
        assert rescue.settle_step(c, error=fault("PERMISSION_DENIED"))
        task = api.task(ACTOR, tid)
        assert task["status"] == "failed" and task["error"]["code"] == "PERMISSION_DENIED"
        assert not task["answer"] and not task["evidence"]
    else:
        now[0] += 2
        assert api.claim_step("fifth", 5) is None
        assert api.task(ACTOR, tid)["error"]["code"] == "STEP_ATTEMPTS_EXHAUSTED"
    assert api.plan_view(tid)["steps"][0]["attempt_no"] == 3
    assert api.task(ACTOR, tid)["tool_calls"][0]["status"] == "completed"
    events = api.events(tid)
    leases = [entry["data"] for entry in events if entry["event"] == "step_leased"]
    assert [(e["phase"], e["attempt_no"]) for e in leases] == [
        ("execution", 1), ("execution", 2), ("execution", 3), ("execution", 3),
    ]
    assert leases[-1]["recovery_only"]
    for entry in events:
        if entry["event"] in ("step_completed", "step_failed", "tool_call_completed"):
            assert (entry["data"]["phase"], entry["data"]["attempt_no"]) == ("execution", 3)


def test_expired_final_lease_preserves_persisted_security_error_on_optional_step(stores, monkeypatch):
    import time
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    api, worker, _ = stores
    tid = submit(api, step("ok"), step("optional", required=False))["task_id"]
    assert worker.settle_step(worker.claim_step("w", 1), result=result("available"))
    for attempt in range(1, 4):
        c = worker.claim_step("w", 1)
        worker.begin_call(tid, "plan.optional", "inspect", "1", {}, lease=c)
        if attempt == 3:
            worker.complete_call(tid, "plan.optional", None, fault("PERMISSION_DENIED"), lease=c)
        now[0] += 2
    assert api.claim_step("sweep", 1) is None
    task = api.task(ACTOR, tid)
    assert task["status"] == "failed" and task["error"]["code"] == "PERMISSION_DENIED"
    assert api.plan_view(tid)["steps"][-1]["error"]["code"] == "PERMISSION_DENIED"
    assert not task["answer"] and not task["evidence"]
