"""本机模型网关：Claude Code（ChatCut Desktop 里的 Agent）→ Studio → DeepSeek。"""
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from chatcut_studio import gateway, settings
from chatcut_studio.app import create_app

from .conftest import FakeLLM, serve, tool_call


class FakeDeepSeekAnthropic:
    """模拟 api.deepseek.com/anthropic：记录收到的请求，按 stream 返回 JSON 或 SSE。"""

    def __init__(self):
        self.requests = []
        app = FastAPI()

        @app.post("/anthropic/v1/messages")
        async def messages(req: Request):
            body = await req.json()
            self.requests.append({"headers": dict(req.headers), "body": body})
            msg = {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                   "content": [{"type": "text", "text": "连接成功"}], "stop_reason": "end_turn",
                   "usage": {"input_tokens": 3, "output_tokens": 2}}
            if not body.get("stream"):
                return msg

            async def gen():
                yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {**msg, 'content': []}})}\n\n"
                yield 'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
                yield 'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好"}}\n\n'
                yield 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            return StreamingResponse(gen(), media_type="text/event-stream")

        self.app = app


@pytest.fixture
def deepseek():
    fake = FakeDeepSeekAnthropic()
    url, srv = serve(fake.app)
    settings.update({"providers": [{"id": "deepseek", "model": "deepseek-v4-pro", "api_key": "sk-ds-real-key-123456",
                                    "anthropic_url": url + "/anthropic"}],
                     "gateway": {"provider": "deepseek", "small_model": "deepseek-v4-flash"}})
    yield fake
    srv.should_exit = True


