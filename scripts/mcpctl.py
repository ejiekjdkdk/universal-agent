"""读取MCP候选目录；此命令不会批准目录，也不会执行远端工具。"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.errors import Fault
from app.mcp_gateway import MCPContext, MCPGateway


def main():
    parser = argparse.ArgumentParser(description="检查受信任配置中的MCP候选目录，不自动批准")
    parser.add_argument("--config", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--principal", default="local-operator")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        gateway = MCPGateway(args.config)
        context = MCPContext(tenant_id=args.tenant, principal_id=args.principal,
                             run_context_id="catalog-inspect", trace_id="catalog-inspect", authorize=lambda: None)
        candidate = asyncio.run(gateway.inspect_catalog(args.server, args.tool, context=context))
        _, server, _ = gateway._resolve(args.server, args.tool, context)
        approved = server.get("p2", {}).get("catalog_digest")
        result = {**candidate, "approved_digest": approved, "matches_approval": approved == candidate["catalog_digest"],
                  "notice": "候选数据来自远端；须审核工具名称、描述及输入输出Schema，再由本机操作者修改批准配置。"}
        encoded = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.write_text(encoded + "\n", encoding="utf-8")
        else:
            print(encoded)
        return 0
    except Fault as exc:
        print(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False), file=sys.stderr)
    except (OSError, ValueError, KeyError):
        print("MCP配置或输出路径不可用。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
