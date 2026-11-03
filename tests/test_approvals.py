import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse

from app.approval_routes import install_approval_routes
from app.catalog import Catalog
from app.errors import Fault
from app.plans import prepare_plan
from app.runtime import Runtime
from app.store import Store
from app.worker import PlanWorker


SID = "change_requests"
CAP = "record_change"
SCOPE = {"object_ids": ["draft-1"]}
BODY = {"business_id": SID, "message": "sensitive proposed contents", "context": SCOPE, "execution_mode": "durable_plan"}


def actor(user, permissions, org="o", tenant="t"):
    return {"user_id": user, "org_id": org, "tenant_id": tenant,
            "grants": {SID: {"permissions": list(permissions), "objects": ["draft-1"]}}}


@pytest.fixture
def system(tmp_path, monkeypatch, request):
    monkeypatch.setenv("AGENT_MOCK_WRITES", "1")
    root = tmp_path / "scenarios"
    folder = root / SID
    folder.mkdir(parents=True)
    def schema(fields):
        return {"type": "object", "properties": {k: {"type": v} for k, v in fields.items()},
                "required": list(fields), "additionalProperties": False}
    tool = {"name": CAP, "permission": "draft.propose", "description": "local ledger",
            "action_level": "commit_write", "adapter": {"type": "mock_ledger"},
            "input_schema": schema({"object_id": "string", "expected_version": "integer", "value": "string"}),
            "output_schema": schema({"summary": "string", "object_id": "string", "version": "integer", "receipt_id": "string"})}
    args = {"object_id": "draft-1", "value": {"$message": True}}
    if getattr(request, "param", None) == "bind_current_version":
        tool["input_schema"]["required"].remove("expected_version")
    else:
        args["expected_version"] = 0
    (folder / "scenario.json").write_text(json.dumps({"id": SID, "name": "Drafts", "version": "1.0.0",
        "description": "local", "example": "edit", "tools": [tool]}), encoding="utf-8")
    steps = [{"step_id": "edit", "capability_id": CAP, "arguments": args}]
    if getattr(request, "param", None) == "two_steps":
        steps.append({"step_id": "second", "capability_id": CAP, "depends_on": ["edit"],
                      "arguments": {**args, "expected_version": 1}})
    (folder / "plan.json").write_text(json.dumps({"schema_version": "2.0", "result_contract": "summary_v1",
        "steps": steps}), encoding="utf-8")
    cat = Catalog(root)
    principals = {"owner": actor("owner", ["draft.propose", "approval.decide"]),
                  "judge": actor("judge", ["approval.decide"]), "judge2": actor("judge2", ["approval.decide"]),
                  "reader": actor("reader", ["draft.propose"]), "foreign": actor("judge", ["approval.decide"], org="x"),
                  "tenant": actor("judge", ["approval.decide"], tenant="x")}
    db = tmp_path / "state.db"
    with closing(Store(db)) as store:
        rt = Runtime(cat, store, principals, {})
        snap = SimpleNamespace(open=lambda *_: cat)
        app = FastAPI()
        def identify(authorization: str = Header()):
            return principals[authorization]
        @app.exception_handler(Fault)
        async def fault_handler(_, exc):
            return JSONResponse({"error": exc.as_dict()}, status_code=exc.status)
        install_approval_routes(app, lambda: rt, identify)
        with TestClient(app, base_url="http://localhost") as client:
            yield SimpleNamespace(store=store, cat=cat, client=client, rt=rt, principals=principals,
                                  worker=PlanWorker(rt, snap), db=db, snap=snap)


def submit(s, scope=SCOPE):
    plan, budget = prepare_plan(s.cat, SID, BODY["message"], {"draft.propose"}, scope)
    task, _ = s.store.start(s.principals["owner"], {**BODY, "context": scope}, None, "1.0.0",
                            plan=plan, budget=budget, snapshot_id="snap")
    return task["task_id"]


def work(s):
    claim = s.store.claim_step("worker", 5)
    assert claim
    asyncio.run(s.worker.execute(claim))


BASE = "/api/v1/agent/approvals"


