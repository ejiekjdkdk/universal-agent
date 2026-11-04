"""通过官方 SDK 执行批准的 MCP 2026-07-28 工具。

配置文件由部署者管理；工具参数和模型消息不能指定地址、进程、凭据、租户或远端工具。
"""
import asyncio
import hashlib
import ipaddress
import json
import os
from dataclasses import dataclass
from contextvars import ContextVar
from collections import OrderedDict, deque
import logging
from pathlib import Path
import re
import time
from typing import Callable
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from .errors import Fault
from .schema_validation import is_valid_async


MAX_RESPONSE = 262144
PROTOCOL = "2026-07-28"
_protected_sdk_logs = ContextVar("protected_mcp_sdk_logs", default=False)


class _SafeSDKLog(logging.Filter):
    """SDK异常可能包含响应原文；网关调用内只保留日志级别和固定诊断。"""

    def filter(self, record):
        if _protected_sdk_logs.get():
            record.msg = "MCP客户端诊断已脱敏，错误类别请查看任务Trace"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


_sdk_log_filter = _SafeSDKLog()
# 锁定SDK的客户端/公共分发器使用这些日志名，包括历史名称client。
for _logger_name in (
    "client", "mcp.client.client", "mcp.client.streamable_http", "mcp.client.caching",
    "mcp.shared.direct_dispatcher", "mcp.shared.dispatcher", "mcp.shared.jsonrpc_dispatcher",
    "mcp.shared.tool_name_validation",
):
    logging.getLogger(_logger_name).addFilter(_sdk_log_filter)


@dataclass(frozen=True)
class MCPContext:
    tenant_id: str
    principal_id: str
    run_context_id: str
    trace_id: str
    authorize: Callable
    trace: Callable = lambda *args: None
    pin_configuration: Callable = lambda key, digest: None


