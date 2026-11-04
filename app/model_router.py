"""可信模型画像和有界请求切换；不管理业务执行状态。"""
import asyncio
import copy
from decimal import Decimal
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any

import httpx

from .errors import Fault
from .schema_validation import is_valid


FEATURES = {
    "supports_tool_calling", "supports_structured_output", "supports_streaming",
    "supports_parallel_tool_calls", "supports_usage_reporting",
}
PROFILE_FIELDS = FEATURES | {
    "provider_id", "model_ref", "base_url", "credential_env", "enabled", "profile_version",
    "verified_at", "expires_at", "verification_kind", "evidence_digest", "context_window_tokens",
    "max_output_tokens", "data_handling_class", "region", "retention_class",
    "input_usd_per_million", "output_usd_per_million",
}
ROUTE_FIELDS = {
    "allowed_providers", "data_class", "allowed_regions", "allowed_retention_classes", "stream",
    "max_attempts", "max_total_tokens", "max_cost_usd", "timeout_s", "result_max_age_s",
}
FINAL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["answer", "limitations", "unresolved_items"],
    "properties": {
        "answer": {"type": "string", "minLength": 1, "maxLength": 16000},
        "limitations": {"type": "array", "maxItems": 16,
                       "items": {"type": "string", "minLength": 1, "maxLength": 1024}},
        "unresolved_items": {"type": "array", "maxItems": 16,
                            "items": {"type": "string", "minLength": 1, "maxLength": 1024}},
    },
}


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_encoded(value).encode("utf-8")).hexdigest()


def _json(raw):
    from .providers import _model_object, _invalid_constant
    return json.loads(raw, object_pairs_hook=_model_object, parse_constant=_invalid_constant)


def _invalid():
    return Fault("MODEL_INVALID_RESPONSE", "模型响应未满足批准协议。", 502)


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", value) is not None


def _number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _labels(value):
    return isinstance(value, list) and 0 < len(value) <= 32 and all(_identifier(item) for item in value)


