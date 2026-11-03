"""SQLite persistence shared by the API and independently leased plan workers."""
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .errors import Fault
from .plan_store import MAX_ATTEMPTS, PLAN_SCHEMA, PlanStore


TERMINAL_EVENTS = {
    "completed": "task_completed", "partial": "task_completed",
    "failed": "task_failed", "cancelled": "task_cancelled",
}
OWNER_FIELDS = ("tenant_id", "org_id", "user_id")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, org_id TEXT NOT NULL,
    user_id TEXT NOT NULL, business_id TEXT NOT NULL, context TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS session_owner ON sessions(tenant_id,org_id,user_id,business_id);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id),
    request_id TEXT UNIQUE NOT NULL, audit_id TEXT NOT NULL, business_version TEXT NOT NULL,
    mode TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT '{}', error TEXT, created_at TEXT NOT NULL, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS task_session ON tasks(session_id);
CREATE INDEX IF NOT EXISTS task_status ON tasks(status);
CREATE TABLE IF NOT EXISTS task_permissions (
    task_id TEXT NOT NULL REFERENCES tasks(task_id), permission TEXT NOT NULL,
    PRIMARY KEY(task_id,permission)
);
CREATE TABLE IF NOT EXISTS idempotency (
    tenant_id TEXT NOT NULL, org_id TEXT NOT NULL, user_id TEXT NOT NULL,
    business_id TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    PRIMARY KEY(tenant_id,org_id,user_id,business_id,key)
);
CREATE TABLE IF NOT EXISTS runs (
    context_id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL REFERENCES tasks(task_id),
    context TEXT NOT NULL, expires_at REAL NOT NULL, revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    task_id TEXT NOT NULL REFERENCES tasks(task_id), event_id INTEGER NOT NULL,
    event TEXT NOT NULL, payload TEXT NOT NULL, timestamp TEXT NOT NULL,
    PRIMARY KEY(task_id,event_id)
);
CREATE TABLE IF NOT EXISTS calls (
    task_id TEXT NOT NULL REFERENCES tasks(task_id), call_id TEXT NOT NULL,
    execution_id TEXT UNIQUE NOT NULL, capability_id TEXT NOT NULL, capability_version TEXT NOT NULL,
    arguments_digest TEXT NOT NULL, status TEXT NOT NULL, started REAL NOT NULL,
    duration_ms INTEGER, result TEXT, error TEXT, PRIMARY KEY(task_id,call_id)
);
CREATE TABLE IF NOT EXISTS audit_logs (
    audit_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, tenant_id TEXT, org_id TEXT, user_id TEXT,
    request_id TEXT, session_id TEXT, task_id TEXT, action TEXT NOT NULL, decision TEXT NOT NULL,
    resource TEXT, code TEXT
);
CREATE INDEX IF NOT EXISTS audit_task ON audit_logs(task_id,timestamp);
"""

TASK_QUERY = """SELECT t.*,s.business_id,s.tenant_id,s.org_id,s.user_id
                FROM tasks t JOIN sessions s USING(session_id)"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _id(prefix):
    return f"{prefix}_{uuid4().hex}"


def _owner(identity):
    return tuple(identity[k] for k in OWNER_FIELDS)


def _scope(session, requested):
    base = session["context"]
    scope = {**base, **(requested or {})}
    if base.get("object_type") and scope.get("object_type") != base["object_type"]:
        raise Fault("PERMISSION_DENIED", "Object type is outside this session.", 403)
    if base.get("object_ids") and not set(scope.get("object_ids", ())).issubset(base["object_ids"]):
        raise Fault("PERMISSION_DENIED", "Objects are outside this session.", 403)
    return scope