def proposal(s, scope=SCOPE):
    tid = submit(s, scope)
    work(s)
    response = s.client.get(BASE, headers={"Authorization": "judge"})
    assert response.status_code == 200, response.text
    row = next(a for a in response.json()["approvals"] if a["task_id"] == tid)
    assert row["approval_status"] == "requested" and row["commit_status"] == "not_started"
    assert s.store.task(s.principals["owner"], tid)["status"] == "waiting_approval"
    assert BODY["message"] not in response.text
    return tid, row


def decide(s, p, who="judge", decision="approve", key="one", **extra):
    return s.client.post(f"{BASE}/{p['approval_id']}/decision", headers={"Authorization": who, "Idempotency-Key": key},
                         json={"decision": decision, "proposal_digest": p["proposal_digest"], **extra})


@pytest.fixture
def typed_proposal(system):
    for who in ("owner", "judge"):
        system.principals[who]["grants"][SID]["resource_types"] = ["draft"]
    return proposal(system, {**SCOPE, "object_type": "draft"})


def test_type_scope_controls_approval_queue_detail_decision_and_receipt(system, typed_proposal):
    s = system
    _, p = typed_proposal
    path = f"{BASE}/{p['approval_id']}"
    s.principals["judge2"]["grants"][SID]["resource_types"] = ["other"]
    outsider = {"Authorization": "judge2"}
    assert s.client.get(BASE, headers=outsider).json()["approvals"] == []
    assert s.client.get(path, headers=outsider).status_code == 404
    assert decide(s, p, who="judge2").status_code == 404
    assert decide(s, p).status_code == 200
    work(s)
    for who in ("owner", "judge"):
        headers = {"Authorization": who}
        assert s.client.get(path + "/receipt", headers=headers).status_code == 200
        s.principals[who]["grants"][SID]["resource_types"].clear()
        assert s.client.get(BASE, headers=headers).json()["approvals"] == []
        assert s.client.get(path, headers=headers).status_code == 404
        assert s.client.get(path + "/receipt", headers=headers).status_code == 404


@pytest.mark.parametrize("who", ["owner", "judge"])
def test_type_revoked_after_approval_prevents_commit(system, typed_proposal, who):
    s = system
    tid, p = typed_proposal
    assert decide(s, p).status_code == 200
    s.principals[who]["grants"][SID]["resource_types"].clear()
    work(s)
    assert s.store.task(s.principals["owner"], tid)["error"]["code"] == "PERMISSION_DENIED"
    with closing(sqlite3.connect(s.db)) as db:
        assert db.execute("SELECT COUNT(*) FROM mock_receipts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM mock_ledger").fetchone()[0] == 0


def test_disabled_and_reactive_paths_cannot_mutate(system, monkeypatch):
    monkeypatch.delenv("AGENT_MOCK_WRITES")
    with pytest.raises(Fault) as denied:
        submit(system)
    assert denied.value.code == "ACTION_NOT_ALLOWED"
    with pytest.raises(Fault) as denied:
        asyncio.run(system.cat.execute(SID, CAP, {}))
    assert denied.value.code == "ACTION_NOT_ALLOWED"


def test_only_independent_same_scope_approver_and_matching_digest(system):
    s = system
    _, p = proposal(s)
    for who in ("owner", "reader", "foreign", "tenant"):
        assert decide(s, p, who).status_code in (403, 404)
    for who in ("reader", "foreign", "tenant"):
        assert s.client.get(BASE, headers={"Authorization": who}).json()["approvals"] == []
    assert decide(s, p, approver_id="judge").status_code in (400, 422)
    mismatch = decide(s, dict(p, proposal_digest="sha256:" + "0"*64))
    assert mismatch.status_code == 409 and mismatch.json()["error"]["code"] == "APPROVAL_PROPOSAL_CHANGED"


