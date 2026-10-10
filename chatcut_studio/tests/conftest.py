import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """每个测试用独立的数据目录，不碰真实设置和 ~/.claude/skills。"""
    from chatcut_studio import chatcut_mcp, jizura, settings, styles

    monkeypatch.setattr(settings, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(jizura, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(chatcut_mcp, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(styles, "DATA_DIR", tmp_path / "styles")
    monkeypatch.setattr(styles, "CLAUDE_SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setenv("CHATCUT_STUDIO_HOME", str(tmp_path / "home"))
    from chatcut_studio import songlab
    monkeypatch.setattr(songlab, "SONGS_ROOT", tmp_path / "songs")   # 查找 ChatCut Desktop 时不读真实的 Agent 配置
    from chatcut_studio import desktop
    monkeypatch.setattr(desktop, "from_processes", lambda: [])
    for k in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "MOONSHOT_API_KEY", "ZHIPUAI_API_KEY",
              "CHATCUT_STUDIO_DEMO"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app) -> tuple[str, uvicorn.Server]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", server


class FakeLLM:
    """假的 OpenAI 兼容服务（模拟 DeepSeek 等）。script 是按调用次序返回的 message 列表。"""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        app = FastAPI()

        @app.get("/v1/models")
        def models():
            return {"data": [{"id": "fake-pro"}, {"id": "fake-flash"}]}

        @app.post("/v1/chat/completions")
        async def chat(req: Request):
            body = await req.json()
            self.requests.append({"auth": req.headers.get("authorization"), "body": body})
            msg = self.script.pop(0)
            if callable(msg):
                msg = msg(body)
            finish = "tool_calls" if msg.get("tool_calls") else "stop"
            return {"choices": [{"message": {"role": "assistant", **msg}, "finish_reason": finish}]}

        self.app = app


@pytest.fixture
def fake_llm():
    servers = []

    def make(script):
        llm = FakeLLM(script)
        url, srv = serve(llm.app)
        servers.append(srv)
        llm.base_url = url + "/v1"
        return llm

    yield make
    for s in servers:
        s.should_exit = True


@pytest.fixture
def fake_chatcut():
    from chatcut_studio import fake_chatcut as fc

    fc.STATE.update({"aspect_ratio": "16:9", "clips": [], "captions": [], "exports": 0})
    url, srv = serve(fc.server.streamable_http_app())
    yield url + "/mcp", fc.STATE
    srv.should_exit = True


def tool_call(cid, name, args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
