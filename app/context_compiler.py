"""构建有预算、可追溯的模型输入；不保存任务执行状态或授予工具权限。"""
from dataclasses import dataclass
import hashlib
import json
import secrets
import time
from typing import Any, Sequence

from .errors import Fault


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ContextItem:
    item_id: str
    scope: tuple[str, ...]
    snapshot_id: str | None
    source_kind: str
    trust_tier: str
    message_json: str
    pinned: bool
    expires_at: float
    summary_json: str | None = None


@dataclass(frozen=True)
class CompiledContext:
    messages_json: tuple[str, ...]
    pinned_item_ids: tuple[str, ...]
    omitted_item_ids: tuple[str, ...]
    summary_refs: tuple[str, ...]
    token_estimate: int
    limitations: tuple[str, ...]


def compile_context(items: Sequence[ContextItem], *, scope: tuple[str, ...], snapshot_id: str | None,
                    input_budget: int, tool_tokens: int, now: float) -> CompiledContext:
    """按字节保守估算，只省略可选历史或替换带完整证据的服务端摘要。"""
    if type(input_budget) is not int or input_budget < 1 or type(tool_tokens) is not int or tool_tokens < 0:
        raise Fault("CONTEXT_INVALID", "上下文预算无效。", 422)
    retained: dict[str, str] = {}
    omitted: list[str] = []
    summaries: list[str] = []
    seen: set[str] = set()
    allowed_roles = {"system_policy": {"system"}, "task_state": {"user"}, "skill_instruction": {"user"},
                     "user_message": {"user"}, "history": {"user", "assistant"},
                     "tool_call": {"assistant"}, "tool_result": {"tool"}}
    for entry in items:
        if entry.scope != scope or entry.snapshot_id != snapshot_id:
            raise Fault("PERMISSION_DENIED", "上下文来源不属于当前任务范围。", 403)
        if entry.item_id in seen:
            raise Fault("CONTEXT_INVALID", "上下文引用重复。", 422)
        seen.add(entry.item_id)
        for raw in (entry.message_json, entry.summary_json):
            if raw is None:
                continue
            message = json.loads(raw)
            role = message.get("role")
            if role not in allowed_roles.get(entry.source_kind, set()):
                raise Fault("CONTEXT_INVALID", "上下文来源与消息角色不匹配。", 422)
            if role == "system" and entry.trust_tier != "trusted_config":
                raise Fault("CONTEXT_INVALID", "外部内容不能成为系统策略。", 422)
        if entry.expires_at <= now:
            if entry.pinned:
                raise Fault("CONTEXT_SOURCE_EXPIRED", "必需上下文已过期。", 410)
            omitted.append(entry.item_id)
            continue
        retained[entry.item_id] = entry.message_json

    # 不借用厂商未核验的分词器；UTF-8 字节数加封装余量仅是保守的本地估计。
    total = tool_tokens + sum(len(raw.encode("utf-8")) + 32 for raw in retained.values())
    for entry in items:
        if total <= input_budget:
            break
        if entry.item_id not in retained or entry.summary_json is None:
            continue
        saved = len(retained[entry.item_id].encode("utf-8")) - len(entry.summary_json.encode("utf-8"))
        if saved > 0:
            retained[entry.item_id] = entry.summary_json
            total -= saved
            summaries.append(entry.item_id)
    for entry in items:
        if total <= input_budget:
            break
        if entry.pinned or entry.item_id not in retained:
            continue
        total -= len(retained.pop(entry.item_id).encode("utf-8")) + 32
        omitted.append(entry.item_id)
    if total > input_budget:
        raise Fault("CONTEXT_BUDGET_EXCEEDED", "必需上下文超出预算，未调用模型。", 422)
    limitations = []
    if omitted:
        limitations.append("部分历史或过期内容未进入本轮模型输入。")
    if summaries:
        limitations.append("部分工具明细以受控引用代替，证据与摘要已保留。")
    pinned = tuple(entry.item_id for entry in items if entry.pinned)
    return CompiledContext(tuple(retained.values()), pinned, tuple(omitted), tuple(summaries), total,
                           tuple(limitations))


