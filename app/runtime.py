"""Shared execution path for the offline and model planners."""
import asyncio
import hashlib
import json
import time

from .errors import Fault
from .providers import run_provider


def preview(value, depth=0):
    """Record structure and size without copying free-form user text into a trace."""
    if isinstance(value, str):
        digest = hashlib.sha256(value.encode()).hexdigest()[:12]
        return {"chars": len(value), "sha256_prefix": digest}
    if isinstance(value, dict) and depth < 2:
        return {k[:80]: preview(v, depth + 1) for k, v in list(value.items())[:16]}
    if isinstance(value, list):
        return {"items": len(value)}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return {"type": type(value).__name__}


class Gateway:
    def __init__(self, runtime, run_id, *, catalog=None, lease=None):
        self.runtime = runtime
        self.store = runtime.store
        self.catalog = catalog or runtime.catalog
        self.lease = lease
        self.run_id = run_id
        self.task_id = self.store.run(run_id)["task_id"]
        self.evidence = {}

    def trace(self, event, data):
        if self.lease:
            data = {**data, "step_id": self.lease["step_id"]}
        self.store.emit(self.task_id, event, data, lease=self.lease)

    def context(self):
        context = self.store.run(self.run_id)
        self.runtime.authorize_task(context, context["business_id"], self.task_id)
        return context

    def check(self, ctx, permission, resource):
        grant = self.runtime.grant(ctx, ctx["business_id"])
        allowed = permission in grant["permissions"]
        self.store.audit("tool_permission", "allow" if allowed else "deny", ctx,
                         self.task_id, resource, None if allowed else "PERMISSION_DENIED")
        self.trace("authorization", {"resource": resource, "permission": permission, "allowed": allowed})
        if not allowed:
            raise Fault("PERMISSION_DENIED", "当前身份没有此工具权限", 403)

    async def discover(self):
        ctx = self.context()
        permissions = self.runtime.grant(ctx, ctx["business_id"])["permissions"]
        tools = [tool for tool in self.catalog.discover(ctx["business_id"])
                 if tool["permission"] in permissions and tool["action_level"] in ("read", "propose_write")]
        self.trace("tools_discovered", {"tools": [tool["name"] for tool in tools]})
        return tools

    def remember(self, evidence):
        for item in evidence:
            self.evidence[(item["source_id"], item["version"], item["snippet"])] = item

    async def retrieve(self, query, top_k=5):
        ctx = self.context()
        self.check(ctx, "knowledge.read", "retrieve_knowledge")
        if not isinstance(query, str) or not 0 < len(query.strip()) <= 16000 or type(top_k) is not int or not 1 <= top_k <= 10:
            raise Fault("INVALID_REQUEST", "检索参数无效")
        hits = self.catalog.retrieve(ctx["business_id"], query, top_k)
        self.remember(hits)
        self.trace("knowledge_retrieved", {"source_ids": [r["source_id"] for r in hits], "query": preview(query)})
        return hits

    async def execute(self, name, arguments, call_id):
        ctx = self.context()
        sid = ctx["business_id"]
        tools = self.catalog.discover(sid)
        tool = next((item for item in tools if item["name"] == name), None)
        if tool is None:
            self.store.audit("execute", "deny", ctx, self.task_id, name, "CAPABILITY_NOT_FOUND")
            raise Fault("CAPABILITY_NOT_FOUND", "场景中没有此工具", 404)
        self.check(ctx, tool["permission"], name)
        if tool["action_level"] not in ("read", "propose_write"):
            raise Fault("ACTION_NOT_ALLOWED", "当前版本仅允许读取或生成草稿", 403)
        if not isinstance(arguments, dict) or len(json.dumps(arguments, ensure_ascii=False)) > 32000:
            raise Fault("SCHEMA_VALIDATION_FAILED", "工具参数无效", 422)
        live_tool = None
        if self.lease:
            live_tool = next((t for t in self.runtime.catalog.discover(sid) if t["name"] == name), None)
            if not live_tool or live_tool["action_level"] != "read" or tool["action_level"] != "read":
                raise Fault("CAPABILITY_REVOKED", "当前场景已撤销此只读能力", 403)
            self.check(ctx, live_tool["permission"], name)
        fields = {t["object_ids_field"] for t in (tool, live_tool) if t and t.get("object_ids_field")}
        for field in fields:
            ids = arguments.get(field)
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                raise Fault("SCHEMA_VALIDATION_FAILED", "对象参数应为 ID 数组", 422)
            if not ids or not set(ids).issubset(ctx["object_ids"]):
                self.store.audit("object_access", "deny", ctx, self.task_id, name, "PERMISSION_DENIED")
                raise Fault("PERMISSION_DENIED", "请求的对象超出了本次任务范围", 403)
        version = tool.get("version", self.catalog.scenarios[sid]["version"])
        execution_id, cached = self.store.begin_call(
            self.task_id, call_id, name, version, arguments,
            max_calls=self.lease["max_tool_calls"] if self.lease else 8, lease=self.lease)
        if cached is not None:
            self.remember(cached.get("evidence", []))
            return cached
        started = time.monotonic()
        self.trace("tool_input", {"execution_id": execution_id, "arguments": preview(arguments)})
        deadline = started + self.runtime.tool_timeout
        if self.lease:
            deadline = min(deadline, started + self.lease["deadline"] - time.time())
        try:
            async with asyncio.timeout(self.runtime.tool_timeout):
                async with self.runtime.slots:
                    for attempt in range(2):
                        try:
                            self.check(self.context(), tool["permission"], name)
                            if live_tool and live_tool["permission"] != tool["permission"]:
                                self.check(self.context(), live_tool["permission"], name)
                            def authorize_retrieval():
                                self.check(self.context(), "knowledge.read", "retrieve_knowledge")
                            output = await self.catalog.execute(sid, name, arguments,
                                                                authorize_retrieval=authorize_retrieval)
                            self.check(self.context(), tool["permission"], name)
                            break
                        except Fault as exc:
                            if attempt or not exc.retryable or tool["action_level"] != "read":
                                raise
                            delay = max(0.2, getattr(exc, "retry_after", 0.2))
                            if time.monotonic() + delay >= deadline:
                                raise
                            self.trace("tool_retry", {"execution_id": execution_id, "error_code": exc.code, "delay_s": delay})
                            await asyncio.sleep(delay)
            result = {"execution_id": execution_id, "capability_id": name, "capability_version": tool.get("version", self.catalog.scenarios[sid]["version"]),
                      "status": "completed", "error": None, **output}
            self.store.complete_call(self.task_id, call_id, result, lease=self.lease)
            self.remember(result["evidence"])
            self.trace("tool_result", {"execution_id": execution_id, "tool": name, "output": preview(result["data"]),
                                       "source_ids": [s["source_id"] for s in result["evidence"]],
                                       "duration_ms": round((time.monotonic() - started) * 1000)})
            return result
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            fault = exc if isinstance(exc, Fault) else (
                Fault("TOOL_TIMEOUT", "工具超时", 504) if isinstance(exc, TimeoutError)
                else Fault("UPSTREAM_DEPENDENCY_FAILED", "工具执行失败", 424))
            self.store.complete_call(self.task_id, call_id, None, fault.as_dict(), lease=self.lease)
            raise fault


