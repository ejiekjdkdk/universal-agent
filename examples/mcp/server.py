"""使用仓库锁定的 SDK 启动虚构数据的本机 MCP 服务器。"""
import asyncio

from mcp.server import MCPServer
from mcp.server.mcpserver import Context


def create_server():
    server = MCPServer("local-demonstration", version="1.0.0")

    @server.tool()
    async def private_lookup(text: str, ctx: Context) -> dict:
        """返回虚构记录，通过保留输入模拟网关需要处理的故障。"""
        if text == "timeout":
            await asyncio.sleep(10)
        if text == "business_error":
            raise ValueError("private internal detail must not reach callers")
        if text == "invalid":
            return {"data": {"summary": 123}, "evidence": []}
        if text == "oversized":
            return {"data": {"summary": "x" * 300000}, "evidence": []}
        if text == "many_nodes":
            from mcp import types
            return types.CallToolResult(content=[], structured_content={"data": {"summary": "结构上限测试"}, "evidence": [0] * 10001})
        if text == "tenant":
            text = (ctx.headers or {}).get("x-agent-tenant", "missing")
        if text == "credential":
            text = (ctx.headers or {}).get("authorization", "missing")
        return {"data": {"summary": text}, "evidence": []}

    return server


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_server().streamable_http_app(json_response=True), host="127.0.0.1", port=8123)
