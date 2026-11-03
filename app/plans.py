"""Prepare reviewed, scenario-local read-only plans for the durable executor."""
import copy
import json
import re
from pathlib import Path
from typing import Protocol

from jsonschema import Draft202012Validator

from .catalog import ID, _object, _reject_duplicate_keys
from .errors import Fault


BUDGET_LIMITS = {"max_steps": 12, "max_parallel_steps": 3, "max_tool_calls": 12,
                 "deadline_ms": 900_000}
MAX_EDGES = 24
MAX_DAG_DEPTH = 6
STEP = _object({
    "step_id": ID, "capability_id": ID, "depends_on": {"type": "array", "items": ID, "uniqueItems": True},
    "arguments": {"type": "object"}, "required": {"type": "boolean"},
}, ["step_id", "capability_id", "arguments"])
PLAN = _object({"schema_version": {"const": "2.0"}, "result_contract": {"const": "summary_v1"},
                "steps": {"type": "array", "minItems": 1, "items": STEP}})


def _invalid(reason):
    return Fault("PLAN_INVALID", reason, 422)


def _over_budget(reason):
    return Fault("EXECUTION_BUDGET_EXCEEDED", reason, 429)


class PlanProvider(Protocol):
    def propose_plan(self, catalog, scene_id: str) -> dict: ...


class MockPlanProvider:
    """Read a trusted server-side template. No prompt or client field selects a file."""

    def propose_plan(self, catalog, scene_id):
        root = Path(catalog.root).resolve()
        directory = (root / scene_id).resolve()
        path = (directory / "plan.json").resolve()
        if directory.parent != root or path.parent != directory:
            raise _invalid("计划文件超出场景目录。")
        try:
            with path.open("rb") as stream:
                raw = stream.read(131073)
            if len(raw) > 131072:
                raise _over_budget("场景计划文件超过大小上限。")
            return json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys,
                              parse_constant=_reject_nonfinite)
        except FileNotFoundError:
            raise Fault("PLAN_NOT_FOUND", "此场景未配置 durable_plan 模板。", 404) from None
        except (OSError, ValueError, RecursionError):
            raise _invalid("无法读取有效的场景 JSON 计划。") from None


def _reject_nonfinite(value):
    raise ValueError("non-finite JSON constant")


def _budget(request):
    if request is None:
        return BUDGET_LIMITS.copy()
    if not isinstance(request, dict) or request.keys() - BUDGET_LIMITS.keys():
        raise _over_budget("包含未知的预算字段。")
    for name, value in request.items():
        if type(value) is not int or not 1 <= value <= BUDGET_LIMITS[name]:
            raise _over_budget(f"{name} 超出服务端允许的范围。")
    return BUDGET_LIMITS | request


def _order(steps):
    """Stable topological order, with all transitive ancestors for reference checks."""
    ids = {s["step_id"] for s in steps}
    if len(ids) != len(steps) or any(set(s["depends_on"]) - ids for s in steps):
        raise _invalid("步骤 ID 重复或引用了不存在的依赖。")
    if sum(len(s["depends_on"]) for s in steps) > MAX_EDGES:
        raise _over_budget("计划依赖边过多。")
    ordered, ancestors, depths = [], {}, {}
    while len(ordered) < len(steps):
        for step in steps:
            sid, deps = step["step_id"], step["depends_on"]
            if sid in ancestors or not all(d in ancestors for d in deps):
                continue
            depth = 1 + max((depths[d] for d in deps), default=0)
            if depth > MAX_DAG_DEPTH:
                raise _over_budget("计划依赖链超过深度上限。")
            depths[sid] = depth
            ancestors[sid] = set(deps).union(*(ancestors[d] for d in deps))
            ordered.append(step)
            break
        else:
            raise Fault("PLAN_CYCLE_DETECTED", "步骤之间存在循环依赖。", 422)
    return ordered, ancestors


def _pointer(path):
    if (not isinstance(path, str) or len(path) > 1024 or not path.startswith("/data/")
            or re.search(r"~(?![01])", path)):
        raise _invalid("输出引用须使用指向 data 内已声明字段的 JSON Pointer。")
    parts = [p.replace("~1", "/").replace("~0", "~") for p in path.split("/")[2:]]
    if len(parts) > 16 or any(p in {"", ".", "..", "*"} or p.startswith("$") for p in parts):
        raise _invalid("输出引用路径无效。")
    return parts


def _declared_output(schema, parts):
    for part in parts:
        if schema["type"] == "object":
            schema = schema.get("properties", {}).get(part)
        elif schema["type"] == "array" and re.fullmatch(r"0|[1-9][0-9]{0,8}", part):
            schema = schema["items"] if int(part) < schema.get("maxItems", 1_000_000_000) else None
        else:
            schema = None
        if not schema:
            raise _invalid("输出引用路径未在前置工具的输出 Schema 中声明。")
    return schema


