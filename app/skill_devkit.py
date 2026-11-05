"""离线检查和打包候选技能。显式安装将已验签的工件登记为本地无权限草稿。"""
import hashlib
import hmac
from importlib import metadata
import json
import multiprocessing
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import sys
import tempfile
from typing import Iterable
import zipfile
import zlib

from jsonschema import Draft202012Validator, SchemaError
from referencing.exceptions import Unresolvable
import yaml

from .skills import SkillError, SkillRegistry, _dependencies, _identifier, _local, _mapping, _package, _safe_key

MAX_FILE = 65536
MAX_TOTAL = 2 * 1024 * 1024
MAX_FILES = 128
TEST_TIMEOUT_SECONDS = 10


def _encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _pairs(values: list[tuple]) -> dict:
    result = {}
    for key, value in values:
        if key in result:
            raise SkillError("JSON 不允许重复键")
        result[key] = value
    return result


def _json(raw: bytes) -> object:
    def invalid_constant(value):
        raise SkillError("JSON 不允许非有限数值")
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise SkillError("JSON 文件无效") from error


def _check_path(name: str) -> None:
    if name in {"SKILL.md", "skill.manifest.yaml"}:
        return
    prefix = name.split("/", 1)[0]
    if prefix not in {"references", "schemas", "tests"}:
        raise SkillError("候选包包含不支持的文件")
    _safe_key(name, prefix)
    suffix = Path(name).suffix.lower()
    allowed = {".md", ".txt", ".json", ".csv"} if prefix == "references" else {".json"}
    if suffix not in allowed:
        raise SkillError("候选包包含脚本或不支持的文件类型")


