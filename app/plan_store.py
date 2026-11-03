"""在 Store 事务中推进持久化计划，包括只读步骤和需要人工审批的本地模拟写入。"""
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
# 仅这些错误允许可选步骤降级。未分类的依赖错误可能来自错误入参或上游拒绝授权。
OPTIONAL_ERRORS = {"UPSTREAM_UNAVAILABLE", "TOOL_TIMEOUT", "RATE_LIMITED", "STEP_ATTEMPTS_EXHAUSTED"}

APPROVAL_JOIN = " FROM plan_steps s LEFT JOIN approvals a USING(task_id, step_id)"
APPROVAL_ATTEMPT_COLUMNS = "a.status AS approval_status, a.proposal_token"


def _phase_attempt_number(step, token):
    return token - step["proposal_token"] if step["approval_status"] == "approved" else token


def _attempt_metrics(step, token):
    """租约 token 始终递增。获批后提交次数重新起算，额外的缓存核验租约不增加次数。"""
    phase = "execution"
    if step["approval_status"] != "approved" and (
        step["approval_status"] is not None or json.loads(step["definition"]).get("requires_approval")
    ):
        # 旧计划没有标记时，仍可根据已经持久化的审批单识别提案阶段。
        phase = "proposal"
    phase_attempt = _phase_attempt_number(step, token)
    return {"phase": phase, "attempt_no": min(phase_attempt, MAX_ATTEMPTS),
            "recovery_only": phase_attempt > MAX_ATTEMPTS}


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _error(code, message):
    return {"code": code, "message": message, "retryable": False, "details": {}}


def _predecessor_outputs(step, rows, restore):
    by_name = {r["step_id"]: r for r in rows}
    remaining = list(step["depends_on"])
    outputs = {}
    while remaining:
        name = remaining.pop()
        if name not in outputs:
            row = by_name[name]
            outputs[name] = restore(json.loads(row["output"]))
            remaining.extend(json.loads(row["definition"])["depends_on"])
    return outputs