def load_policy(settings):
    """配置只能来自可信设置或指定文件；每次读取拒绝重复键和未知字段。"""
    source = settings.get("provider_policy")
    try:
        if source is None:
            filename = os.getenv("AGENT_PROVIDER_POLICY")
            if not filename:
                return None
            with Path(filename).open("rb") as stream:
                raw = stream.read(262145)
        else:
            raw = _encoded(source).encode("utf-8")
        if len(raw) > 262144:
            raise ValueError()
        policy = _json(raw)
        if not isinstance(policy, dict) or set(policy) != {"version", "profiles", "routes"}:
            raise ValueError()
        if not _identifier(policy["version"]):
            raise ValueError()
        profiles, routes = policy["profiles"], policy["routes"]
        if not isinstance(profiles, dict) or not 1 <= len(profiles) <= 32:
            raise ValueError()
        if not isinstance(routes, dict) or not 1 <= len(routes) <= 128:
            raise ValueError()
        from .providers import _configuration
        for name, profile in profiles.items():
            if not _identifier(name) or not isinstance(profile, dict) or set(profile) != PROFILE_FIELDS:
                raise ValueError()
            if profile["provider_id"] != name or type(profile["enabled"]) is not bool:
                raise ValueError()
            for field in FEATURES:
                if type(profile[field]) is not bool and profile[field] != "unknown":
                    raise ValueError()
            for field in ("profile_version", "region", "retention_class"):
                if not _identifier(profile[field]):
                    raise ValueError()
            if not _labels(profile["data_handling_class"]):
                raise ValueError()
            if profile["verification_kind"] not in {"mock", "operator_verified"}:
                raise ValueError()
            if not isinstance(profile["evidence_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", profile["evidence_digest"]):
                raise ValueError()
            for field in ("verified_at", "expires_at"):
                if not _number(profile[field], 0, 2**40):
                    raise ValueError()
            for field in ("context_window_tokens", "max_output_tokens"):
                if type(profile[field]) is not int or not 1 <= profile[field] <= 1000000:
                    raise ValueError()
            for field in ("input_usd_per_million", "output_usd_per_million"):
                if not _number(profile[field], 0, 1000000):
                    raise ValueError()
            credential = profile["credential_env"]
            if not isinstance(credential, str) or credential and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", credential):
                raise ValueError()
            # 验证端点格式时用占位符，真正凭据仅在选定候选后读取。
            _configuration({"base_url": profile["base_url"], "model": profile["model_ref"], "api_key": "configured"})
            if profile["verification_kind"] == "mock":
                host = httpx.URL(profile["base_url"]).host
                if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
                    raise ValueError()
        for business, route in routes.items():
            if not _identifier(business) or not isinstance(route, dict) or set(route) != ROUTE_FIELDS:
                raise ValueError()
            providers = route["allowed_providers"]
            if not _labels(providers) or len(providers) > 8 or len(set(providers)) != len(providers):
                raise ValueError()
            if any(name not in profiles for name in providers):
                raise ValueError()
            if not _identifier(route["data_class"]) or not _labels(route["allowed_regions"]) or not _labels(route["allowed_retention_classes"]):
                raise ValueError()
            if type(route["stream"]) is not bool:
                raise ValueError()
            for field, upper in (("max_attempts", 8), ("max_total_tokens", 32000000), ("result_max_age_s", 3600)):
                if type(route[field]) is not int or not 1 <= route[field] <= upper:
                    raise ValueError()
            if not _number(route["timeout_s"], 0.01, 90) or not _number(route["max_cost_usd"], 0, 10000):
                raise ValueError()
        return policy
    except (OSError, ValueError, TypeError, KeyError, RecursionError, Fault):
        raise Fault("MODEL_NOT_CONFIGURED", "模型策略格式或来源无效。", 503) from None


def _stream_body(raw):
    """整段有界收齐后解析流，不把半截参数交给执行器。"""
    message: dict[str, Any] = {"role": "assistant", "content": ""}
    calls: dict[int, dict[str, Any]] = {}
    usage = None
    finished = None
    done = False
    for frame in raw.replace("\r\n", "\n").split("\n\n"):
        data = []
        for line in frame.splitlines():
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif line and not line.startswith(":"):
                raise _invalid()
        if not data:
            continue
        if done:
            raise _invalid()
        payload = "\n".join(data)
        if payload == "[DONE]":
            done = True
            continue
        chunk = _json(payload)
        if not isinstance(chunk, dict) or not isinstance(chunk.get("choices"), list):
            raise _invalid()
        if chunk.get("usage") is not None:
            if usage is not None:
                raise _invalid()
            usage = chunk["usage"]
        if not chunk["choices"]:
            continue
        if len(chunk["choices"]) != 1 or finished is not None:
            raise _invalid()
        choice = chunk["choices"][0]
        if not isinstance(choice, dict) or choice.get("index") != 0:
            raise _invalid()
        delta = choice.get("delta")
        if not isinstance(delta, dict) or delta.get("role", "assistant") != "assistant" or delta.get("refusal"):
            raise _invalid()
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise _invalid()
            message["content"] += content
        for part in delta.get("tool_calls") or []:
            index = part.get("index")
            if type(index) is not int or not 0 <= index < 32:
                raise _invalid()
            call = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            if part.get("type", "function") != "function":
                raise _invalid()
            if "id" in part:
                if not isinstance(part["id"], str):
                    raise _invalid()
                call["id"] += part["id"]
            function = part.get("function", {})
            if not isinstance(function, dict):
                raise _invalid()
            for field in ("name", "arguments"):
                value = function.get(field, "")
                if not isinstance(value, str):
                    raise _invalid()
                call["function"][field] += value
        if choice.get("finish_reason") is not None:
            finished = choice["finish_reason"]
    if not done or finished is None:
        raise Fault("MODEL_STREAM_INCOMPLETE", "模型流未完整结束。", 502, True)
    if calls:
        if sorted(calls) != list(range(len(calls))):
            raise _invalid()
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    return {"choices": [{"message": message, "finish_reason": finished}], "usage": usage}


def reject_model_credentials(payload, credentials):
    """检查所有固定画像的凭据，包括嵌套JSON文本，阻止它们进入工具存储或其他模型。"""
    secrets = [secret for secret in credentials if secret]
    if not secrets:
        return
    pending = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 32:
            raise _invalid()
        if isinstance(value, dict):
            for key, item in value.items():
                pending.append((key, depth + 1))
                pending.append((item, depth + 1))
        elif isinstance(value, list):
            for item in value:
                pending.append((item, depth + 1))
        elif isinstance(value, str):
            if any(secret in value for secret in secrets):
                raise Fault("SENSITIVE_OUTPUT", "内容含有模型访问凭据，已阻止传递。", 502)
            if value.lstrip().startswith(("{", "[", '"')):
                try:
                    pending.append((_json(value), depth + 1))
                except ValueError:
                    pass


def _reply(body, credentials, profile):
    if not isinstance(body, dict) or not isinstance(body.get("choices"), list) or len(body["choices"]) != 1:
        raise _invalid()
    choice = body["choices"][0]
    if not isinstance(choice, dict) or choice.get("finish_reason") not in {"stop", "tool_calls"}:
        raise _invalid()
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("refusal"):
        raise _invalid()
    reject_model_credentials(message, credentials)
    calls = message.get("tool_calls")
    if calls is not None and not isinstance(calls, list):
        raise _invalid()
    if calls:
        if choice["finish_reason"] != "tool_calls":
            raise _invalid()
        if len(calls) > 1 and profile["supports_parallel_tool_calls"] is not True:
            raise _invalid()
    else:
        if choice["finish_reason"] != "stop":
            raise _invalid()
        answer = message.get("content")
        if not isinstance(answer, str) or not is_valid(FINAL_SCHEMA, _json(answer)):
            raise _invalid()
    return message, body.get("usage")


class ProviderRouter:
    """一次任务的候选和请求预算；业务执行与证据仍归原网关所有。"""

    def __init__(self, settings, gateway, tools):
        self.settings = settings
        self.gateway = gateway
        self.tools = tools
        self.policy = load_policy(settings)
        self.context = gateway.context()
        self.business = self.context["business_id"]
        if self.policy is None or self.business not in self.policy["routes"]:
            raise Fault("MODEL_CAPABILITY_UNAVAILABLE", "业务没有批准的模型路由。", 503)
        self.route = self.policy["routes"][self.business]
        self.keys = {}
        for name, profile in self.policy["profiles"].items():
            self.keys[name] = os.getenv(profile["credential_env"], "") if profile["credential_env"] else ""
        # 网关在任何工具结果入库前使用同一任务冻结的全部凭据检查。
        self.gateway._model_credentials = tuple(self.keys.values())
        self.failed = set()
        self.generation = 0
        self.selected = None
        self.requests = 0
        self.reserved_tokens = 0
        self.reserved_cost = Decimal("0")
        self.rates = (None, None)
        self.results = {}

    def _audit(self, data):
        self.gateway.store.audit("model.attempt", "allow", self.context, self.gateway.task_id, _encoded(data))

    def _guard(self):
        self.gateway.context()
        if load_policy(self.settings) != self.policy:
            raise Fault("MODEL_CAPABILITY_UNAVAILABLE", "任务固定的模型策略已改变。", 503)
        for name, profile in self.policy["profiles"].items():
            current = os.getenv(profile["credential_env"], "") if profile["credential_env"] else ""
            if current != self.keys[name]:
                raise Fault("MODEL_CAPABILITY_UNAVAILABLE", "任务固定的模型凭据已撤销或轮换。", 503)
        for saved in self.results.values():
            if time.monotonic() - saved["at"] > self.route["result_max_age_s"] or saved["freshness"] != self._freshness():
                raise Fault("CONTEXT_SOURCE_EXPIRED", "恢复所需的工具观察结果已过期。", 410)
            content = _json(saved["entry"]["content"])
            if isinstance(content, dict) and "result_ref" in content:
                self.gateway._read_result_page(content["result_ref"], None)

    def _freshness(self):
        runtime = getattr(self.gateway, "runtime", None)
        cache = getattr(runtime, "read_cache", None)
        return _digest(cache.policies if cache else {})

    def remember(self, function, arguments, entry):
        if function not in {"execute_capability", "retrieve_knowledge"}:
            return
        key = _encoded([function, arguments])
        if key not in self.results:
            self.results[key] = {"entry": copy.deepcopy(entry), "at": time.monotonic(), "freshness": self._freshness()}

    def reuse(self, function, arguments, call_id):
        if not self.generation:
            return None
        saved = self.results.get(_encoded([function, arguments]))
        if saved is None:
            return None
        self._guard()
        name = arguments["name"] if function == "execute_capability" else function
        inputs = arguments["arguments"] if function == "execute_capability" else arguments
        self.gateway._authorize_result_source(name, inputs)
        return {**saved["entry"], "tool_call_id": call_id}

    def _eligible(self, profile):
        now = time.time()
        if not profile["enabled"] or not profile["verified_at"] <= now < profile["expires_at"]:
            return False
        if profile["supports_tool_calling"] is not True or profile["supports_structured_output"] is not True:
            return False
        if self.route["stream"] and profile["supports_streaming"] is not True:
            return False
        if profile["credential_env"] and not self.keys[profile["provider_id"]]:
            return False
        return (self.route["data_class"] in profile["data_handling_class"]
                and profile["region"] in self.route["allowed_regions"]
                and profile["retention_class"] in self.route["allowed_retention_classes"])

    async def complete(self, messages, context_session, authorize):
        from .providers import _configuration
        self._guard()
        last_error = Fault("MODEL_CAPABILITY_UNAVAILABLE", "没有满足数据策略与能力要求的模型。", 503)
        unknown_usage = False
        attempts = 0
        for name in self.route["allowed_providers"]:
            profile = self.policy["profiles"][name]
            if name in self.failed or not self._eligible(profile):
                continue
            if attempts >= self.route["max_attempts"]:
                break
            self._guard()
            authorize()
            turn = messages
            limit = profile["context_window_tokens"] - profile["max_output_tokens"] - 1024
            if limit < 1:
                last_error = Fault("CONTEXT_BUDGET_EXCEEDED", "候选模型窗口不足以保留输出与安全余量。", 422)
                continue
            if context_session is not None:
                context_session.input_budget = limit
                try:
                    compiled = context_session.compile()
                except Fault as error:
                    if error.code != "CONTEXT_BUDGET_EXCEEDED":
                        raise
                    last_error = error
                    continue
                turn = [json.loads(raw) for raw in compiled.messages_json]
            estimate = len(_encoded(turn).encode("utf-8")) + len(_encoded(self.tools).encode("utf-8")) + 256
            if estimate > limit:
                last_error = Fault("CONTEXT_BUDGET_EXCEEDED", "必需内容无法容纳于候选模型窗口。", 422)
                continue
            if len(_encoded(turn).encode("utf-8")) > self.settings.get("max_context_bytes", 256000):
                raise Fault("BUDGET_EXCEEDED", "模型输入超过服务端字节预算。", 422)
            reject_model_credentials({"messages": turn, "tools": self.tools}, self.keys.values())
            reserve = estimate + profile["max_output_tokens"]
            input_rate = Decimal(str(profile["input_usd_per_million"]))
            output_rate = Decimal(str(profile["output_usd_per_million"]))
            cost = (estimate * input_rate + profile["max_output_tokens"] * output_rate) / 1000000
            if (self.requests >= 32 or self.reserved_tokens + reserve > self.route["max_total_tokens"]
                    or self.reserved_cost + cost > Decimal(str(self.route["max_cost_usd"]))):
                raise Fault("BUDGET_EXCEEDED", "模型请求预留超过任务预算。", 422)
            self.requests += 1
            attempts += 1
            self.reserved_tokens += reserve
            self.reserved_cost += cost
            if self.selected is not None and self.selected != name:
                self.generation += 1
            self.selected = name
            self.rates = (profile["input_usd_per_million"], profile["output_usd_per_million"])
            url, model, key = _configuration({"base_url": profile["base_url"], "model": profile["model_ref"], "api_key": self.keys[name]})
            payload = {"model": model, "messages": turn, "tools": self.tools, "tool_choice": "auto",
                       "stream": self.route["stream"], "parallel_tool_calls": False,
                       "max_completion_tokens": profile["max_output_tokens"],
                       "response_format": {"type": "json_schema", "json_schema": {
                           "name": "agent_result", "strict": True, "schema": FINAL_SCHEMA}}}
            if self.route["stream"]:
                payload["stream_options"] = {"include_usage": True}
            started = time.monotonic()
            metadata = {"attempt": self.requests, "provider_id": name, "profile_version": profile["profile_version"],
                        "policy_digest": _digest(self.policy), "reserved_tokens": reserve, "reserved_cost_usd": float(cost)}
            try:
                async with (
                    asyncio.timeout(self.route["timeout_s"]),
                    httpx.AsyncClient(headers={"Authorization": "Bearer " + key} if key else {},
                                      transport=self.settings.get("transport"), trust_env=False,
                                      follow_redirects=False, timeout=self.route["timeout_s"]) as client,
                ):
                    async with client.stream("POST", url, json=payload) as upstream:
                        if upstream.status_code != 200:
                            raise Fault("MODEL_UPSTREAM_ERROR", "模型服务请求失败。", 502,
                                        upstream.status_code in {408, 429, 503})
                        raw = bytearray()
                        async for piece in upstream.aiter_bytes():
                            if len(raw) + len(piece) > 256000:
                                raise _invalid()
                            raw.extend(piece)
                body = _stream_body(raw.decode("utf-8")) if self.route["stream"] else _json(raw)
                reply, usage = _reply(body, self.keys.values(), profile)
                self._guard()
                authorize()
                known = False
                prompt = 0
                output = 0
                if profile["supports_usage_reporting"] is True and isinstance(usage, dict):
                    prompt, output = usage.get("prompt_tokens", -1), usage.get("completion_tokens", -1)
                    known = (type(prompt) is int and type(output) is int and 0 <= prompt <= estimate
                             and 0 <= output <= profile["max_output_tokens"]
                             and usage.get("total_tokens") == prompt + output)
                if known:
                    actual_cost = (prompt * input_rate + output * output_rate) / 1000000
                    self.reserved_tokens -= reserve - prompt - output
                    self.reserved_cost -= cost - actual_cost
                    metadata.update(observed_tokens=prompt + output, observed_cost_usd=float(actual_cost))
                metadata.update(outcome="succeeded", usage_known=known, duration_ms=round((time.monotonic() - started) * 1000))
                self._audit(metadata)
                return reply, usage if known and not unknown_usage else None
            except asyncio.CancelledError:
                # 已发送请求仍可能计费；保留预留并记录未知用量，然后继续原取消语义。
                metadata.update(outcome="cancelled", usage_known=False,
                                duration_ms=round((time.monotonic() - started) * 1000))
                self._audit(metadata)
                raise
            except (TimeoutError, httpx.TimeoutException, httpx.RequestError) as error:
                last_error = Fault("MODEL_TIMEOUT" if isinstance(error, (TimeoutError, httpx.TimeoutException)) else "MODEL_CONNECTION_ERROR",
                                   "模型服务暂时不可用。", 504, True)
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                last_error = _invalid()
            except Fault as error:
                last_error = error
            metadata.update(outcome="failed", code=last_error.code, usage_known=False,
                            duration_ms=round((time.monotonic() - started) * 1000))
            self._audit(metadata)
            if not last_error.retryable:
                raise last_error
            unknown_usage = True
            self.failed.add(name)
        raise last_error
