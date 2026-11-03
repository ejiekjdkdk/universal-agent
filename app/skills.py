"""本地 Skill 包注册表。只有受信任的发布操作可以绑定场景工具。"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import os
import secrets
import asyncio
import uuid
import time
from datetime import datetime, timezone
import json
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import stat

from jsonschema import Draft202012Validator, SchemaError
import yaml


class SkillError(ValueError):
    """包校验失败、版本冲突或该场景的发布已失效。"""


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _identifier(value, pattern, label):
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(pattern, value):
        raise SkillError(f"{label}格式无效")


def _version(value):
    _identifier(value, r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", "版本")


def _local(path, *, directory=False):
    info = path.lstat()
    # 同时拒绝 Windows junction/reparse point 和 POSIX 符号链接。
    if (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink > 1)):
        raise SkillError("Skill 包只允许本地目录与普通文件")


def _read(path):
    _local(path)
    with path.open("rb") as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise SkillError("单个 Skill 文件超过 64 KiB")
    return data.decode("utf-8-sig")


class _UniqueKeysLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = set()
        for key, _ in node.value:
            if not isinstance(key, yaml.ScalarNode) or key.tag != "tag:yaml.org,2002:str" or key.value in keys:
                raise SkillError("YAML 各级键名须为唯一的字符串")
            keys.add(key.value)
        return super().construct_mapping(node, deep=deep)


def _mapping(text):
    # 禁用锚点/别名和复杂键，避免非可信元信息生成递归对象。
    if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
           for token in yaml.scan(text)):
        raise SkillError("Skill 元信息不支持 YAML 锚点")
    value = yaml.load(text, Loader=_UniqueKeysLoader)
    if not isinstance(value, dict):
        raise SkillError("Skill 元信息应为 YAML 对象")
    return value


def _safe_key(key, prefix):
    if not isinstance(key, str) or not key.startswith(prefix + "/") or len(key) > 256:
        raise SkillError("Skill 引用必须位于指定的包内目录")
    for part in key.split("/"):
        if (not re.fullmatch(r"[\w-][\w .-]{0,95}", part) or part.endswith((" ", "."))
                or PureWindowsPath(part).is_reserved()):
            raise SkillError("Skill 引用路径无效")
    return key


def _collect_references(root):
    result: dict[str, str] = {}
    directory = root / "references"
    if not directory.exists() and not directory.is_symlink():
        return result
    pending = [directory]
    count = 0
    while pending:
        current = pending.pop()
        _local(current, directory=True)
        for path in current.iterdir():
            count += 1
            key = _safe_key(path.relative_to(root).as_posix(), "references")
            if count > 32:
                raise SkillError("Skill 引用项超过 32 个")
            if path.is_dir():
                pending.append(path)
            elif path.suffix.lower() in {".md", ".txt", ".json", ".csv"}:
                result[key] = _read(path)
            else:
                raise SkillError("Skill 引用仅支持 Markdown、文本、JSON 或 CSV")
    return result


@dataclass
class _Package:
    name: str
    version: str
    description: str
    digest: str
    body: str
    refs: dict
    manifest_yaml: str
    schemas: dict
    dependencies: list


def _dependencies(manifest):
    required = manifest.get("required_capabilities", [])
    if not isinstance(required, list) or len(required) > 128:
        raise SkillError("required_capabilities 须为有限的版本化能力列表")
    for ref in required:
        if not isinstance(ref, str):
            raise SkillError("能力依赖须为 ID@版本")
        name, _, version = ref.partition("@")
        _identifier(name, r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)*", "能力 ID")
        _version(version)
    if len(set(required)) != len(required):
        raise SkillError("能力依赖重复")
    return sorted(required)


def _policy(manifest, schemas):
    """平台支持的 Skill 执行策略；所有规则来自冻结内容且在导入与发布时校验。"""
    execution = manifest.get("execution", {})
    if (not isinstance(execution, dict) or execution.get("trust_level", "instructions_only") != "instructions_only"
            or execution.get("allow_scripts", False) is not False):
        raise SkillError("Skill 仅支持 instructions_only，不能启用脚本")
    limit = execution.get("max_tool_calls", 8)
    if type(limit) is not int or not 1 <= limit <= 32:
        raise SkillError("max_tool_calls 必须是 1 至 32 的整数")
    scopes = manifest.get("business_scopes")
    if scopes is not None:
        if not isinstance(scopes, list) or not scopes or len(scopes) > 128:
            raise SkillError("business_scopes 必须为非空业务列表")
        for scope in scopes:
            _identifier(scope, r"[a-z][a-z0-9_]*", "业务 ID")
        if len(set(scopes)) != len(scopes):
            raise SkillError("business_scopes 不能重复")
    for schema in schemas.values():
        pending = [schema]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ("$ref", "$dynamicRef", "$recursiveRef") and (
                            not isinstance(value, str) or not value.startswith("#")):
                        raise SkillError("Schema 只能引用同一文件内的定义，禁止外部引用")
                    pending.append(value)
            elif isinstance(node, list):
                pending.extend(node)
    return {"max_tool_calls": limit, "business_scopes": scopes,
            "input_schema": schemas.get(manifest.get("input_schema_ref")),
            "output_schema": schemas.get(manifest.get("output_schema_ref"))}


def _package(directory, version):
    root = Path(directory).absolute()
    _local(root, directory=True)
    entry = _read(root / "SKILL.md").replace("\r\n", "\n")
    parsed = re.fullmatch(r"---[ \t]*\n(.*?)\n---[ \t]*\n(.*)", entry, re.S)
    if not parsed or not parsed[2].strip():
        raise SkillError("SKILL.md 需要 YAML frontmatter 和正文")
    header, body = _mapping(parsed[1]), parsed[2]
    name, description = header.get("name"), header.get("description")
    _identifier(name, r"[a-z0-9]+(?:-[a-z0-9]+)*", "Skill 名称")
    if name != root.name:
        raise SkillError("Skill 名称必须与包目录名一致")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024:
        raise SkillError("Skill 描述须为 1 至 1024 个字符")
    refs = _collect_references(root)
    for match in re.finditer(r"\[[^\]\n]*\]\((<[^>\n]+>|[^)\s]+)(?:\s+[^)\n]+)?\)", body):
        target = match[1].strip("<>").split("#", 1)[0]
        if target and not target.startswith(("https://", "http://")):
            if _safe_key(target, "references") not in refs:
                raise SkillError("SKILL.md 引用的资料不存在")
    manifest_path = root / "skill.manifest.yaml"
    manifest_text = ""
    manifest = {}
    schemas = {}
    if manifest_path.exists() or manifest_path.is_symlink():
        manifest_text = _read(manifest_path)
        manifest = _mapping(manifest_text)
        if manifest.get("skill_id", name) != name:
            raise SkillError("manifest 中的 skill_id 与 SKILL.md 不一致")
        if version is not None and manifest.get("version", version) != version:
            raise SkillError("manifest 与导入版本不一致")
        version = manifest.get("version", version)
        resources = manifest.get("resources", [])
        if not isinstance(resources, list) or any(_safe_key(key, "references") not in refs for key in resources):
            raise SkillError("manifest 引用的资料不存在")
        for field in ("input_schema_ref", "output_schema_ref"):
            if field in manifest:
                key = _safe_key(manifest[field], "schemas")
                if Path(key).suffix != ".json":
                    raise SkillError("Schema 必须是本地 JSON 文件")
                path = root / key
                for parent in path.relative_to(root).parents:
                    _local(root / parent, directory=True)
                schemas[key] = _read(path)
                schema = json.loads(schemas[key])
                Draft202012Validator.check_schema(schema)
    version = "1.0.0" if version is None else version
    _version(version)
    _policy(manifest, {key: json.loads(value) for key, value in schemas.items()})
    digest = _sha(_encode([entry, manifest_text, schemas, refs]))
    return _Package(name, version, description.strip(), digest, body, refs, manifest_text,
                    {key: json.loads(value) for key, value in schemas.items()}, _dependencies(manifest))


class SkillRegistry:
    def __init__(self, root, *, operator_secret=None):
        directory = Path(root)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "skills.sqlite3"
        # 本地受信任操作员密钥不来自 Skill 包；环境密钥适合由独立部署系统提供。
        supplied = operator_secret or os.environ.get("AGENT_SKILL_SIGNING_KEY")
        keyfile = directory / ".operator-key"
        if supplied is None:
            try:
                fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "w", encoding="ascii") as stream:
                    stream.write(secrets.token_hex(32))
            # 同时启动 API 与 Worker 时，O_EXCL 获胜者可能正在完成首次写入。
            for attempt in range(50):
                supplied = keyfile.read_text(encoding="ascii")
                if len(supplied) == 64:
                    break
                time.sleep(0.01)
            else:
                raise SkillError("本地操作员密钥未完成写入或已损坏")
        self._secret = supplied.encode() if isinstance(supplied, str) else supplied
        if not isinstance(self._secret, bytes) or len(self._secret) < 32:
            raise SkillError("操作员签名密钥至少需要 32 字节")
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS packages(
                    name TEXT, version TEXT, description TEXT, digest TEXT, body TEXT,
                    PRIMARY KEY(name,version));
                CREATE TABLE IF NOT EXISTS refs(
                    name TEXT, version TEXT, path TEXT, content TEXT, PRIMARY KEY(name,version,path));
                CREATE TABLE IF NOT EXISTS releases(
                    business TEXT, name TEXT, version TEXT, tools TEXT, release_snapshot_id TEXT,
                    status TEXT, PRIMARY KEY(business,name,version));
                CREATE TABLE IF NOT EXISTS package_details(
                    name TEXT, version TEXT, manifest_yaml TEXT, schemas TEXT, dependencies TEXT,
                    PRIMARY KEY(name,version));
                CREATE TABLE IF NOT EXISTS release_details(
                    release_snapshot_id TEXT PRIMARY KEY, capability_bindings TEXT);
                CREATE TABLE IF NOT EXISTS package_auth(
                    name TEXT, version TEXT, author TEXT, content_seal TEXT, PRIMARY KEY(name,version));
                CREATE TABLE IF NOT EXISTS skill_governance(
                    release_snapshot_id TEXT PRIMARY KEY, payload TEXT, signature TEXT,
                    evaluation_run_id TEXT, report TEXT);
                CREATE TABLE IF NOT EXISTS skill_evaluations(
                    evaluation_run_id TEXT PRIMARY KEY, release_snapshot_id TEXT, report TEXT);
                CREATE TABLE IF NOT EXISTS skill_rollouts(
                    business TEXT, name TEXT, stable_version TEXT, canary_version TEXT, percent INTEGER,
                    PRIMARY KEY(business,name));
                CREATE TABLE IF NOT EXISTS skill_audit(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT,
                    action TEXT, business TEXT, name TEXT, version TEXT, actor TEXT, details TEXT);
                CREATE TABLE IF NOT EXISTS skill_legacy(
                    business TEXT, name TEXT, version TEXT, previous_status TEXT,
                    PRIMARY KEY(business,name,version));
            """)
            # 旧 P1 发布缺少审核和评测凭证：隔离后由显式迁移操作重新过门禁。
            rows = db.execute("SELECT * FROM releases WHERE release_snapshot_id NOT IN "
                              "(SELECT release_snapshot_id FROM skill_governance)").fetchall()
            for row in rows:
                if row["status"] in ("active", "deprecated"):
                    db.execute("INSERT OR IGNORE INTO skill_legacy VALUES(?,?,?,?)",
                               (row["business"], row["name"], row["version"], row["status"]))
                    db.execute("UPDATE releases SET status='legacy_pending' WHERE business=? AND name=? AND version=?",
                               (row["business"], row["name"], row["version"]))

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_package(self, directory, version=None, *, author="local-author"):
        self._identity(author)
        try:
            pkg = _package(directory, version)
        except (OSError, UnicodeError, yaml.YAMLError, RecursionError, SchemaError, json.JSONDecodeError) as err:
            raise SkillError("Skill 包文件或元信息无效") from err
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            key = (pkg.name, pkg.version)
            old = db.execute("SELECT digest FROM packages WHERE name=? AND version=?", key).fetchone()
            if old and old["digest"] != pkg.digest:
                raise SkillError("同名同版本包已存在且内容不同")
            if not old:
                db.execute("INSERT INTO packages VALUES(?,?,?,?,?)", (*key, pkg.description, pkg.digest, pkg.body))
                db.executemany("INSERT INTO refs VALUES(?,?,?,?)",
                               [(*key, path, text) for path, text in pkg.refs.items()])
            db.execute("INSERT OR IGNORE INTO package_details VALUES(?,?,?,?,?)",
                       (*key, pkg.manifest_yaml, _encode(pkg.schemas), _encode(pkg.dependencies)))
            old_auth = db.execute("SELECT author FROM package_auth WHERE name=? AND version=?", key).fetchone()
            if old_auth and old_auth[0] != author:
                raise SkillError("已导入包的作者不可改写")
            db.execute("INSERT OR IGNORE INTO package_auth VALUES(?,?,?,?)",
                       (*key, author, self._content_seal(db, *key)))
            self._audit(db, "import", "", *key, author, {"digest": pkg.digest})
        return dict(name=pkg.name, version=pkg.version, description=pkg.description, digest=pkg.digest,
                    status="draft", required_capabilities=pkg.dependencies)

    def inspect_package(self, name, version):
        """供本地审核者查看原始 manifest 和冻结的输入输出 Schema。"""
        with self._db() as db:
            row = db.execute("SELECT * FROM packages JOIN package_details USING(name,version) "
                             "WHERE name=? AND version=?", (name, version)).fetchone()
            if not row:
                raise SkillError("找不到已校验的包，请先导入")
            return {key: row[key] for key in ("name", "version", "description", "body", "digest", "manifest_yaml")} | {
                "schemas": json.loads(row["schemas"]), "required_capabilities": json.loads(row["dependencies"])}

    @staticmethod
    def _identity(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise SkillError("作者和审核者必须是非空的可信操作员标识")

    def _audit(self, db, action, business, name, version, actor, details=None):
        db.execute("INSERT INTO skill_audit VALUES(NULL,?,?,?,?,?,?,?)",
                   (datetime.now(timezone.utc).isoformat(), action, business, name, version, actor,
                    _encode(details or {})))

    def _content_seal(self, db, name, version):
        row = db.execute("SELECT * FROM packages JOIN package_details USING(name,version) "
                         "WHERE name=? AND version=?", (name, version)).fetchone()
        if row is None:
            raise SkillError("找不到已导入的包")
        refs = [list(r) for r in db.execute("SELECT path,content FROM refs WHERE name=? AND version=? ORDER BY path",
                                          (name, version))]
        return _sha(_encode([dict(row), refs]))

    def _signature(self, payload):
        return hmac.new(self._secret, _encode(payload).encode(), hashlib.sha256).hexdigest()

    def _verify(self, db, row):
        proof = db.execute("SELECT * FROM skill_governance WHERE release_snapshot_id=?",
                           (row["release_snapshot_id"],)).fetchone()
        if not proof:
            raise SkillError("发布缺少治理凭证；旧发布须显式迁移")
        payload = json.loads(proof["payload"])
        if not hmac.compare_digest(proof["signature"], self._signature(payload)):
            raise SkillError("发布签名无效")
        report = json.loads(proof["report"])
        if (not report.get("passed") or payload["report_digest"] != _sha(_encode(report))
                or payload["evaluation_run_id"] != proof["evaluation_run_id"]
                or payload["release_snapshot_id"] != row["release_snapshot_id"]
                or [payload["business_id"], payload["name"], payload["version"]] != [row["business"], row["name"], row["version"]]
                or payload["tools"] != json.loads(row["tools"])
                or payload["content_seal"] != self._content_seal(db, row["name"], row["version"])):
            raise SkillError("冻结内容或评测凭证已变化")
        details = db.execute("SELECT capability_bindings FROM release_details WHERE release_snapshot_id=?",
                             (row["release_snapshot_id"],)).fetchone()
        if not details or json.loads(details[0]) != payload["capability_bindings"]:
            raise SkillError("冻结能力映射已变化")
        return payload, report

    def _evaluate(self, name, version):
        """固定本地门禁：执行真实 Gateway 安全用例，并读取冻结包验证契约。"""
        from .evaluations import _mandatory
        pkg = self.inspect_package(name, version)
        cases = asyncio.run(_mandatory())
        text = pkg["body"] + "\n" + pkg["manifest_yaml"]
        suspicious = bool(re.search(r"ignore\s+(?:all\s+)?(?:previous|system)\s+instructions|"
                                    r"绕过(?:鉴权|权限)|忽略(?:系统|安全)(?:指令|限制)", text, re.I))
        cases.append({"case_id": "skill_instruction_scan", "group": "skill", "passed": not suspicious,
                      "issues": ["unsafe_instruction"] if suspicious else []})
        cases.append({"case_id": "frozen_package_load", "group": "skill", "passed": bool(pkg["body"].strip()),
                      "issues": []})
        denied = False
        try:
            self.load("evaluation_unbound_business", name, version)
        except SkillError:
            denied = True
        cases.append({"case_id": "unbound_business_denied", "group": "skill", "passed": denied, "issues": []})
        return {"suite": "skillops-local-v1", "assurance": "local-fixed-behavioral-tests",
                "cases": cases, "passed": all(case["passed"] for case in cases)}

    def validate(self, business_id, name, version, tools=(), *, available_tools=(),
                 capability_bindings=None, author=None, reviewer="local-reviewer"):
        _identifier(business_id, r"[a-z][a-z0-9_]*", "业务 ID")
        self._identity(reviewer)
        if isinstance(tools, str):
            raise SkillError("绑定的工具须为名称列表")
        grants = sorted(set(tools))
        for tool in grants:
            _identifier(tool, r"[a-z][a-z0-9_]*", "工具名称")
        if set(grants) - set(available_tools):
            raise SkillError("工具不在受信任的场景目录中")
        bindings = {} if capability_bindings is None else capability_bindings
        if not isinstance(bindings, dict) or any(tool not in grants for tool in bindings.values()):
            raise SkillError("能力映射只能使用该发布明确绑定的工具")
        pkg = self.inspect_package(name, version)
        policy = _policy(_mapping(pkg["manifest_yaml"]) if pkg["manifest_yaml"] else {}, pkg["schemas"])
        if policy["business_scopes"] is not None and business_id not in policy["business_scopes"]:
            raise SkillError("业务不在冻结包的 business_scopes 中")
        if set(bindings) != set(pkg["required_capabilities"]):
            raise SkillError("请为每个所需的能力和确切版本提供受信任的工具映射")
        key = (business_id, name, version)
        snapshot = _sha(_encode([*key, pkg["digest"], grants, bindings]))
        with self._db() as db:
            auth = db.execute("SELECT * FROM package_auth WHERE name=? AND version=?", (name, version)).fetchone()
            if not auth:
                raise SkillError("旧包缺少作者记录，请显式迁移")
            author = auth["author"] if author is None else author
            self._identity(author)
            if author != auth["author"] or author == reviewer:
                raise SkillError("审核者必须与冻结的作者不同")
            if auth["content_seal"] != self._content_seal(db, name, version):
                raise SkillError("冻结包已被改写")
            old = db.execute("SELECT * FROM releases WHERE business=? AND name=? AND version=?", key).fetchone()
            if old and (old["release_snapshot_id"] != snapshot or old["status"] == "revoked"):
                raise SkillError("已发布的绑定不可覆盖，请发布新版本")
        report = self._evaluate(name, version)
        run_id = "seval_" + uuid.uuid4().hex
        payload = {"skill_id": name, "name": name, "version": version, "business_id": business_id,
                   "digest": pkg["digest"], "schema_version": (_mapping(pkg["manifest_yaml"]).get("schema_version", "3.0")
                                                                if pkg["manifest_yaml"] else "3.0"),
                   "dependency_versions": pkg["required_capabilities"], "author": author, "reviewer": reviewer,
                   "evaluation_run_id": run_id, "release_snapshot_id": snapshot, "tools": grants,
                   "capability_bindings": bindings, "content_seal": auth["content_seal"],
                   "report_digest": _sha(_encode(report)), "assurance": "local-hmac-sha256"}
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO skill_evaluations VALUES(?,?,?)", (run_id, snapshot, _encode(report)))
            self._audit(db, "evaluate", *key, reviewer, {"evaluation_run_id": run_id, "passed": report["passed"]})
            if report["passed"]:
                if auth["content_seal"] != self._content_seal(db, name, version):
                    raise SkillError("评测期间冻结包已变化")
                old = db.execute("SELECT * FROM releases WHERE business=? AND name=? AND version=?", key).fetchone()
                if old and (old["status"] not in ("validated", "legacy_pending") or old["release_snapshot_id"] != snapshot):
                    raise SkillError("当前状态不可重新校验；请发布新版本")
                if old:
                    db.execute("UPDATE releases SET status='validated' WHERE business=? AND name=? AND version=?", key)
                else:
                    db.execute("INSERT INTO releases VALUES(?,?,?,?,?,'validated')", (*key, _encode(grants), snapshot))
                db.execute("INSERT OR REPLACE INTO release_details VALUES(?,?)", (snapshot, _encode(bindings)))
                db.execute("INSERT OR REPLACE INTO skill_governance VALUES(?,?,?,?,?)",
                           (snapshot, _encode(payload), self._signature(payload), run_id, _encode(report)))
                self._audit(db, "validate", *key, reviewer, {"release_snapshot_id": snapshot})
        if not report["passed"]:
            raise SkillError("固定评测未通过，发布门禁拒绝")
        return self.governance(*key)

    def governance(self, business, name, version):
        with self._db() as db:
            row = db.execute("SELECT * FROM releases WHERE business=? AND name=? AND version=?",
                             (business, name, version)).fetchone()
            if not row:
                raise SkillError("找不到发布")
            payload, report = self._verify(db, row)
            return payload | {"status": row["status"], "evaluation": report}

    def stage(self, business, name, version, *, reviewer="local-reviewer"):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM releases WHERE business=? AND name=? AND version=?",
                             (business, name, version)).fetchone()
            if not row or row["status"] != "validated":
                raise SkillError("只有 validated 发布可以进入 staged")
            payload, _ = self._verify(db, row)
            if reviewer != payload["reviewer"]:
                raise SkillError("必须由记录中的独立审核者执行")
            db.execute("UPDATE releases SET status='staged' WHERE business=? AND name=? AND version=?",
                       (business, name, version))
            self._audit(db, "stage", business, name, version, reviewer)
        return self.governance(business, name, version)

    def activate(self, business, name, version, *, reviewer="local-reviewer", canary_percent=100, _rollback=False):
        if type(canary_percent) is not int or not 1 <= canary_percent <= 100:
            raise SkillError("灰度比例须为 1 至 100 的整数")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM releases WHERE business=? AND name=? AND version=?",
                             (business, name, version)).fetchone()
            allowed = ("deprecated",) if _rollback else ("staged", "active")
            if not row or row["status"] not in allowed:
                raise SkillError("发布必须先通过 staged，回滚仅允许 deprecated")
            payload, _ = self._verify(db, row)
            if reviewer != payload["reviewer"]:
                raise SkillError("必须由记录中的独立审核者执行")
            rollout = db.execute("SELECT * FROM skill_rollouts WHERE business=? AND name=?", (business, name)).fetchone()
            stable = rollout["stable_version"] if rollout else None
            if canary_percent < 100:
                stable_release = db.execute("SELECT * FROM releases WHERE business=? AND name=? AND version=? "
                                            "AND status='active'", (business, name, stable)).fetchone()
                if not stable_release or stable == version:
                    raise SkillError("灰度需要另一个已激活的稳定版本")
                self._verify(db, stable_release)
            keep = [version] + ([stable] if canary_percent < 100 else [])
            for active in db.execute("SELECT version FROM releases WHERE business=? AND name=? AND status='active'",
                                     (business, name)).fetchall():
                if active[0] not in keep:
                    db.execute("UPDATE releases SET status='deprecated' WHERE business=? AND name=? AND version=?",
                               (business, name, active[0]))
            db.execute("UPDATE releases SET status='active' WHERE business=? AND name=? AND version=?",
                       (business, name, version))
            db.execute("INSERT OR REPLACE INTO skill_rollouts VALUES(?,?,?,?,?)",
                       (business, name, version if canary_percent == 100 else stable,
                        None if canary_percent == 100 else version, canary_percent))
            self._audit(db, "rollback" if _rollback else "activate", business, name, version, reviewer,
                        {"canary_percent": canary_percent})
        return self.governance(business, name, version)

    def rollback(self, business, name, version, *, reviewer="local-reviewer"):
        return self.activate(business, name, version, reviewer=reviewer, _rollback=True)

    def publish(self, business_id, name, version, tools=(), *, available_tools=(), capability_bindings=None):
        """兼容 P1 本地 API：完整执行同一审核、评测、暂存和激活门禁。"""
        with self._db() as db:
            row = db.execute("SELECT * FROM releases WHERE business=? AND name=? AND version=?",
                             (business_id, name, version)).fetchone()
            if row and row["status"] == "active":
                payload, _ = self._verify(db, row)
                if payload["tools"] != sorted(set(tools)) or payload["capability_bindings"] != (capability_bindings or {}):
                    raise SkillError("已发布的绑定不可覆盖，请发布新版本")
                return self._metadata(db, row)
        self.validate(business_id, name, version, tools, available_tools=available_tools,
                      capability_bindings=capability_bindings)
        self.stage(business_id, name, version)
        self.activate(business_id, name, version)
        return next(item for item in self.active(business_id) if item["name"] == name and item["version"] == version)

    def _metadata(self, db, row):
        pkg = db.execute("SELECT description,digest FROM packages WHERE name=? AND version=?",
                         (row["name"], row["version"])).fetchone()
        return {key: row[key] for key in ("name", "version", "release_snapshot_id")} | dict(pkg) | {
            "tools": json.loads(row["tools"])}

    def active(self, business_id):
        with self._db() as db:
            result = []
            for row in db.execute("SELECT * FROM releases WHERE business=? AND status='active' ORDER BY name,version",
                                  (business_id,)):
                self._verify(db, row)
                result.append(self._metadata(db, row))
            return result

    def active_for(self, actor, business_id):
        candidates = {(r["name"], r["version"]): r for r in self.active(business_id)}
        result = []
        with self._db() as db:
            for row in db.execute("SELECT * FROM skill_rollouts WHERE business=? ORDER BY name", (business_id,)):
                bucket = int(_sha(_encode([business_id, row["name"],
                                          [actor.get(k, "") for k in ("tenant_id", "org_id", "user_id")]]))[:8], 16) % 100
                version = row["canary_version"] if row["canary_version"] and bucket < row["percent"] else row["stable_version"]
                selected = candidates.get((row["name"], version))
                if selected:
                    result.append(selected)
        return result

    select = active_for

    def has_bindings(self, business_id):
        with self._db() as db:
            # 待发布版本不改变已有入口；历史发布被吊销或待迁移时仍禁止绕过技能门禁。
            return db.execute("SELECT 1 FROM releases WHERE business=? "
                              "AND status NOT IN ('validated','staged') "
                              "UNION ALL SELECT 1 FROM skill_legacy WHERE business=? LIMIT 1",
                              (business_id, business_id)).fetchone() is not None

    def load(self, business_id, name, version):
        with self._db() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM releases WHERE business=? AND name=? AND version=? "
                             "AND status IN ('active','deprecated')", (business_id, name, version)).fetchone()
            if not row:
                raise SkillError("该 Skill 未发布或已撤回")
            payload, _ = self._verify(db, row)
            pkg = db.execute("SELECT body,manifest_yaml,schemas FROM packages JOIN package_details USING(name,version) "
                             "WHERE name=? AND version=?", (name, version)).fetchone()
            policy = _policy(_mapping(pkg["manifest_yaml"]) if pkg["manifest_yaml"] else {}, json.loads(pkg["schemas"]))
            refs = db.execute("SELECT path FROM refs WHERE name=? AND version=? ORDER BY path", (name, version))
            return self._metadata(db, row) | {"body": pkg[0], "references": [r[0] for r in refs],
                                              "capability_bindings": payload["capability_bindings"], **policy}

    def read_reference(self, business_id, name, version, path):
        path = _safe_key(path, "references")
        with self._db() as db:
            # 同一个 SQLite 读快照完成签名校验和内容读取，避免跨连接校验后被替换。
            db.execute("BEGIN")
            release = db.execute("SELECT * FROM releases WHERE business=? AND name=? AND version=? "
                                 "AND status IN ('active','deprecated')", (business_id, name, version)).fetchone()
            if not release:
                raise SkillError("该 Skill 未发布或已撤回")
            self._verify(db, release)
            row = db.execute("SELECT content FROM releases JOIN refs USING(name,version) WHERE business=? "
                             "AND name=? AND version=? AND path=? AND status IN ('active','deprecated')",
                             (business_id, name, version, path)).fetchone()
            if not row:
                raise SkillError("该引用未发布或已撤回")
            return row[0]

    def revoke(self, business_id, name, version, *, reviewer="local-reviewer"):
        self._identity(reviewer)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE releases SET status='revoked' WHERE business=? AND name=? AND version=?",
                                 (business_id, name, version))
            if not changed.rowcount:
                raise SkillError("场景中找不到该发布版本")
            rollout = db.execute("SELECT * FROM skill_rollouts WHERE business=? AND name=?", (business_id, name)).fetchone()
            if rollout and rollout["canary_version"] == version:
                db.execute("UPDATE skill_rollouts SET canary_version=NULL,percent=100 WHERE business=? AND name=?",
                           (business_id, name))
            self._audit(db, "revoke", business_id, name, version, reviewer)

    def audit(self, business=None):
        with self._db() as db:
            rows = db.execute("SELECT * FROM skill_audit " + ("WHERE business=? " if business else "") + "ORDER BY sequence",
                              (business,) if business else ())
            return [dict(row) | {"details": json.loads(row["details"])} for row in rows]

    def compare(self, business, name, first, second):
        a, b = self.governance(business, name, first), self.governance(business, name, second)
        old = {r["case_id"]: r["passed"] for r in a["evaluation"]["cases"]}
        new = {r["case_id"]: r["passed"] for r in b["evaluation"]["cases"]}
        return {"first": first, "second": second, "same_suite": a["evaluation"]["suite"] == b["evaluation"]["suite"],
                "regressions": sorted(k for k in old if old[k] and not new.get(k, False)),
                "bindings_compatible": a["capability_bindings"] == b["capability_bindings"],
                "first_run_id": a["evaluation_run_id"], "second_run_id": b["evaluation_run_id"]}

    def migrate_legacy(self, business, name, version, *, author, reviewer, available_tools):
        self._identity(author)
        with self._db() as db:
            row = db.execute("SELECT * FROM releases WHERE business=? AND name=? AND version=? AND status='legacy_pending'",
                             (business, name, version)).fetchone()
            if not row:
                raise SkillError("没有等待迁移的旧发布")
            db.execute("INSERT OR IGNORE INTO package_auth VALUES(?,?,?,?)",
                       (name, version, author, self._content_seal(db, name, version)))
            details = db.execute("SELECT capability_bindings FROM release_details WHERE release_snapshot_id=?",
                                 (row["release_snapshot_id"],)).fetchone()
        self.validate(business, name, version, json.loads(row["tools"]), available_tools=available_tools,
                      capability_bindings=json.loads(details[0]) if details else {}, author=author, reviewer=reviewer)
        self.stage(business, name, version, reviewer=reviewer)
        self.activate(business, name, version, reviewer=reviewer)
        return self.governance(business, name, version)