class PlanStore:
    def _create_plan(self, task_id, plan, snapshot_id, budget):
        steps = plan["steps"]
        if (not snapshot_id or plan.get("result_contract") != "summary_v1"
                or not steps or len(steps) > budget["max_steps"]):
            raise Fault("INVALID_PLAN", "Invalid plan contract or budget.", 422)
        plan_id = "plan_" + uuid4().hex
        # 此标记只解释监控数据。审批继续绑定原计划参数及快照，沿用旧版本的计划摘要。
        contract_steps = [{key: value for key, value in step.items() if key != "requires_approval"} for step in steps]
        digest = hashlib.sha256(_dump({**plan, "steps": contract_steps}).encode()).hexdigest()
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
        query = "SELECT s.*, " + APPROVAL_ATTEMPT_COLUMNS + APPROVAL_JOIN + " WHERE s.task_id=? ORDER BY s.ordinal"
        return self._db.execute(query, (task_id,)).fetchall()

    def _step_event_metrics(self, task_id, event, data):
        if not event.startswith(("step_", "tool_call_")) or not isinstance(data.get("step_id"), str):
            return data
        query = ("SELECT s.token, s.definition, " + APPROVAL_ATTEMPT_COLUMNS + APPROVAL_JOIN
                 + " WHERE s.task_id=? AND s.step_id=?")
        step = self._db.execute(query, (task_id, data["step_id"])).fetchone()
        if step is None:
            return data
        attempt = _attempt_metrics(step, step["token"])
        data = {**data, "phase": attempt["phase"], "attempt_no": attempt["attempt_no"]}
        if event == "step_retrying":
            data["attempt"] = attempt["attempt_no"]
        return data

    def plan_view(self, task_id):
        with self._lock:
            p = self._db.execute("SELECT * FROM plans WHERE task_id=?", (task_id,)).fetchone()
            if not p:
                raise Fault("PLAN_NOT_FOUND", "No plan exists for this task.", 404)
            steps = []
            for row in self._steps(task_id):
                definition = json.loads(row["definition"])
                error = json.loads(row["error"] or "null")
                attempt = _attempt_metrics(row, row["token"])
                steps.append({"step_id": row["step_id"], "capability_id": definition["capability_id"],
                              "required": definition.get("required", True), "depends_on": definition["depends_on"],
                              "status": row["status"], "phase": attempt["phase"], "attempt_no": attempt["attempt_no"],
                              "error": {"code": error["code"]} if error else None})
            return {"task_id": task_id, "plan_id": p["plan_id"], "revision": 1, "digest": p["digest"],
                    "snapshot_id": p["snapshot_id"], "schema_version": p["schema_version"],
                    "result_contract": p["result_contract"], "steps": steps}

    def _change_step(self, task_id, step_id, status, error=None, result=None):
        if result is not None:
            result = self._persist_tool_result(task_id, result)
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
            try:
                result = self._restore_tool_result(json.loads(r["output"]), task_id)
            except Fault as exc:
                self._finish(task_id, "failed", {}, exc.as_dict())
                return
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

    def _recover_expired_steps(self, task_id, rows, now):
        """保留已落库的错误，为可重试步骤留下重新领取租约的机会。"""
        states = {step_row["step_id"]: step_row["status"] for step_row in rows}
        for step_row in rows:
            if step_row["status"] not in ACTIVE or step_row["lease_until"] > now:
                continue
            call = self._db.execute("""SELECT status,error FROM calls WHERE task_id=? AND call_id=?""",
                                    (task_id, "plan." + step_row["step_id"])).fetchone()
            operation_attempt = _phase_attempt_number(step_row, step_row["token"])
            if call and call["status"] == "failed":
                error = json.loads(call["error"])
            elif operation_attempt < MAX_ATTEMPTS or (
                operation_attempt == MAX_ATTEMPTS and call and call["status"] == "completed"
            ):
                # 尝试次数耗尽时，仅允许领取最后一次租约以核验已保存的成功结果。
                continue
            else:
                error = _error("STEP_ATTEMPTS_EXHAUSTED", "Worker lease attempts exhausted.")
            self._change_step(task_id, step_row["step_id"], "failed", error)
            states[step_row["step_id"]] = "failed"
        return states

    def _advance_dependencies(self, task_id, rows, states):
        # 一次依赖失败可连续跳过多个后继，即使计划文件未按依赖顺序排列。
        changed = True
        while changed:
            changed = False
            for step_row in rows:
                step_id = step_row["step_id"]
                if states[step_id] != "blocked":
                    continue
                dependency_states = [states[d] for d in json.loads(step_row["definition"])["depends_on"]]
                if any(state in {"failed", "skipped", "cancelled"} for state in dependency_states):
                    state = "skipped"
                elif all(state == "succeeded" for state in dependency_states):
                    state = "ready"
                else:
                    continue
                self._change_step(task_id, step_id, state)
                states[step_id] = state
                changed = True

    def _advance(self, p, now):
        task_id = p["task_id"]
        self._expire_approvals(task_id, now)
        rows = self._steps(task_id)
        states = self._recover_expired_steps(task_id, rows, now)
        if p["deadline"] <= now:
            self._finish(task_id, "failed", {}, _error("TASK_TIMEOUT", "Plan deadline exceeded."))
            return

        self._advance_dependencies(task_id, rows, states)
        rows = self._steps(task_id)
        failed_steps = [step_row for step_row in rows if step_row["status"] == "failed"]
        for step_row in failed_steps:
            error = json.loads(step_row["error"])
            if json.loads(step_row["definition"]).get("required", True) or error["code"] not in OPTIONAL_ERRORS:
                self._finish(task_id, "failed", {}, error)
                return
        required_step_skipped = any(
            row["status"] == "skipped" and json.loads(row["definition"]).get("required", True)
            for row in rows
        )
        if required_step_skipped:
            self._finish(task_id, "failed", {}, _error("DEPENDENCY_FAILED", "Required dependency failed."))
            return
        if all(step_row["status"] in {"succeeded", "failed", "skipped"} for step_row in rows):
            self._aggregate(task_id, rows)
            return
        awaiting_approval = any(row["status"] == "waiting_approval" for row in rows)
        if awaiting_approval and not any(row["status"] in ACTIVE | {"ready"} for row in rows):
            self._db.execute("UPDATE tasks SET status='waiting_approval' WHERE task_id=?", (task_id,))

    def claim_step(self, worker_id, lease_seconds):
        """在一个写事务中确认剩余并发额度、登记新租约并分配递增的 token。"""
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
            for plan in plans:
                task_id = plan["task_id"]
                self._advance(plan, now)
                if self._task_row(task_id)["status"] != "running":
                    continue
                rows = self._steps(task_id)
                occupied_slots = sum(row["status"] in ACTIVE and row["lease_until"] > now for row in rows)
                if occupied_slots >= plan["parallel_limit"]:
                    continue
                for step_row in rows:
                    lease_expired = step_row["status"] in ACTIVE and step_row["lease_until"] <= now
                    if step_row["status"] != "ready" and not lease_expired:
                        continue
                    step = json.loads(step_row["definition"])
                    token = step_row["token"] + 1
                    step_id = step_row["step_id"]
                    key = (task_id, step_id)
                    self._db.execute("""UPDATE plan_steps SET status='leased',token=?,lease_owner=?,lease_until=?
                        WHERE task_id=? AND step_id=?""", (token, worker_id, min(now + lease_seconds, plan["deadline"]), *key))
                    if lease_expired:
                        self._event(task_id, "step_retrying", {
                            "step_id": step_id, "error_code": "LEASE_EXPIRED",
                        })
                    attempt = _attempt_metrics(step_row, token)
                    self._event(task_id, "step_leased", {"step_id": step_id, **attempt})
                    try:
                        outputs = _predecessor_outputs(step, rows, lambda value: self._restore_tool_result(value, task_id))
                    except Fault as exc:
                        self._finish(task_id, "failed", {}, exc.as_dict())
                        break
                    return {"task_id": task_id, "step_id": step_id, "token": token, **attempt,
                            "worker_id": worker_id, "business_id": self._task_row(task_id)["business_id"],
                            "run_context_id": self.context_id(task_id), "snapshot_id": plan["snapshot_id"],
                            "step": step, "outputs": outputs,
                            "max_tool_calls": plan["call_limit"], "deadline": plan["deadline"]}
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
