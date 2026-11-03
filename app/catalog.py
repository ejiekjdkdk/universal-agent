"""加载已审核的本地场景，在单个场景的权限范围内调度工具。"""
import asyncio
import copy
from concurrent.futures import Future
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import importlib.util
import inspect
import ipaddress
import json
import ntpath
import re
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator, SchemaError

from app.errors import Fault


_unfinished_threads = ContextVar("unfinished_tool_threads", default=None)


@asynccontextmanager
async def tool_slot(slots):
    await slots.acquire()
    unfinished = []
    token = _unfinished_threads.set(unfinished)
    try:
        yield
    finally:
        _unfinished_threads.reset(token)
        if unfinished:
            # 等待者已取消，仍在执行的线程负责归还本次调用的名额。
            asyncio.gather(*unfinished).add_done_callback(lambda _: slots.release())
        else:
            slots.release()


async def _call_sync(handler, arguments, retrieve):
    unfinished = _unfinished_threads.get()
    if unfinished is None:
        return await asyncio.to_thread(handler, arguments, retrieve)
    # asyncio 的等待对象被取消时，线程可能尚在排队，也可能已进入处理器。
    finished = Future()

    def invoke():
        if finished.set_running_or_notify_cancel():
            try:
                return handler(arguments, retrieve)
            finally:
                finished.set_result(None)

    try:
        return await asyncio.to_thread(invoke)
    except asyncio.CancelledError:
        if not finished.cancel():
            unfinished.append(asyncio.wrap_future(finished))
        raise


def _object(properties, required=None):
    return {"type": "object", "properties": properties, "additionalProperties": False,
            "required": list(properties) if required is None else required}


def _string(limit):
    return {"type": "string", "minLength": 1, "maxLength": limit}


ID = _string(64) | {"pattern": r"^[a-z][a-z0-9_]*$"}
VERSION = _string(64) | {"pattern": r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"}
EVIDENCE = _object({k: _string(limit) for k, limit in
                    (("source_id", 200), ("title", 300), ("snippet", 4000), ("version", 64))})
TOOL = _object({
    "name": ID, "description": _string(1024), "action_level": {"enum": ["read", "propose_write", "commit_write"]},
    "permission": _string(128), "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
    "object_ids_field": _string(128),
    "adapter": {"oneOf": [
        _object({"type": {"const": "python"}, "handler": ID}),
        _object({"type": {"const": "mock_ledger"}}),
        _object({"type": {"const": "http"}, "url": _string(2048), "method": {"enum": ["POST", "GET"]}}),
    ]},
}, ["name", "description", "action_level", "permission", "input_schema", "output_schema", "adapter"])
MANIFEST = _object({
    "id": ID, "name": _string(128), "description": _string(1024), "example": _string(8192),
    "version": VERSION, "tools": {"type": "array", "items": TOOL, "maxItems": 128},
    "offline_tool": {"type": "string", "maxLength": 64}, "documents": _string(128),
    "snapshot_files": {"type": "array", "maxItems": 32, "uniqueItems": True,
                       "items": {"type": "string", "maxLength": 128,
                                 "pattern": "^[A-Za-z0-9_-][A-Za-z0-9_.-]*[.](json|txt|csv|md)$"}},
}, ["id", "name", "description", "example", "version", "tools"])
ENVELOPE = _object({"data": {"type": "object"}, "evidence": {"type": "array", "maxItems": 32, "items": EVIDENCE}})
DOCS = {"type": "array", "maxItems": 4096, "items": EVIDENCE}
SCHEMA_KEYWORDS = {
    "$schema", "title", "description", "type", "properties", "additionalProperties", "required",
    "items", "minItems", "maxItems", "uniqueItems", "minLength", "maxLength", "pattern",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "enum", "const",
    "minProperties", "maxProperties", "default", "examples",
}
PUBLIC_FIELDS = ("name", "description", "input_schema", "output_schema", "action_level", "permission", "object_ids_field")


def _check(value, schema):
    error = next(Draft202012Validator(schema).iter_errors(value), None)
    if error:
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        raise ValueError(f"{location}: violates {error.validator}")


def _check_schema(schema):
    """仅支持内联且声明类型的 Schema，校验过程中不获取外部引用。"""
    if (not isinstance(schema, dict) or set(schema) - SCHEMA_KEYWORDS
            or not isinstance(schema.get("type"), str)):
        raise ValueError("unsupported schema keyword or missing explicit type")
    if schema["type"] == "object":
        if schema.get("additionalProperties") is not False:
            raise ValueError("objects require additionalProperties: false")
        for child in schema.get("properties", {}).values():
            _check_schema(child)
    elif schema["type"] == "array":
        if not isinstance(schema.get("items"), dict):
            raise ValueError("arrays require an explicit item schema")
        _check_schema(schema["items"])


def _local_file(directory, name):
    if hasattr(ntpath, "isreserved"):
        reserved = ntpath.isreserved(name)
    else:
        reserved = PureWindowsPath(name).is_reserved()
    if (not name or name.startswith(".") or name.endswith((" ", ".")) or Path(name).name != name
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or reserved):
        raise ValueError("invalid local filename")
    target = directory / name
    if target.is_symlink() or target.resolve() != directory.resolve() / name or not target.is_file():
        raise ValueError(f"missing or nonlocal file {name}")
    return target


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key}")
        result[key] = value
    return result