def test_passthrough_to_deepseek(deepseek):
    tok = settings.load().gateway.token
    with TestClient(create_app()) as c:
        body = {"model": "claude-sonnet-4", "max_tokens": 100, "metadata": {"user_id": "user_abc@x.com/session:1"},
                "messages": [{"role": "user", "content": "hi"}]}
        assert c.post("/gateway/v1/messages", json=body).status_code == 401              # 没带 Token
        r = c.post("/gateway/v1/messages", json=body, headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200 and r.json()["content"][0]["text"] == "连接成功"
        r = c.post("/gateway/v1/messages", json={**body, "model": "claude-haiku-4-5", "stream": True},
                   headers={"x-api-key": tok, "anthropic-beta": "some-beta"})
        assert r.headers["content-type"].startswith("text/event-stream") and "你好" in r.text
        n = c.post("/gateway/v1/messages/count_tokens", json=body, headers={"x-api-key": tok}).json()
    first, second = deepseek.requests
    assert first["body"]["model"] == "deepseek-v4-pro" and second["body"]["model"] == "deepseek-v4-flash"   # haiku 档 → 快速模型
    assert first["headers"]["authorization"] == "Bearer sk-ds-real-key-123456"     # 换成真正的 DeepSeek Key
    assert first["body"]["metadata"]["user_id"] == "user_abc_x_com_session_1"     # DeepSeek 只认字母数字
    assert "anthropic-beta" not in second["headers"]                              # 第三方接口不转发 beta 头
    assert n["input_tokens"] > 0


def test_self_test_and_errors(deepseek):
    r = __import__("asyncio").run(gateway.self_test())
    assert r == {"ok": True, "message": "连接成功", "model": "deepseek-v4-pro"}
    settings.update({"providers": [{"id": "deepseek", "api_key": "-"}]})
    r = __import__("asyncio").run(gateway.self_test())
    assert not r["ok"] and "API Key" in r["message"]


def test_link_and_unlink_claude_code(isolated_data):
    home = isolated_data / "home" / ".claude"
    home.mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_MODEL": "my-old", "FOO": "1"}, "theme": "dark"}), "utf-8")
    settings.update({"providers": [{"id": "deepseek", "model": "deepseek-v4-pro", "api_key": "sk-x-1234567890"}]})
    st = gateway.link()
    data = json.loads((home / "settings.json").read_text("utf-8"))
    assert st["linked"] and data["theme"] == "dark" and data["env"]["FOO"] == "1"
    assert data["env"]["ANTHROPIC_BASE_URL"] == gateway.gateway_url() and data["env"]["ANTHROPIC_MODEL"] == "deepseek-v4-pro"
    assert data["env"]["ANTHROPIC_AUTH_TOKEN"] == settings.load().gateway.token
    assert "sk-x" not in json.dumps(data)                                    # 真正的 Key 不写进 Claude Code
    gateway.link()                                                           # 再接一次不覆盖备份
    settings.update({"providers": [{"id": "deepseek", "model": "deepseek-v4-flash"}]})
    gateway.refresh_if_linked()
    assert json.loads((home / "settings.json").read_text("utf-8"))["env"]["ANTHROPIC_MODEL"] == "deepseek-v4-flash"
    assert not gateway.unlink()["linked"]
    assert json.loads((home / "settings.json").read_text("utf-8")) == {"env": {"ANTHROPIC_MODEL": "my-old", "FOO": "1"}, "theme": "dark"}


def test_openai_translation_with_tools(fake_llm):
    """只有 OpenAI 兼容接口时：工具调用来回翻译，流式按 Anthropic 事件发回。"""
    llm = fake_llm([{"content": "我先看看项目", "tool_calls": [tool_call("c1", "get_project", {"id": 1})]}])
    settings.update({"providers": [{"id": "custom", "base_url": llm.base_url, "model": "m1", "api_key": "sk-c-1234567890"}],
                     "gateway": {"provider": "custom"}})
    tok = settings.load().gateway.token
    body = {"model": "x", "max_tokens": 50000, "stream": True, "system": [{"type": "text", "text": "你是剪辑助手"}],
            "tools": [{"name": "get_project", "description": "读项目", "input_schema": {"type": "object"}},
                      {"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{"role": "user", "content": "看看"},
                         {"role": "assistant", "content": [{"type": "tool_use", "id": "t0", "name": "get_project", "input": {}}]},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t0", "content": "空项目"},
                                                      {"type": "text", "text": "继续"}]}]}
    with TestClient(create_app()) as c:
        r = c.post("/gateway/v1/messages", json=body, headers={"x-api-key": tok})
    sent = llm.requests[0]["body"]
    assert [m["role"] for m in sent["messages"]] == ["system", "user", "assistant", "tool", "user"]
    assert sent["messages"][3] == {"role": "tool", "tool_call_id": "t0", "content": "空项目"}
    assert [t["function"]["name"] for t in sent["tools"]] == ["get_project"] and sent["max_tokens"] == 8192
    events = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "message_start" and kinds[-1] == "message_stop"
    tool = next(e for e in events if e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use")
    assert tool["content_block"]["name"] == "get_project"
    assert json.loads(next(e for e in events if e.get("delta", {}).get("type") == "input_json_delta")["delta"]["partial_json"]) == {"id": 1}
    assert next(e for e in events if e["type"] == "message_delta")["delta"]["stop_reason"] == "tool_use"


def test_auto_route_follows_base_url():
    """官方 DeepSeek 地址走 Anthropic 兼容接口；中转等其他 OpenAI 兼容地址自动走翻译。"""
    settings.update({"providers": [{"id": "deepseek", "base_url": "https://api.deepseek.com", "anthropic_url": ""}],
                     "gateway": {"provider": "deepseek", "mode": "auto"}})
    assert gateway.route(settings.load())[1] == "anthropic"
    settings.update({"providers": [{"id": "deepseek", "base_url": "https://my-relay.example.com/v1"}]})
    assert gateway.route(settings.load())[1] == "openai"
    settings.update({"providers": [{"id": "deepseek", "base_url": "https://api.deepseek.com"}], "gateway": {"mode": "openai"}})
    assert gateway.route(settings.load())[1] == "openai"


def test_reasoning_content_round_trip():
    """DeepSeek 思考模式：reasoning_content ↔ thinking 块，下一轮原样带回。"""
    msg = gateway.from_openai({"choices": [{"finish_reason": "tool_calls", "message": {
        "content": "", "reasoning_content": "先读文件",
        "tool_calls": [{"id": "c1", "function": {"name": "Read", "arguments": "{\"file_path\": \"a.txt\"}"}}]}}]}, "m")
    assert [b["type"] for b in msg["content"]] == ["thinking", "tool_use"] and msg["stop_reason"] == "tool_use"
    back = gateway.to_openai({"messages": [{"role": "user", "content": "读"}, {"role": "assistant", "content": msg["content"]}]}, "m")
    assert back["messages"][1]["reasoning_content"] == "先读文件"
    assert back["messages"][1]["tool_calls"][0]["function"]["name"] == "Read"
    ev = b"".join(gateway.sse_events(msg)).decode()
    assert '"thinking_delta"' in ev and '"signature_delta"' in ev and '"input_json_delta"' in ev


def test_diagnose_finds_qwen_overrides(isolated_data, monkeypatch):
    """诊断：找出环境变量、高优先级设置、ChatCut Desktop 配置里残留的 Qwen。"""
    import asyncio
    from chatcut_studio import diagnose

    home = isolated_data / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.local.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://dashscope.aliyuncs.com/apps/anthropic"}}), "utf-8")
    cfg = home / "AppData" / "Roaming" / "ChatCut" / "agents.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"claudeCode": {"model": "qwen3-coder-plus", "apiKey": "sk-abcdefghijklmnop"}}, indent=1), "utf-8")
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("ANTHROPIC_MODEL", "qwen3-coder-plus")
    settings.update({"providers": [{"id": "deepseek", "model": "deepseek-v4-pro", "api_key": "sk-x-1234567890"}]})
    gateway.link()
    d = asyncio.run(diagnose.diagnose(run_claude=False))
    text = " ".join(f["text"] for f in d["findings"])
    assert "settings.local.json" in text and "Qwen" in text                  # 本地设置优先级更高，指向阿里云
    assert "ANTHROPIC_MODEL" in text                                         # 环境变量冲突
    assert any("qwen3-coder-plus" in c["line"] for c in d["desktop_configs"])
    assert all("abcdefghijklmnop" not in c["line"] for c in d["desktop_configs"])   # Key 打码
    assert "ChatCut Desktop 自己的配置里提到了 Qwen" in text