class Store(PlanStore):
    def __init__(self, path, run_ttl_seconds=900, *, recover_reactive=True):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._ttl = run_ttl_seconds
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA + PLAN_SCHEMA)
        if recover_reactive:
            with self._transaction():
                running = self._db.execute("""SELECT task_id FROM tasks t WHERE finished_at IS NULL
                    AND NOT EXISTS(SELECT 1 FROM plans p WHERE p.task_id=t.task_id)""").fetchall()
                for row in running:
                    error = Fault("TASK_INTERRUPTED", "The service restarted during this task.", 503).as_dict()
                    self._finish(row["task_id"], "failed", {}, error)

    @contextmanager
    def _transaction(self, identity=None, action=None, resource=None, *, write=True):
        # Acquire the interprocess writer lock before reading state used for a transition.
        with self._lock:
            try:
                with self._db:
                    self._db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                    yield
            except Fault as exc:
                if action:
                    with self._db:
                        self._audit(action, "deny", identity, None, resource, exc.code)
                raise

    def close(self):
        with self._lock:
            self._db.close()

    def _audit(self, action, decision, identity, task_id, resource, code, audit_id=None):
        audit_id = audit_id or _id("aud")
        task = self._db.execute(TASK_QUERY + " WHERE task_id=?", (task_id,)).fetchone()
        subject = identity or (dict(task) if task else {})
        self._db.execute("INSERT INTO audit_logs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            audit_id, _now(), *(subject.get(k) for k in OWNER_FIELDS),
            task["request_id"] if task else None, task["session_id"] if task else None,
            task_id, action, decision, resource, code,
        ))
        return audit_id

    def audit(self, action, decision, identity=None, task_id=None, resource=None, code=None):
        with self._transaction():
            return self._audit(action, decision, identity, task_id, resource, code)

    def _create_session(self, identity, business_id, context):
        session_id, created = _id("ses"), _now()
        self._db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)",
                         (session_id, *_owner(identity), business_id, _json(context), created))
        return {"session_id": session_id, "business_id": business_id, "context": context,
                "status": "active", "created_at": created}

    def create_session(self, identity, business_id, context):
        with self._transaction():
            return self._create_session(identity, business_id, context or {})

    def _session(self, identity, session_id, business_id):
        row = self._db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if (not row or _owner(row) != _owner(identity)
                or (business_id is not None and business_id != row["business_id"])):
            raise Fault("SESSION_NOT_FOUND", "Session is unavailable.", 404)
        return {"session_id": session_id, "business_id": row["business_id"], "context": json.loads(row["context"]),
                "status": "active", "created_at": row["created_at"]}

    def session(self, identity, session_id, business_id=None):
        with self._transaction(identity, "session.read", session_id, write=False):
            return self._session(identity, session_id, business_id)

    def _replay(self, identity, body, key):
        if key is None:
            return None
        row = self._db.execute("""SELECT request_hash,task_id FROM idempotency
            WHERE tenant_id=? AND org_id=? AND user_id=? AND business_id=? AND key=?""",
            (*_owner(identity), body["business_id"], key)).fetchone()
        if not row:
            return None
        if row["request_hash"] != _digest(body):
            raise Fault("IDEMPOTENCY_CONFLICT", "Key already used for a different request.", 409)
        return self._task(row["task_id"])

    def find_replay(self, identity, body, key):
        with self._transaction(identity, "task.replay", body["business_id"], write=False):
            return self._replay(identity, body, key)

    def start(self, identity, body, key, business_version, *, plan=None, snapshot_id=None, budget=None):
        business_id = body["business_id"]
        with self._transaction(identity, "task.start", business_id):
            existing = self._replay(identity, body, key)
            if existing is not None:
                return existing, False
            if body.get("session_id"):
                session = self._session(identity, body["session_id"], business_id)
            else:
                session = self._create_session(identity, business_id, body.get("context") or {})
            context = _scope(session, body.get("context"))
            task_id, audit_id = _id("task"), _id("aud")
            self._db.execute("""INSERT INTO tasks(task_id,session_id,request_id,audit_id,business_version,
                mode,message,status,created_at) VALUES(?,?,?,?,?,?,?,'running',?)""",
                (task_id, session["session_id"], _id("req"), audit_id, business_version,
                 body.get("mode", "offline"), body["message"], _now()))
            self._db.execute("INSERT INTO runs(context_id,task_id,context,expires_at) VALUES(?,?,?,?)",
                             (_id("ctx"), task_id, _json(context), time.time() + self._ttl))
            if key is not None:
                self._db.execute("INSERT INTO idempotency VALUES(?,?,?,?,?,?,?)",
                                 (*_owner(identity), business_id, key, _digest(body), task_id))
            startup = {"session_id": session["session_id"], "status": "running"}
            if plan is not None:
                startup["execution_mode"] = "durable_plan"
            self._event(task_id, "task_started", startup)
            if plan is not None:
                self._create_plan(task_id, plan, snapshot_id, budget)
            self._audit("task.start", "allow", identity, task_id, business_id, None, audit_id)
            return self._task(task_id), True

    def _task_row(self, task_id, identity=None):
        row = self._db.execute(TASK_QUERY + " WHERE t.task_id=?", (task_id,)).fetchone()
        if not row or (identity is not None and _owner(row) != _owner(identity)):
            raise Fault("TASK_NOT_FOUND", "Task is unavailable.", 404)
        return row

    def _task(self, task_id, identity=None):
        row = self._task_row(task_id, identity)
        result = json.loads(row["result"])
        calls = self._db.execute("""SELECT execution_id,capability_id,capability_version,status,duration_ms
                                    FROM calls WHERE task_id=? ORDER BY rowid""", (task_id,))
        summary = {k: row[k] for k in ("task_id", "session_id", "business_id", "business_version",
                   "audit_id", "request_id", "mode", "status", "message", "created_at", "finished_at")}
        if self.is_durable(task_id):
            summary["execution_mode"] = "durable_plan"
            for key in ("failed_steps", "missing_outputs", "limitations"):
                if key in result:
                    summary[key] = result[key]
        return {**summary, "answer": result.get("answer", ""), "artifacts": result.get("artifacts", []),
                "evidence": result.get("evidence", []), "error": json.loads(row["error"] or "null"),
                "tool_calls": [dict(call) for call in calls], "result": result}

    def task(self, identity, task_id):
        with self._transaction(identity, "task.read", task_id, write=False):
            return self._task(task_id, identity)

    def recent(self, identity, business_id=None, limit=30):
        query = TASK_QUERY + " WHERE tenant_id=? AND org_id=? AND user_id=?"
        params = list(_owner(identity))
        if business_id is not None:
            query += " AND s.business_id=?"
            params.append(business_id)
        with self._lock:
            rows = self._db.execute(query + " ORDER BY t.rowid DESC LIMIT ?", (*params, max(0, min(limit, 100))))
            return [{**{k: row[k] for k in ("task_id", "session_id", "business_id", "request_id", "mode",
                    "message", "status", "created_at", "finished_at")}, "answer": json.loads(row["result"]).get("answer", "")}
                    for row in rows]

    def permissions(self, task_id):
        with self._lock:
            return {row[0] for row in self._db.execute(
                "SELECT permission FROM task_permissions WHERE task_id=?", (task_id,))}

    def history(self, identity, session_id, limit=6, *, allowed_objects=None, allowed_types=None,
                allowed_permissions=None, inherit_to=None):
        with self._transaction(identity, "session.history", session_id, write=bool(inherit_to)):
            self._session(identity, session_id, None)
            limit = max(0, min(limit, 12))
            if not limit:
                return []
            allowed = set(allowed_objects) if allowed_objects is not None else None
            rows = self._db.execute("""SELECT t.task_id,t.message,t.result,r.context FROM tasks t JOIN runs r USING(task_id)
                WHERE t.session_id=? AND t.status='completed' ORDER BY t.rowid DESC""", (session_id,))
            visible = []
            for row in rows:
                scope = json.loads(row["context"])
                if allowed is not None and not allowed.issuperset(scope.get("object_ids", ())):
                    continue
                if allowed_types is not None and scope.get("object_type") and scope["object_type"] not in allowed_types:
                    continue
                required = self.permissions(row["task_id"])
                if allowed_permissions is not None and not required.issubset(allowed_permissions):
                    continue
                if inherit_to:
                    self._db.executemany("INSERT OR IGNORE INTO task_permissions VALUES (?,?)",
                                         [(inherit_to, permission) for permission in required])
                visible.append(row)
                if len(visible) == limit:
                    break
            messages = []
            for row in reversed(visible):
                messages.extend([{"role": "user", "content": row["message"]},
                                 {"role": "assistant", "content": json.loads(row["result"]).get("answer", "")}])
            return messages

    def scope(self, task_id):
        """Server-side authorization metadata, including for completed tasks."""
        with self._lock:
            row = self._db.execute("SELECT context FROM runs WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise Fault("TASK_NOT_FOUND", "Task is unavailable.", 404)
            context = json.loads(row["context"])
            return {"object_type": context.get("object_type"), "object_ids": context.get("object_ids", [])}

    def context_id(self, task_id):
        with self._lock:
            row = self._db.execute("SELECT context_id FROM runs WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise Fault("TASK_NOT_FOUND", "Task is unavailable.", 404)
            return row["context_id"]

    def run(self, run_context_id):
        with self._lock:
            row = self._db.execute("""SELECT r.*,t.session_id,t.status,s.business_id,s.tenant_id,s.org_id,s.user_id
                FROM runs r JOIN tasks t USING(task_id) JOIN sessions s USING(session_id) WHERE r.context_id=?""",
                                   (run_context_id,)).fetchone()
            if not row or row["revoked_at"] or row["expires_at"] <= time.time() or row["status"] != "running":
                raise Fault("PERMISSION_DENIED", "Run context is unavailable.", 403)
            context = json.loads(row["context"])
            result = {k: row[k] for k in ("task_id", "session_id", "business_id", *OWNER_FIELDS, "expires_at")}
            return {**result, "context": context, "object_ids": context.get("object_ids", []),
                    "object_type": context.get("object_type")}

    def _event(self, task_id, event, data):
        self._db.execute("""INSERT INTO events(task_id,event_id,event,payload,timestamp)
            SELECT ?, COALESCE(MAX(event_id),0)+1, ?, ?, ? FROM events WHERE task_id=?""",
            (task_id, event, _json({**data, "task_id": task_id}), _now(), task_id))

    def emit(self, task_id, event, data, *, lease=None):
        if event in TERMINAL_EVENTS.values() or event == "task_started":
            raise Fault("TASK_STATE_CONFLICT", "Lifecycle events are controlled by task transitions.", 409)
        with self._transaction():
            step = self._guard_lease(task_id, lease)
            if step:
                data = {**data, "step_id": step["step_id"]}
            if self._task_row(task_id)["status"] == "running":
                if event == "authorization" and data["allowed"]:
                    self._db.execute("INSERT OR IGNORE INTO task_permissions VALUES (?,?)",
                                     (task_id, data["permission"]))
                self._event(task_id, event, data)

    def events(self, task_id, after=0):
        with self._lock:
            rows = self._db.execute("""SELECT * FROM events WHERE task_id=? AND event_id>? ORDER BY event_id""",
                                    (task_id, after))
            return [{"id": row["event_id"], "event": row["event"], "data": json.loads(row["payload"]),
                     "timestamp": row["timestamp"]} for row in rows]

    def _finish(self, task_id, status, result, error):
        if status not in TERMINAL_EVENTS:
            raise Fault("TASK_STATE_CONFLICT", "Invalid terminal state.", 409)
        if self._task_row(task_id)["status"] in TERMINAL_EVENTS:
            return False
        if self.is_durable(task_id):
            self._interrupt_plan(task_id, status)
        finished = _now()
        self._db.execute("UPDATE tasks SET status=?,result=?,error=?,finished_at=? WHERE task_id=?",
                         (status, _json(result), _json(error), finished, task_id))
        self._db.execute("UPDATE runs SET revoked_at=? WHERE task_id=?", (finished, task_id))
        self._db.execute("""UPDATE calls SET status='unknown_outcome',duration_ms=MAX(0,CAST((?-started)*1000 AS INT))
            WHERE task_id=? AND status='running'""", (time.time(), task_id))
        # Completed contexts cannot replay tools. Keep only the public answer and call metadata.
        self._db.execute("UPDATE calls SET result=NULL WHERE task_id=? AND result IS NOT NULL", (task_id,))
        data = {"status": status}
        if status == "failed":
            data["error"] = error
        elif status in ("completed", "partial"):
            data["result"] = result
        self._event(task_id, TERMINAL_EVENTS[status], data)
        self._audit("task.finish", status, None, task_id, None, (error or {}).get("code"))
        return True

    def finish(self, task_id, status, result, error=None):
        with self._transaction():
            if self.is_durable(task_id):
                raise Fault("TASK_STATE_CONFLICT", "Plan tasks finish through step transitions.", 409)
            return self._finish(task_id, status, result, error)

    def cancel(self, identity, task_id):
        with self._transaction(identity, "task.cancel", task_id):
            self._task_row(task_id, identity)
            self._finish(task_id, "cancelled", {}, None)
            return self._task(task_id)

    def begin_call(self, task_id, call_id, cap_id, version, arguments, max_calls=8, *, lease=None):
        with self._transaction():
            step = self._guard_lease(task_id, lease, call_id, cap_id)
            if self._task_row(task_id)["status"] != "running":
                raise Fault("TASK_STATE_CONFLICT", "Task is no longer running.", 409)
            self.run(self.context_id(task_id))
            digest = _digest(arguments)
            row = self._db.execute("SELECT * FROM calls WHERE task_id=? AND call_id=?", (task_id, call_id)).fetchone()
            if step and step["token"] > MAX_ATTEMPTS and (not row or row["status"] != "completed"):
                raise Fault("STEP_ATTEMPTS_EXHAUSTED", "Recovery lease only permits a cached result.", 409)
            if row:
                if (row["capability_id"], row["capability_version"], row["arguments_digest"]) != (cap_id, version, digest):
                    raise Fault("IDEMPOTENCY_CONFLICT", "Call ID already used for a different operation.", 409)
                if row["status"] == "completed":
                    return row["execution_id"], json.loads(row["result"])
                if row["status"] == "failed":
                    error = json.loads(row["error"])
                    raise Fault(error["code"], error["message"], error.get("status", 424), error.get("retryable", False))
                if step and step["call_token"] != step["token"] and row["status"] == "running":
                    self._mark_call_started(step)
                    return row["execution_id"], None
                raise Fault("TASK_STATE_CONFLICT", "Call already in progress.", 409)
            count = self._db.execute("SELECT COUNT(*) FROM calls WHERE task_id=?", (task_id,)).fetchone()[0]
            if count >= (step["call_limit"] if step else max_calls):
                raise Fault("RATE_LIMITED", "Task tool-call budget exhausted.", 429)
            execution = _id("exec")
            self._db.execute("""INSERT INTO calls(task_id,call_id,execution_id,capability_id,capability_version,
                arguments_digest,status,started) VALUES(?,?,?,?,?,?,'running',?)""",
                (task_id, call_id, execution, cap_id, version, digest, time.time()))
            detail = {"capability_id": cap_id, "execution_id": execution}
            if step:
                self._mark_call_started(step)
                detail.update(step_id=step["step_id"], attempt_no=step["token"])
            self._event(task_id, "step_started", detail)
            return execution, None

    def _mark_call_started(self, step):
        self._db.execute("""UPDATE plan_steps SET call_token=token,status='running' WHERE task_id=? AND step_id=?""",
                         (step["task_id"], step["step_id"]))

    def complete_call(self, task_id, call_id, result, error=None, *, lease=None):
        with self._transaction():
            step = self._guard_lease(task_id, lease, call_id)
            if step and step["call_token"] != step["token"]:
                raise Fault("LEASE_LOST", "This attempt has not started the tool.", 409)
            row = self._db.execute("SELECT * FROM calls WHERE task_id=? AND call_id=?", (task_id, call_id)).fetchone()
            if not row:
                raise Fault("TASK_STATE_CONFLICT", "Call has not started.", 409)
            if row["status"] != "running":
                return
            duration = max(0, round((time.time() - row["started"]) * 1000))
            status = "failed" if error is not None else "completed"
            self._db.execute("""UPDATE calls SET status=?,duration_ms=?,result=?,error=?
                WHERE task_id=? AND call_id=?""", (status, duration, _json(result) if error is None else None,
                                                   _json(error), task_id, call_id))
            detail = {"capability_id": row["capability_id"], "execution_id": row["execution_id"],
                      "status": status, "duration_ms": duration}
            if error is not None:
                detail["error"] = error
            if step:
                detail.update(step_id=step["step_id"], attempt_no=step["token"])
            self._event(task_id, ("tool_call_" if step else "step_") + status, detail)