def _read_json(directory, name):
    return json.loads(_local_file(directory, name).read_text(encoding="utf-8"),
                      object_pairs_hook=_reject_duplicate_keys)


def _validate_documents(name, documents):
    if name and name.casefold() in {"scenario.json", "plan.json", "adapter.py"}:
        raise ValueError("document collection must be a separate data file")
    _check(documents, DOCS)
    if len({doc["source_id"] for doc in documents}) != len(documents):
        raise ValueError("duplicate source_id")


def _check_url(url):
    parts = urlsplit(url)
    host = parts.hostname or ""
    if (parts.scheme != "https" or not host or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or any(c.isspace() or c in "\\{}" for c in url)):
        raise ValueError("HTTP tools require a fixed HTTPS URL without credentials, query, or fragment")
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise ValueError("invalid port")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host.rstrip(".").endswith((".localhost", ".local")):
            raise ValueError("local HTTP destinations are not allowed")
    else:
        if not ip.is_global:
            raise ValueError("local HTTP destinations are not allowed")


def _terms(text):
    """英文按词匹配，中文使用相邻双字，无需额外分词库。"""
    terms = set()
    for token in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", text.casefold()):
        if len(token) > 1 and "\u3400" <= token[0] <= "\u9fff":
            terms.update(token[index:index+2] for index in range(len(token)-1))
        else:
            terms.add(token)
    return terms


async def _call_http(config, arguments, transport):
    options = {"json": arguments} if config["method"] == "POST" else {"params": arguments}
    try:
        async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=8,
                                     follow_redirects=False) as client:
            async with client.stream(config["method"], config["url"], **options) as response:
                status = response.status_code
                if status == 429:
                    error = Fault("RATE_LIMITED", "下游服务限流", 429, True)
                    retry_after = response.headers.get("Retry-After", "")
                    if retry_after.isascii() and retry_after.isdecimal():
                        # 更长的延迟同样会超出请求预算。
                        seconds = retry_after.lstrip("0") or "0"
                        error.retry_after = int(seconds) if len(seconds) <= 9 else 1_000_000_000
                    elif retry_after:
                        try:
                            when = parsedate_to_datetime(retry_after)
                            error.retry_after = max(0, (when - datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError, OverflowError):
                            pass
                    raise error
                if status in (408, 500, 502, 503, 504):
                    raise Fault("UPSTREAM_UNAVAILABLE", "下游服务暂不可用", 503, True)
                if not 200 <= status < 300:
                    raise Fault("UPSTREAM_DEPENDENCY_FAILED", "下游请求未成功", 424)
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=32768):
                    if len(body) + len(chunk) > 262144:
                        raise Fault("UPSTREAM_INVALID_RESPONSE", "下游响应过大", 502)
                    body.extend(chunk)
        return json.loads(body, object_pairs_hook=_reject_duplicate_keys)
    except httpx.TimeoutException as exc:
        raise Fault("TOOL_TIMEOUT", "下游请求超时", 504, True) from exc
    except httpx.NetworkError as exc:
        raise Fault("UPSTREAM_UNAVAILABLE", "无法连接下游服务", 503, True) from exc
    except (ValueError, httpx.HTTPError, RecursionError) as exc:
        raise Fault("UPSTREAM_INVALID_RESPONSE", "下游未返回有效 JSON", 502) from exc