class Runtime:
    def __init__(self, catalog, store, principals, settings):
        self.catalog, self.store = catalog, store
        self.principals, self.settings = principals, settings
        self.workers = {}
        self.gateways = {}
        self.slots = asyncio.Semaphore(8)
        self.tool_timeout = 20

    def grant(self, identity, scenario):
        for principal in self.principals.values():
            if all(identity.get(k) == principal[k] for k in ("tenant_id", "org_id", "user_id")):
                return principal["grants"].get(scenario, {})
        return {}

    def authorize(self, identity, scenario, context=None):
        grant = self.grant(identity, scenario)
        valid = scenario in self.catalog.scenarios and bool(grant)
        if context:
            valid = valid and set(context.get("object_ids", [])).issubset(grant.get("objects", []))
            if context.get("object_type"):
                valid = valid and context["object_type"] in grant.get("resource_types", [])
        if not valid:
            self.store.audit("scenario_access", "deny", identity, resource=scenario, code="PERMISSION_DENIED")
            raise Fault("PERMISSION_DENIED", "没有场景或对象访问权限", 403)

    def gateway(self, run_id):
        ctx = self.store.run(run_id)
        if self.store.is_durable(ctx["task_id"]):
            raise Fault("PERMISSION_DENIED", "持久计划步骤仅允许由持有租约的 Worker 执行", 403)
        return self.gateways.get(run_id) or Gateway(self, run_id)

    def authorize_task(self, actor, scenario, task_id):
        self.authorize(actor, scenario, self.store.scope(task_id))
        if not self.store.permissions(task_id).issubset(self.grant(actor, scenario)["permissions"]):
            self.store.audit("task_access", "deny", actor, task_id, scenario, "PERMISSION_DENIED")
            raise Fault("PERMISSION_DENIED", "读取本次任务所需的权限已撤销", 403)

    def launch(self, identity, body, task):
        task_id = task["task_id"]
        worker = asyncio.create_task(self.drive(identity, body, task))
        self.workers[task_id] = worker
        worker.add_done_callback(lambda _: self.workers.pop(task_id, None))

    async def drive(self, identity, body, task):
        task_id = task["task_id"]
        run_id = self.store.context_id(task_id)
        try:
            gw = Gateway(self, run_id)
            self.gateways[run_id] = gw
            mode = body["mode"]
            scene = self.catalog.scenarios[body["business_id"]]
            gw.trace("planner_started", {"mode": mode, "scenario": scene["id"], "version": scene["version"]})
            scope = gw.context()
            allowed_types = [scope["object_type"]] if scope["object_type"] else self.grant(identity, body["business_id"]).get("resource_types", [])
            history = self.store.history(identity, task["session_id"], allowed_objects=scope["object_ids"],
                                         allowed_types=allowed_types,
                                         allowed_permissions=self.grant(identity, scene["id"])["permissions"],
                                         inherit_to=task_id) if mode == "openai" else []
            async with asyncio.timeout(120):
                answer = await run_provider(mode, scene, body["message"], history, gw, self.settings)
            if not isinstance(answer, str) or not answer.strip():
                raise Fault("UPSTREAM_INVALID_RESPONSE", "回复为空", 502)
            gw.context()
            evidence = list(gw.evidence.values())
            gw.trace("message_delta", {"delta": answer})
            self.store.finish(task_id, "completed", {"answer": answer, "evidence": evidence, "artifacts": []})
        except asyncio.CancelledError:
            self.store.finish(task_id, "cancelled", {})
            raise
        except Exception as exc:
            fault = exc if isinstance(exc, Fault) else (
                Fault("TASK_TIMEOUT", "任务超时", 504) if isinstance(exc, TimeoutError)
                else Fault("INTERNAL_ERROR", "任务执行失败", 500))
            self.store.finish(task_id, "failed", {}, fault.as_dict())
        finally:
            self.gateways.pop(run_id, None)
