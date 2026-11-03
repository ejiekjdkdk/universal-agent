"""离线规划器和模型规划器共用的鉴权、执行与审计流程。"""
import asyncio
import hashlib
import json
import os
import re
import time
from jsonschema import Draft202012Validator
from referencing.exceptions import Unresolvable

from .catalog import tool_slot
from .errors import Fault
from .providers import run_provider
from .skills import SkillError, SkillRegistry
from .mcp_gateway import MCPContext
from .result_store import ResultStore, load_p2_policy
from .read_cache import ReadCache


def preview(value, depth=0):
    """只记录结构、长度和摘要，避免将用户原文复制到执行轨迹。"""
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
        self.lease = lease
        self.run_id = run_id
        ctx = self.store.run(run_id)
        self.task_id = ctx["task_id"]
        self.snapshot_id = lease["snapshot_id"] if lease else self.store.task_snapshot(self.task_id)
        self.catalog = catalog or runtime.release_catalog(self.snapshot_id, ctx["business_id"])
        self.evidence = {}
        self.pin = ctx["context"].get("skill")
        self.read_only = bool(self.pin) or ctx["mode"] == "mock"
        self.skill = None

    def trace(self, event, data):
        if self.lease:
            data = {**data, "step_id": self.lease["step_id"]}
        self.store.emit(self.task_id, event, data, lease=self.lease)

    def context(self):
        context = self.store.run(self.run_id)
        self.runtime.authorize_task(context, context["business_id"], self.task_id)
        if self.runtime.releases:
            self.runtime.releases.guard(self.snapshot_id, context["business_id"])
        if self.pin:
            self.skill = self.runtime.load_skill(context, self.pin)
            tools = self.runtime.visible_tools(context, context["business_id"], self.catalog, self.snapshot_id)
            available = self.runtime.skill_capabilities(context, context["business_id"], tools, self.snapshot_id)
            if not set(self.skill["tools"]) <= available:
                self.store.audit("skill.dependencies", "deny", context, self.task_id, self.pin["name"], "SKILL_UNAVAILABLE")
                raise Fault("SKILL_UNAVAILABLE", "绑定的技能依赖已停用或权限已撤销", 403)
        return context

    def check_skill_tool(self, ctx, name):
        if self.skill and name not in self.skill["tools"]:
            self.store.audit("skill.capability", "deny", ctx, self.task_id, name, "PERMISSION_DENIED")
            raise Fault("PERMISSION_DENIED", "能力超出已选技能范围", 403)

    def _read_result_page(self, ref, cursor):
        context = self.context()
        if self.runtime.result_store is None:
            raise Fault("PERMISSION_DENIED", "当前任务未启用持久结果引用。", 403)
        from .result_routes import authorize_result
        def authorize(metadata, provenance):
            if metadata["task_id"] != self.task_id:
                raise Fault("PERMISSION_DENIED", "模型只能读取当前任务的结果。", 403)
            self.context()
            authorize_result(self.runtime, context, metadata, provenance)
        return self.runtime.result_store.page(ref, cursor, context, authorize)

    def read_skill_reference(self, path):
        ctx = self.context()
        if not self.skill or path not in self.skill["references"]:
            raise Fault("PERMISSION_DENIED", "引用不在已发布技能的资源清单内", 403)
        try:
            value = self.runtime.skills.read_reference(ctx["business_id"], self.pin["name"], self.pin["version"], path)
        except SkillError:
            raise Fault("SKILL_UNAVAILABLE", "技能引用已不可用", 403) from None
        if len(value) > 8192:
            raise Fault("PROVIDER_LIMIT", "技能引用超过模型读取上限", 422)
        self.trace("skill_reference_read", {"path": path, "chars": len(value)})
        return value

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
        tools = self.runtime.visible_tools(ctx, ctx["business_id"], self.catalog, self.snapshot_id)
        if self.read_only:
            tools = [tool for tool in tools if tool["action_level"] == "read"]
        if self.skill:
            tools = [tool for tool in tools if tool["name"] in self.skill["tools"]]
        self.trace("tools_discovered", {"tools": [tool["name"] for tool in tools]})
        return tools

    def remember(self, evidence):
        for item in evidence:
            # 同一版本在不同来源或时点的观察不能被当成同一条证据覆盖。
            key = (item["source_id"], item["version"], item["snippet"],
                   item.get("source_ref"), item.get("data_as_of"))
            self.evidence[key] = item

    def check_output_secrets(self, payload):
        """在工具数据进入模型、事件或数据库之前，拒绝带有凭据的结果。"""
        text = json.dumps(payload, ensure_ascii=False)
        known = [*self.runtime.principals, self.runtime.settings.get("api_key")]
        known.extend(os.getenv(key) for key in ("AGENT_SERVICE_TOKEN", "AGENT_ADMIN_TOKEN"))
        for secret in known:
            if not secret:
                continue
            encoded = json.dumps(secret, ensure_ascii=False)
            if encoded in text or (len(secret) >= 12 and encoded[1:-1] in text):
                raise Fault("SENSITIVE_OUTPUT", "返回内容含有访问凭据，已阻止传递", 502)
        if re.search(r'(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}|"(?i:api_key|access_token|refresh_token|password|client_secret)"\s*:\s*"[^"]+"', text):
            raise Fault("SENSITIVE_OUTPUT", "返回内容含有访问凭据，已阻止传递", 502)

    async def retrieve(self, query, top_k=5):
        ctx = self.context()
        self.check_skill_tool(ctx, "retrieve_knowledge")
        self.check(ctx, "knowledge.read", "retrieve_knowledge")
        if not isinstance(query, str) or not 0 < len(query.strip()) <= 16000 or type(top_k) is not int or not 1 <= top_k <= 10:
            raise Fault("INVALID_REQUEST", "检索参数无效")
        if self.read_only:
            self.store.consume_tool_attempt(self.task_id, self.skill.get("max_tool_calls", 16) if self.skill else 16,
                                           lease=self.lease)
        hits = self.catalog.retrieve(ctx["business_id"], query, top_k, object_ids=ctx["object_ids"])
        self.check_output_secrets(hits)
        self.remember(hits)
        self.trace("knowledge_retrieved", {"source_ids": [r["source_id"] for r in hits], "query": preview(query)})
        return hits

    def authorize_tool(self, tool, arguments):
        context = self.context()
        scenario_id = context["business_id"]
        name = tool["name"]
        self.check_skill_tool(context, name)
        self.check(context, tool["permission"], name)
        if tool["action_level"] not in (("read",) if self.read_only else ("read", "propose_write")):
            raise Fault("ACTION_NOT_ALLOWED", "当前版本仅允许读取或生成草稿", 403)
        if not isinstance(arguments, dict) or len(json.dumps(arguments, ensure_ascii=False)) > 32000:
            raise Fault("SCHEMA_VALIDATION_FAILED", "工具参数无效", 422)
        live_tool = None
        if self.runtime.releases:
            self.runtime.releases.guard(self.snapshot_id, scenario_id, name)
            live_tool = self.runtime.releases.current_tool(scenario_id, context, "", name)
        elif self.lease:
            live_tool = next((t for t in self.runtime.catalog.discover(scenario_id) if t["name"] == name), None)
        if self.lease or self.runtime.releases:
            if not live_tool or live_tool["action_level"] != tool["action_level"]:
                raise Fault("CAPABILITY_REVOKED", "当前场景已撤销此能力", 403)
            if live_tool["permission"] != tool["permission"]:
                self.check(context, live_tool["permission"], name)
        # 固定版本和当前版本可能用不同字段限定对象，两者都要检查。
        fields = set()
        for definition in (tool, live_tool):
            if definition and definition.get("object_ids_field"):
                fields.add(definition["object_ids_field"])
        for field in fields:
            object_ids = arguments.get(field)
            if not isinstance(object_ids, list) or not all(isinstance(i, str) for i in object_ids):
                raise Fault("SCHEMA_VALIDATION_FAILED", "对象参数应为 ID 数组", 422)
            if not object_ids or not set(object_ids).issubset(context["object_ids"]):
                self.store.audit("object_access", "deny", context, self.task_id, name, "PERMISSION_DENIED")
                raise Fault("PERMISSION_DENIED", "请求的对象超出了本次任务范围", 403)

    def _mcp_binding(self, tool, arguments):
        """幂等调用和共享缓存都必须重新核验远端映射及任务固定摘要。"""
        context = self.context()
        scenario = context["business_id"]
        adapter = self.catalog._tools[scenario][tool["name"]]["adapter"]
        remote_context = MCPContext(tenant_id=context["tenant_id"],
            principal_id=json.dumps([context["org_id"], context["user_id"]]), run_context_id=self.run_id,
            trace_id=self.task_id, authorize=lambda: self.authorize_tool(tool, arguments))
        remote_gateway = self.catalog._mcp_gateway
        endpoint, server, mapped = remote_gateway._resolve(adapter["server_id"], adapter["tool_id"], remote_context)
        for field in ("action_level", "input_schema", "output_schema"):
            if mapped[field] != tool[field]:
                raise Fault("MCP_PROTOCOL_INCOMPATIBLE", "MCP缓存映射与本地声明不符", 502)
        version = tool.get("version", self.catalog.scenarios[scenario]["version"])
        if mapped["capability_id"] != tool["name"] or mapped["version"] != version:
            raise Fault("MCP_PROTOCOL_INCOMPATIBLE", "MCP缓存映射与本地版本不符", 502)
        from .mcp_gateway import _json
        digest = hashlib.sha256(_json([endpoint, server, mapped]).encode()).hexdigest()
        self.store.pin_mcp_configuration(self.task_id, adapter["server_id"] + "/" + adapter["tool_id"], digest)
        return remote_gateway._scope_key(endpoint, server, remote_context), "p2" in server

    def _cache_scope(self, tool, arguments):
        cache = self.runtime.read_cache
        context = self.context()
        scenario = context["business_id"]
        adapter = self.catalog._tools[scenario][tool["name"]]["adapter"]
        if cache is None or tool["action_level"] != "read" or adapter["type"] == "agent":
            return None
        policy = cache.policies.get(scenario + "/" + tool["name"])
        if policy is None:
            return None
        current = tool
        if self.runtime.releases:
            current = self.runtime.releases.current_tool(scenario, context, "", tool["name"])
        key_parts = {"identity": [context[key] for key in ("tenant_id", "org_id", "user_id")],
                     "business": scenario, "grant": self.runtime.grant(context, scenario),
                     "scope": self.store.scope(self.task_id), "tool": tool, "current": current,
                     "adapter": adapter, "arguments": arguments, "policy": policy,
                     "skill": self.pin, "snapshot": self.snapshot_id}
        if adapter["type"] == "mcp":
            binding, governed = self._mcp_binding(tool, arguments)
            if not governed:
                return None
            key_parts["mcp"] = binding
        encoded = json.dumps(key_parts, ensure_ascii=False, sort_keys=True, allow_nan=False)
        return hashlib.sha256(encoded.encode()).hexdigest(), policy["ttl_s"]

    async def _shared_output(self, tool, arguments, call_id, cache_scope):
        key, ttl = cache_scope
        def guard():
            self.authorize_tool(tool, arguments)
            self.store._guard_lease(self.task_id, self.lease, call_id)
            if self._cache_scope(tool, arguments) != cache_scope:
                raise Fault("CAPABILITY_REVOKED", "只读缓存的授权范围或数据版本已改变", 403)

        def charge():
            if self.read_only:
                limit = self.skill.get("max_tool_calls", 16) if self.skill else 16
                self.store.consume_tool_attempt(self.task_id, limit, lease=self.lease)

        async def operation(select):
            # 只有实际执行者占并发名额；等待者不会阻塞自己等待的查询。
            async with asyncio.timeout(self.runtime.tool_timeout):
                async with tool_slot(self.runtime.slots):
                    deadline = time.monotonic() + self.runtime.tool_timeout
                    def authorize_retrieval():
                        gateway = select()
                        context = gateway.context()
                        gateway.check_skill_tool(context, "retrieve_knowledge")
                        gateway.check(context, "knowledge.read", "retrieve_knowledge")
                        return context["object_ids"]
                    options = {"authorize_retrieval": authorize_retrieval}
                    if self.runtime.result_store:
                        options["validate_output"] = self.runtime.result_store.validate_output
                    gateway = select()
                    context = gateway.context()
                    adapter = self.catalog._tools[context["business_id"]][tool["name"]]["adapter"]
                    if adapter["type"] == "mcp":
                        options["mcp_context"] = MCPContext(tenant_id=context["tenant_id"],
                            principal_id=json.dumps([context["org_id"], context["user_id"]]),
                            run_context_id=gateway.run_id, trace_id=gateway.task_id,
                            authorize=lambda: select(),
                            trace=lambda event, data: select().trace(event, data),
                            pin_configuration=lambda name, digest: select().store.pin_mcp_configuration(
                                select().task_id, name, digest))
                    for attempt in range(2):
                        try:
                            gateway = select(True)
                            scenario = gateway.context()["business_id"]
                            output = await self.catalog.execute(scenario, tool["name"], arguments, **options)
                            gateway = select()
                            context = gateway.context()
                            for item in output["evidence"]:
                                if "object_id" in item and item["object_id"] not in context["object_ids"]:
                                    raise Fault("PERMISSION_DENIED", "工具返回了范围外的证据", 403)
                            gateway.check_output_secrets(output)
                            return output
                        except Fault as exc:
                            if attempt or not exc.retryable:
                                raise
                            delay = max(0.2, getattr(exc, "retry_after", 0.2))
                            if time.monotonic() + delay >= deadline:
                                raise
                            select().trace("tool_retry", {"capability_id": tool["name"],
                                           "error_code": exc.code, "delay_s": delay})
                            await asyncio.sleep(delay)
        participant = {"guard": guard, "charge": charge, "value": self}
        output = await self.runtime.read_cache.run(key, ttl, participant, operation)
        self.trace("read_cache_result", {"capability_id": tool["name"], "scope_digest": key})
        return output

    async def execute(self, name, arguments, call_id):
        context = self.context()
        scenario_id = context["business_id"]
        tool = None
        for candidate in self.catalog.discover(scenario_id):
            if candidate["name"] == name:
                tool = candidate
                break
        if tool is None:
            self.store.audit("execute", "deny", context, self.task_id, name, "CAPABILITY_NOT_FOUND")
            raise Fault("CAPABILITY_NOT_FOUND", "场景中没有此工具", 404)
        self.authorize_tool(tool, arguments)
        adapter = self.catalog._tools[scenario_id][name]["adapter"]
        if adapter["type"] == "agent":
            if not self.runtime.agents_enabled or self.lease:
                raise Fault("ACTION_NOT_ALLOWED", "专业 Agent 仅在显式启用的交互任务中可用", 403)
        version = tool.get("version", self.catalog.scenarios[scenario_id]["version"])
        call_budget = self.lease["max_tool_calls"] if self.lease else 8
        if self.skill:
            call_budget = min(call_budget, self.skill.get("max_tool_calls", 8))
        execution_id, cached = self.store.begin_call(
            self.task_id, call_id, name, version, arguments, max_calls=call_budget, lease=self.lease
        )
        if cached is not None:
            if adapter["type"] == "mcp":
                self._mcp_binding(tool, arguments)
            self.remember(cached.get("evidence", []))
            return cached
        started = time.monotonic()
        self.trace("tool_input", {"execution_id": execution_id, "arguments": preview(arguments)})
        self.trace("tool_started", {"execution_id": execution_id, "capability_id": name, "capability_version": version})
        deadline = started + self.runtime.tool_timeout
        if self.lease:
            deadline = min(deadline, started + self.lease["deadline"] - time.time())
        try:
            async with asyncio.timeout(self.runtime.tool_timeout):
                cache_scope = self._cache_scope(tool, arguments)
                if cache_scope is not None:
                    output = await self._shared_output(tool, arguments, call_id, cache_scope)
                else:
                    slots = self.runtime.slots
                    if adapter["type"] == "agent":
                        # 委派等待子工具时不能占用子工具需要的同一组并发名额。
                        slots = self.runtime.delegate_slots
                    async with tool_slot(slots):
                        # 回调只准备一次；每次实际调用时仍重新读取身份并核验权限。
                        def authorize_retrieval():
                            ctx = self.context()
                            self.check_skill_tool(ctx, "retrieve_knowledge")
                            self.check(ctx, "knowledge.read", "retrieve_knowledge")
                            return ctx["object_ids"]

                        options = {"authorize_retrieval": authorize_retrieval}
                        if self.runtime.result_store:
                            options["validate_output"] = self.runtime.result_store.validate_output
                        if adapter["type"] == "agent":
                            async def delegate(target, payload):
                                self.authorize_tool(tool, arguments)
                                self.trace("agent_delegated", {"execution_id": execution_id, "capability_id": name})
                                child_id = "agent." + hashlib.sha256((call_id + "/" + target).encode()).hexdigest()[:32]
                                child = await self.execute(target, payload, child_id)
                                self.trace("agent_returned", {"execution_id": execution_id,
                                           "child_execution_id": child["execution_id"]})
                                return {"data": child["data"], "evidence": child["evidence"]}
                            options["delegate"] = delegate
                        if adapter["type"] == "mcp":
                            options["mcp_context"] = MCPContext(
                                tenant_id=context["tenant_id"],
                                principal_id=json.dumps([context["org_id"], context["user_id"]]),
                                run_context_id=self.run_id,
                                trace_id=self.task_id,
                                authorize=lambda: self.authorize_tool(tool, arguments),
                                trace=lambda event, data: self.trace(event, {**data, "execution_id": execution_id}),
                                pin_configuration=lambda key, digest: self.store.pin_mcp_configuration(self.task_id, key, digest),
                            )

                        for attempt in range(2):
                            try:
                                # 排队或退避期间可能撤权，每次发出请求前都要复查。
                                self.authorize_tool(tool, arguments)
                                if self.read_only and adapter["type"] != "agent":
                                    attempt_limit = 16
                                    if self.skill:
                                        attempt_limit = self.skill.get("max_tool_calls", 16)
                                    self.store.consume_tool_attempt(self.task_id, attempt_limit, lease=self.lease)

                                output = await self.catalog.execute(scenario_id, name, arguments, **options)
                                self.authorize_tool(tool, arguments)
                                if any("object_id" in item and item["object_id"] not in context["object_ids"]
                                       for item in output["evidence"]):
                                    raise Fault("PERMISSION_DENIED", "工具返回了范围外的证据", 403)
                                self.check_output_secrets(output)
                                break
                            except Fault as exc:
                                if attempt or not exc.retryable or tool["action_level"] != "read":
                                    raise
                                delay = max(0.2, getattr(exc, "retry_after", 0.2))
                                if time.monotonic() + delay >= deadline:
                                    raise
                                self.trace("tool_retry", {"execution_id": execution_id, "error_code": exc.code, "delay_s": delay})
                                await asyncio.sleep(delay)
            result = {"execution_id": execution_id, "capability_id": name, "capability_version": tool.get("version", self.catalog.scenarios[scenario_id]["version"]),
                      "status": "completed", "error": None, **output}
            if self.skill:
                # 此标记仅表示网关已取得并校验适配器返回值。
                result.update(result_schema_version="1", verified=True, verification_scope="adapter_output_schema")
            if self.runtime.result_store:
                provenance = {"capability_id": name, "capability_version": version, "snapshot_id": self.snapshot_id,
                              "permission": tool["permission"], "action_level": tool["action_level"],
                              "arguments": arguments, "skill": self.pin}
                if adapter["type"] == "mcp":
                    binding = self.store._db.execute("SELECT digest FROM task_mcp_bindings WHERE task_id=? AND capability_key=?",
                                                    (self.task_id, adapter["server_id"] + "/" + adapter["tool_id"])).fetchone()
                    provenance["mcp"] = {"server_id": adapter["server_id"], "tool_id": adapter["tool_id"],
                                         "config_digest": binding[0] if binding else None}
                with self.store._transaction():
                    self.store._guard_lease(self.task_id, self.lease, call_id)
                    self.context()
                    self.runtime.result_store.put(self.task_id, result, provenance)
                    self.store.complete_call(self.task_id, call_id, result, lease=self.lease)
            else:
                self.store.complete_call(self.task_id, call_id, result, lease=self.lease)
            self.remember(result["evidence"])
            self.trace("tool_result", {"execution_id": execution_id, "tool": name, "output": preview(result["data"]),
                                       "capability_version": version, "verification_scope": "adapter_output_schema",
                                       "source_ids": [s["source_id"] for s in result["evidence"]],
                                       "duration_ms": round((time.monotonic() - started) * 1000)})
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, Fault):
                fault = exc
            elif isinstance(exc, TimeoutError):
                fault = Fault("TOOL_TIMEOUT", "工具超时", 504)
            else:
                fault = Fault("UPSTREAM_DEPENDENCY_FAILED", "工具执行失败", 424)
            self.store.complete_call(self.task_id, call_id, None, fault.as_dict(), lease=self.lease)
            raise fault


