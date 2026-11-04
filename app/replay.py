"""显式本地测试的脱敏录制和回放；所有工具与模型传输强制为录制响应。"""
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import io
from importlib.metadata import distributions
import json
import os
from pathlib import Path
import re
import platform
import tempfile
import time
from unittest.mock import patch
import zipfile

import httpx

from .catalog import Catalog, _reject_duplicate_keys
from .errors import Fault
from .result_store import _bounded
from .runtime import Runtime
from .schema_validation import _simple_schema
from .store import Store


VERSION = "3.1-p3.1"
MAX_BYTES = 4 * 1024 * 1024
CASE_FIELDS = {
    "schema_version", "case_id", "version", "fixture_policy", "release_snapshot_id", "model_profile_version",
    "time", "message", "permissions", "object_ids", "redactions", "expected_status", "tools", "responses",
}
TOOL_FIELDS = {"name", "permission", "action_level", "input_schema", "output_schema", "recordings"}


def _bytes(value):
    _bounded(value)
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > MAX_BYTES // 2:
        raise ValueError("回放内容超过大小限制")
    return raw


def _load(raw):
    if len(raw) > MAX_BYTES // 2:
        raise ValueError("回放内容超过大小限制")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        _bounded(value)
        return value
    except (ValueError, TypeError, RecursionError, Fault):
        raise ValueError("回放JSON无效") from None


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _engine_digest():
    root = Path(__file__).parent
    files = {}
    for path in sorted(root.rglob("*.py")):
        files[path.relative_to(root).as_posix()] = _sha(path.read_bytes().replace(b"\r\n", b"\n"))
    lock = root.parent / "requirements-win-py311.lock"
    files[lock.name] = _sha(lock.read_bytes().replace(b"\r\n", b"\n"))
    # 覆盖传递依赖的实际版本，避免同一锁文件下不同环境被误认为同一执行器。
    packages = sorted((item.metadata["Name"], item.version) for item in distributions())
    return _sha(_bytes({"files": files, "python": platform.python_version(),
                        "platform": platform.system(), "packages": packages}))


def _key(key):
    if not isinstance(key, bytes) or not 32 <= len(key) <= 4096:
        raise ValueError("需要独立可信的回放签名密钥")
    return key


def _validate_case(case):
    if not isinstance(case, dict) or set(case) != CASE_FIELDS:
        raise ValueError("回放用例字段无效")
    _bytes(case)
    if case["schema_version"] != "3.1" or case["fixture_policy"] not in {"synthetic", "redacted"}:
        raise ValueError("只接受显式虚构或已脱敏夹具")
    for field in ("case_id", "version", "release_snapshot_id", "model_profile_version"):
        if not isinstance(case[field], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", case[field]):
            raise ValueError("回放版本标识无效")
    if type(case["time"]) not in (int, float) or not 1 <= case["time"] <= 4000000000:
        raise ValueError("回放时点无效")
    if not isinstance(case["message"], str) or not 0 < len(case["message"]) <= 16000:
        raise ValueError("回放消息无效")
    for field in ("permissions", "object_ids", "redactions"):
        values = case[field]
        if not isinstance(values, list) or len(values) > 128:
            raise ValueError("回放范围无效")
        if any(not isinstance(value, str) or not 0 < len(value) <= 1024 for value in values):
            raise ValueError("回放范围无效")
    if case["expected_status"] not in {"completed", "failed"}:
        raise ValueError("回放预期状态无效")
    if not isinstance(case["tools"], list) or not 1 <= len(case["tools"]) <= 16:
        raise ValueError("回放工具数量无效")
    names = set()
    for tool in case["tools"]:
        if not isinstance(tool, dict) or set(tool) != TOOL_FIELDS or tool["action_level"] != "read":
            raise ValueError("回放只接受内置录制的只读工具")
        name = tool["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or name in names:
            raise ValueError("回放工具标识无效")
        names.add(name)
        if not isinstance(tool["permission"], str) or not 0 < len(tool["permission"]) <= 128:
            raise ValueError("回放权限声明无效")
        for field in ("input_schema", "output_schema"):
            # 工件不能借正则、外部引用或组合规则让回放进程执行无界校验。
            if not isinstance(tool[field], dict) or not _simple_schema(tool[field]):
                raise ValueError("回放仅支持有界基础Schema")
        recordings = tool["recordings"]
        if not isinstance(recordings, list) or not 1 <= len(recordings) <= 64:
            raise ValueError("回放工具响应数量无效")
        arguments = set()
        for item in recordings:
            if not isinstance(item, dict) or set(item) != {"arguments", "output"} or not isinstance(item["arguments"], dict):
                raise ValueError("回放工具响应无效")
            encoded = _bytes(item["arguments"])
            if encoded in arguments:
                raise ValueError("同一参数存在歧义录制")
            arguments.add(encoded)
    responses = case["responses"]
    if not isinstance(responses, list) or not 1 <= len(responses) <= 32:
        raise ValueError("模型录制数量无效")
    for response in responses:
        if not isinstance(response, dict) or set(response) != {"status_code", "body"}:
            raise ValueError("模型录制字段无效")
        if type(response["status_code"]) is not int or not 100 <= response["status_code"] <= 599:
            raise ValueError("模型录制状态码无效")


def _redact(value, secrets, depth=0):
    if depth > 32:
        raise ValueError("脱敏内容嵌套过深")
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            safe_key = _redact(key, secrets, depth + 1)
            if safe_key in result:
                raise ValueError("脱敏造成字段重名，拒绝静默覆盖")
            result[safe_key] = _redact(item, secrets, depth + 1)
        return result
    if isinstance(value, list):
        return [_redact(item, secrets, depth + 1) for item in value]
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(("{", "[", '"')):
        try:
            decoded = _load(value.encode("utf-8"))
        except ValueError:
            pass
        else:
            return _bytes(_redact(decoded, secrets, depth + 1)).decode("utf-8")
    for secret in secrets:
        if secret:
            value = value.replace(secret, "REDACTED")
    value = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "REDACTED_EMAIL", value)
    value = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|Bearer [A-Za-z0-9_.-]{8,})", "REDACTED_CREDENTIAL", value)
    return value


