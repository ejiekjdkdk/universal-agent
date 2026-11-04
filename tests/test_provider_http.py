"""真实回环 HTTP 验证协议；模型与数据均为虚构，不连接商业服务。"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from test_model_router import policy, response
from test_provider_router_integration import AUTH, BODY, call_reply


@contextmanager
def model_server(handler):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers["Content-Length"])
            body = json.loads(self.rfile.read(size))
            status, content, mime = handler(body)
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                # 刻意拆成小片段，包括 UTF-8 字符与 SSE 帧内部。
                for start in range(0, len(content), 7):
                    self.wfile.write(content[start:start + 7])
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def encode(body):
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize("status", [429, 503])
def test_http_fallback_preserves_one_tool_execution(tmp_path, status):
    calls = []
    def primary(body):
        calls.append(("primary", body))
        if len(calls) == 1:
            return 200, encode(call_reply("first")), "application/json"
        return status, b"{}", "application/json"
    def secondary(body):
        calls.append(("secondary", body))
        reply = call_reply("different-id") if len(calls) == 3 else response("虚构结果 [E1]")
        return 200, encode(reply), "application/json"
    config = policy()
    with model_server(primary) as first, model_server(secondary) as second:
        config["profiles"]["primary"]["base_url"] = first
        config["profiles"]["secondary"]["base_url"] = second
        app = create_app(database=tmp_path / "http.db", token="reader", model_settings={"provider_policy": config})
        with TestClient(app, base_url="http://localhost") as client:
            result = client.post("/api/v1/agent/chat", headers=AUTH, json=BODY).json()
            assert result.get("status") == "completed", result
            assert len(result["tool_calls"]) == 1
    assert [name for name, _ in calls] == ["primary", "primary", "secondary", "secondary"]
    assert all(body["response_format"]["json_schema"]["strict"] is True for _, body in calls)


def stream_reply(body, complete=True):
    choice = body["choices"][0]
    message = choice["message"]
    chunks = []
    if message.get("tool_calls"):
        call = message["tool_calls"][0]
        arguments = call["function"]["arguments"]
        for index, fragment in enumerate((arguments[:9], arguments[9:])):
            part = {"index": 0, "function": {"arguments": fragment}}
            if index == 0:
                part.update(id=call["id"], type="function")
                part["function"]["name"] = call["function"]["name"]
            chunks.append({"choices": [{"index": 0, "delta": {"tool_calls": [part]}, "finish_reason": None}]})
    else:
        chunks.append({"choices": [{"index": 0, "delta": message, "finish_reason": None}]})
    if complete:
        chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]})
    raw = b"".join(b"data: " + encode(chunk) + b"\n\n" for chunk in chunks)
    if complete:
        raw += b"data: [DONE]\n\n"
    return 200, raw, "text/event-stream"


def test_http_incomplete_stream_cannot_dispatch_partial_tool(tmp_path):
    calls = []
    def primary(body):
        calls.append("primary")
        return stream_reply(call_reply("must-not-run"), complete=False)
    def secondary(body):
        calls.append("secondary")
        # 第一次备选请求之前必须尚无物理工具执行。
        if len(calls) == 2:
            assert app.state.runtime.store._db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
            return stream_reply(call_reply("complete-call"))
        return stream_reply(response("完整虚构结果 [E1]"))
    config = policy()
    config["routes"]["notes"]["stream"] = True
    with model_server(primary) as first, model_server(secondary) as second:
        config["profiles"]["primary"]["base_url"] = first
        config["profiles"]["secondary"]["base_url"] = second
        app = create_app(database=tmp_path / "stream.db", token="reader", model_settings={"provider_policy": config})
        with TestClient(app, base_url="http://localhost") as client:
            result = client.post("/api/v1/agent/chat", headers=AUTH, json=BODY).json()
            assert result.get("status") == "completed", result
            assert len(result["tool_calls"]) == 1
    assert calls == ["primary", "secondary", "secondary"]