class Runtime:
    def __init__(self, catalog, store, principals, settings):
        self.catalog, self.store = catalog, store
        self.p2_policy = load_p2_policy(settings)
        self.result_store = None
        if "results" in self.p2_policy:
            self.result_store = ResultStore(store, self.p2_policy["results"])
        self.read_cache = None
        if "read_cache" in self.p2_policy:
            self.read_cache = ReadCache(self.p2_policy["read_cache"])
        self.store._result_store = self.result_store
        self.store.result_validator = self.validate_task_result
        self._principals, self.settings = principals, settings
        self.role_source = None
        self.workers = {}
        self.gateways = {}
        self.slots = asyncio.Semaphore(8)
        self.delegate_slots = asyncio.Semaphore(3)
        self.agents_enabled = os.getenv("AGENT_SPECIALISTS", "0") == "1"
        self.tool_timeout = 20
        self.releases = None
        self.snapshots = None
        self.skills: SkillRegistry | None = None

    def bind_releases(self, releases, snapshots):
        self.releases = releases
        self.snapshots = snapshots
        self.store.release_guard = releases.guard

    def validate_skill_input(self, business, pin, message, context):
        if not pin:
            return
        if self.skills is None:
            raise Fault("SKILL_UNAVAILABLE", "技能注册表不可用", 403)
        try:
            skill = self.skills.load(business, pin["name"], pin["version"])
        except SkillError:
            raise Fault("SKILL_UNAVAILABLE", "技能版本不可用", 403) from None
        self._validate_skill_value(skill.get("input_schema"), {"message": message, "context": context})

    def validate_task_result(self, task_id, result):
        pin = self.store.skill_pin(task_id)
        if not pin:
            return
        row = self.store._task_row(task_id)
        skill = self.load_skill(dict(row), pin)
        self._validate_skill_value(skill.get("output_schema"), result)

    @staticmethod
    def _validate_skill_value(schema, value):
        if schema is None:
            return
        try:
            if Draft202012Validator(schema).is_valid(value):
                return
        except (ValueError, TypeError, RecursionError, Unresolvable):
            pass
        raise Fault("SKILL_SCHEMA_INVALID", "技能输入或结果不符合已发布的结构", 422)

    @property
    def principals(self):
        return self.role_source() if self.role_source else self._principals

    def release_catalog(self, snapshot_id, scene_id):
        if snapshot_id and self.snapshots:
            return self.snapshots.open(snapshot_id, scene_id)
        return self.catalog

    def grant(self, identity, scenario):
        for principal in self.principals.values():
            if all(identity.get(k) == principal[k] for k in ("tenant_id", "org_id", "user_id")):
                return principal["grants"].get(scenario, {})
        return {}

    def visible_tools(self, actor, scenario, catalog, snapshot_id):
        grant = self.authorize(actor, scenario)
        tools = []
        for tool in catalog.discover(scenario):
            if tool["permission"] not in grant["permissions"] or tool["action_level"] not in ("read", "propose_write"):
                continue
            if tool.get("object_ids_field") and not grant.get("objects"):
                continue
            if self.releases:
                try:
                    self.releases.guard(snapshot_id, scenario, tool["name"])
                    current = self.releases.current_tool(scenario, actor, "", tool["name"])
                except Fault as exc:
                    if exc.code == "CAPABILITY_REVOKED":
                        continue
                    raise
                if not current or current["permission"] not in grant["permissions"] or current["action_level"] != tool["action_level"]:
                    continue
            tools.append(tool)
        return tools

    def skill_capabilities(self, actor, scenario, tools, snapshot_id):
        available = {tool["name"] for tool in tools if tool["action_level"] == "read"}
        if "knowledge.read" in self.grant(actor, scenario)["permissions"]:
            if self.releases:
                try:
                    self.releases.guard(snapshot_id, scenario, "retrieve_knowledge")
                except Fault as exc:
                    if exc.code == "CAPABILITY_REVOKED":
                        return available
                    raise
            available.add("retrieve_knowledge")
        return available

    def eligible_skills(self, actor, scenario, tools, snapshot_id=None):
        if self.skills is None:
            return []
        available = self.skill_capabilities(actor, scenario, tools, snapshot_id)
        return [entry for entry in self.skills.active_for(actor, scenario) if set(entry["tools"]) <= available]

    def select_skill(self, actor, scenario, requested, catalog, snapshot_id):
        if not requested and (self.skills is None or not self.skills.has_bindings(scenario)):
            return None
        candidates = self.eligible_skills(actor, scenario, self.visible_tools(actor, scenario, catalog, snapshot_id), snapshot_id)
        if requested:
            candidates = [entry for entry in candidates if entry["name"] == requested]
        if len(candidates) != 1:
            if candidates:
                raise Fault("SKILL_REQUIRED", "请指定本次任务使用的技能", 422)
            self.store.audit("skill.select", "deny", actor, resource=scenario, code="SKILL_UNAVAILABLE")
            raise Fault("SKILL_UNAVAILABLE", "技能不可用或依赖能力未授权", 403)
        entry = candidates[0]
        return {key: entry[key] for key in ("name", "version", "digest", "release_snapshot_id")}

    def load_skill(self, context, pin):
        try:
            if self.skills is None:
                raise SkillError("技能目录未配置")
            skill = self.skills.load(context["business_id"], pin["name"], pin["version"])
            if (skill["digest"], skill["release_snapshot_id"]) == (pin["digest"], pin["release_snapshot_id"]):
                return skill
        except SkillError:
            pass
        self.store.audit("skill.load", "deny", context, context["task_id"], pin["name"], "SKILL_UNAVAILABLE")
        raise Fault("SKILL_UNAVAILABLE", "绑定的技能版本已失效", 403)

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
        return grant

    def gateway(self, run_id):
        ctx = self.store.run(run_id)
        if self.store.is_durable(ctx["task_id"]):
            raise Fault("PERMISSION_DENIED", "持久计划步骤仅允许由持有租约的 Worker 执行", 403)
        return self.gateways.get(run_id) or Gateway(self, run_id)

    def authorize_task(self, actor, scenario, task_id):
        if getattr(self, "triggers", None):
            self.triggers.guard_task(task_id)
        grant = self.authorize(actor, scenario, self.store.scope(task_id))
        if (not self.store.permissions(task_id).issubset(grant.get("permissions", []))
                or not self.store.history_types(task_id).issubset(grant.get("resource_types", []))):
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
        gateway = None
        try:
            gateway = Gateway(self, run_id)
            self.gateways[run_id] = gateway
            mode = body["mode"]
            scene = gateway.catalog.scenarios[body["business_id"]]
            gateway.trace("planner_started", {"mode": mode, "scenario": scene["id"], "version": scene["version"]})
            scope = gateway.context()
            if scope["object_type"]:
                allowed_types = [scope["object_type"]]
            else:
                allowed_types = self.grant(identity, body["business_id"]).get("resource_types", [])

            history = []
            if mode == "openai":
                history = self.store.history(
                    identity, task["session_id"], allowed_objects=scope["object_ids"],
                    allowed_types=allowed_types,
                    allowed_permissions=self.grant(identity, scene["id"])["permissions"],
                    inherit_to=task_id, skill=gateway.pin,
                )
            async with asyncio.timeout(120):
                response = await run_provider(mode, scene, body["message"], history, gateway, self.settings)
            result = response if isinstance(response, dict) else {"answer": response}
            answer = result["answer"]
            if not isinstance(answer, str) or not answer.strip():
                raise Fault("UPSTREAM_INVALID_RESPONSE", "回复为空", 502)
            gateway.context()
            evidence = list(gateway.evidence.values())
            gateway.check_output_secrets(result)
            result = {**result, "evidence": evidence, "artifacts": []}
            self.validate_task_result(task_id, result)
            gateway.trace("message_delta", {"delta": answer})
            self.store.finish(task_id, "completed", result)
        except asyncio.CancelledError:
            self.store.finish(task_id, "cancelled", {})
            raise
        except Exception as exc:
            if isinstance(exc, Fault):
                fault = exc
            elif isinstance(exc, TimeoutError):
                fault = Fault("TASK_TIMEOUT", "任务超时", 504)
            else:
                fault = Fault("INTERNAL_ERROR", "任务执行失败", 500)
            partial = {}
            if gateway is not None and fault.code == "BUDGET_EXCEEDED":
                # 仅返回已完成调用的证据，失败任务不会伪造最终回答。
                partial = {"evidence": list(gateway.evidence.values()), "limitations": ["预算已用尽，未生成最终回答。"],
                           "unresolved_items": ["剩余调用未执行。"]}
            self.store.finish(task_id, "failed", partial, fault.as_dict())
        finally:
            self.gateways.pop(run_id, None)
