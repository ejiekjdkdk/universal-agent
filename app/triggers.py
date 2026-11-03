"""可信本地事件与定时器在同一事务中登记事件并创建既有持久任务。"""
import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import time

from .errors import Fault
from .plans import prepare_plan
from .skills import SkillError

LOG = logging.getLogger(__name__)
ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
ENV = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
SCHEMA = """
CREATE TABLE IF NOT EXISTS trigger_events (
 source_id TEXT NOT NULL, event_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
 task_id TEXT UNIQUE NOT NULL REFERENCES tasks(task_id), received_at REAL NOT NULL,
 PRIMARY KEY(source_id,event_id));
CREATE TABLE IF NOT EXISTS trigger_nonces (
 source_id TEXT NOT NULL, nonce TEXT NOT NULL, event_id TEXT NOT NULL,
 signature_hash TEXT NOT NULL, PRIMARY KEY(source_id,nonce));
CREATE TABLE IF NOT EXISTS trigger_schedules (
 schedule_id TEXT PRIMARY KEY, definition_hash TEXT NOT NULL, next_due INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS trigger_received ON trigger_events(source_id,received_at);
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _identifier(value):
    return isinstance(value, str) and ID.fullmatch(value)


def _number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        # JSON 整数可超出浮点表示范围；仍作为无效输入交给入口记录拒绝审计。
        return False


def _context(value):
    if not isinstance(value, dict) or set(value) - {"object_type", "object_ids"}:
        return False
    object_type = value.get("object_type")
    if object_type is not None and not _identifier(object_type):
        return False
    object_ids = value.get("object_ids", [])
    if not isinstance(object_ids, list) or len(object_ids) > 64:
        return False
    for object_id in object_ids:
        if not _identifier(object_id):
            return False
    return True


def sign(secret, source, timestamp, nonce, raw):
    """版本化签名覆盖来源、时间、随机数和未经改写的请求字节。"""
    data = f"v1\n{source}\n{timestamp}\n{nonce}\n".encode() + raw
    return hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()


class TriggerGateway:
    def __init__(self, runtime, snapshots, config_path=None):
        self.runtime = runtime
        self.store = runtime.store
        self.snapshots = snapshots
        configured_path = config_path or os.getenv("AGENT_TRIGGERS_FILE")
        self.path = None
        if configured_path:
            self.path = Path(configured_path)
        with self.store._lock:
            self.store._db.executescript(SCHEMA)
        if self.path:
            self._config()

    def _config(self):
        if self.path is None:
            raise Fault("TRIGGERS_DISABLED", "Trusted triggers are not configured.", 404)
        try:
            raw = self.path.read_bytes()
            if len(raw) > 262144:
                raise ValueError("configuration too large")
            config = json.loads(raw, object_pairs_hook=_pairs)
            if not isinstance(config, dict) or set(config) != {"sources", "schedules"}:
                raise ValueError("configuration fields")
            if not isinstance(config["sources"], list) or not isinstance(config["schedules"], list):
                raise ValueError("configuration lists")
            if len(config["sources"]) > 64 or len(config["schedules"]) > 128:
                raise ValueError("configuration limits")
            sources = {}
            for source in config["sources"]:
                required = {"source_id", "token_env", "signing_key_env", "businesses"}
                if not isinstance(source, dict) or not required <= source.keys() or set(source) - required - {
                        "enabled", "max_active", "max_per_minute"}:
                    raise ValueError("source fields")
                if not _identifier(source["source_id"]) or source["source_id"] in sources:
                    raise ValueError("source ID")
                for key in ("token_env", "signing_key_env"):
                    if not isinstance(source[key], str) or not ENV.fullmatch(source[key]):
                        raise ValueError("environment variable")
                if type(source.get("enabled", True)) is not bool:
                    raise ValueError("enabled")
                for key, default in (("max_active", 4), ("max_per_minute", 30)):
                    if type(source.get(key, default)) is not int or not 1 <= source.get(key, default) <= 100:
                        raise ValueError("limit")
                if not isinstance(source["businesses"], dict) or not source["businesses"]:
                    raise ValueError("business filter")
                for business, scope in source["businesses"].items():
                    if not _identifier(business) or not isinstance(scope, dict) or set(scope) != {"object_types", "object_ids"}:
                        raise ValueError("business scope")
                    for items in scope.values():
                        if not isinstance(items, list) or len(items) > 64 or not all(_identifier(x) for x in items):
                            raise ValueError("object filter")
                sources[source["source_id"]] = source
            schedules = {}
            for schedule in config["schedules"]:
                if not isinstance(schedule, dict) or set(schedule) != {
                        "schedule_id", "source_id", "business_id", "message", "context", "interval_seconds", "start_at"}:
                    raise ValueError("schedule fields")
                if not _identifier(schedule["schedule_id"]) or len(schedule["schedule_id"]) > 80 or schedule["schedule_id"] in schedules:
                    raise ValueError("schedule ID")
                if schedule["source_id"] not in sources or not _identifier(schedule["business_id"]):
                    raise ValueError("schedule source")
                if type(schedule["interval_seconds"]) is not int or not 1 <= schedule["interval_seconds"] <= 86400:
                    raise ValueError("interval")
                if type(schedule["start_at"]) is not int or not 0 <= schedule["start_at"] <= 9999999999:
                    raise ValueError("schedule start")
                if not isinstance(schedule["message"], str) or not 1 <= len(schedule["message"].strip()) <= 16000 or not _context(schedule["context"]):
                    raise ValueError("schedule request")
                schedules[schedule["schedule_id"]] = schedule
            return sources, schedules
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise Fault("TRIGGER_CONFIG_UNAVAILABLE", "Trusted trigger configuration is unavailable.", 503) from None

    def _source(self, sources, source_id):
        source = sources.get(source_id)
        if not source or not source.get("enabled", True):
            raise Fault("TRIGGER_SOURCE_DENIED", "Trigger source is unavailable.", 403)
        return source

    def _actor(self, source):
        token = os.getenv(source["token_env"])
        actor = self.runtime.principals.get(token) if token else None
        if not actor:
            raise Fault("TRIGGER_ACTOR_UNAVAILABLE", "Trusted trigger actor is unavailable.", 403)
        return actor

    def _filter(self, source, event):
        scope = source["businesses"].get(event["business_id"])
        context = event.get("context", {})
        if (scope is None or not set(context.get("object_ids", [])).issubset(scope["object_ids"])
                or (context.get("object_type") and context["object_type"] not in scope["object_types"])):
            raise Fault("TRIGGER_SCOPE_DENIED", "Trigger business or objects are outside source scope.", 403)

    def _event(self, raw, now):
        try:
            if len(raw) > 32768:
                raise ValueError("event size")
            event = json.loads(raw, object_pairs_hook=_pairs)
            if not isinstance(event, dict) or set(event) != {
                    "event_id", "business_id", "message", "context", "issued_at", "expires_at", "execution_mode"}:
                raise ValueError("event fields")
            if not _identifier(event["event_id"]) or not _identifier(event["business_id"]):
                raise ValueError("event ID")
            if not isinstance(event["message"], str) or not 1 <= len(event["message"].strip()) <= 16000 or not _context(event["context"]):
                raise ValueError("task content")
            if event["execution_mode"] != "durable_plan":
                raise ValueError("durable execution required")
            if not _number(event["issued_at"]) or not _number(event["expires_at"]):
                raise ValueError("event times")
            if not 0 < event["expires_at"] - event["issued_at"] <= 300 or event["issued_at"] > now + 30:
                raise ValueError("event lifetime")
        except (ValueError, TypeError, KeyError, RecursionError):
            raise Fault("TRIGGER_INVALID", "Trigger event is invalid.", 422) from None
        if event["expires_at"] <= now:
            raise Fault("TRIGGER_EXPIRED", "Trigger event has expired.", 410)
        return event

    def ingest(self, raw, headers, *, now=None):
        now = time.time() if now is None else now
        try:
            headers = {k.lower(): v for k, v in headers.items()}
            source_id = headers.get("x-trigger-source", "")
            source = self._source(self._config()[0], source_id)
            timestamp = headers.get("x-trigger-timestamp", "")
            nonce = headers.get("x-trigger-nonce", "")
            secret = os.getenv(source["signing_key_env"], "")
            signature = headers.get("x-trigger-signature", "")
            if (len(raw) > 32768 or not re.fullmatch(r"[0-9]{1,12}", timestamp) or not _identifier(nonce)
                    or abs(now - int(timestamp)) > 300 or len(secret) < 32
                    or not re.fullmatch(r"[a-f0-9]{64}", signature)
                    or not hmac.compare_digest(sign(secret, source_id, timestamp, nonce, raw), signature)):
                raise Fault("TRIGGER_UNAUTHORIZED", "Trigger signature or freshness is invalid.", 401)
            event = self._event(raw, now)
            with self.store._transaction():
                result = self._accept(source_id, event, _hash(raw), nonce, _hash(signature.encode()), now)
            return result
        except Fault as exc:
            self.store.audit("trigger.ingress", "deny", code=exc.code)
            raise

    def _accept(self, source_id, event, digest, nonce, signature_hash, now):
        # 调用者持有写事务；任务、随机数和事件回执一起提交或回滚。
        source = self._source(self._config()[0], source_id)
        actor = self._actor(source)
        self._filter(source, event)
        business = event["business_id"]
        context = event["context"]
        self.runtime.authorize(actor, business, context)
        db = self.store._db
        seen = db.execute("SELECT * FROM trigger_nonces WHERE source_id=? AND nonce=?", (source_id, nonce)).fetchone()
        if seen and (seen["event_id"], seen["signature_hash"]) != (event["event_id"], signature_hash):
            raise Fault("TRIGGER_REPLAY", "Trigger nonce was already used.", 409)
        previous = db.execute("SELECT * FROM trigger_events WHERE source_id=? AND event_id=?", (source_id, event["event_id"])).fetchone()
        if previous:
            if previous["payload_hash"] != digest:
                raise Fault("TRIGGER_EVENT_CONFLICT", "Event ID has different request bytes.", 409)
            self.guard_task(previous["task_id"])
            # 重投也消耗本次随机数，防止它随后被用于不同事件。
            db.execute("INSERT OR IGNORE INTO trigger_nonces VALUES(?,?,?,?)",
                       (source_id, nonce, event["event_id"], signature_hash))
            return {"source_id": source_id, "event_id": event["event_id"], "task_id": previous["task_id"], "created": False}
        active = db.execute("""SELECT COUNT(*) FROM trigger_events e JOIN tasks t USING(task_id)
            WHERE source_id=? AND t.finished_at IS NULL""", (source_id,)).fetchone()[0]
        recent = db.execute("SELECT COUNT(*) FROM trigger_events WHERE source_id=? AND received_at>?", (source_id, now - 60)).fetchone()[0]
        if active >= source.get("max_active", 4) or recent >= source.get("max_per_minute", 30):
            raise Fault("TRIGGER_RATE_LIMITED", "Trigger admission limit reached.", 429)
        selected = self.runtime.releases.select(business, actor, "") if self.runtime.releases else None
        snapshot = selected or self.snapshots.current.get(business)
        if not snapshot:
            raise Fault("PLAN_NOT_FOUND", "Trigger requires a registered durable plan.", 404)
        catalog = self.snapshots.open(snapshot, business)
        skill = self.runtime.select_skill(actor, business, None, catalog, snapshot)
        plan, budget = prepare_plan(catalog, business, event["message"], self.runtime.grant(actor, business)["permissions"], context)

        def check_skill(current_actor):
            if skill is None:
                return
            try:
                current = self.runtime.select_skill(current_actor, business, skill["name"], catalog, snapshot)
                loaded = self.runtime.skills.load(business, skill["name"], skill["version"])
                if current != skill or not {step["capability_id"] for step in plan["steps"]} <= set(loaded["tools"]):
                    raise SkillError("计划超出技能范围")
                self.runtime.validate_skill_input(business, skill, event["message"], context)
                if len(plan["steps"]) > loaded["max_tool_calls"]:
                    raise Fault("BUDGET_EXCEEDED", "触发计划超过已发布技能的调用预算", 422)
                budget["max_tool_calls"] = min(budget["max_tool_calls"], loaded["max_tool_calls"])
            except SkillError:
                raise Fault("SKILL_UNAVAILABLE", "Trigger plan exceeds the selected skill.", 403) from None

        check_skill(actor)

        def admit():
            fresh = self._source(self._config()[0], source_id)
            current_actor = self._actor(fresh)
            if any(current_actor[k] != actor[k] for k in ("tenant_id", "org_id", "user_id")):
                raise Fault("TRIGGER_ACTOR_UNAVAILABLE", "Trigger actor changed during admission.", 403)
            self._filter(fresh, event)
            self.runtime.authorize(current_actor, business, context)
            if self.runtime.releases:
                self.runtime.releases.guard_selection(selected, business, current_actor, "")
                for step in plan["steps"]:
                    self.runtime.releases.guard(snapshot, business, step["capability_id"])
            current_permissions = self.runtime.grant(current_actor, business).get("permissions", [])
            prepare_plan(catalog, business, event["message"], current_permissions, context)
            check_skill(current_actor)

        body = {"business_id": business, "context": context, "message": event["message"],
                "mode": "offline", "execution_mode": "durable_plan"}
        task, _ = self.store.start(actor, body, None, catalog.scenarios[business]["version"],
                                   plan=plan, budget=budget, snapshot_id=snapshot, admit=admit, skill=skill)
        db.execute("INSERT INTO trigger_events VALUES(?,?,?,?,?)", (source_id, event["event_id"], digest, task["task_id"], now))
        db.execute("INSERT INTO trigger_nonces VALUES(?,?,?,?)", (source_id, nonce, event["event_id"], signature_hash))
        self.store._event(task["task_id"], "trigger_accepted", {"source_id": source_id, "event_id": event["event_id"]})
        self.store._audit("trigger.accept", "allow", actor, task["task_id"], source_id, None)
        return {"source_id": source_id, "event_id": event["event_id"], "task_id": task["task_id"], "created": True}

    def guard_task(self, task_id):
        with self.store._lock:
            receipt = self.store._db.execute("SELECT source_id FROM trigger_events WHERE task_id=?", (task_id,)).fetchone()
            if not receipt:
                return
            source = self._source(self._config()[0], receipt[0])
            actor = self._actor(source)
            task = self.store._task_row(task_id, actor)
            self._filter(source, {"business_id": task["business_id"], "context": self.store.scope(task_id)})

    def tick(self, *, now=None):
        now = time.time() if now is None else now
        schedules = self._config()[1]
        results = []
        for schedule_id, schedule in schedules.items():
            try:
                with self.store._transaction():
                    # 取得写锁后重读配置，已经删除的定时器不能继续创建任务。
                    if self._config()[1].get(schedule_id) != schedule:
                        continue
                    definition_hash = _hash(_json(schedule).encode())
                    db = self.store._db
                    db.execute("INSERT OR IGNORE INTO trigger_schedules VALUES(?,?,?)", (schedule_id, definition_hash, schedule["start_at"]))
                    row = db.execute("SELECT * FROM trigger_schedules WHERE schedule_id=?", (schedule_id,)).fetchone()
                    if row["definition_hash"] != definition_hash:
                        raise Fault("TRIGGER_SCHEDULE_CONFLICT", "Changed schedules require a new schedule ID.", 409)
                    if row["next_due"] > now:
                        continue
                    interval = schedule["interval_seconds"]
                    # 错过的周期合并到最近一个到期时点，重启不会补发整个积压队列。
                    due_at = row["next_due"] + int((now - row["next_due"]) // interval) * interval
                    event = {k: schedule[k] for k in ("business_id", "message", "context")}
                    event.update(event_id=f"schedule.{schedule_id}.{due_at}", issued_at=due_at, expires_at=due_at + 300,
                                 execution_mode="durable_plan")
                    if event["expires_at"] <= now:
                        db.execute("UPDATE trigger_schedules SET next_due=? WHERE schedule_id=?", (due_at + interval, schedule_id))
                        self.store._audit("trigger.schedule.expired", "skip", None, None, schedule_id, "TRIGGER_EXPIRED")
                        continue
                    event_hash = _hash(_json(event).encode())
                    result = self._accept(schedule["source_id"], event, event_hash, event["event_id"], event_hash, now)
                    db.execute("UPDATE trigger_schedules SET next_due=? WHERE schedule_id=?", (due_at + interval, schedule_id))
                    results.append(result)
            except Fault as exc:
                self.store.audit("trigger.schedule", "deny", resource=schedule_id, code=exc.code)
        return results

    async def run(self):
        if self.path is None:
            return
        while True:
            try:
                self.tick()
            except Fault as exc:
                LOG.warning("Trusted trigger scheduler unavailable (%s)", exc.code)
            await asyncio.sleep(1)