def test_concurrent_approvals_then_worker_crash_and_exactly_one_receipt(system, monkeypatch):
    import time
    s = system
    tid, p = proposal(s)
    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(lambda u: decide(s, p, who=u, key=u), ("judge", "judge2")))
    assert sorted(r.status_code for r in replies) == [200, 409]
    winner = ["judge", "judge2"][next(i for i,r in enumerate(replies) if r.status_code == 200)]
    assert decide(s, p, who=winner, key=winner).status_code == 200
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    lost = s.store.claim_step("dead", 1)
    now[0] += 2
    with closing(Store(s.db, recover_reactive=False)) as worker_store:
        worker = PlanWorker(Runtime(s.cat, worker_store, s.principals, {}), s.snap)
        asyncio.run(worker.execute(worker_store.claim_step("survivor", 5)))
    asyncio.run(s.worker.execute(lost))
    assert s.store.task(s.principals["owner"], tid)["status"] == "completed"
    with closing(sqlite3.connect(s.db)) as db:
        assert db.execute("SELECT version,value FROM mock_ledger").fetchall() == [(1, BODY["message"])]
        assert db.execute("SELECT COUNT(*) FROM mock_receipts").fetchone()[0] == 1
    assert s.client.get(f"{BASE}/{p['approval_id']}", headers={"Authorization": "judge"}).json()["commit_status"] == "succeeded"


@pytest.mark.parametrize("scenario", ["reject", "cancel", "expire", "requester_revoked", "approver_revoked", "release_revoked"])
def test_rejection_expiry_cancellation_and_revocations_never_write(system, monkeypatch, scenario):
    import time
    s = system
    tid, p = proposal(s)
    if scenario == "reject":
        assert decide(s, p, decision="reject").status_code == 200
    elif scenario == "expire":
        monkeypatch.setattr(time, "time", lambda: p["expires_at_unix"] + 1)
        assert s.store.claim_step("sweep", 1) is None
    elif scenario == "cancel":
        s.store.cancel(s.principals["owner"], tid)
        assert decide(s, p).status_code == 409
    else:
        assert decide(s, p).status_code == 200
        if scenario == "release_revoked":
            def revoked(*_):
                raise Fault("CAPABILITY_REVOKED", "disabled", 403)
            s.store.release_guard = revoked
        else:
            who = "owner" if scenario == "requester_revoked" else "judge"
            s.principals[who]["grants"][SID]["permissions"].clear()
        work(s)
    assert s.store.task(s.principals["owner"], tid)["status"] in ("cancelled", "failed")
    with closing(sqlite3.connect(s.db)) as db:
        assert db.execute("SELECT COUNT(*) FROM mock_ledger").fetchone()[0] == 0


def test_object_version_changed_since_proposal(system):
    s = system
    first, a = proposal(s)
    second, b = proposal(s)
    assert decide(s, a).status_code == 200 and decide(s, b).status_code == 200
    work(s)
    work(s)
    assert s.store.task(s.principals["owner"], first)["status"] == "completed"
    assert s.store.task(s.principals["owner"], second)["error"]["code"] == "APPROVAL_PROPOSAL_CHANGED"


@pytest.mark.parametrize("system", ["bind_current_version"], indirect=True)
def test_server_binds_current_version_for_each_fresh_proposal(system):
    s = system
    for version in (0, 1):
        tid, p = proposal(s)
        assert p["field_diff"]["version"] == {"before": version, "after": version+1}
        assert decide(s, p).status_code == 200
        work(s)
        assert s.store.task(s.principals["owner"], tid)["status"] == "completed"
    _, p = proposal(s)
    stale_tid, stale = proposal(s)
    assert decide(s, p).status_code == 200 and decide(s, stale).status_code == 200
    work(s)
    work(s)
    assert s.store.task(s.principals["owner"], stale_tid)["error"]["code"] == "APPROVAL_PROPOSAL_CHANGED"


def test_reactive_snapshot_pinning_rechecks_new_admission_after_replay(system):
    s = system
    body = {"business_id": SID, "message": "test managed reads"}
    calls = []
    task, _ = s.store.start(s.principals["owner"], body, "once", "1", snapshot_id="one", admit=lambda: calls.append(1))
    def revoked(*_):
        raise Fault("CAPABILITY_REVOKED", "disabled", 403)
    s.store.release_guard = revoked
    replay, new = s.store.start(s.principals["owner"], body, "once", "2", snapshot_id="two", admit=revoked)
    assert not new and replay["snapshot_id"] == "one"
    assert s.store.task_snapshot(task["task_id"]) == "one" and calls == [1]
    with pytest.raises(Fault):
        s.store.start(s.principals["owner"], body, "new", "2", snapshot_id="two")
    with pytest.raises(Fault):
        s.store.start(s.principals["owner"], body, None, "2")