class ContextSession:
    """仅在一次请求内保存输入副本；每次编译和引用读取重新核对任务授权。"""

    def __init__(self, gateway: Any, settings: dict, tools: list[dict]):
        self.gateway = gateway
        context = gateway.context()
        self.scope = tuple(context[key] for key in ("tenant_id", "org_id", "user_id", "business_id", "task_id"))
        self.snapshot_id = gateway.snapshot_id
        self.expires_at = context["expires_at"]
        self.items: list[ContextItem] = []
        self.sources: list[tuple[str, str]] = []
        self.bytes_stored = 0
        self.limitations: list[str] = []
        values = []
        for key, default in (("context_window_tokens", 65536), ("context_output_reserve", 2048),
                             ("context_safety_margin", 1024)):
            value = settings.get(key, default)
            if type(value) is not int or not 1 <= value <= 1_000_000:
                raise Fault("MODEL_NOT_CONFIGURED", "上下文预算配置无效。", 503)
            values.append(value)
        self.input_budget = values[0] - values[1] - values[2]
        if self.input_budget < 1:
            raise Fault("MODEL_NOT_CONFIGURED", "上下文预留超过本地窗口预算。", 503)
        self.tool_tokens = len(_json(tools).encode("utf-8")) + 128

    def add(self, message: dict, source_kind: str, pinned: bool, *, summarize: bool = False,
            capability: str | None = None, arguments: dict | None = None) -> str:
        raw = _json(message)
        self.bytes_stored += len(raw.encode("utf-8"))
        if self.bytes_stored > 4 * 1024 * 1024 or len(self.items) >= 128:
            raise Fault("CONTEXT_BUDGET_EXCEEDED", "请求内上下文存储已达上限。", 422)
        ref = "ctx_" + secrets.token_hex(16)
        tier = "untrusted_data"
        if source_kind in ("system_policy", "task_state"):
            tier = "trusted_config"
        elif source_kind == "skill_instruction":
            tier = "approved_instruction"
        elif source_kind == "user_message":
            tier = "user_input"
        summary = None
        if summarize:
            result = json.loads(message["content"])
            if isinstance(result, dict) and isinstance(result.get("data"), dict):
                compact_data = {}
                if "summary" in result["data"]:
                    compact_data["summary"] = result["data"]["summary"]
                compact = {"data": compact_data, "evidence": result.get("evidence", []),
                           "context_ref": ref, "limitations": ["工具明细未全部进入上下文，可按引用读取。"]}
                if "result_ref" in result:
                    compact["result_ref"] = result["result_ref"]
                    compact["result_digest"] = result["result_digest"]
                    compact["truncated"] = True
                summary = _json({**message, "content": _json(compact)})
        self.items.append(ContextItem(ref, self.scope, self.snapshot_id, source_kind, tier, raw, pinned,
                                      self.expires_at, summary))
        if capability is not None:
            self.sources.append((capability, _json(arguments or {})))
        return ref

    def _authorized_context(self) -> dict:
        """缓存不能延长原工具授权；同时复核固定版本、当前版本与对象范围。"""
        current = self.gateway.context()
        scope = tuple(current[key] for key in ("tenant_id", "org_id", "user_id", "business_id", "task_id"))
        if scope != self.scope or self.gateway.snapshot_id != self.snapshot_id:
            raise Fault("PERMISSION_DENIED", "上下文引用不属于当前任务。", 403)
        for name, arguments in self.sources:
            self.gateway._authorize_result_source(name, json.loads(arguments))
        return current

    def read(self, ref: str) -> str:
        self._authorized_context()
        for entry in self.items:
            if entry.item_id != ref:
                continue
            if entry.expires_at <= time.time():
                raise Fault("CONTEXT_SOURCE_EXPIRED", "上下文引用已过期。", 410)
            # 调用者将读取结果作为不带摘要的工具响应加入本轮，放不下就失败。
            return json.loads(entry.message_json).get("content", "")
        raise Fault("PERMISSION_DENIED", "上下文引用不可用。", 403)

    def compile(self) -> CompiledContext:
        current = self._authorized_context()
        scope = tuple(current[key] for key in ("tenant_id", "org_id", "user_id", "business_id", "task_id"))
        result = compile_context(self.items, scope=scope, snapshot_id=self.gateway.snapshot_id,
                                 input_budget=self.input_budget, tool_tokens=self.tool_tokens, now=time.time())
        for limitation in result.limitations:
            if limitation not in self.limitations:
                self.limitations.append(limitation)
        report = {"compiler_version": "1.0", "budget_basis": "operator_conservative",
                  "input_budget": self.input_budget, "token_estimate": result.token_estimate,
                  "omitted_item_ids": result.omitted_item_ids, "summary_refs": result.summary_refs,
                  "pinned_item_ids": result.pinned_item_ids,
                  "source_digests": [hashlib.sha256(entry.message_json.encode()).hexdigest() for entry in self.items]}
        self.gateway.store.audit("context.compile", "allow", current, current["task_id"], _json(report))
        return result