def _check_content(raw: bytes) -> None:
    if len(raw) > MAX_FILE:
        raise SkillError("单文件超过64 KiB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as error:
        raise SkillError("候选包只允许UTF-8文本") from error
    if re.search(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----|\bbearer\s+[a-z0-9._~+/=-]{8,}|"
                 r"(?:api_key|access_token|refresh_token|password|client_secret)[\"']?\s*[:=]\s*[\"']?[^\s\"',}]{8,}", text):
        raise SkillError("候选包包含疑似明文凭据")


def _files(directory: Path) -> dict[str, bytes]:
    _local(directory, directory=True)
    pending = [directory]
    files = {}
    names: set[str] = set()
    total = 0
    count = 0
    while pending:
        current = pending.pop()
        _local(current, directory=True)
        for path in current.iterdir():
            count += 1
            if count > MAX_FILES:
                raise SkillError("候选包文件及目录数超过128")
            name = path.relative_to(directory).as_posix()
            if path.is_dir():
                if name not in {"references", "schemas", "tests"}:
                    _safe_key(name, name.split("/", 1)[0])
                if name.split("/", 1)[0] not in {"references", "schemas", "tests"}:
                    raise SkillError("候选包包含不支持的目录")
                _local(path, directory=True)
                pending.append(path)
                continue
            _check_path(name)
            _local(path)
            if name.casefold() in names:
                raise SkillError("候选包路径大小写冲突")
            names.add(name.casefold())
            before = path.stat()
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or opened.st_nlink != 1:
                    raise SkillError("读取期间文件被替换")
                raw = stream.read(MAX_FILE + 1)
            _check_content(raw)
            total += len(raw)
            if total > MAX_TOTAL:
                raise SkillError("候选包总大小超过2 MiB")
            files[name] = raw
    return files


def _validate_bytes(name: str, files: dict[str, bytes], capabilities: Iterable[str]):
    """在私有临时目录解析同一份字节，避免验证源文件后又打包另一份内容。"""
    _identifier(name, r"[a-z0-9]+(?:-[a-z0-9]+)*", "Skill 名称")
    if PureWindowsPath(name).is_reserved():
        raise SkillError("Skill 名称不能是 Windows 设备名")
    approved = set(_dependencies({"required_capabilities": list(capabilities)}))
    try:
        manifest = _mapping(files["skill.manifest.yaml"].decode("utf-8-sig")) if "skill.manifest.yaml" in files else {}
    except (yaml.YAMLError, UnicodeError, RecursionError) as error:
        raise SkillError("manifest 格式无效") from error
    if manifest.get("schema_version") not in (None, "3.0", "3.1"):
        raise SkillError("DevKit 不支持该 manifest 版本")
    if manifest.get("status", "draft") != "draft" or set(manifest) & {"signature", "reviewer", "evaluation_run_id", "release_snapshot_id"}:
        raise SkillError("候选包不得自称已发布或携带审核凭证")
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / name
        root.mkdir()
        for key, raw in files.items():
            _check_path(key)
            target = root / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        try:
            package = _package(root, None)
        except (ValueError, OSError, RecursionError, SchemaError, yaml.YAMLError) as error:
            raise SkillError("技能正文、manifest 或 Schema 无效") from error
    if not set(package.dependencies) <= approved:
        raise SkillError("技能依赖未出现在操作者批准的能力清单")
    return package, manifest


def init_skill(directory: Path, business: str) -> dict:
    directory = Path(directory)
    _identifier(directory.name, r"[a-z0-9]+(?:-[a-z0-9]+)*", "Skill 名称")
    _identifier(business, r"[a-z][a-z0-9_]*", "业务 ID")
    directory.mkdir(parents=True, exist_ok=False)
    for name in ("references", "schemas", "tests"):
        (directory / name).mkdir()
    (directory / "SKILL.md").write_text(
        f"---\nname: {directory.name}\ndescription: 在授权业务范围内读取资料并保留证据。\n---\n"
        "# 执行说明\n\n先读取已批准能力，再查询授权对象；保留来源和版本，缺少资料时明确说明。\n"
        "操作边界见 [使用说明](references/usage.md)。\n", encoding="utf-8")
    (directory / "references" / "usage.md").write_text("仅使用已批准的只读工具；不能执行包内脚本或自行构造地址。\n", encoding="utf-8")
    manifest = (f'schema_version: "3.1"\nskill_id: {directory.name}\nversion: "1.0.0"\nstatus: draft\n'
                f'business_scopes: [{business}]\nrequired_capabilities: []\n'
                'input_schema_ref: schemas/input.schema.json\noutput_schema_ref: schemas/output.schema.json\n'
                'resources: [references/usage.md]\nexecution:\n  trust_level: instructions_only\n  allow_scripts: false\n  max_tool_calls: 4\n')
    (directory / "skill.manifest.yaml").write_text(manifest, encoding="utf-8")
    input_schema = {"type": "object", "required": ["message", "context"], "properties": {
        "message": {"type": "string", "minLength": 1}, "context": {"type": "object"}}, "additionalProperties": False}
    output_schema = {"type": "object", "required": ["answer", "evidence"], "properties": {
        "answer": {"type": "string", "minLength": 1}, "evidence": {"type": "array"}}}
    (directory / "schemas" / "input.schema.json").write_bytes(_encode(input_schema))
    (directory / "schemas" / "output.schema.json").write_bytes(_encode(output_schema))
    cases = [{"id": "input-valid", "target": "input", "value": {"message": "查询", "context": {}}, "valid": True},
             {"id": "input-missing", "target": "input", "value": {}, "valid": False},
             {"id": "output-valid", "target": "output", "value": {"answer": "无资料", "evidence": []}, "valid": True}]
    (directory / "tests" / "cases.json").write_bytes(_encode(cases))
    return {"status": "PASS", "directory": str(directory), "publication": "draft"}


def validate_skill(directory: Path, capabilities: Iterable[str]) -> dict:
    directory = Path(directory)
    files = _files(directory)
    package, _ = _validate_bytes(directory.name, files, capabilities)
    return {"status": "PASS", "skill_id": package.name, "version": package.version,
            "content_digest": package.digest, "dependencies": package.dependencies, "files": sorted(files), "publication": "draft"}


def test_skill(directory: Path, capabilities: Iterable[str]) -> dict:
    directory = Path(directory)
    files = _files(directory)
    # Schema 的正则与引用可能耗时；隔离整个校验过程，超时后回收进程。
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=_contract_worker, args=(directory.name, files, list(capabilities), sender), daemon=True)
    try:
        worker.start()
        sender.close()
        if not receiver.poll(TEST_TIMEOUT_SECONDS):
            return {"status": "FAIL", "test_level": "contract", "cases": [],
                    "semantic_quality": "NOT_RUN", "error": "VALIDATION_TIMEOUT"}
        try:
            return receiver.recv()
        except EOFError:
            return {"status": "FAIL", "test_level": "contract", "cases": [],
                    "semantic_quality": "NOT_RUN", "error": "VALIDATION_WORKER_FAILED"}
    finally:
        if worker.pid is not None:
            if worker.is_alive():
                worker.terminate()
            worker.join()
        worker.close()
        receiver.close()
        sender.close()