def test_modified_step_arguments_invalidate_approval_without_writing(system):
    s = system
    tid, p = proposal(s)
    assert decide(s, p).status_code == 200
    with closing(sqlite3.connect(s.db)) as db, db:
        row = db.execute("SELECT definition FROM plan_steps WHERE task_id=?", (tid,)).fetchone()
        altered = json.loads(row[0])
        altered["arguments"]["value"] = "changed after authorization"
        db.execute("UPDATE plan_steps SET definition=? WHERE task_id=?", (json.dumps(altered), tid))
    work(s)
    assert s.store.task(s.principals["owner"], tid)["error"]["code"] == "APPROVAL_PROPOSAL_CHANGED"
    with closing(sqlite3.connect(s.db)) as db:
        assert db.execute("SELECT COUNT(*) FROM mock_receipts").fetchone()[0] == 0


def test_lost_ack_never_resends(system, monkeypatch):
    from app import approvals
    s = system
    tid, p = proposal(s)
    assert decide(s, p).status_code == 200
    def lost_ack(*_):
        raise TimeoutError("simulated missing acknowledgement")
    monkeypatch.setattr(approvals, "verify_receipt", lost_ack)
    work(s)
    assert s.store.task(s.principals["owner"], tid)["error"]["code"] == "OUTCOME_UNKNOWN"
    assert s.store.claim_step("replacement", 1) is None
    detail = s.client.get(f"{BASE}/{p['approval_id']}", headers={"Authorization": "judge"}).json()
    assert detail["commit_status"] == "outcome_unknown" and detail["receipt_id"]
    path = f"{BASE}/{p['approval_id']}/receipt"
    for who in ("owner", "judge", "judge"):
        confirmation = s.client.get(path, headers={"Authorization": who})
        assert confirmation.status_code == 200, confirmation.text
        data = confirmation.json()
        assert data["commit_status"] == "outcome_unknown" and data["verification_status"] == "confirmed"
        assert data["receipt_id"] == detail["receipt_id"] and data["object_version"] == data["current_version"] == 1
        assert BODY["message"] not in confirmation.text
    for who in ("judge2", "foreign", "tenant", "reader"):
        assert s.client.get(path, headers={"Authorization": who}).status_code == 404
    terminal = s.store.events(tid)[-1]
    assert terminal["event"] == "task_failed" and terminal["data"]["mock_commits"][0]["receipt_id"] == detail["receipt_id"]
    with closing(sqlite3.connect(s.db)) as db:
        assert db.execute("SELECT COUNT(*) FROM mock_receipts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM audit_logs WHERE action='approval.receipt' AND decision='allow'").fetchone()[0] == 3


@pytest.mark.parametrize("system", ["two_steps"], indirect=True)
def test_cancel_reports_already_committed_earlier_step(system):
    s = system
    tid, p = proposal(s)
    assert decide(s, p).status_code == 200
    work(s)
    cancelled = s.store.cancel(s.principals["owner"], tid)
    assert cancelled["status"] == "cancelled"
    commits = cancelled["mock_commits"]
    assert len(commits) == 1 and commits[0]["step_id"] == "edit" and commits[0]["object_version"] == 1
    assert commits[0]["commit_status"] == "succeeded" and BODY["message"] not in str(commits)
    assert s.store.events(tid)[-1]["data"]["mock_commits"] == commits
    assert s.store.claim_step("replacement", 1) is None


