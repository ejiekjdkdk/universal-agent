"""仅显式运行本地测试夹具的录制、核验、回放与差分命令。"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.errors import Fault
from app.replay import MAX_BYTES, _load, compare_bundles, inspect_bundle, record_case, run_replay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record")
    record.add_argument("--case", required=True)
    record.add_argument("--out", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--bundle", required=True)
    run = commands.add_parser("run")
    run.add_argument("--bundle", required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--candidate", required=True)
    for command in (record, inspect, run, compare):
        command.add_argument("--key-env", required=True, help="本地可信签名密钥所在环境变量，工件内不保存密钥")
    args = parser.parse_args()
    try:
        key = os.environ[args.key_env].encode("utf-8")
        if args.command == "record":
            with Path(args.case).open("rb") as stream:
                case = _load(stream.read(MAX_BYTES + 1))
            result = record_case(case, args.out, key)
            # 控制台默认不打印整个夹具或回答，完整脱敏报告保存在显式输出工件内。
            result = {field: result[field] for field in ("status", "case_id", "bundle_digest")}
        elif args.command == "inspect":
            bundle = inspect_bundle(args.bundle, key)
            result = {"status": bundle["status"], "manifest": bundle["manifest"]}
        elif args.command == "run":
            result = run_replay(args.bundle, key)
        else:
            result = compare_bundles(args.baseline, args.candidate, key)
    except (OSError, ValueError, KeyError, Fault):
        result = {"status": "FAIL", "error": "REPLAY_INVALID", "message": "回放输入、版本、密钥或输出路径无效。"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
