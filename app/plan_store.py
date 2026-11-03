"""SQLite state transitions for validated, read-only plans. Mutations run inside Store transactions."""
import hashlib
import json
import time
from uuid import uuid4

from .errors import Fault


PLAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id), plan_id TEXT NOT NULL UNIQUE,
    schema_version TEXT NOT NULL, result_contract TEXT NOT NULL, digest TEXT NOT NULL,
    snapshot_id TEXT NOT NULL, deadline REAL NOT NULL, parallel_limit INTEGER NOT NULL,
    call_limit INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_steps (
    task_id TEXT NOT NULL REFERENCES plans(task_id), step_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
    definition TEXT NOT NULL, status TEXT NOT NULL, token INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT, lease_until REAL, call_token INTEGER, output TEXT, error TEXT,
    PRIMARY KEY(task_id,step_id), UNIQUE(task_id,ordinal)
);
CREATE INDEX IF NOT EXISTS plan_step_claim ON plan_steps(task_id,status,lease_until);
"""

MAX_ATTEMPTS = 3
ACTIVE = {"leased", "running"}
# A generic dependency failure may conceal malformed input or an upstream denial.
OPTIONAL_ERRORS = {"UPSTREAM_UNAVAILABLE", "TOOL_TIMEOUT", "RATE_LIMITED", "STEP_ATTEMPTS_EXHAUSTED"}


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _error(code, message):
    return {"code": code, "message": message, "retryable": False, "details": {}}


def _predecessor_outputs(step, rows):
    by_name = {r["step_id"]: r for r in rows}
    remaining = list(step["depends_on"])
    outputs = {}
    while remaining:
        name = remaining.pop()
        if name not in outputs:
            row = by_name[name]
            outputs[name] = json.loads(row["output"])
            remaining.extend(json.loads(row["definition"])["depends_on"])
    return outputs


class PlanStore:
    def _create_plan(self, task_id, plan, snapshot_id, budget):
        steps = plan["steps"]
        if (not snapshot_id or plan.get("result_contract") != "summary_v1"
                or not steps or len(steps) > budget["max_steps"]):
            raise Fault("INVALID_PLAN", "Invalid plan contract or budget.", 422)
        plan_id = "plan_" + uuid4().hex
        digest = hashlib.sha256(_dump(plan).encode()).hexdigest()
        deadline = time.time() + budget["deadline_ms"] / 1000
        self._db.execute("INSERT INTO plans VALUES(?,?,?,?,?,?,?,?,?)",
                         (task_id, plan_id, str(plan["schema_version"]), plan["result_contract"], digest,
                          snapshot_id, deadline, budget["max_parallel_steps"], budget["max_tool_calls"]))
        self._db.execute("UPDATE runs SET expires_at=MAX(expires_at,?) WHERE task_id=?", (deadline, task_id))
        meta = {"plan_id": plan_id, "revision": 1, "step_count": len(steps), "snapshot_id": snapshot_id}
        self._event(task_id, "plan_created", meta)
        self._event(task_id, "plan_validated", meta)
        for ordinal, step in enumerate(steps):
            status = "blocked" if step["depends_on"] else "ready"
            self._db.execute("""INSERT INTO plan_steps(task_id,step_id,ordinal,definition,status)
                                VALUES(?,?,?,?,?)""", (task_id, step["step_id"], ordinal, _dump(step), status))
            if status == "ready":
                self._event(task_id, "step_queued", {"step_id": step["step_id"]})

    def is_durable(self, task_id):
        with self._lock:
            return self._db.execute("SELECT 1 FROM plans WHERE task_id=?", (task_id,)).fetchone() is not None

    def _steps(self, task_id):
        return self._db.execute("SELECT * FROM plan_steps WHERE task_id=? ORDER BY ordinal", (task_id,)).fetchall()

    def plan_view(self, task_id):
        with self._lock:
            p = self._db.execute("SELECT * FROM plans WHERE task_id=?", (task_id,)).fetchone()
            if not p:
                raise Fault("PLAN_NOT_FOUND", "No plan exists for this task.", 404)
            steps = []
            for row in self._steps(task_id):
                definition = json.loads(row["definition"])
                error = json.loads(row["error"] or "null")
                steps.append({"step_id": row["step_id"], "capability_id": definition["capability_id"],
                              "required": definition.get("required", True), "depends_on": definition["depends_on"],
                              "status": row["status"], "attempt_no": min(row["token"], MAX_ATTEMPTS),
                              "error": {"code": error["code"]} if error else None})
            return {"task_id": task_id, "plan_id": p["plan_id"], "revision": 1, "digest": p["digest"],
                    "snapshot_id": p["snapshot_id"], "schema_version": p["schema_version"],
                    "result_contract": p["result_contract"], "steps": steps}

    def _change_step(self, task_id, step_id, status, error=None, result=None):
        if status == "failed":
            self._db.execute("""UPDATE approvals SET commit_status='failed' WHERE task_id=? AND step_id=?
                                AND status='approved' AND commit_status='not_started'""", (task_id, step_id))
        self._db.execute("""UPDATE plan_steps SET status=?,error=?,output=?,lease_owner=NULL,lease_until=NULL
                            WHERE task_id=? AND step_id=?""",
                         (status, _dump(error) if error else None, _dump(result) if result is not None else None,
                          task_id, step_id))
        kind = {"succeeded": "step_completed", "ready": "step_queued"}.get(status, "step_" + status)
        payload = {"step_id": step_id, "status": status}
        if error:
            payload["error"] = error
        record = self._db.execute("""SELECT definition FROM plan_steps WHERE task_id=? AND step_id=?""",
                                  (task_id, step_id)).fetchone()
        payload["capability_id"] = json.loads(record["definition"])["capability_id"]
        call = self._db.execute("SELECT execution_id FROM calls WHERE task_id=? AND call_id=?",
                                (task_id, "plan." + step_id)).fetchone()
        if call:
            payload["execution_id"] = call[0]
        self._event(task_id, kind, payload)

    def _interrupt_plan(self, task_id, status):
        self._cancel_approvals(task_id)
        for row in self._steps(task_id):
            if row["status"] in ACTIVE | {"blocked", "ready", "waiting_approval"}:
                self._change_step(task_id, row["step_id"], "cancelled" if status == "cancelled" else "skipped")
        self._db.execute("UPDATE plan_steps SET output=NULL WHERE task_id=? AND output IS NOT NULL", (task_id,))

    def _aggregate(self, task_id, rows):
        definitions = {r["step_id"]: json.loads(r["definition"]) for r in rows}
        parents = {p for step in definitions.values() for p in step["depends_on"]}
        successful = [r for r in rows if r["status"] == "succeeded"]
        missing = [r["step_id"] for r in rows if r["status"] != "succeeded"]
        failed = [r["step_id"] for r in rows if r["status"] == "failed"]
        sinks = [r for r in successful if r["step_id"] not in parents]
        summaries = []
        evidence, seen = [], set()
        for r in successful:
            result = json.loads(r["output"])
            if r in (sinks or successful):
                summary = result.get("data", {}).get("summary")
                if not isinstance(summary, str) or not summary.strip():
                    self._finish(task_id, "failed", {}, _error("CAPABILITY_OUTPUT_INVALID", "Missing result summary."))
                    return
                summaries.append(summary)
            for item in result.get("evidence", []):
                key = _dump(item)
                if key not in seen:
                    seen.add(key)
                    evidence.append(item)
        if not summaries:
            self._finish(task_id, "failed", {}, _error("NO_USABLE_RESULT", "No usable step outputs."))
            return
        result = {"answer": "\n".join(summaries), "artifacts": [], "evidence": evidence}
        if missing:
            result.update(failed_steps=failed, missing_outputs=missing,
                          limitations=["部分可选步骤未能提供结果：" + "、".join(missing)])
        self._finish(task_id, "partial" if missing else "completed", result, None)

    def _advance(self, p, now):
        task_id = p["task_id"]
        self._expire_approvals(task_id, now)
        rows = self._steps(task_id)
        states = {r["step_id"]: r["status"] for r in rows}
        for r in rows:
            if r["status"] not in ACTIVE or r["lease_until"] > now:
                continue
            call = self._db.execute("""SELECT status,error FROM calls WHERE task_id=? AND call_id=?""",
                                    (task_id, "plan." + r["step_id"])).fetchone()
            prior_proposal = self._db.execute("""SELECT proposal_token FROM approvals WHERE task_id=? AND step_id=?
                AND status='approved'""", (task_id, r["step_id"])).fetchone()
            attempt = r["token"] - (prior_proposal[0] if prior_proposal else 0)
            if call and call["status"] == "failed":
                # A committed denial or schema error must keep its original classification after a crash.
                error = json.loads(call["error"])
            elif attempt < MAX_ATTEMPTS or (attempt == MAX_ATTEMPTS and call and call["status"] == "completed"):
                # At the attempt limit, only a committed result can receive a final cache-only lease.
                continue
            else:
                error = _error("STEP_ATTEMPTS_EXHAUSTED", "Worker lease attempts exhausted.")
            self._change_step(task_id, r["step_id"], "failed", error)
            states[r["step_id"]] = "failed"
        if p["deadline"] <= now:
            self._finish(task_id, "failed", {}, _error("TASK_TIMEOUT", "Plan deadline exceeded."))
            return
        # Fixed point also handles valid plans whose file order is not topological.
        changed = True
        while changed:
            changed = False
            for r in rows:
                name = r["step_id"]
                if states[name] != "blocked":
                    continue
                deps = [states[d] for d in json.loads(r["definition"])["depends_on"]]
                if any(s in {"failed", "skipped", "cancelled"} for s in deps):
                    state = "skipped"
                elif all(s == "succeeded" for s in deps):
                    state = "ready"
                else:
                    continue
                self._change_step(task_id, name, state)
                states[name] = state
                changed = True
        rows = self._steps(task_id)
        failed = [r for r in rows if r["status"] == "failed"]
        for r in failed:
            error = json.loads(r["error"])
            if json.loads(r["definition"]).get("required", True) or error["code"] not in OPTIONAL_ERRORS:
                self._finish(task_id, "failed", {}, error)
                return
        if any(r["status"] == "skipped" and json.loads(r["definition"]).get("required", True) for r in rows):
            self._finish(task_id, "failed", {}, _error("DEPENDENCY_FAILED", "Required dependency failed."))
            return
        if all(r["status"] in {"succeeded", "failed", "skipped"} for r in rows):
            self._aggregate(task_id, rows)
        elif any(r["status"] == "waiting_approval" for r in rows) and not any(r["status"] in ACTIVE | {"ready"} for r in rows):
            self._db.execute("UPDATE tasks SET status='waiting_approval' WHERE task_id=?", (task_id,))

    def claim_step(self, worker_id, lease_seconds):
        if not worker_id or not lease_seconds > 0:
            raise ValueError("worker_id and positive lease duration are required")
        with self._lock:
            active_plan = self._db.execute("""SELECT 1 FROM plans p JOIN tasks t USING(task_id)
                WHERE t.status='running' OR (t.status='waiting_approval' AND
                (p.deadline<=? OR EXISTS(SELECT 1 FROM approvals a WHERE a.task_id=p.task_id
                AND a.status='requested' AND a.expires<=?))) LIMIT 1""", (time.time(), time.time())).fetchone()
        if not active_plan:
            return None
        with self._transaction():
            now = time.time()
            plans = self._db.execute("""SELECT p.* FROM plans p JOIN tasks t USING(task_id)
                WHERE t.status IN ('running','waiting_approval') ORDER BY t.rowid""").fetchall()
            for p in plans:
                tid = p["task_id"]
                self._advance(p, now)
                if self._task_row(tid)["status"] != "running":
                    continue
                rows = self._steps(tid)
                active = sum(r["status"] in ACTIVE and r["lease_until"] > now for r in rows)
                if active >= p["parallel_limit"]:
                    continue
                for r in rows:
                    if r["status"] != "ready" and not (r["status"] in ACTIVE and r["lease_until"] <= now):
                        continue
                    step, token = json.loads(r["definition"]), r["token"] + 1
                    key = (tid, r["step_id"])
                    self._db.execute("""UPDATE plan_steps SET status='leased',token=?,lease_owner=?,lease_until=?
                        WHERE task_id=? AND step_id=?""", (token, worker_id, min(now + lease_seconds, p["deadline"]), *key))
                    if r["status"] in ACTIVE:
                        self._event(tid, "step_retrying", {"step_id": r["step_id"], "attempt": min(token, MAX_ATTEMPTS),
                                                          "error_code": "LEASE_EXPIRED"})
                    proposal = self._db.execute("""SELECT proposal_token FROM approvals WHERE task_id=? AND step_id=?
                        AND status='approved'""", key).fetchone()
                    operation_attempt = token - (proposal[0] if proposal else 0)
                    recovery_only = operation_attempt > MAX_ATTEMPTS
                    attempt = min(operation_attempt, MAX_ATTEMPTS)
                    self._event(tid, "step_leased", {"step_id": r["step_id"], "attempt_no": attempt,
                                                      "recovery_only": recovery_only})
                    return {"task_id": tid, "step_id": r["step_id"], "token": token, "attempt_no": attempt,
                            "recovery_only": recovery_only,
                            "worker_id": worker_id, "business_id": self._task_row(tid)["business_id"],
                            "run_context_id": self.context_id(tid), "snapshot_id": p["snapshot_id"],
                            "step": step, "outputs": _predecessor_outputs(step, rows),
                            "max_tool_calls": p["call_limit"], "deadline": p["deadline"]}
            return None

    def _lease(self, claim):
        if not claim:
            return None
        now = time.time()
        return self._db.execute("""SELECT s.*,p.deadline,p.call_limit FROM plan_steps s JOIN plans p USING(task_id)
            JOIN tasks t USING(task_id) WHERE s.task_id=? AND s.step_id=? AND s.token=? AND s.lease_owner=?
            AND s.status IN ('leased','running') AND s.lease_until>? AND p.deadline>? AND t.status='running'""",
            (claim["task_id"], claim["step_id"], claim["token"], claim["worker_id"], now, now)).fetchone()

    def _guard_lease(self, task_id, claim, call_id=None, cap_id=None):
        if claim is None and not self.is_durable(task_id):
            return None
        row = self._lease(claim)
        if (not row or row["task_id"] != task_id or (call_id is not None and call_id != "plan." + row["step_id"])
                or (cap_id is not None and cap_id != json.loads(row["definition"])["capability_id"])):
            raise Fault("LEASE_LOST", "Step lease is unavailable.", 409)
        return row

    def renew_step(self, claim, lease_seconds):
        if not lease_seconds > 0:
            return False
        with self._transaction():
            row = self._lease(claim)
            if not row:
                return False
            self._db.execute("UPDATE plan_steps SET lease_until=? WHERE task_id=? AND step_id=?",
                             (min(time.time() + lease_seconds, row["deadline"]), row["task_id"], row["step_id"]))
            return True

    def settle_step(self, claim, result=None, error=None):
        with self._transaction():
            row = self._lease(claim)
            if not row:
                return False
            tid = row["task_id"]
            if error is None and (not isinstance(result, dict) or not isinstance(result.get("data"), dict)
                                  or not isinstance(result.get("evidence", []), list)):
                error = _error("CAPABILITY_OUTPUT_INVALID", "Invalid step output.")
            self._change_step(tid, row["step_id"], "failed" if error else "succeeded", error, result if not error else None)
            p = self._db.execute("SELECT * FROM plans WHERE task_id=?", (tid,)).fetchone()
            self._advance(p, time.time())
            return True