def test_cancellation_from_other_connection_invalidates_approved_commit_lease(system):
    s = system
    tid, p = proposal(s)
    assert decide(s, p).status_code == 200
    c = s.store.claim_step("worker", 10)
    with closing(Store(s.db, recover_reactive=False)) as api:
        assert api.cancel(s.principals["owner"], tid)["status"] == "cancelled"
    asyncio.run(s.worker.execute(c))
    assert s.store.task(s.principals["owner"], tid)["status"] == "cancelled"
    assert s.client.get(f"{BASE}/{p['approval_id']}/receipt", headers={"Authorization": "judge"}).status_code == 404
    assert s.store.events(tid)[-1]["event"] == "task_cancelled"


def test_proposal_and_execution_attempts_match_views_events_and_claims(system, monkeypatch):
    import time
    s = system
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    tid = submit(s)

    def view_count():
        step = s.store.plan_view(tid)["steps"][0]
        return step["phase"], step["attempt_no"]

    def claim_count(worker):
        claim = s.store.claim_step(worker, 1)
        assert (claim["phase"], claim["attempt_no"]) == view_count()
        return claim

    assert view_count() == ("proposal", 0)
    lost_proposal = claim_count("lost-proposal")
    now[0] += 2
    retry_proposal = claim_count("prepare")
    assert not s.store.settle_step(lost_proposal, error={"code": "stale"})
    asyncio.run(s.worker.execute(retry_proposal))
    assert view_count() == ("proposal", 2)
    approval = s.client.get(BASE, headers={"Authorization": "judge"}).json()["approvals"][0]
    assert decide(s, approval).status_code == 200
    assert view_count() == ("execution", 0)
    lost_commit = claim_count("lost-commit")
    assert (lost_commit["attempt_no"], lost_commit["token"]) == (1, 3)
    now[0] += 2
    final_commit = claim_count("commit")
    assert (final_commit["attempt_no"], final_commit["token"]) == (2, 4)
    assert not s.store.renew_step(lost_commit, 1)
    asyncio.run(s.worker.execute(final_commit))
    assert view_count() == ("execution", 2)
    events = s.store.events(tid)
    for name, expected in {
        "step_leased": [("proposal", 1), ("proposal", 2), ("execution", 1), ("execution", 2)],
        "step_retrying": [("proposal", 2), ("execution", 2)],
        "step_started": [("execution", 2)], "step_completed": [("execution", 2)],
    }.items():
        data = [e["data"] for e in events if e["event"] == name]
        assert [(d["phase"], d["attempt_no"]) for d in data] == expected
        if name == "step_retrying":
            assert all(d["attempt"] == d["attempt_no"] for d in data)


def test_legacy_approved_step_uses_persisted_proposal_token_without_rewriting_events(system):
    s = system
    tid, p = proposal(s)
    assert decide(s, p).status_code == 200
    with closing(sqlite3.connect(s.db)) as db, db:
        old = json.loads(db.execute("SELECT definition FROM plan_steps WHERE task_id=?", (tid,)).fetchone()[0])
        old.pop("requires_approval", None)
        db.execute("UPDATE plan_steps SET definition=? WHERE task_id=?", (json.dumps(old), tid))
        legacy = '{"task_id":"legacy","attempt":2}'
        db.execute("UPDATE events SET payload=? WHERE task_id=? AND event='step_leased'", (legacy, tid))
    work(s)
    step = s.store.plan_view(tid)["steps"][0]
    assert step["attempt_no"] == 1
    assert step["phase"] == "execution"
    assert next(e["data"] for e in s.store.events(tid) if e["event"] == "step_leased") == json.loads(legacy)


def test_plan_template_cannot_declare_internal_approval_marker(system):
    s = system
    path = s.cat.root / SID / "plan.json"
    template = json.loads(path.read_text(encoding="utf-8"))
    template["steps"][0]["requires_approval"] = False
    path.write_text(json.dumps(template), encoding="utf-8")
    with pytest.raises(Fault) as invalid:
        submit(s)
    assert invalid.value.code == "PLAN_INVALID"


def test_internal_phase_marker_preserves_existing_plan_digest(system):
    s = system
    plan, _ = prepare_plan(s.cat, SID, BODY["message"], {"draft.propose"}, SCOPE)
    assert plan["steps"][0].pop("requires_approval") is True
    old_encoding = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert s.store.plan_view(submit(s))["digest"] == hashlib.sha256(old_encoding).hexdigest()
