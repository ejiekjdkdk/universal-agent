"""Human approval and exactly-once writes to the local SQLite demonstration ledger."""
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

from jsonschema import Draft202012Validator

from .errors import Fault


APPROVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES plans(task_id), step_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL, org_id TEXT NOT NULL, user_id TEXT NOT NULL, business_id TEXT NOT NULL,
    capability_id TEXT NOT NULL, capability_version TEXT NOT NULL, permission TEXT NOT NULL,
    object_id TEXT NOT NULL, expected_version INTEGER NOT NULL, payload_digest TEXT NOT NULL,
    plan_digest TEXT NOT NULL, snapshot_id TEXT NOT NULL, proposal_digest TEXT NOT NULL,
    expires REAL NOT NULL, created REAL NOT NULL, proposal_token INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested', commit_status TEXT NOT NULL DEFAULT 'not_started',
    decided_by TEXT, decided_at REAL, decision_key TEXT, decision_hash TEXT, receipt_id TEXT,
    UNIQUE(task_id,step_id)
);
CREATE INDEX IF NOT EXISTS approval_queue ON approvals(tenant_id,org_id,status,created);
CREATE TABLE IF NOT EXISTS mock_ledger (
    tenant_id TEXT NOT NULL, org_id TEXT NOT NULL, business_id TEXT NOT NULL, object_id TEXT NOT NULL,
    version INTEGER NOT NULL, value TEXT NOT NULL, PRIMARY KEY(tenant_id,org_id,business_id,object_id)
);
CREATE TABLE IF NOT EXISTS mock_receipts (
    receipt_id TEXT PRIMARY KEY, approval_id TEXT NOT NULL UNIQUE REFERENCES approvals(approval_id),
    payload_digest TEXT NOT NULL, object_version INTEGER NOT NULL, recorded_at REAL NOT NULL
);
"""

RULE = "independent_object_approver_v1"


def enabled(setting=None):
    return os.getenv("AGENT_MOCK_WRITES") == "1" if setting is None else setting is True


def mock_tool(catalog, sid, name):
    for tool in catalog.scenarios.get(sid, {}).get("tools", []):
        if tool["name"] == name and tool["action_level"] == "commit_write" and tool["adapter"] == {"type": "mock_ledger"}:
            return tool
    return None


def validate_write(args, context):
    if (set(args) not in ({"object_id", "expected_version", "value"}, {"object_id", "value"})
            or not isinstance(args["object_id"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", args["object_id"])
            or ("expected_version" in args and (type(args["expected_version"]) is not int or not 0 <= args["expected_version"] < 2**31))
            or not isinstance(args["value"], str) or not 1 <= len(args["value"]) <= 4096):
        raise Fault("SCHEMA_VALIDATION_FAILED", "无效的本地账本参数。", 422)
    if args["object_id"] not in context.get("object_ids", []):
        raise Fault("PERMISSION_DENIED", "对象不在任务范围内。", 403)


def _digest(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(data.encode()).hexdigest()


def _stamp(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat() if t is not None else None


def _denied():
    return Fault("PERMISSION_DENIED", "无权执行此审批操作。", 403)


def _changed():
    return Fault("APPROVAL_PROPOSAL_CHANGED", "提案摘要、对象版本或能力已改变。", 409)


def _visible(actor, row):
    if actor["tenant_id"] != row["tenant_id"] or actor["org_id"] != row["org_id"]:
        return False, False
    grant = actor.get("grants", {}).get(row["business_id"], {})
    if row["object_id"] not in grant.get("objects", []):
        return False, False
    permissions = grant.get("permissions", [])
    own = actor["user_id"] == row["user_id"] and row["permission"] in permissions
    judge = actor["user_id"] != row["user_id"] and "approval.decide" in permissions
    return own, judge


def verify_receipt(db, receipt_id, digest, version):
    row = db.execute("""SELECT r.payload_digest,r.object_version,l.object_id,l.value,l.version,a.expected_version
        FROM mock_receipts r JOIN approvals a USING(approval_id)
        JOIN mock_ledger l ON l.tenant_id=a.tenant_id AND l.org_id=a.org_id AND l.business_id=a.business_id
        AND l.object_id=a.object_id WHERE r.receipt_id=?""", (receipt_id,)).fetchone()
    if not row or row["payload_digest"] != digest or row["version"] != version or row["object_version"] != version:
        raise TimeoutError("mock ledger confirmation unavailable")
    if _digest({k: row[k] for k in ("object_id", "expected_version", "value")}) != digest:
        raise TimeoutError("mock receipt unavailable")


class ApprovalStore:
    def _present_approval(self, a, actor):
        _, judge = _visible(actor, a)
        data = {k: a[k] for k in ("approval_id", "task_id", "step_id", "business_id", "capability_id", "capability_version",
                                   "payload_digest", "plan_digest", "proposal_digest", "commit_status", "receipt_id", "decided_by")}
        return {**data, "approval_status": a["status"], "task_status": self._task_row(a["task_id"])["status"],
                "subject_id": a["user_id"], "target_object_ids": [a["object_id"]], "release_snapshot_id": a["snapshot_id"],
                "action_summary": f"更新本地模拟账本对象 {a['object_id']}，版本 {a['expected_version']} → {a['expected_version']+1}",
                "field_diff": {"version": {"before": a["expected_version"], "after": a["expected_version"]+1},
                               "value": {"operation": "replace", "redacted": True}},
                "approval_policy_id": RULE, "required_approver_rule": "same_org_independent_approval.decide",
                "created_at": _stamp(a["created"]), "expires_at": _stamp(a["expires"]), "expires_at_unix": a["expires"],
                "decided_at": _stamp(a["decided_at"]), "can_decide": judge and a["status"] == "requested" and time.time() < a["expires"]}

    def _approval(self, actor, approval_id):
        a = self._db.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if not a or not any(_visible(actor, a)):
            raise Fault("APPROVAL_NOT_FOUND", "审批单不可见。", 404)
        return a

    def approvals(self, actor, status, limit, offset):
        with self._transaction(actor, "approval.list"):
            rows = self._db.execute("""SELECT * FROM approvals WHERE tenant_id=? AND org_id=? AND (? IS NULL OR status=?)
                ORDER BY created DESC,approval_id""", (actor["tenant_id"], actor["org_id"], status, status))
            result = []
            for row in rows:
                if any(_visible(actor, row)):
                    if offset:
                        offset -= 1
                        continue
                    result.append(self._present_approval(row, actor))
                    if len(result) == limit:
                        break
            self._audit("approval.list", "allow", actor, None, None, None)
            return result

    def approval(self, actor, approval_id):
        with self._transaction(actor, "approval.detail", approval_id):
            a = self._approval(actor, approval_id)
            self._audit("approval.detail", "allow", actor, a["task_id"], approval_id, None)
            return self._present_approval(a, actor)

    def _mock_commits(self, task_id):
        rows = self._db.execute("""SELECT a.approval_id,a.step_id,a.object_id,a.commit_status,
            r.receipt_id,r.object_version,r.recorded_at FROM mock_receipts r JOIN approvals a USING(approval_id)
            WHERE a.task_id=? ORDER BY r.rowid""", (task_id,))
        return [{**dict(row), "recorded_at": _stamp(row["recorded_at"])} for row in rows]

    def approval_receipt(self, actor, approval_id):
        with self._transaction(actor, "approval.receipt", approval_id):
            a = self._approval(actor, approval_id)
            own, judge = _visible(actor, a)
            if not own and not (judge and actor["user_id"] == a["decided_by"]):
                raise Fault("RECEIPT_NOT_FOUND", "回执不可见。", 404)
            receipt = self._db.execute("SELECT * FROM mock_receipts WHERE approval_id=?", (approval_id,)).fetchone()
            if not receipt:
                raise Fault("RECEIPT_NOT_FOUND", "没有可核对的提交回执。", 404)
            ledger = self._db.execute("""SELECT version,value FROM mock_ledger
                WHERE tenant_id=? AND org_id=? AND business_id=? AND object_id=?""",
                (a["tenant_id"], a["org_id"], a["business_id"], a["object_id"])).fetchone()
            version = receipt["object_version"]
            confirmed = (ledger is not None and ledger["version"] >= version and version == a["expected_version"] + 1
                         and receipt["payload_digest"] == a["payload_digest"] and receipt["receipt_id"] == a["receipt_id"])
            if confirmed and ledger["version"] == version:
                confirmed = _digest({"object_id": a["object_id"], "expected_version": a["expected_version"],
                                     "value": ledger["value"]}) == a["payload_digest"]
            # Later versions may have replaced the value. The immutable receipt and version remain its proof.
            self._audit("approval.receipt", "allow", actor, a["task_id"], approval_id, None)
            return {"approval_id": approval_id, "receipt_id": receipt["receipt_id"], "task_id": a["task_id"],
                    "step_id": a["step_id"], "object_id": a["object_id"], "object_version": version,
                    "current_version": ledger["version"] if ledger else None, "commit_status": a["commit_status"],
                    "verification_status": "confirmed" if confirmed else "unconfirmed",
                    "recorded_at": _stamp(receipt["recorded_at"])}

    def _expire_approvals(self, task_id, now):
        rows = self._db.execute("""SELECT * FROM approvals WHERE task_id=? AND expires<=? AND
            (status='requested' OR (status='approved' AND commit_status='not_started'))""", (task_id, now)).fetchall()
        for a in rows:
            self._db.execute("""UPDATE approvals SET status=CASE WHEN status='requested' THEN 'expired' ELSE status END,
                commit_status=CASE WHEN status='approved' THEN 'failed' ELSE commit_status END WHERE approval_id=?""", (a["approval_id"],))
            error = Fault("APPROVAL_EXPIRED", "审批有效期已结束。", 409).as_dict()
            self._change_step(task_id, a["step_id"], "failed", error)
            self._event(task_id, "approval_expired", {"approval_id": a["approval_id"], "step_id": a["step_id"]})

    def decide_approval(self, actor, approval_id, decision, digest, key, comment=""):
        body_hash = _digest([decision, digest, comment])
        with self._transaction(actor, "approval.decide", approval_id):
            a = self._approval(actor, approval_id)
            if not _visible(actor, a)[1]:
                raise _denied()
            if not hmac.compare_digest(a["proposal_digest"].encode(), digest.encode()):
                raise _changed()
            if a["status"] != "requested":
                if a["decided_by"] == actor["user_id"] and a["decision_key"] == key and a["decision_hash"] == body_hash:
                    return self._present_approval(a, actor)
                raise Fault("APPROVAL_STATE_CONFLICT", "审批单已经终结。", 409)
            now = time.time()
            tid = a["task_id"]
            plan = self._db.execute("SELECT * FROM plans WHERE task_id=?", (tid,)).fetchone()
            if now >= a["expires"]:
                self._expire_approvals(tid, now)
                self._advance(plan, now)
            else:
                state = "approved" if decision == "approve" else "rejected"
                self._db.execute("""UPDATE approvals SET status=?,decided_by=?,decided_at=?,decision_key=?,decision_hash=?
                    WHERE approval_id=?""", (state, actor["user_id"], now, key, body_hash, approval_id))
                self._audit("approval.decide", state, actor, tid, approval_id, None)
                self._event(tid, "approval_decided", {"approval_id": approval_id, "step_id": a["step_id"],
                                                      "decision": decision, "actor_id": actor["user_id"]})
                self._db.execute("UPDATE tasks SET status='running' WHERE task_id=? AND status='waiting_approval'", (tid,))
                if state == "approved":
                    self._change_step(tid, a["step_id"], "ready")
                else:
                    self._change_step(tid, a["step_id"], "failed", Fault("APPROVAL_REJECTED", "提案已被拒绝。", 403).as_dict())
                self._advance(plan, now)
                return self._present_approval(self._approval(actor, approval_id), actor)
        raise Fault("APPROVAL_EXPIRED", "审批有效期已结束。", 409)

    def _cancel_approvals(self, tid):
        self._db.execute("""UPDATE approvals SET status='cancelled' WHERE task_id=? AND status='requested'""", (tid,))
        self._db.execute("""UPDATE approvals SET commit_status='failed' WHERE task_id=? AND status='approved'
                            AND commit_status='not_started'""", (tid,))

    def _mock_requester(self, runtime, task, tool, args):
        scope = self.scope(task["task_id"])
        validate_write(args, scope)
        grant = runtime.grant(dict(task), task["business_id"])
        required = self.permissions(task["task_id"]) | {tool["permission"]}
        if (not required.issubset(grant.get("permissions", []))
                or not set(scope["object_ids"]).issubset(grant.get("objects", []))
                or (scope["object_type"] and scope["object_type"] not in grant.get("resource_types", []))):
            raise _denied()
        if not Draft202012Validator(tool["input_schema"]).is_valid(args):
            raise Fault("SCHEMA_VALIDATION_FAILED", "模拟提交输入未通过场景校验。", 422)
        if hasattr(runtime, "releases") and runtime.releases is not None:
            live = runtime.releases.current_tool(task["business_id"], dict(task), "", tool["name"])
            if not live or live.get("adapter_type") != "mock_ledger":
                raise _changed()
        else:
            live = mock_tool(runtime.catalog, task["business_id"], tool["name"])
        if not live or live["permission"] != tool["permission"] or live["action_level"] != "commit_write":
            raise _changed()

    def process_mock_write(self, runtime, claim, tool, args):
        tid, sid = claim["task_id"], claim["step_id"]
        with self._transaction():
            lease = self._guard_lease(tid, claim, cap_id=tool["name"])
            if not enabled(getattr(runtime, "mock_writes", None)):
                raise Fault("ACTION_NOT_ALLOWED", "本地模拟写入未开启。", 403)
            task = self._task_row(tid)
            plan = self._db.execute("SELECT * FROM plans WHERE task_id=?", (tid,)).fetchone()
            self._mock_requester(runtime, task, tool, args)
            if getattr(self, "release_guard", None):
                self.release_guard(plan["snapshot_id"], task["business_id"], tool["name"])
            version = task["business_version"]
            subject = (task["tenant_id"], task["org_id"], task["user_id"], task["business_id"])
            a = self._db.execute("SELECT * FROM approvals WHERE task_id=? AND step_id=?", (tid, sid)).fetchone()
            keys = (task["tenant_id"], task["org_id"], task["business_id"], args["object_id"])
            prior = self._db.execute("SELECT version FROM mock_ledger WHERE tenant_id=? AND org_id=? AND business_id=? AND object_id=?", keys).fetchone()
            current_version = prior[0] if prior else 0
            if "expected_version" not in args:
                args = {**args, "expected_version": a["expected_version"] if a else current_version}
            payload = _digest(args)
            if current_version != args["expected_version"]:
                raise _changed()
            now = time.time()
            expires = a["expires"] if a else min(now + 300, plan["deadline"])
            binding = _digest([tid, sid, *subject, tool["name"], tool["permission"], version, plan["digest"],
                               plan["snapshot_id"], payload, expires, RULE])
            if not a:
                aid = "apr_" + uuid4().hex
                self._db.execute("""INSERT INTO approvals(approval_id,task_id,step_id,tenant_id,org_id,user_id,business_id,
                    capability_id,capability_version,permission,object_id,expected_version,payload_digest,plan_digest,snapshot_id,
                    proposal_digest,expires,created,proposal_token) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (aid, tid, sid, *subject, tool["name"], version, tool["permission"], args["object_id"], args["expected_version"],
                     payload, plan["digest"], plan["snapshot_id"], binding, expires, now, lease["token"]))
                self._db.execute("INSERT OR IGNORE INTO task_permissions VALUES(?,?)", (tid, tool["permission"]))
                self._change_step(tid, sid, "waiting_approval")
                self._audit("approval.propose", "allow", dict(task), tid, aid, None)
                self._event(tid, "approval_required", {"approval_id": aid, "step_id": sid,
                    "action_summary": f"更新本地模拟对象 {args['object_id']}，版本 {args['expected_version']} → {args['expected_version']+1}",
                    "expires_at": _stamp(expires)})
                self._advance(plan, now)
                return
            if (a["proposal_digest"] != binding or a["status"] != "approved" or a["commit_status"] != "not_started"):
                raise _changed()
            if now >= a["expires"]:
                raise Fault("APPROVAL_EXPIRED", "审批有效期已结束。", 409)
            judge = next((p for p in runtime.principals.values() if p["tenant_id"] == a["tenant_id"]
                          and p["org_id"] == a["org_id"] and p["user_id"] == a["decided_by"]), None)
            if not judge or not _visible(judge, a)[1]:
                raise _denied()
            rid = "receipt_" + uuid4().hex
            new_version = args["expected_version"] + 1
            data = {"summary": f"本地模拟账本对象 {args['object_id']} 已更新至版本 {new_version}。",
                    "object_id": args["object_id"], "version": new_version, "receipt_id": rid}
            if not Draft202012Validator(tool["output_schema"]).is_valid(data):
                raise Fault("CAPABILITY_OUTPUT_INVALID", "模拟提交回执不符合输出结构。", 502)
            count = self._db.execute("SELECT COUNT(*) FROM calls WHERE task_id=?", (tid,)).fetchone()[0]
            if count >= lease["call_limit"]:
                raise Fault("EXECUTION_BUDGET_EXCEEDED", "工具调用预算已耗尽。", 429)
            self._guard_lease(tid, claim)
            if time.time() >= a["expires"]:
                raise Fault("APPROVAL_EXPIRED", "审批有效期已结束。", 409)
            self._db.execute("UPDATE approvals SET commit_status='in_progress' WHERE approval_id=?", (a["approval_id"],))
            execution_id = "exec_" + uuid4().hex
            self._event(tid, "step_started", {"step_id": sid, "capability_id": tool["name"], "execution_id": execution_id})
            self._db.execute("""INSERT INTO mock_ledger VALUES(?,?,?,?,?,?) ON CONFLICT(tenant_id,org_id,business_id,object_id)
                DO UPDATE SET version=excluded.version,value=excluded.value""", (*keys, new_version, args["value"]))
            self._db.execute("INSERT INTO mock_receipts VALUES(?,?,?,?,?)", (rid, a["approval_id"], payload, new_version, now))
            error = None
            try:
                verify_receipt(self._db, rid, payload, new_version)
            except TimeoutError:
                error = Fault("OUTCOME_UNKNOWN", "模拟提交的确认超时。请按回执核对，禁止自动重发。", 504).as_dict()
            status = "outcome_unknown" if error else "succeeded"
            self._db.execute("UPDATE approvals SET receipt_id=?,commit_status=? WHERE approval_id=?", (rid, status, a["approval_id"]))
            self._db.execute("""INSERT INTO calls(task_id,call_id,execution_id,capability_id,capability_version,arguments_digest,
                status,started,duration_ms,error) VALUES(?,?,?,?,?,?,?,?,?,?)""", (tid, "plan."+sid, execution_id, tool["name"],
                version, payload, "unknown_outcome" if error else "completed", now, max(0, int((time.time()-now)*1000)), json.dumps(error)))
            self._audit("mock_ledger.commit", status, dict(task), tid, rid, (error or {}).get("code"))
            self._event(tid, "mock_commit_verified" if not error else "mock_commit_unknown",
                        {"step_id": sid, "approval_id": a["approval_id"], "receipt_id": rid, "execution_id": execution_id})
            self._change_step(tid, sid, status, error, None if error else {"data": data, "evidence": []})
            if error:
                self._finish(tid, "failed", {}, error)
            else:
                self._advance(plan, time.time())
