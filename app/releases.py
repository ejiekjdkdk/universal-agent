"""发布可信场景包，按稳定分组灰度，并在运行期间执行紧急禁用。"""
import asyncio
from contextlib import contextmanager
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import time

from .errors import Fault


SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    release_id TEXT PRIMARY KEY, scene TEXT NOT NULL, version TEXT NOT NULL,
    state TEXT NOT NULL, report TEXT, UNIQUE(scene,version)
);
CREATE TABLE IF NOT EXISTS release_bindings (
    scene TEXT PRIMARY KEY, stable TEXT NOT NULL, canary TEXT, percent INTEGER NOT NULL,
    previous TEXT, generation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS release_revocations (
    scene TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
    PRIMARY KEY(scene,kind,target)
);
CREATE TABLE IF NOT EXISTS release_predecessors (
    release_id TEXT PRIMARY KEY, predecessor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS release_evaluations (
    sequence INTEGER PRIMARY KEY, scene TEXT NOT NULL, release_id TEXT NOT NULL,
    operation TEXT NOT NULL, created_at REAL NOT NULL, report TEXT NOT NULL
);
"""


def _conflict(message):
    return Fault("RELEASE_VERSION_CONFLICT", message, 409)


def _version(text):
    return tuple(int(part) for part in text.split("."))


def _breaking_schema(old, new, output=False):
    """保守判断契约是否发生破坏性变化，忽略说明文字等注解。"""
    # 为兼容旧调用方，输入可接受更多值，输出可缩小原有取值范围。
    subset, superset = (new, old) if output else (old, new)
    lower = {"minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties"}
    upper = {"maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties"}
    if any(subset.get(k, float("-inf")) < superset.get(k, float("-inf")) for k in lower):
        return True
    if any(subset.get(k, float("inf")) > superset.get(k, float("inf")) for k in upper):
        return True
    if "enum" in superset:
        if "enum" not in subset:
            return True
        accepted = {json.dumps(x, sort_keys=True) for x in superset["enum"]}
        if any(json.dumps(x, sort_keys=True) not in accepted for x in subset["enum"]):
            return True
    special = lower | upper | {"title", "description", "examples", "$schema", "enum", "properties", "required", "items"}
    if {k: v for k, v in old.items() if k not in special} != {
        k: v for k, v in new.items() if k not in special
    }:
        return True
    if old.get("type") == "array":
        return _breaking_schema(old["items"], new["items"], output)
    if old.get("type") != "object":
        return False
    before, after = old.get("properties", {}), new.get("properties", {})
    required_before, required_after = set(old.get("required", [])), set(new.get("required", []))
    if before.keys() - after.keys():
        return True
    if output:
        # 旧调用方可能使用封闭的输出 Schema，也可能依赖原先保证返回的字段。
        if after.keys() - before.keys() or required_before - required_after:
            return True
    elif required_after - required_before:
        return True
    return any(_breaking_schema(schema, after[key], output) for key, schema in before.items())


def _compatibility(old, new):
    reasons = []
    tools = {tool["name"]: tool for tool in new["tools"]}
    for prior in old["tools"]:
        name = prior["name"]
        current = tools.get(name)
        if current is None:
            reasons.append(name + ".removed")
            continue
        for key in ("permission", "action_level", "object_ids_field", "adapter"):
            if current.get(key) != prior.get(key):
                reasons.append(name + "." + key)
        for key in ("input_schema", "output_schema"):
            if _breaking_schema(prior[key], current[key], key == "output_schema"):
                reasons.append(name + "." + key)
    return reasons


class Releases:
    def __init__(self, store, snapshots, *, candidates=None, evaluate=None):
        self.store = store
        self.snapshots = snapshots
        self.db = store._db
        location = candidates or os.getenv("AGENT_RELEASE_CANDIDATES")
        self.candidates = Path(location).resolve() if location else None
        self.evaluate = evaluate
        with store._lock:
            self.db.executescript(SCHEMA)

    def _baseline(self, scene):
        digest = self.snapshots.baseline(scene)
        if not digest:
            raise Fault("RELEASE_NOT_FOUND", "未找到可管理的场景", 404)
        return digest

    def _binding(self, scene):
        return self.db.execute("SELECT * FROM release_bindings WHERE scene=?", (scene,)).fetchone()

    def _row(self, rid):
        row = self.db.execute("SELECT * FROM releases WHERE release_id=?", (rid,)).fetchone()
        if row is None:
            raise Fault("RELEASE_NOT_FOUND", "未找到发布版本", 404)
        return row

    @contextmanager
    def _admin(self, action, actor, resource):
        try:
            yield
        except Fault as exc:
            self.store.audit("release." + action, "deny", actor, resource=resource, code=exc.code)
            raise

    def _audit(self, action, actor, resource):
        self.store._audit("release." + action, "allow", actor, None, resource, None)

    def guard(self, snapshot_id, scene_id, capability=None):
        digest = snapshot_id or self.snapshots.baseline(scene_id)
        with self.store._lock:
            denied = self.db.execute("""
                SELECT 1 FROM release_revocations WHERE scene=? AND
                ((kind='snapshot' AND target=?) OR (kind='capability' AND target=?))
            """, (scene_id, digest, capability)).fetchone()
        if denied:
            raise Fault("CAPABILITY_REVOKED", "发布版本或工具已紧急停用", 403)

    def select(self, scene_id, actor, cohort_key):
        with self.store._lock:
            row = self._binding(scene_id)
            result = None
            if row:
                result = row["stable"]
                if row["canary"]:
                    identity = [actor.get(k) for k in ("tenant_id", "org_id", "user_id")]
                    key = json.dumps([*identity, scene_id, cohort_key], ensure_ascii=True).encode()
                    bucket = int.from_bytes(hashlib.sha256(key).digest()[:8], "big") % 100
                    if bucket < row["percent"]:
                        result = row["canary"]
            self.guard(result, scene_id)
            return result

    def guard_selection(self, snapshot_id, scene_id, actor, cohort_key):
        if self.select(scene_id, actor, cohort_key) != snapshot_id:
            raise _conflict("发布绑定已变更，请重新提交")

    def current_tool(self, scene_id, actor, cohort_key, name):
        digest = self.select(scene_id, actor, cohort_key)
        self.guard(digest, scene_id, name)
        catalog = self.snapshots.open(digest, scene_id) if digest else self.snapshots.live
        scene = catalog.scenarios.get(scene_id)
        if scene:
            for tool in scene["tools"]:
                if tool["name"] == name:
                    return {k: v for k, v in tool.items() if k != "adapter"} | {"adapter_type": tool["adapter"]["type"]}
        return None

    def _capture(self, scene, candidate):
        self._baseline(scene)
        if not self.candidates:
            raise Fault("RELEASE_CANDIDATES_UNCONFIGURED", "未配置可信候选目录", 503)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", candidate):
            raise Fault("INVALID_REQUEST", "候选标识不合法")
        folder = self.candidates / candidate / scene
        if folder.resolve() != folder or not folder.is_dir():
            raise Fault("RELEASE_NOT_FOUND", "未找到可信候选目录", 404)
        try:
            return self.snapshots.capture(folder, scene)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise Fault("RELEASE_INVALID", "候选场景校验失败", 422) from exc

    async def _gate(self, operation, digest, scene, catalog):
        self._eligible(digest, scene, catalog)
        if self.evaluate is None:
            raise Fault("EVALUATION_GATE_FAILED", "未配置发布评测器", 422)
        try:
            if inspect.iscoroutinefunction(self.evaluate):
                report = await self.evaluate(catalog, scene)
            else:
                report = await asyncio.to_thread(self.evaluate, catalog, scene)
                if inspect.isawaitable(report):
                    report = await report
            if not isinstance(report, dict):
                raise ValueError("missing report")
            encoded = json.dumps(report, ensure_ascii=False, allow_nan=False)
        except Exception as exc:
            raise Fault("EVALUATION_GATE_FAILED", "发布评测未完成", 422) from exc
        with self.store._transaction():
            self.db.execute("INSERT INTO release_evaluations(scene,release_id,operation,created_at,report) VALUES(?,?,?,?,?)",
                            (scene, digest, operation, time.time(), encoded))
        if report.get("passed") is not True:
            raise Fault("EVALUATION_GATE_FAILED", "发布评测未通过", 422)
        return report

    def _eligible(self, digest, scene, catalog):
        self.guard(digest, scene)
        for tool in catalog.scenarios[scene]["tools"]:
            self.guard(digest, scene, tool["name"])

    def _record(self, digest, scene, version, state, report):
        occupied = self.db.execute("SELECT release_id FROM releases WHERE scene=? AND version=?", (scene, version)).fetchone()
        if occupied and occupied[0] != digest:
            raise _conflict("相同场景版本已绑定不同内容")
        encoded = json.dumps(report, ensure_ascii=False) if report is not None else None
        self.db.execute("""INSERT INTO releases VALUES(?,?,?,?,?)
            ON CONFLICT(release_id) DO UPDATE SET
            state=CASE WHEN releases.state='validated' OR
              (releases.state='deprecated' AND excluded.state='staged') THEN excluded.state ELSE releases.state END,
            report=COALESCE(excluded.report,releases.report)""", (digest, scene, version, state, encoded))

    def _check_upgrade(self, scene, stable, digest, current):
        old = self.snapshots.open(stable, scene).scenarios[scene]
        if stable == digest:
            return []
        before, after = _version(old["version"]), _version(current["version"])
        if after <= before:
            raise _conflict("发布版本必须递增")
        reasons = _compatibility(old, current)
        if reasons and after[0] <= before[0]:
            raise _conflict("破坏性变更需要新主版本：" + ", ".join(reasons))
        return reasons

    async def _prepare(self, operation, scene, candidate, actor):
        with self._admin(operation, actor, scene):
            digest, catalog = self._capture(scene, candidate)
            report = await self._gate(operation, digest, scene, catalog)
            current = self.snapshots.open(digest, scene).scenarios[scene]
            with self.store._transaction():
                binding = self._binding(scene)
                stable = binding["stable"] if binding else self._baseline(scene)
                reasons = self._check_upgrade(scene, stable, digest, current)
                self._eligible(digest, scene, catalog)
                if operation == "stage" and binding is None:
                    incumbent = self.snapshots.open(stable, scene).scenarios[scene]
                    self._record(stable, scene, incumbent["version"], "active", None)
                    self.db.execute("INSERT INTO release_bindings(scene,stable,percent) VALUES(?,?,100)", (scene, stable))
                self._record(digest, scene, current["version"], "staged" if operation == "stage" else "validated", report)
                self._audit(operation, actor, digest)
                return self.get(digest) | {"breaking_changes": reasons}

    async def validate(self, scene, candidate, *, actor):
        return await self._prepare("validate", scene, candidate, actor)

    async def stage(self, scene, candidate, *, actor):
        return await self._prepare("stage", scene, candidate, actor)

    def get(self, rid):
        with self.store._lock:
            row = self._row(rid)
            binding = self._binding(row["scene"])
            return {"release_id": rid, "business_id": row["scene"], "version": row["version"],
                    "status": row["state"], "evaluation": json.loads(row["report"]) if row["report"] else None,
                    "binding": dict(binding) if binding else None}

    async def activate(self, rid, canary_percent=100, *, actor):
        with self._admin("activate", actor, rid):
            if type(canary_percent) is not int or not 0 <= canary_percent <= 100:
                raise Fault("INVALID_REQUEST", "灰度比例必须在 0 到 100 之间")
            with self.store._lock:
                scene = self._row(rid)["scene"]
            catalog = self.snapshots.inspect(rid, scene)
            await self._gate("activate", rid, scene, catalog)
            catalog = self.snapshots.open(rid, scene)
            with self.store._transaction():
                self._eligible(rid, scene, catalog)
                binding = self._binding(scene)
                if not binding or self._row(rid)["state"] not in {"staged", "active"}:
                    raise _conflict("仅已暂存或生效的版本可激活")
                stable, canary = binding["stable"], binding["canary"]
                if canary not in (None, rid) or (stable == rid and canary_percent != 100):
                    raise _conflict("场景当前有不同的发布绑定")
                self._check_upgrade(scene, stable, rid, catalog.scenarios[scene])
                if stable != rid:
                    if canary_percent == 100:
                        self.db.execute("""INSERT INTO release_predecessors VALUES(?,?)
                            ON CONFLICT(release_id) DO UPDATE SET predecessor=excluded.predecessor""", (rid, stable))
                        self.db.execute("UPDATE release_bindings SET previous=stable,stable=?,canary=NULL,percent=100,generation=generation+1 WHERE scene=?",
                                        (rid, scene))
                        self.db.execute("UPDATE releases SET state='deprecated' WHERE release_id=? AND state!='revoked'", (stable,))
                    else:
                        self.db.execute("UPDATE release_bindings SET canary=?,percent=?,generation=generation+1 WHERE scene=?",
                                        (rid, canary_percent, scene))
                    self.db.execute("UPDATE releases SET state='active' WHERE release_id=?", (rid,))
                self._audit("activate", actor, rid)
                return self.get(rid)

    async def rollback(self, rid, *, actor):
        with self._admin("rollback", actor, rid):
            with self.store._lock:
                scene = self._row(rid)["scene"]
                binding = self._binding(scene)
                if not binding or (binding["canary"] or binding["stable"]) != rid:
                    raise _conflict("只能回滚当前发布")
                target = binding["stable"] if binding["canary"] else binding["previous"]
                if not target:
                    raise _conflict("没有前一版可回滚")
            catalog = self.snapshots.inspect(target, scene)
            await self._gate("rollback", target, scene, catalog)
            catalog = self.snapshots.open(target, scene)
            with self.store._transaction():
                if self._binding(scene)["generation"] != binding["generation"]:
                    raise _conflict("绑定已变更，请重新校验回滚目标")
                self._eligible(target, scene, catalog)
                previous = self.db.execute("SELECT predecessor FROM release_predecessors WHERE release_id=?",
                                           (target,)).fetchone()
                self.db.execute("UPDATE release_bindings SET stable=?,canary=NULL,percent=100,previous=?,generation=generation+1 WHERE scene=?",
                                (target, previous[0] if previous else None, scene))
                self.db.execute("UPDATE releases SET state='active' WHERE release_id=?", (target,))
                self.db.execute("UPDATE releases SET state='deprecated' WHERE release_id=? AND state!='revoked'", (rid,))
                self._audit("rollback", actor, rid)
                return self.get(target)

    def revoke(self, rid, *, actor):
        with self._admin("revoke", actor, rid):
            with self.store._transaction():
                scene = next((s for s, d in self.snapshots.baselines.items() if d == rid), None)
                if scene:
                    version = self.snapshots.open(rid, scene).scenarios[scene]["version"]
                    self._record(rid, scene, version, "revoked", None)
                else:
                    scene = self._row(rid)["scene"]
                self.db.execute("INSERT OR IGNORE INTO release_revocations VALUES(?,'snapshot',?)", (scene, rid))
                self.db.execute("UPDATE releases SET state='revoked' WHERE release_id=?", (rid,))
                self._audit("revoke", actor, rid)
                return self.get(rid)

    def revoke_capability(self, scene, name, *, actor):
        with self._admin("revoke_capability", actor, name):
            self._baseline(scene)
            with self.store._transaction():
                self.db.execute("INSERT OR IGNORE INTO release_revocations VALUES(?,'capability',?)", (scene, name))
                self._audit("revoke_capability", actor, scene + "/" + name)
                return {"business_id": scene, "capability_id": name, "status": "revoked"}
