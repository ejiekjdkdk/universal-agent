"""从受信任的本地终端导入、发布、查询和撤回 Skill 包。"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.catalog import Catalog
from app.skills import SkillError, SkillRegistry, _mapping


def _reject_spoofed_manifest(text):
    manifest = {}
    if text:
        manifest = _mapping(text)
    if manifest.get("status", "draft") != "draft" or set(manifest) & {
        "signature", "reviewer", "evaluation_run_id", "release_snapshot_id"}:
        raise SkillError("manifest 不得伪造发布状态、审核者、签名或评测凭证")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="与服务使用相同的本地 Skill 注册表目录")
    parser.add_argument("--reviewer", default="local-reviewer")
    parser.add_argument("--author", default="local-author")
    actions = parser.add_subparsers(dest="action", required=True)
    ingest = actions.add_parser("import", help="将标准 SKILL.md 包导入为无权限草稿")
    ingest.add_argument("directory", type=Path)
    ingest.add_argument("--version", help="默认使用 manifest 版本，未声明时为 1.0.0")
    publish = actions.add_parser("publish", help="为业务发布指定版本并显式绑定工具")
    publish.add_argument("--scenarios", type=Path, default=os.environ.get("AGENT_SCENARIOS") or ROOT / "scenarios",
                         help="受信任的本地场景目录（其 Python 适配器经审核后方可加载）")
    publish.add_argument("--tool", action="append", default=[], help="允许使用的现有场景工具名，可重复")
    publish.add_argument("--bind-capability", action="append", default=[], metavar="ID@VERSION=TOOL",
                         help="审核者将包所需的精确能力版本映射到已显式绑定的工具")
    inspect = actions.add_parser("inspect", help="查看已冻结的草稿正文、manifest 和 Schema")
    inspect.add_argument("name")
    inspect.add_argument("version")
    revoke = actions.add_parser("revoke", help="撤回一个场景版本，包括旧任务绑定")
    operations = []
    for action in ("stage", "activate", "rollback", "details"):
        operation = actions.add_parser(action)
        if action == "activate":
            operation.add_argument("--canary-percent", type=int, default=100)
        operations.append(operation)
    validate = actions.add_parser("validate", parents=[], help="执行独立审核和固定评测")
    for flag, options in (("--scenarios", {"type": Path, "default": ROOT / "scenarios"}),
                          ("--tool", {"action": "append", "default": []}),
                          ("--bind-capability", {"action": "append", "default": []})):
        validate.add_argument(flag, **options)
    migrate = actions.add_parser("migrate", help="将隔离的 P1 旧发布显式升级为 V3")
    migrate.add_argument("--scenarios", type=Path, default=ROOT / "scenarios")
    actions.add_parser("audit").add_argument("--business-id")
    compare = actions.add_parser("compare")
    for field in ("business_id", "name", "first", "second"):
        compare.add_argument(field)
    for command in (publish, revoke, validate, migrate, *operations):
        command.add_argument("business_id")
        command.add_argument("name")
        command.add_argument("version")
    actions.add_parser("list", help="列出场景当前已发布的 Skill").add_argument("business_id")
    args = parser.parse_args(argv)
    try:
        registry = SkillRegistry(args.root)
        if args.action == "import":
            manifest = args.directory / "skill.manifest.yaml"
            manifest_text = ""
            if manifest.exists():
                manifest_text = manifest.read_text(encoding="utf-8-sig")
            _reject_spoofed_manifest(manifest_text)
            result = registry.import_package(args.directory, args.version, author=args.author)
        elif args.action == "list":
            result = registry.active(args.business_id)
        elif args.action == "inspect":
            result = registry.inspect_package(args.name, args.version)
        elif args.action == "audit":
            result = registry.audit(args.business_id)
        elif args.action == "compare":
            result = registry.compare(args.business_id, args.name, args.first, args.second)
        elif args.action == "details":
            result = registry.governance(args.business_id, args.name, args.version)
        elif args.action == "stage":
            result = registry.stage(args.business_id, args.name, args.version, reviewer=args.reviewer)
        elif args.action == "activate":
            result = registry.activate(
                args.business_id, args.name, args.version,
                reviewer=args.reviewer, canary_percent=args.canary_percent,
            )
        elif args.action == "rollback":
            result = registry.rollback(args.business_id, args.name, args.version, reviewer=args.reviewer)
        elif args.action == "revoke":
            registry.revoke(args.business_id, args.name, args.version, reviewer=args.reviewer)
            result = {"status": "revoked", "business_id": args.business_id, "name": args.name, "version": args.version}
        elif args.action in ("validate", "publish", "migrate"):
            _reject_spoofed_manifest(registry.inspect_package(args.name, args.version)["manifest_yaml"])
            catalog = Catalog(args.scenarios)
            if args.business_id not in catalog.scenarios:
                raise SkillError("受信任目录中无此业务")
            names = []
            for tool in catalog.discover(args.business_id):
                if tool["action_level"] == "read":
                    names.append(tool["name"])
            if catalog.scenarios[args.business_id].get("documents"):
                names.append("retrieve_knowledge")
            if args.action == "migrate":
                result = registry.migrate_legacy(args.business_id, args.name, args.version,
                                                author=args.author, reviewer=args.reviewer, available_tools=names)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return
            bindings = {}
            for binding in args.bind_capability:
                ref, separator, tool = binding.partition("=")
                if not separator or not ref or ref in bindings or not tool:
                    raise SkillError("能力映射需要唯一的 ID@VERSION=TOOL")
                bindings[ref] = tool
            result = registry.validate(args.business_id, args.name, args.version, args.tool,
                                       available_tools=names, capability_bindings=bindings, reviewer=args.reviewer)
            if args.action == "publish":
                registry.stage(args.business_id, args.name, args.version, reviewer=args.reviewer)
                registry.activate(args.business_id, args.name, args.version, reviewer=args.reviewer)
                result = next(item for item in registry.active(args.business_id)
                              if item["name"] == args.name and item["version"] == args.version)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Skill 操作失败：{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
