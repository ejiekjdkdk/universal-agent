"""本地 Skill 包注册表。只有受信任的发布操作可以绑定场景工具。"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
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
    digest = _sha(_encode([entry, manifest_text, schemas, refs]))
    return _Package(name, version, description.strip(), digest, body, refs, manifest_text,
                    {key: json.loads(value) for key, value in schemas.items()}, _dependencies(manifest))


class SkillRegistry:
    def __init__(self, root):
        directory = Path(root)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "skills.sqlite3"
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
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_package(self, directory, version=None):
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

    def publish(self, business_id, name, version, tools=(), *, available_tools=(), capability_bindings=None):
        _identifier(business_id, r"[a-z][a-z0-9_]*", "业务 ID")
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
        key = (business_id, name, version)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            pkg = db.execute("SELECT name,version,description,digest,dependencies FROM packages "
                             "JOIN package_details USING(name,version) WHERE name=? AND version=?",
                             (name, version)).fetchone()
            if not pkg:
                raise SkillError("找不到待发布草稿")
            if set(bindings) != set(json.loads(pkg["dependencies"])):
                raise SkillError("请为每个所需的能力和确切版本提供受信任的工具映射")
            snapshot = _sha(_encode([*key, pkg["digest"], grants, bindings]))
            existing = db.execute("SELECT release_snapshot_id,status FROM releases WHERE business=? "
                                  "AND name=? AND version=?", key).fetchone()
            if existing:
                if existing["release_snapshot_id"] != snapshot or existing["status"] != "active":
                    raise SkillError("已发布的绑定不可覆盖，请发布新版本")
            else:
                db.execute("UPDATE releases SET status='deprecated' WHERE business=? AND name=? "
                           "AND status='active'", key[:2])
                db.execute("INSERT INTO releases VALUES(?,?,?,?,?,'active')", (*key, _encode(grants), snapshot))
                db.execute("INSERT INTO release_details VALUES(?,?)", (snapshot, _encode(bindings)))
            return {k: pkg[k] for k in ("name", "version", "description", "digest")} | {
                "release_snapshot_id": snapshot, "tools": grants}

    def active(self, business_id):
        with self._db() as db:
            return [dict(row) | {"tools": json.loads(row["tools"])} for row in db.execute("""
                SELECT name,version,description,digest,release_snapshot_id,tools FROM releases JOIN packages
                USING(name,version) JOIN release_details USING(release_snapshot_id)
                WHERE business=? AND status='active' ORDER BY name""", (business_id,))]

    def has_bindings(self, business_id):
        with self._db() as db:
            return db.execute("SELECT 1 FROM releases WHERE business=? LIMIT 1", (business_id,)).fetchone() is not None

    def load(self, business_id, name, version):
        with self._db() as db:
            db.execute("BEGIN")
            row = db.execute("""SELECT name,version,description,digest,release_snapshot_id,body,tools,capability_bindings
                          FROM releases JOIN packages USING(name,version) JOIN release_details USING(release_snapshot_id)
                          WHERE business=? AND name=? AND version=? AND status IN ('active','deprecated')""",
                             (business_id, name, version)).fetchone()
            if not row:
                raise SkillError("该 Skill 未发布或已撤回")
            refs = db.execute("SELECT path FROM refs WHERE name=? AND version=? ORDER BY path", (name, version))
            return dict(row) | {"tools": json.loads(row["tools"]), "references": [r[0] for r in refs],
                                "capability_bindings": json.loads(row["capability_bindings"])}

    def read_reference(self, business_id, name, version, path):
        with self._db() as db:
            row = db.execute("""SELECT content FROM releases JOIN refs USING(name,version)
                            JOIN release_details USING(release_snapshot_id)
                            WHERE business=? AND name=? AND version=? AND path=?
                            AND status IN ('active','deprecated')""",
                             (business_id, name, version, _safe_key(path, "references"))).fetchone()
            if not row:
                raise SkillError("该引用未发布或已撤回")
            return row[0]

    def revoke(self, business_id, name, version):
        with self._db() as db:
            changed = db.execute("UPDATE releases SET status='revoked' WHERE business=? AND name=? AND version=?",
                                 (business_id, name, version))
            if not changed.rowcount:
                raise SkillError("场景中找不到该发布版本")