def _prepare_arguments(step, tools, ancestors, step_tools, message):
    """Check literals now and retain typed predecessor references for runtime validation."""
    refs = set()
    tool = tools[step["capability_id"]]
    inputs = tool["input_schema"]

    def walk(value, schema, location):
        if len(location) > 16:
            raise _invalid("参数模板嵌套过深。")
        if isinstance(value, dict):
            if any(k.startswith("$") for k in value):
                if set(value) == {"$message"} and value["$message"] is True:
                    return message
                if set(value) != {"$step_output", "path"}:
                    raise _invalid("参数模板包含未知或不完整的指令。")
                source = value["$step_output"]
                if not isinstance(source, str) or source not in ancestors:
                    raise _invalid("步骤仅可引用自身的前置依赖。")
                source_schema = _declared_output(step_tools[source]["output_schema"], _pointer(value["path"]))
                types = (source_schema["type"], schema.get("type"))
                if types[0] != types[1] and types != ("integer", "number"):
                    raise _invalid("引用字段与目标输入类型不匹配。")
                refs.add(location)
                return value
            props = schema.get("properties", {})
            return {k: walk(v, props.get(k, {}), location + (k,)) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, schema.get("items", {}), location + (i,)) for i, v in enumerate(value)]
        return value

    arguments = walk(step["arguments"], inputs, ())
    if not isinstance(arguments, dict):
        raise _invalid("步骤输入必须是对象。")
    for err in Draft202012Validator(inputs).iter_errors(arguments):
        location = tuple(err.absolute_path)
        if not any(location[:len(ref)] == ref for ref in refs):
            raise _invalid(f"步骤 {step['step_id']} 的输入不符合 {err.validator} 校验。")
    field = tool.get("object_ids_field")
    if field and any(not ref or ref[0] == field for ref in refs):
        raise _invalid("对象访问范围不能由前置工具输出决定。")
    _check_size(arguments)
    return arguments


def _check_size(arguments):
    try:
        size = len(json.dumps(arguments, ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError, RecursionError):
        raise _invalid("步骤参数必须为有限、可序列化的 JSON 值。") from None
    if size > 32000:
        raise _over_budget("步骤参数超过大小上限。")
    return size


def prepare_plan(catalog, scene_id, message, permissions, context, *, budget=None):
    budget = _budget(budget)
    tools = {tool["name"]: tool for tool in catalog.discover(scene_id)}
    plan = MockPlanProvider().propose_plan(catalog, scene_id)
    if not isinstance(message, str) or not 1 <= len(message) <= 16000:
        raise _invalid("任务文本长度无效。")
    if not Draft202012Validator(PLAN).is_valid(plan):
        raise _invalid("计划 Schema、字段或结果类型无效。")
    if len(plan["steps"]) > min(budget["max_steps"], budget["max_tool_calls"]):
        raise _over_budget("计划步骤数超出步骤或工具调用预算。")
    for step in plan["steps"]:
        step.setdefault("depends_on", [])
        step.setdefault("required", True)
    ordered, ancestors = _order(plan["steps"])
    step_tools = {}
    for step in ordered:
        sid = step["step_id"]
        tool = tools.get(step["capability_id"])
        if not tool:
            raise _invalid("计划引用了场景之外的能力。")
        if tool["permission"] not in permissions:
            raise Fault("PERMISSION_DENIED", "当前身份没有计划所需的能力权限。", 403)
        if tool["action_level"] != "read":
            raise Fault("ACTION_NOT_ALLOWED", "持久化计划仅支持只读能力。", 403)
        args = _prepare_arguments(step, tools, ancestors[sid], step_tools, message)
        field = tool.get("object_ids_field")
        if field and (not args.get(field) or not set(args[field]).issubset(context.get("object_ids", []))):
            raise Fault("PERMISSION_DENIED", "请求的对象超出了任务授权范围。", 403)
        step["arguments"] = args
        step_tools[sid] = tool
    plan["steps"] = ordered
    return plan, budget


def resolve_arguments(step, outputs):
    """Resolve a validated, persisted template. Never interpret returned data as instructions."""
    expanded_size = 0

    def resolve(value):
        nonlocal expanded_size
        if isinstance(value, dict):
            if "$step_output" in value:
                try:
                    node = outputs[value["$step_output"]]["data"]
                    for part in _pointer(value["path"]):
                        if isinstance(node, list):
                            if not re.fullmatch(r"0|[1-9][0-9]{0,8}", part):
                                raise ValueError("invalid array index")
                            node = node[int(part)]
                        else:
                            node = node[part]
                    expanded_size += _check_size(node)
                    if expanded_size > 32000:
                        raise _over_budget("引用数据展开后超过步骤大小上限。")
                    return copy.deepcopy(node)
                except (KeyError, IndexError, ValueError, TypeError):
                    raise Fault("STEP_OUTPUT_UNAVAILABLE", "依赖步骤未返回计划声明的字段。", 422) from None
            return {k: resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v) for v in value]
        return value

    args = resolve(step["arguments"])
    if not isinstance(args, dict):
        raise _invalid("解析后的步骤参数必须是对象。")
    _check_size(args)
    return args