def _json(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _schema(schema):
    from .result_store import _bounded
    _bounded(schema)
    Draft202012Validator.check_schema(schema)
    def walk(value):
        if isinstance(value, dict):
            # 所有层级均禁止引用解析，避免校验时访问网络或外部资源。
            if any(k in value for k in ("$ref", "$dynamicRef", "$id")):
                raise ValueError("MCP schemas must be inline without references")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(schema)
    if schema.get("type") != "object":
        raise ValueError("MCP schema must describe an object")


def _validate(value, schema, code):
    try:
        from .result_store import _bounded
        _bounded(value)
        invalid = next(Draft202012Validator(schema).iter_errors(value), None)
        if invalid or len(_json(value).encode()) > MAX_RESPONSE:
            raise ValueError("invalid bounded value")
    except (ValueError, TypeError, RecursionError) as exc:
        status = 502
        if code == "SCHEMA_VALIDATION_FAILED":
            status = 422
        raise Fault(code, "MCP 数据与批准结构不符", status) from exc


def _endpoint(item):
    if set(item) - {"url", "allow_loopback"}:
        raise ValueError("unknown endpoint settings")
    url = item["url"]
    parts = urlsplit(url)
    if (not isinstance(url, str) or len(url) > 2048 or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment
            or any(c.isspace() or c in "\\{}" for c in url)):
        raise ValueError("invalid MCP endpoint")
    # 字面 IP 消除 DNS 重绑定、代理与不明确主机名造成的地址变更。
    address = ipaddress.ip_address(parts.hostname or "")
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise ValueError("invalid MCP port")
    if address.is_loopback and item.get("allow_loopback") is True:
        if parts.scheme not in {"http", "https"}:
            raise ValueError("invalid loopback scheme")
    elif parts.scheme != "https" or not address.is_global:
        raise ValueError("MCP requires HTTPS global literal IP or explicit development loopback")
    return url


def _leaves(exc):
    if not isinstance(exc, BaseExceptionGroup):
        return [exc]
    leaves = []
    for child in exc.exceptions:
        leaves.extend(_leaves(child))
    return leaves


def catalog_digest(tools):
    """摘要仅包含操作者审核的目录字段，不接受远端提供的摘要替代计算。"""
    selected = []
    for tool in tools:
        selected.append({key: tool.get(key) for key in ("name", "description", "inputSchema", "outputSchema")})
    selected.sort(key=lambda item: item["name"])
    return hashlib.sha256(_json(selected).encode()).hexdigest()


def _policy(server):
    policy = server.get("p2")
    if policy is None:
        return None
    limits = {
        "failure_threshold": (3, 1, 20),
        "cooldown_s": (10, 0.01, 300),
        "max_concurrency": (4, 1, 32),
        "rate_per_minute": (120, 1, 6000),
        "cache_ttl_s": (30, 0, 300),
    }
    if not isinstance(policy, dict) or set(policy) - {*limits, "catalog_digest"}:
        raise ValueError("MCP P2策略字段无效")
    if not isinstance(policy.get("catalog_digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", policy["catalog_digest"]):
        raise ValueError("MCP P2需要操作者批准的目录摘要")
    checked = {"catalog_digest": policy["catalog_digest"]}
    for name, (default, low, high) in limits.items():
        value = policy.get(name, default)
        allowed_types: tuple[type, ...] = (int,)
        if name in {"cooldown_s", "cache_ttl_s"}:
            allowed_types = (int, float)
        if type(value) not in allowed_types or not low <= value <= high:
            raise ValueError("MCP P2策略数值超限")
        checked[name] = value
    return checked


class MCPGateway:
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()
        self._read()
        self.health = OrderedDict()
        self.directories = OrderedDict()

    @classmethod
    def from_env(cls):
        path = os.getenv("AGENT_MCP_SERVERS")
        if not path:
            return None
        return cls(path)

    def _read(self):
        from .catalog import _reject_duplicate_keys
        with self.config_path.open("rb") as stream:
            raw = stream.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("MCP configuration too large")
        config = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        if set(config) != {"endpoints", "servers"}:
            raise ValueError("MCP configuration requires endpoints and servers")
        for endpoint in config["endpoints"].values():
            _endpoint(endpoint)
        for server in config["servers"].values():
            if set(server) - {"p2"} != {"endpoint_ref", "enabled", "approved_protocol_versions", "tenants", "tools"}:
                raise ValueError("unknown or missing MCP server settings")
            _policy(server)
            if server["endpoint_ref"] not in config["endpoints"] or type(server["enabled"]) is not bool:
                raise ValueError("unknown endpoint or invalid enabled state")
            if (not server["approved_protocol_versions"] or
                    not all(isinstance(v, str) for v in server["approved_protocol_versions"])):
                raise ValueError("explicit protocol approval required")
            for tenant, binding in server["tenants"].items():
                if not tenant or set(binding) != {"credential_env"}:
                    raise ValueError("invalid tenant credential binding")
                env = binding["credential_env"]
                if env is not None and (not isinstance(env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", env)):
                    raise ValueError("credentials require an environment variable reference")
            names = set()
            for tool in server["tools"].values():
                if set(tool) != {"remote_tool_name", "enabled", "capability_id", "version", "action_level",
                                 "input_schema", "output_schema", "timeout_ms"}:
                    raise ValueError("unknown or missing MCP tool settings")
                if not all(isinstance(tool[key], str) and 0 < len(tool[key]) <= 128
                           for key in ("remote_tool_name", "capability_id", "version")):
                    raise ValueError("invalid MCP tool identity")
                if tool["remote_tool_name"] in names:
                    raise ValueError("duplicate remote tool mapping")
                names.add(tool["remote_tool_name"])
                if (type(tool["enabled"]) is not bool or tool["action_level"] not in {"read", "propose_write"}
                        or type(tool["timeout_ms"]) is not int or not 50 <= tool["timeout_ms"] <= 30000):
                    raise ValueError("invalid MCP execution policy")
                for key in ("input_schema", "output_schema"):
                    _schema(tool[key])
        return config

    def _resolve(self, server_id, tool_id, context):
        if not isinstance(context, MCPContext) or not all(isinstance(v, str) and v for v in
                (context.tenant_id, context.principal_id, context.run_context_id, context.trace_id)):
            raise Fault("PERMISSION_DENIED", "MCP 调用缺少可信运行上下文", 403)
        context.authorize()
        try:
            config = self._read()
            server = config["servers"].get(server_id)
            tool = None
            if server:
                tool = server["tools"].get(tool_id)
            if tool is None:
                raise Fault("CAPABILITY_NOT_FOUND", "MCP 能力未注册", 404)
            if not server["enabled"] or not tool["enabled"]:
                raise Fault("CAPABILITY_REVOKED", "MCP 能力已停用", 403)
            if context.tenant_id not in server["tenants"]:
                raise Fault("PERMISSION_DENIED", "当前租户不能使用此 MCP 服务", 403)
            if PROTOCOL not in server["approved_protocol_versions"]:
                raise Fault("MCP_PROTOCOL_INCOMPATIBLE", "MCP 协议版本未经批准", 502)
            credential_name = server["tenants"][context.tenant_id]["credential_env"]
            credential = os.getenv(credential_name) if credential_name else None
            if credential_name and (not credential or "\r" in credential or "\n" in credential):
                raise Fault("PERMISSION_DENIED", "MCP服务凭据不可用", 403)
            return config["endpoints"][server["endpoint_ref"]], server, tool
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise Fault("CAPABILITY_REVOKED", "MCP 配置不可用", 403) from exc

    def _scope_key(self, endpoint, server, context):
        credential_name = server["tenants"][context.tenant_id]["credential_env"]
        credential = ""
        if credential_name:
            credential = os.getenv(credential_name, "")
        credential_digest = hashlib.sha256(credential.encode()).hexdigest()
        value = [endpoint, server, context.tenant_id, context.principal_id, credential_digest]
        return hashlib.sha256(_json(value).encode()).hexdigest()

    def binding_digest(self, endpoint, server, tool, context):
        value = [endpoint, server, tool]
        if "p2" in server:
            # P2固定凭据范围；轮换后旧任务和持久结果不能继承新的远端身份。
            value.append(self._scope_key(endpoint, server, context))
        return hashlib.sha256(_json(value).encode()).hexdigest()

    def authorize_reuse(self, server_id, tool_id, context):
        endpoint, server, tool = self._resolve(server_id, tool_id, context)
        state = self.health.get(self._scope_key(endpoint, server, context))
        if state and state["state"] == "incompatible":
            raise Fault("MCP_PROTOCOL_INCOMPATIBLE", "已发现MCP目录或协议不兼容，禁止复用旧结果", 502)
        return endpoint, server, tool

    async def execute(self, server_id, tool_id, arguments, *, context, action_level, input_schema, output_schema,
                      capability_id=None, capability_version=None):
        endpoint, server, _ = self._resolve(server_id, tool_id, context)
        policy = _policy(server)
        options = {
            "context": context,
            "action_level": action_level,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "capability_id": capability_id,
            "capability_version": capability_version,
        }
        if policy is None:
            return await self._execute(server_id, tool_id, arguments, **options)
        key = self._scope_key(endpoint, server, context)
        state = self.health.get(key)
        if state is None:
            if len(self.health) >= 256:
                # 不驱逐活跃或熔断状态，避免通过身份轮换重置保护窗口。
                raise Fault("RATE_LIMITED", "MCP健康记录容量已满，请重启或收敛配置范围", 429)
            state = {"state": "healthy", "failures": 0, "until": 0, "inflight": 0, "requests": deque()}
            self.health[key] = state
        now = time.monotonic()
        if state["state"] == "incompatible":
            raise Fault("MCP_PROTOCOL_INCOMPATIBLE", "MCP配置需操作者重新审核", 502)
        if state["state"] == "half_open" or (state["state"] == "open" and now < state["until"]):
            raise Fault("MCP_CIRCUIT_OPEN", "MCP服务熔断中", 503)
        while state["requests"] and state["requests"][0] <= now - 60:
            state["requests"].popleft()
        if len(state["requests"]) >= policy["rate_per_minute"] or state["inflight"] >= policy["max_concurrency"]:
            raise Fault("RATE_LIMITED", "MCP请求频率或并发已达上限", 429)
        is_probe = state["state"] == "open"
        if is_probe:
            state["state"] = "half_open"
        state["inflight"] += 1
        state["requests"].append(now)
        try:
            result = await self._execute(server_id, tool_id, arguments, **options)
            if state["state"] in {"healthy", "degraded"} or (is_probe and state["state"] == "half_open"):
                state.update(state="healthy", failures=0, until=0)
            return result
        except Fault as exc:
            if exc.code in {"MCP_PROTOCOL_INCOMPATIBLE", "MCP_CATALOG_DRIFT"}:
                state["state"] = "incompatible"
            elif state["state"] != "incompatible" and exc.code in {"UPSTREAM_UNAVAILABLE", "TOOL_TIMEOUT", "RATE_LIMITED", "OUTCOME_UNKNOWN"}:
                state["failures"] += 1
                if is_probe or state["failures"] >= policy["failure_threshold"]:
                    state.update(state="open", until=time.monotonic() + policy["cooldown_s"])
                else:
                    state["state"] = "degraded"
            raise
        finally:
            state["inflight"] -= 1
            if state["state"] == "half_open":
                # 取消或权限拒绝不是成功探测，也不计作新的上游故障。
                state.update(state="open", until=time.monotonic() + policy["cooldown_s"])
            context.trace("mcp_health", {"server_id": server_id, "state": state["state"], "failures": state["failures"]})

    async def inspect_catalog(self, server_id, tool_id, *, context):
        _, _, tool = self._resolve(server_id, tool_id, context)
        return await self._execute(server_id, tool_id, {}, context=context, action_level=tool["action_level"],
                                   input_schema=tool["input_schema"], output_schema=tool["output_schema"], inspect_only=True)

    def _approve_listing(self, listing, endpoint, server, context, server_id, inspect_only):
        tools = []
        for item in listing.tools:
            tools.append({
                "name": item.name,
                "description": item.description,
                "inputSchema": item.input_schema,
                "outputSchema": item.output_schema,
            })
        digest = catalog_digest(tools)
        candidate = {"catalog_digest": digest, "tools": tools}
        if inspect_only:
            return candidate
        policy = _policy(server)
        if policy is None:
            return candidate
        if digest != policy["catalog_digest"]:
            context.trace("mcp_catalog_drift", {"server_id": server_id, "approved_digest": policy["catalog_digest"],
                                               "observed_digest": digest})
            raise Fault("MCP_CATALOG_DRIFT", "MCP目录或Schema发生变化，需操作者重新审核", 502)
        # SDK只保护输入Schema；完整目录仍每次刷新。缓存用于已审核目录快照，不跳过线上比对。
        ttl_ms = getattr(listing, "ttl_ms", 0)
        cache_scope = getattr(listing, "cache_scope", None)
        key = self._scope_key(endpoint, server, context)
        now = time.monotonic()
        for expired in list(self.directories):
            if self.directories[expired]["expires_at"] <= now:
                del self.directories[expired]
        if type(ttl_ms) is int and ttl_ms > 0 and cache_scope in {"private", "public"}:
            ttl = min(ttl_ms / 1000, policy["cache_ttl_s"])
            if ttl > 0:
                self.directories[key] = {**candidate, "expires_at": now + ttl, "scope": "private"}
                self.directories.move_to_end(key)
                while len(self.directories) > 128:
                    self.directories.popitem(last=False)
        else:
            self.directories.pop(key, None)
        return candidate

    async def _execute(self, server_id, tool_id, arguments, *, context, action_level, input_schema, output_schema,
                      capability_id=None, capability_version=None, inspect_only=False):
        endpoint, server, tool = self._resolve(server_id, tool_id, context)
        if (tool["action_level"] != action_level or tool["input_schema"] != input_schema
                or tool["output_schema"] != output_schema
                or (capability_id is not None and tool["capability_id"] != capability_id)
                or (capability_version is not None and tool["version"] != capability_version)):
            raise Fault("MCP_PROTOCOL_INCOMPATIBLE", "MCP 本地能力声明与批准映射不符", 502)
        if not inspect_only:
            _validate(arguments, input_schema, "SCHEMA_VALIDATION_FAILED")
        expected = _json([endpoint, server, tool])
        expected_binding = self.binding_digest(endpoint, server, tool, context)
        # 同一任务首次使用时固定配置摘要，后续调用不能悄然切换端点或工具映射。
        context.pin_configuration(f"{server_id}/{tool_id}", expected_binding)
        def guard():
            current_endpoint, current_server, current_tool = self._resolve(server_id, tool_id, context)
            current = [current_endpoint, current_server, current_tool]
            if _json(current) != expected:
                raise Fault("CAPABILITY_REVOKED", "MCP 配置在调用期间发生变更", 403)
            current_binding = self.binding_digest(current_endpoint, current_server, current_tool, context)
            if current_binding != expected_binding:
                raise Fault("CAPABILITY_REVOKED", "MCP 配置在调用期间发生变更", 403)

        from mcp import Client, types
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared.exceptions import MCPError
        import httpx2

        credential_name = server["tenants"][context.tenant_id]["credential_env"]
        secret = os.getenv(credential_name) if credential_name else None
        if credential_name and (not secret or "\r" in secret or "\n" in secret):
            raise Fault("PERMISSION_DENIED", "MCP 服务凭据不可用", 403)
        # 关联标签只传递摘要，不暴露原始身份或凭据。
        identity_fields = {
            "tenant": context.tenant_id,
            "principal": context.principal_id,
            "run": context.run_context_id,
            "trace": context.trace_id,
        }
        tags = {}
        headers = {}
        for key, value in identity_fields.items():
            identity_digest = hashlib.sha256(value.encode()).hexdigest()
            tags[key] = identity_digest
            headers["X-Agent-" + key.title()] = identity_digest
        if secret:
            headers["Authorization"] = "Bearer " + secret
        url = endpoint["url"]
        violations = []

        class BoundedStream(httpx2.AsyncByteStream):
            def __init__(self, wrapped):
                self.wrapped = wrapped

            async def __aiter__(self):
                size = 0
                async for chunk in self.wrapped:
                    size += len(chunk)
                    if size > MAX_RESPONSE:
                        fault = Fault("TOOL_OUTPUT_INVALID", "MCP 响应超过大小限制", 502)
                        violations.append(fault)
                        raise fault
                    yield chunk

            async def aclose(self):
                await self.wrapped.aclose()

        class ApprovedTransport(httpx2.AsyncBaseTransport):
            def __init__(self):
                self.transport = httpx2.AsyncHTTPTransport(trust_env=False, retries=0)

            async def handle_async_request(self, request):
                try:
                    guard()
                    if str(request.url) != url:
                        raise Fault("MCP_PROTOCOL_INCOMPATIBLE", "MCP 重定向或地址变更被阻止", 502)
                    response = await self.transport.handle_async_request(request)
                    if response.status_code in (301, 302, 303, 307, 308):
                        await response.aclose()
                        raise Fault("MCP_PROTOCOL_INCOMPATIBLE", "MCP 重定向被阻止", 502)
                    if response.status_code == 429:
                        delay = response.headers.get("retry-after", "")
                        await response.aclose()
                        fault = Fault("RATE_LIMITED", "MCP 上游限流", 429, action_level == "read")
                        if delay.isascii() and delay.isdecimal():
                            fault.retry_after = int(delay) if len(delay) <= 9 else 1000000000
                        raise fault
                    if response.status_code in (401, 403):
                        await response.aclose()
                        raise Fault("PERMISSION_DENIED", "MCP 服务拒绝访问", 403)
                    if response.status_code >= 500:
                        await response.aclose()
                        code = "UPSTREAM_UNAVAILABLE"
                        if sent and action_level != "read":
                            code = "OUTCOME_UNKNOWN"
                        raise Fault(code, "MCP 上游不可用", 503, action_level == "read")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        await response.aclose()
                        raise Fault("TOOL_OUTPUT_INVALID", "MCP 压缩响应不在批准协议内", 502)
                    response.stream = BoundedStream(response.stream)
                    return response
                except Fault as exc:
                    violations.append(exc)
                    raise
                except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
                    code = "UPSTREAM_UNAVAILABLE"
                    if isinstance(exc, httpx2.TimeoutException):
                        code = "TOOL_TIMEOUT"
                    if sent and action_level != "read":
                        code = "OUTCOME_UNKNOWN"
                    violations.append(Fault(code, "MCP 网络请求未完成", 503, action_level == "read"))
                    raise

            async def aclose(self):
                await self.transport.aclose()

        started = time.monotonic()
        # 有副作用的调用发出后遇到网络故障，无法确定远端结果，不能当作普通可重试失败。
        sent = False
        protocol = None
        log_scope = _protected_sdk_logs.set(True)
        try:
            # 按顺序进入超时、HTTP连接和MCP会话，退出时仍按相反顺序释放。
            async with (
                asyncio.timeout(tool["timeout_ms"] / 1000),
                httpx2.AsyncClient(
                    transport=ApprovedTransport(),
                    headers=headers,
                    timeout=tool["timeout_ms"] / 1000,
                    trust_env=False,
                    follow_redirects=False,
                ) as http,
                Client(
                    streamable_http_client(url, http_client=http, max_sse_event_size=MAX_RESPONSE),
                    mode=PROTOCOL,
                    read_timeout_seconds=tool["timeout_ms"] / 1000,
                    cache=None,
                ) as client,
            ):
                discovered = types.DiscoverResult.model_validate(await client.session.send_discover(PROTOCOL))
                if PROTOCOL not in discovered.supported_versions:
                    raise Fault("MCP_PROTOCOL_INCOMPATIBLE", "MCP 服务不支持批准的版本", 502)
                client.session.adopt(discovered)
                protocol = client.protocol_version
                if protocol not in server["approved_protocol_versions"] or not client.server_capabilities.tools:
                    raise Fault("MCP_PROTOCOL_INCOMPATIBLE", "MCP 协议或工具能力不兼容", 502)
                listing = await client.list_tools()
                if listing.next_cursor is not None or len(listing.tools) > 128:
                    raise Fault("MCP_PROTOCOL_INCOMPATIBLE", "MCP 工具列表超出批准范围", 502)
                names = [item.name for item in listing.tools]
                if len(names) != len(set(names)):
                    raise Fault("MCP_PROTOCOL_INCOMPATIBLE", "MCP 工具名称冲突", 502)
                candidate = self._approve_listing(listing, endpoint, server, context, server_id, inspect_only)
                if inspect_only:
                    guard()
                    return candidate
                remote = None
                for listed_tool in listing.tools:
                    if listed_tool.name == tool["remote_tool_name"]:
                        remote = listed_tool
                        break
                if remote is None:
                    raise Fault("CAPABILITY_NOT_FOUND", "批准的 MCP 工具不可用", 404)
                # 远端声明不能放宽本地约束；SDK 和本地网关分别校验输出。
                for schema in (remote.input_schema, remote.output_schema):
                    if schema is not None:
                        _schema(schema)
                # 远端Schema包含正则等可耗尽CPU的规则，必须在可回收进程中执行。
                if not await is_valid_async(remote.input_schema, arguments):
                    raise Fault("SCHEMA_VALIDATION_FAILED", "MCP 数据与批准结构不符", 422)
                async def validate_bounded_result(name, result):
                    # 接管SDK的同步校验入口，保留原Schema语义并先限制结构。
                    from .result_store import _bounded
                    if result.structured_content is not None:
                        _bounded(result.structured_content)
                    if remote.output_schema is None:
                        return
                    if result.structured_content is None and "structured_content" not in result.model_fields_set:
                        raise Fault("TOOL_OUTPUT_INVALID", "MCP 工具未返回结构化结果", 502)
                    if not await is_valid_async(remote.output_schema, result.structured_content, dialect="auto"):
                        raise Fault("TOOL_OUTPUT_INVALID", "MCP 数据与批准结构不符", 502)
                client.session.validate_tool_result = validate_bounded_result
                guard()
                sent = True
                # 低层调用避免自动重发和补充输入循环，只读重试统一由 Runtime 控制。
                result = await client.session.call_tool(tool["remote_tool_name"], arguments,
                    meta=types.RequestParamsMeta(progress_token=tags["trace"]))
                if result.is_error:
                    raise Fault("UPSTREAM_DEPENDENCY_FAILED", "MCP 工具返回业务错误", 424)
                payload = result.structured_content
                if payload is None:
                    if len(result.content) != 1 or result.content[0].type != "text":
                        raise Fault("TOOL_OUTPUT_INVALID", "MCP 工具未返回结构化结果", 502)
                    from .catalog import _reject_duplicate_keys
                    payload = json.loads(result.content[0].text, object_pairs_hook=_reject_duplicate_keys)
                from .catalog import ENVELOPE
                _validate(payload, ENVELOPE, "TOOL_OUTPUT_INVALID")
                _validate(payload["data"], output_schema, "TOOL_OUTPUT_INVALID")
                if secret and _json(secret)[1:-1] in _json(payload):
                    raise Fault("SENSITIVE_OUTPUT", "MCP 返回包含访问凭据", 502)
                guard()
                return payload
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            leaves = _leaves(exc)
            # 本地传输层记录的拒绝优先于 SDK 包装异常，避免丢失权限或限流错误。
            for error in [*violations, *leaves]:
                if isinstance(error, Fault):
                    raise error from None
            if any(isinstance(e, (TimeoutError, httpx2.TimeoutException)) for e in leaves):
                code = "TOOL_TIMEOUT"
                if sent and action_level != "read":
                    code = "OUTCOME_UNKNOWN"
                raise Fault(code, "MCP 请求超时", 504, action_level == "read") from None
            if any(isinstance(e, (httpx2.NetworkError, httpx2.HTTPStatusError)) for e in leaves):
                code = "UPSTREAM_UNAVAILABLE"
                if sent and action_level != "read":
                    code = "OUTCOME_UNKNOWN"
                raise Fault(code, "MCP 连接不可用", 503, action_level == "read") from None
            if any(isinstance(e, MCPError) for e in leaves) and not sent:
                raise Fault("MCP_PROTOCOL_INCOMPATIBLE", "MCP 协议协商失败", 502) from None
            code = "MCP_PROTOCOL_INCOMPATIBLE"
            if sent:
                code = "TOOL_OUTPUT_INVALID"
            raise Fault(code, "MCP 返回协议无效", 502) from None
        finally:
            _protected_sdk_logs.reset(log_scope)
            context.trace("mcp_execution", {"server_id": server_id, "capability_id": tool["capability_id"],
                "capability_version": tool["version"], "protocol_version": protocol,
                "duration_ms": round((time.monotonic() - started) * 1000)})