def _contract_worker(name: str, files: dict[str, bytes], capabilities: list[str], connection) -> None:
    """只解析已捕获的包内容；结果通过私有管道返回，不执行技能脚本。"""
    try:
        report = _contract_report(name, files, capabilities)
    except (ValueError, OSError, RecursionError):
        report = {"status": "FAIL", "test_level": "contract", "cases": [],
                  "semantic_quality": "NOT_RUN", "error": "VALIDATION_INVALID"}
    try:
        connection.send(report)
    finally:
        connection.close()


def _contract_report(name: str, files: dict[str, bytes], capabilities: list[str]) -> dict:
    package, manifest = _validate_bytes(name, files, capabilities)
    results = []
    seen = set()
    for path, raw in sorted(files.items()):
        if not path.startswith("tests/"):
            continue
        cases = _json(raw)
        if not isinstance(cases, list) or len(cases) > 128:
            raise SkillError("测试文件必须是至多128条声明式用例的列表")
        for case in cases:
            if (not isinstance(case, dict) or set(case) != {"id", "target", "value", "valid"}
                    or not isinstance(case["id"], str) or not 1 <= len(case["id"]) <= 64
                    or case["id"] in seen or case["target"] not in ("input", "output") or type(case["valid"]) is not bool):
                raise SkillError("声明式测试结构无效或用例ID重复")
            seen.add(case["id"])
            schema = package.schemas.get(manifest.get(case["target"] + "_schema_ref"))
            status = "NOT_RUN"
            if schema is not None:
                try:
                    valid = Draft202012Validator(schema).is_valid(case["value"])
                except (Unresolvable, RecursionError) as error:
                    raise SkillError("测试使用了无法解析的 Schema") from error
                status = "PASS" if valid == case["valid"] else "FAIL"
            results.append({"id": case["id"], "status": status, "fixture": path})
    status = "PASS"
    if not results or any(row["status"] == "NOT_RUN" for row in results):
        status = "NOT_RUN"
    if any(row["status"] == "FAIL" for row in results):
        status = "FAIL"
    return {"status": status, "test_level": "contract", "cases": results,
            "semantic_quality": "NOT_RUN", "command": "python scripts/agent.py skill test <directory> --offline"}


def pack_skill(directory: Path, output: Path, capabilities: Iterable[str], signing_key: bytes | None = None) -> dict:
    directory, output = Path(directory), Path(output)
    files = _files(directory)
    package, _ = _validate_bytes(directory.name, files, capabilities)
    if signing_key is not None and (not isinstance(signing_key, bytes) or len(signing_key) < 32):
        raise SkillError("候选工件签名密钥至少32字节")
    listing = {}
    for name, raw in sorted(files.items()):
        listing[name] = {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    payload = {"format": "agent-skill-candidate-1", "skill_id": package.name, "version": package.version,
               "files": listing, "dependencies": package.dependencies,
               "dependency_lock_digest": hashlib.sha256(_encode(package.dependencies)).hexdigest()}
    digest = hashlib.sha256(_encode(payload)).hexdigest()
    signature = hmac.new(signing_key, _encode(payload), hashlib.sha256).hexdigest() if signing_key is not None else None
    files["artifact.json"] = _encode({**payload, "package_digest": digest, "signature": signature})
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"{package.name}-{package.version}.zip"
    # x 模式拒绝覆盖已有工件；固定元数据和不压缩格式使同一字节集可重现。
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_STORED) as archive:
        for name, raw in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(entry, raw)
    return {"status": "PASS", "path": str(destination), "package_digest": digest, "publication": "draft"}


def inspect_artifact(path: Path, signing_key: bytes | None = None) -> dict:
    report, _ = _read_artifact(path, signing_key)
    return report


def install_artifact(path: Path, root: Path, capabilities: Iterable[str], *, signing_key: bytes, author: str) -> dict:
    if signing_key is None:
        raise SkillError("安装需要受信任的工件验证密钥")
    report, package = _read_artifact(path, signing_key)
    approved = _dependencies({"required_capabilities": list(capabilities)})
    if not set(package.dependencies) <= set(approved):
        raise SkillError("技能依赖未出现在操作者批准的能力清单")
    SkillRegistry._identity(author)
    draft = SkillRegistry(root)._store_package(package, author, artifact_digest=report["package_digest"])
    return {**report, "installation": draft["installation"], "content_digest": package.digest}