class Catalog:
    def __init__(self, root: Path, *, http_transport=None, adapter_sources=None):
        self._adapter_sources = adapter_sources
        self.scenarios = {}
        self.entries = self.scenarios
        self._tools = {}
        self._documents = {}
        self._handlers = {}
        self._http_transport = http_transport
        root = self.root = Path(root).resolve()
        if not root.exists():
            return
        if not root.is_dir():
            raise ValueError("the scenario catalog must be a directory")
        for directory in sorted(root.iterdir()):
            if not directory.is_dir() or directory.name.startswith((".", "__")):
                continue
            try:
                if directory.resolve().parent != root.resolve():
                    raise ValueError("scenario directory is outside the catalog")
                self._load(directory)
            except (ValueError, OSError, SchemaError, ImportError, SyntaxError) as exc:
                raise ValueError(f"{directory.name}/scenario.json: {exc}") from exc

    def _load(self, directory):
        scene = _read_json(directory, "scenario.json")
        _check(scene, MANIFEST)
        sid = scene["id"]
        if sid != directory.name or sid in self.scenarios:
            raise ValueError("scenario ID must be unique and match its directory")
        documents = _read_json(directory, scene["documents"]) if "documents" in scene else []
        _validate_documents(scene.get("documents"), documents)
        self._documents[sid] = documents
        module = None
        tools = {}
        for tool in scene["tools"]:
            name = tool["name"]
            if name in tools:
                raise ValueError(f"duplicate tool {name}")
            for key in ("input_schema", "output_schema"):
                schema = tool[key]
                Draft202012Validator.check_schema(schema)
                if schema.get("type") != "object":
                    raise ValueError(f"{name}.{key} must describe an object")
                _check_schema(schema)
            if "object_ids_field" in tool:
                field = tool["object_ids_field"]
                inputs = tool["input_schema"]
                binding = inputs.get("properties", {}).get(field, {})
                if (field not in inputs.get("required", []) or binding.get("type") != "array"
                        or binding.get("items", {}).get("type") != "string"):
                    raise ValueError(f"{name}.object_ids_field must reference a required array of strings")
            config = tool["adapter"]
            if config["type"] == "http":
                _check_url(config["url"])
            elif config["type"] == "mock_ledger":
                if tool["action_level"] != "commit_write":
                    raise ValueError("mock_ledger requires commit_write")
                for key, fields in (("input_schema", {"object_id": "string", "expected_version": "integer", "value": "string"}),
                                    ("output_schema", {"summary": "string", "object_id": "string", "version": "integer", "receipt_id": "string"})):
                    schema = tool[key]
                    types = {k: v.get("type") for k, v in schema.get("properties", {}).items()}
                    required = set(schema.get("required", []))
                    expected = set(fields)
                    valid_required = required == expected
                    if not valid_required and key == "input_schema":
                        # 模拟提交可省略前置版本，此时由服务端在创建提案时读取。
                        valid_required = required == expected - {"expected_version"}
                    if types != fields or not valid_required:
                        raise ValueError(f"mock_ledger requires its fixed {key} fields")
            else:
                if module is None:
                    file = _local_file(directory, "adapter.py")
                    spec = importlib.util.spec_from_file_location(f"scenario_{sid}", file)
                    module = importlib.util.module_from_spec(spec)
                    # 直接执行审核过的源码，防止邻近的 .pyc 缓存替换适配器。
                    source = file.read_bytes() if self._adapter_sources is None else self._adapter_sources[sid]
                    exec(compile(source, str(file), "exec"), module.__dict__)
                handler = getattr(module, config["handler"], None)
                if not callable(handler):
                    raise ValueError(f"missing adapter handler for {name}")
                self._handlers[(sid, name)] = handler
            tools[name] = tool
        if scene.get("offline_tool"):
            offline = tools.get(scene["offline_tool"])
            if offline is None:
                raise ValueError("offline_tool must name a registered tool")
            inputs, outputs = offline["input_schema"], offline["output_schema"]
            if (inputs.get("properties", {}).get("text", {}).get("type") != "string"
                    or set(inputs.get("required", [])) - {"text"}):
                raise ValueError("offline_tool must accept a single text string argument")
            if ("summary" not in outputs.get("required", [])
                    or outputs.get("properties", {}).get("summary", {}).get("type") != "string"):
                raise ValueError("offline_tool must return a required summary string")
        self._tools[sid] = tools
        self.scenarios[sid] = scene

    def _scene(self, scenario_id):
        if scenario_id not in self.scenarios:
            raise Fault("PERMISSION_DENIED", "场景不可用", 403)
        return self.scenarios[scenario_id]

    def discover(self, scenario_id):
        scene = self._scene(scenario_id)
        return [{k: copy.deepcopy(tool[k]) for k in PUBLIC_FIELDS if k in tool} for tool in scene["tools"]]

    def retrieve(self, scenario_id, query, top_k=5):
        self._scene(scenario_id)
        if (not isinstance(query, str) or not query.strip() or len(query) > 16000 or
                type(top_k) is not int or not 1 <= top_k <= 20):
            raise Fault("INVALID_REQUEST", "检索文本或返回数量超出范围", 400)
        terms = _terms(query)
        phrase = query.strip().casefold()
        ranked = []
        for doc in self._documents[scenario_id]:
            score = 2 * len(terms & _terms(doc["title"])) + len(terms & _terms(doc["snippet"]))
            if phrase in doc["title"].casefold() or phrase in doc["snippet"].casefold():
                score += 3
            if score:
                ranked.append((score, doc))
        ranked.sort(key=lambda item: (-item[0], item[1]["source_id"]))
        return [doc.copy() for _, doc in ranked[:top_k]]

    async def execute(self, scenario_id, name, arguments, *, authorize_retrieval=None):
        self._scene(scenario_id)
        tool = self._tools[scenario_id].get(name)
        if tool is None:
            raise Fault("CAPABILITY_NOT_FOUND", "本场景未注册此工具", 404)
        if tool["action_level"] == "commit_write":
            raise Fault("ACTION_NOT_ALLOWED", "提交写入操作已禁用", 403)
        try:
            _check(arguments, tool["input_schema"])
        except ValueError as exc:
            raise Fault("SCHEMA_VALIDATION_FAILED", f"工具入参无效 ({exc})", 422) from exc
        config = tool["adapter"]
        if config["type"] == "http":
            result = await _call_http(config, arguments, self._http_transport)
        else:
            def retrieve(query, top_k=5):
                if authorize_retrieval is not None:
                    authorize_retrieval()
                return self.retrieve(scenario_id, query, top_k)
            handler = self._handlers[(scenario_id, name)]
            if inspect.iscoroutinefunction(handler):
                result = await handler(arguments, retrieve)
            else:
                result = await _call_sync(handler, arguments, retrieve)
        try:
            _check(result, ENVELOPE)
            _check(result["data"], tool["output_schema"])
            json.dumps(result, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise Fault("CAPABILITY_OUTPUT_INVALID", "工具响应与声明的结构不符", 502) from exc
        return result