def _policy(case):
    # 全部端点与画像由运行器固定，工件不能提供候选地址、凭据或代码。
    profile = {
        "provider_id": "recorded", "model_ref": "recorded-model", "base_url": "http://127.0.0.1:9/model",
        "credential_env": "", "enabled": True, "profile_version": case["model_profile_version"],
        "verified_at": case["time"] - 1, "expires_at": case["time"] + 3600,
        "verification_kind": "mock", "evidence_digest": _sha(_bytes(case["responses"])),
        "supports_tool_calling": True, "supports_structured_output": True, "supports_streaming": False,
        "supports_parallel_tool_calls": False, "supports_usage_reporting": True,
        "context_window_tokens": 65536, "max_output_tokens": 4096, "data_handling_class": ["synthetic"],
        "region": "local", "retention_class": "no_store", "input_usd_per_million": 1, "output_usd_per_million": 2,
    }
    return {"version": "replay-1", "profiles": {"recorded": profile}, "routes": {"replay": {
        "allowed_providers": ["recorded"], "data_class": "synthetic", "allowed_regions": ["local"],
        "allowed_retention_classes": ["no_store"], "result_max_age_s": 60, "stream": False,
        "max_attempts": 1, "max_total_tokens": 1000000, "max_cost_usd": 100, "timeout_s": 10,
    }}}


async def _run_case(case):
    requests = []
    responses = iter(case["responses"])
    recorded_tools = {}
    definitions = []
    for tool in case["tools"]:
        url = "https://recorded.invalid/tools/" + tool["name"]
        definitions.append({"name": tool["name"], "description": "录制的虚构只读工具", "action_level": "read",
                            "permission": tool["permission"], "input_schema": tool["input_schema"],
                            "output_schema": tool["output_schema"], "adapter": {"type": "http", "url": url, "method": "POST"}})
        for item in tool["recordings"]:
            recorded_tools[(url, _bytes(item["arguments"]))] = item["output"]

    def tool_response(request):
        lookup = (str(request.url), _bytes(_load(request.content)))
        if request.method != "POST" or lookup not in recorded_tools:
            raise Fault("REPLAY_RECORDING_MISSING", "工具调用没有精确匹配的录制。", 422)
        requests.append({"kind": "tool", "digest": _sha(_bytes([request.url.path, _load(request.content)]))})
        return httpx.Response(200, json=recorded_tools[lookup])

    def model_response(request):
        if request.method != "POST" or str(request.url) != "http://127.0.0.1:9/model/chat/completions":
            raise Fault("REPLAY_NETWORK_DENIED", "回放模型端点不匹配。", 403)
        # 只保存脱敏请求摘要，随机上下文标识不影响结构性重复比较。
        normalized = request.content.decode("utf-8").replace(task_id, "fixture-task")
        normalized = re.sub(r"ctx_[0-9a-f]{32}", "fixture-context", normalized)
        requests.append({"kind": "model", "digest": _sha(normalized.encode("utf-8"))})
        reply = next(responses, None)
        if reply is None:
            raise Fault("REPLAY_RECORDING_MISSING", "模型请求超出录制序列。", 422)
        return httpx.Response(reply["status_code"], json=reply["body"])

    actor = {"tenant_id": "fixture", "org_id": "fixture", "user_id": "fixture", "grants": {"replay": {
        "permissions": case["permissions"], "objects": case["object_ids"], "resource_types": []}}}
    body = {"business_id": "replay", "mode": "openai", "message": case["message"], "context": {"object_ids": case["object_ids"]}}
    with tempfile.TemporaryDirectory(prefix="agent-replay-") as directory, closing(Store(":memory:")) as store:
        root = Path(directory)
        scene = root / "replay"
        scene.mkdir()
        (scene / "scenario.json").write_bytes(_bytes({"id": "replay", "version": "1.0.0", "name": "虚构回放",
                                                     "description": "仅使用录制响应", "example": "本地回放", "tools": definitions}))
        catalog = Catalog(root, http_transport=httpx.MockTransport(tool_response))
        settings = {"provider_policy": _policy(case), "transport": httpx.MockTransport(model_response), "p2": {}, "max_rounds": 12}
        runtime = Runtime(catalog, store, {"fixture": actor}, settings)
        task, _ = store.start(actor, body, None, "1.0.0", snapshot_id=case["release_snapshot_id"])
        task_id = task["task_id"]
        await runtime.drive(actor, body, task)
        result = store.task(actor, task_id)
        calls = []
        for call in result["tool_calls"]:
            calls.append({"capability_id": call["capability_id"], "status": call["status"]})
        usage = []
        for row in store._db.execute("SELECT resource FROM audit_logs WHERE action='model.attempt' ORDER BY rowid"):
            attempt = _load(row[0].encode("utf-8"))
            # 耗时单列测量，不混入确定性结构。
            attempt.pop("duration_ms", None)
            usage.append(attempt)
        terminal_count = store._db.execute("SELECT COUNT(*) FROM events WHERE event IN ('task_completed','task_failed','task_cancelled')").fetchone()[0]
        return {"status": result["status"], "answer": result.get("answer", ""), "error": result.get("error"),
                "evidence": result.get("evidence", []), "tool_calls": calls, "permissions": sorted(store.permissions(task_id)),
                "permission_scope": sorted(set(case["permissions"])), "terminal_count": terminal_count,
                "requests": requests, "usage": usage}