def _read_artifact(path: Path, signing_key: bytes | None):
    path = Path(path)
    _local(path)
    if path.stat().st_size > 2 * MAX_TOTAL:
        raise SkillError("工件超过大小上限")
    files = {}
    names: set[str] = set()
    directories = {}
    total = 0
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES + 1:
                raise SkillError("工件条目过多")
            for entry in entries:
                name = entry.filename
                if entry.orig_filename != name:
                    raise SkillError("工件路径包含非规范分隔符或空字符")
                if name != "artifact.json":
                    _check_path(name)
                parts = name.split("/")
                for depth in range(1, len(parts)):
                    directory = "/".join(parts[:depth])
                    folded = directory.casefold()
                    if folded in names or directories.setdefault(folded, directory) != directory:
                        raise SkillError("工件目录路径冲突")
                if name.casefold() in directories:
                    raise SkillError("工件文件与目录路径冲突")
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if (name.casefold() in names or mode not in (0, stat.S_IFREG) or entry.is_dir()
                        or entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise SkillError("工件包含重复、加密、链接或非法条目")
                names.add(name.casefold())
                limit = 128 * 1024 if name == "artifact.json" else MAX_FILE
                total += entry.file_size
                if (entry.file_size > limit or total > MAX_TOTAL + 128 * 1024
                        or entry.file_size > max(1, entry.compress_size) * 100):
                    raise SkillError("工件解压大小或压缩比超限")
                with archive.open(entry) as stream:
                    raw = stream.read(limit + 1)
                if len(raw) != entry.file_size or len(raw) > limit:
                    raise SkillError("工件声明长度与实际不符")
                if name != "artifact.json":
                    _check_content(raw)
                files[name] = raw
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError) as error:
        raise SkillError("工件ZIP结构无效") from error
    descriptor = _json(files.pop("artifact.json", b"{}"))
    required = {"format", "skill_id", "version", "files", "dependencies", "dependency_lock_digest", "package_digest", "signature"}
    if not isinstance(descriptor, dict) or set(descriptor) != required or descriptor["format"] != "agent-skill-candidate-1":
        raise SkillError("工件清单结构无效")
    signature = descriptor.pop("signature")
    expected_digest = descriptor.pop("package_digest")
    calculated = hashlib.sha256(_encode(descriptor)).hexdigest()
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_digest) or not hmac.compare_digest(calculated, expected_digest):
        raise SkillError("工件摘要不匹配")
    if signature is not None and (not isinstance(signature, str) or not re.fullmatch(r"[a-f0-9]{64}", signature)):
        raise SkillError("工件签名格式无效")
    signature_status = "unsigned" if signature is None else "unverified"
    if signing_key is not None:
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise SkillError("候选工件验证密钥至少32字节")
        expected = hmac.new(signing_key, _encode(descriptor), hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            raise SkillError("工件签名无效或缺失")
        signature_status = "verified"
    listing = descriptor["files"]
    if not isinstance(listing, dict) or set(listing) != set(files):
        raise SkillError("工件清单与文件不一致")
    for name, raw in files.items():
        if listing[name] != {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}:
            raise SkillError("工件文件摘要不匹配")
    dependencies = descriptor["dependencies"]
    if not isinstance(dependencies, list) or descriptor["dependency_lock_digest"] != hashlib.sha256(_encode(dependencies)).hexdigest():
        raise SkillError("依赖锁无效")
    package, _ = _validate_bytes(descriptor["skill_id"], files, dependencies)
    if package.version != descriptor["version"] or package.dependencies != dependencies:
        raise SkillError("工件内容与身份或依赖不一致")
    return {"status": "PASS", "skill_id": package.name, "version": package.version,
            "package_digest": calculated, "signature": signature_status, "publication": "draft",
            "authorization": "NOT_GRANTED", "installation": "NOT_RUN", "files": sorted(files)}, package


def doctor() -> dict:
    versions: dict[str, str | None] = {}
    compatible = sys.version_info >= (3, 11)
    mismatches = []
    lock = Path(__file__).resolve().parents[1] / "requirements-win-py311.lock"
    for line in lock.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, separator, expected = line.partition("==")
        if not separator:
            raise SkillError("依赖锁需要精确版本")
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
        if versions[name] != expected:
            mismatches.append({"package": name, "expected": expected, "installed": versions[name]})
            compatible = False
    return {"status": "PASS" if compatible else "FAIL", "python": sys.version.split()[0], "packages": versions,
            "lock_mismatches": mismatches, "validation_target": "Windows Python 3.11 locked environment",
            "context_compiler": os.getenv("AGENT_CONTEXT_COMPILER", "0") == "1",
            "model_configured": bool(os.getenv("AGENT_MODEL_NAME")), "network_probe": "NOT_RUN"}
