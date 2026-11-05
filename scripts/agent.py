"""离线技能开发工具；工件不是发布凭据，发布继续使用 skillctl.py。"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.skill_devkit import doctor, init_skill, inspect_artifact, install_artifact, pack_skill, test_skill, validate_skill


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    health = commands.add_parser("doctor", help="只读检查本地依赖状态")
    health.add_argument("--scope", choices=["local"], required=True)
    skills = commands.add_parser("skill").add_subparsers(dest="action", required=True)
    initialize = skills.add_parser("init", help="生成不覆盖已有目录的技能模板")
    initialize.add_argument("directory", type=Path)
    initialize.add_argument("--business", required=True)
    for name in ("validate", "test", "pack"):
        action = skills.add_parser(name)
        action.add_argument("directory", type=Path)
        action.add_argument("--capabilities", type=Path, help="操作者批准的 ID@VERSION JSON 列表")
        if name == "test":
            action.add_argument("--offline", action="store_true", required=True)
        if name == "pack":
            action.add_argument("--out", type=Path, required=True)
            action.add_argument("--signing-key-env", help="保存本地工件HMAC密钥的环境变量名")
    inspect = skills.add_parser("inspect", help="有界读取并验证候选工件，不解压安装")
    inspect.add_argument("artifact", type=Path)
    inspect.add_argument("--signing-key-env")
    install = skills.add_parser("install", help="验证签名并安装为本地无权限草稿")
    install.add_argument("artifact", type=Path)
    install.add_argument("--root", required=True, type=Path)
    install.add_argument("--author", required=True, help="本地操作员确认的作者标识")
    install.add_argument("--signing-key-env", required=True)
    install.add_argument("--capabilities", type=Path, help="操作者批准的 ID@VERSION JSON 列表")
    args = parser.parse_args(argv)
    try:
        key = None
        if getattr(args, "signing_key_env", None):
            raw = os.environ.get(args.signing_key_env)
            if not raw:
                raise ValueError("指定的签名密钥环境变量不可用")
            key = raw.encode("utf-8")
        capabilities = []
        if getattr(args, "capabilities", None):
            capabilities = json.loads(args.capabilities.read_text(encoding="utf-8-sig"))
            if not isinstance(capabilities, list) or len(capabilities) > 128:
                raise ValueError("批准能力清单必须为至多128项的JSON列表")
        if args.command == "doctor":
            result = doctor()
        elif args.action == "init":
            result = init_skill(args.directory, args.business)
        elif args.action == "validate":
            result = validate_skill(args.directory, capabilities)
        elif args.action == "test":
            result = test_skill(args.directory, capabilities)
        elif args.action == "pack":
            result = pack_skill(args.directory, args.out, capabilities, signing_key=key)
        elif args.action == "install":
            result = install_artifact(args.artifact, args.root, capabilities, signing_key=key, author=args.author)
        else:
            result = inspect_artifact(args.artifact, signing_key=key)
    except (ValueError, OSError) as error:
        result = {"status": "FAIL", "error": type(error).__name__, "message": "技能检查或文件操作失败，请检查路径、格式、依赖和可信密钥。"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    configure_stdout = getattr(sys.stdout, "reconfigure", None)
    if configure_stdout:
        configure_stdout(encoding="utf-8")
    raise SystemExit(main())