def _execute(case):
    _validate_case(case)
    stamp = datetime.fromtimestamp(case["time"], timezone.utc).isoformat()
    # 仅供独立本地CLI；不得在运行中的API进程调用此同步测试工具。
    environment = {}
    for name in ("SystemRoot", "WINDIR", "TEMP", "TMP"):
        if name in os.environ:
            environment[name] = os.environ[name]
    with patch("time.time", return_value=case["time"]), patch("app.store._now", return_value=stamp), patch.dict(
        os.environ, environment, clear=True
    ):
        return asyncio.run(_run_case(case))


def record_case(case, out, key):
    """先脱敏再运行，输出候选测试工件，不保存任何日常任务资料。"""
    key = _key(key)
    _validate_case(case)
    secrets = list(case["redactions"])
    secrets.append(key.decode("utf-8", errors="ignore"))
    for name in ("AGENT_MODEL_API_KEY", "AGENT_ACCESS_TOKEN", "AGENT_SERVICE_TOKEN", "AGENT_ADMIN_TOKEN"):
        if os.getenv(name):
            secrets.append(os.environ[name])
    # 首次执行使用与工件重新读取时相同的字段顺序，避免协议消息字符串因序列化顺序产生假差异。
    safe = _load(_bytes(_redact({**case, "redactions": []}, secrets)))
    _validate_case(safe)
    started = time.perf_counter()
    result = _execute(safe)
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    recording = {"result": result, "latency_ms": elapsed, "real_model_validation": "NOT_RUN"}
    files = {"case.json": _bytes(safe), "recording.json": _bytes(recording)}
    manifest = {"schema_version": "3.1", "harness_version": VERSION, "engine_digest": _engine_digest(),
                "files": {name: _sha(raw) for name, raw in files.items()}}
    manifest["signature"] = hmac.new(key, _bytes(manifest), hashlib.sha256).hexdigest()
    files["manifest.json"] = _bytes(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(files):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(entry, files[name])
    if output.tell() > MAX_BYTES:
        raise ValueError("工件超过大小限制")
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(output.getvalue())
    return {"status": "PASS" if result["status"] == safe["expected_status"] else "FAIL",
            "case_id": safe["case_id"], "bundle_digest": _sha(output.getvalue()), "result": result}


def inspect_bundle(path, key):
    """先核验工件完整性和可信签名，不执行任何内容或向磁盘解压。"""
    key = _key(key)
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("工件超过大小限制")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) != 3 or {entry.filename for entry in entries} != {"case.json", "recording.json", "manifest.json"}:
                raise ValueError("工件路径或文件清单无效")
            if sum(entry.file_size for entry in entries) > MAX_BYTES:
                raise ValueError("工件展开大小超限")
            files = {}
            for entry in entries:
                if entry.compress_type != zipfile.ZIP_STORED or entry.flag_bits & 1 or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("不接受压缩、加密或链接条目")
                with archive.open(entry) as stream:
                    content = stream.read(MAX_BYTES // 2 + 1)
                if len(content) != entry.file_size:
                    raise ValueError("工件条目大小不匹配")
                files[entry.filename] = content
        manifest = _load(files["manifest.json"])
        if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "harness_version", "engine_digest", "files", "signature"}:
            raise ValueError("工件清单格式无效")
        signature = manifest.pop("signature")
        expected = hmac.new(key, _bytes(manifest), hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            raise ValueError("工件签名不匹配")
        if manifest["schema_version"] != "3.1" or manifest["harness_version"] != VERSION:
            raise ValueError("不支持的回放版本")
        if manifest["files"] != {name: _sha(files[name]) for name in ("case.json", "recording.json")}:
            raise ValueError("工件摘要不匹配")
        case = _load(files["case.json"])
        recording = _load(files["recording.json"])
        _validate_case(case)
        if not isinstance(recording, dict) or set(recording) != {"result", "latency_ms", "real_model_validation"}:
            raise ValueError("录制报告格式无效")
        return {"status": "PASS", "manifest": manifest, "case": case, "recording": recording}
    except (zipfile.BadZipFile, KeyError, TypeError, RecursionError, Fault):
        raise ValueError("回放工件无效") from None


def run_replay(path, key):
    bundle = inspect_bundle(path, key)
    if bundle["manifest"]["engine_digest"] != _engine_digest():
        raise ValueError("运行代码与录制版本不一致；请使用差分而不是假定确定性复现")
    result = _execute(bundle["case"])
    matched = result == bundle["recording"]["result"] and result["status"] == bundle["case"]["expected_status"]
    return {"status": "PASS" if matched else "FAIL", "result": result, "real_model_validation": "NOT_RUN"}


def _input_digest(case):
    return _sha(_bytes({field: case[field] for field in ("message", "object_ids", "tools")}))


def compare_bundles(baseline, candidate, key):
    left, right = inspect_bundle(baseline, key), inspect_bundle(candidate, key)
    before, after = left["recording"]["result"], right["recording"]["result"]
    input_left, input_right = _input_digest(left["case"]), _input_digest(right["case"])
    added = sorted(set(after["permission_scope"]) - set(before["permission_scope"]))
    removed = sorted(set(before["permission_scope"]) - set(after["permission_scope"]))
    evidence_before = {_sha(_bytes(item)) for item in before["evidence"]}
    evidence_after = {_sha(_bytes(item)) for item in after["evidence"]}
    lost = sorted(evidence_before - evidence_after)
    schema_codes = {"MODEL_INVALID_RESPONSE", "SKILL_SCHEMA_INVALID", "CAPABILITY_OUTPUT_INVALID", "SCHEMA_VALIDATION_FAILED"}
    schema_ok = not after["error"] or after["error"]["code"] not in schema_codes
    differences = []
    for field in ("answer", "status", "evidence", "tool_calls", "permissions", "usage"):
        if before[field] != after[field]:
            differences.append(field)
    bad_state = before["status"] == "completed" and after["status"] != "completed"
    blocked = bool(added or lost or bad_state or not schema_ok or after["terminal_count"] != 1)
    status = "FAIL" if blocked else "PASS"
    if input_left != input_right:
        status = "INCOMPARABLE"
    def usage_total(result):
        if not result["usage"] or any(not row["usage_known"] for row in result["usage"]):
            return None
        return sum(row["observed_cost_usd"] for row in result["usage"])
    old_cost, new_cost = usage_total(before), usage_total(after)
    cost_difference = None
    if old_cost is not None and new_cost is not None:
        cost_difference = new_cost - old_cost
    return {
        "case_id": left["case"]["case_id"], "baseline_version": left["case"]["version"], "candidate_version": right["case"]["version"],
        "baseline_engine_digest": left["manifest"]["engine_digest"], "candidate_engine_digest": right["manifest"]["engine_digest"],
        "input_fixture_digest": input_left, "candidate_input_fixture_digest": input_right,
        "release_snapshot_id": right["case"]["release_snapshot_id"], "model_profile_version": right["case"]["model_profile_version"],
        "differences": differences, "permission_delta": {"added": added, "removed": removed},
        "state_delta": {"before": before["status"], "after": after["status"]},
        "evidence_delta": {"lost_digests": lost, "added_digests": sorted(evidence_after - evidence_before)},
        "schema_delta": {"candidate_valid": schema_ok},
        "usage_delta": {"baseline": before["usage"], "candidate": after["usage"], "cost_difference_usd": cost_difference},
        "latency_delta": {"baseline_ms": left["recording"]["latency_ms"], "candidate_ms": right["recording"]["latency_ms"]},
        "status": status, "real_model_validation": "NOT_RUN",
    }
