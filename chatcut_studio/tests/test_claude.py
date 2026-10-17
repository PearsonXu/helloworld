"""切换到 Claude：Codex / Claude Code 经网关用 Claude（官方 SDK），一键切换，配图提示词用 Claude。"""
import asyncio
import base64
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from chatcut_studio import gateway, settings
from chatcut_studio.app import create_app

from .conftest import serve


class FakeClaude:
    """模拟 Claude Messages API：按脚本依次返回内容块；stream=true 时按官方事件格式分段发。"""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        app = FastAPI()

        @app.post("/v1/messages")
        async def messages(req: Request):
            body = await req.json()
            self.requests.append({"headers": dict(req.headers), "body": body})
            blocks, stop = self.script.pop(0)
            if callable(blocks):
                blocks, stop = blocks(body)
            msg = {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"], "content": blocks,
                   "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 7}}
            if not body.get("stream"):
                return msg

            def ev(name, data):
                return f"event: {name}\ndata: {json.dumps({'type': name, **data}, ensure_ascii=False)}\n\n"

            async def gen():
                yield ev("message_start", {"message": {**msg, "content": [], "stop_reason": None, "usage": {"input_tokens": 12, "output_tokens": 0}}})
                for i, b in enumerate(blocks):
                    if b["type"] == "thinking":
                        yield ev("content_block_start", {"index": i, "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
                        yield ev("content_block_delta", {"index": i, "delta": {"type": "thinking_delta", "thinking": b["thinking"]}})
                        yield ev("content_block_delta", {"index": i, "delta": {"type": "signature_delta", "signature": b["signature"]}})
                    elif b["type"] == "text":
                        yield ev("content_block_start", {"index": i, "content_block": {"type": "text", "text": ""}})
                        yield ev("content_block_delta", {"index": i, "delta": {"type": "text_delta", "text": b["text"]}})
                    else:
                        yield ev("content_block_start", {"index": i, "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}})
                        yield ev("content_block_delta", {"index": i, "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}})
                    yield ev("content_block_stop", {"index": i})
                yield ev("message_delta", {"delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 7}})
                yield ev("message_stop", {})
            return StreamingResponse(gen(), media_type="text/event-stream")

        self.app = app


@pytest.fixture
def claude():
    servers = []

    def make(script):
        fake = FakeClaude(script)
        url, srv = serve(fake.app)
        servers.append(srv)
        settings.update({"providers": [{"id": "claude", "base_url": url, "model": "claude-opus-5-5", "api_key": "sk-ant-test-1234567890"}]})
        return fake
    yield make
    for s in servers:
        s.should_exit = True


def use(pid):
    settings.update({"agent_provider": pid, "style_provider": pid, "gateway": {"provider": pid}})


def test_codex_to_claude_round_trip_keeps_thinking_blocks(claude):
    """Codex（Responses）→ 网关 → Claude：工具调用往返，上一轮的思考块（带签名）原样带回。"""
    first = [{"type": "thinking", "thinking": "", "signature": "sig-AAA"},
             {"type": "text", "text": "我先看看文件。"},
             {"type": "tool_use", "id": "toolu_1", "name": "exec_command", "input": {"cmd": "cat a.txt"}}]
    fake = claude([(first, "tool_use"), ([{"type": "text", "text": "文件内容是：暗号"}], "end_turn")])
    use("claude")
    tok = settings.load().gateway.token
    tools = [{"type": "function", "name": "exec_command", "description": "运行命令",
              "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}]
    base = {"model": "gpt-5", "instructions": "你是助手", "tools": tools, "tool_choice": "auto", "stream": True,
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "读 a.txt"}]}]}
    with TestClient(create_app()) as c:
        r1 = c.post("/gateway/openai/v1/responses", json=base, headers={"Authorization": f"Bearer {tok}"})
        events = [json.loads(l[6:]) for l in r1.text.splitlines() if l.startswith("data: ")]
        out = events[-1]["response"]["output"]
        assert events[-1]["type"] == "response.completed", events[-1]
        assert [o["type"] for o in out] == ["reasoning", "message", "function_call"]
        assert out[0]["encrypted_content"].startswith("ccsA:") and out[2]["call_id"] == "toolu_1"
        second = {**base, "input": base["input"] + [out[0], out[1], out[2],
                                                    {"type": "function_call_output", "call_id": "toolu_1", "output": "暗号"}]}
        r2 = c.post("/gateway/openai/v1/responses", json=second, headers={"Authorization": f"Bearer {tok}"})
        assert "文件内容是：暗号" in r2.text
    req1, req2 = fake.requests[0]["body"], fake.requests[1]["body"]
    assert req1["model"] == "claude-opus-5-5" and req1["system"] == "你是助手" and req1["stream"] is True
    assert req1["thinking"] == {"type": "adaptive"} and req1["output_config"] == {"effort": "high"}
    assert req1["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in fake.requests[0]["headers"]["anthropic-beta"]
    assert req1["tool_choice"] == {"type": "auto"} and req1["tools"][0]["input_schema"]["properties"]["cmd"]
    assert fake.requests[0]["headers"]["x-api-key"] == "sk-ant-test-1234567890"
    assistant = req2["messages"][1]
    assert assistant["role"] == "assistant" and assistant["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig-AAA"}
    assert [b["type"] for b in assistant["content"]] == ["thinking", "text", "tool_use"]
    assert req2["messages"][2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "暗号"}]}


def test_claude_code_passthrough_to_claude(claude):
    """Claude Code → 网关 → Claude：原样转发，换成真正的 Key；Haiku 小任务照用 Haiku。"""
    fake = claude([([{"type": "text", "text": "好"}], "end_turn")] * 2)
    use("claude")
    tok = settings.load().gateway.token
    with TestClient(create_app()) as c:
        body = {"model": "claude-sonnet-5-5", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
        r = c.post("/gateway/v1/messages", json=body, headers={"Authorization": f"Bearer {tok}", "anthropic-beta": "some-beta"})
        assert r.status_code == 200 and r.json()["content"][0]["text"] == "好"
        c.post("/gateway/v1/messages", json={**body, "model": "claude-haiku-5-5"}, headers={"x-api-key": tok})
    h0, b0 = fake.requests[0]["headers"], fake.requests[0]["body"]
    assert h0["x-api-key"] == "sk-ant-test-1234567890" and "authorization" not in h0 and h0["anthropic-beta"] == "some-beta"
    assert b0["model"] == "claude-opus-5-5"                      # Studio 里选的模型优先
    assert fake.requests[1]["body"]["model"] == "claude-haiku-5-5"
    st = gateway.status()
    assert st["how"] == "anthropic" and st["model"] == "claude-opus-5-5" and not st["problem"]


def test_one_click_switch_and_prompts_with_claude(claude):
    """一键切换：剪辑 Agent / 风格优化 / Desktop 一起换；歌曲工坊的配图提示词改用 Claude（结构化输出）。"""
    from chatcut_studio import songlab
    scenes = {"scenes": [{"title": "巷口暖灯", "lyrics": "巷口灯光忽明忽灭", "description": "雨后小巷", "prompt_en": "rainy alley, no text"},
                         {"title": "咖啡", "lyrics": "手中甜咖啡已冷却", "description": "窗边咖啡", "prompt_en": "coffee, no text"},
                         {"title": "弯月", "lyrics": "仲夏的弯月", "description": "湖上弯月", "prompt_en": "moon, no text"}]}
    fake = claude([([{"type": "text", "text": json.dumps(scenes, ensure_ascii=False)}], "end_turn")])
    with TestClient(create_app()) as c:
        pub = c.put("/api/settings", json={"agent_provider": "claude", "style_provider": "claude", "gateway": {"provider": "claude"}}).json()
        assert pub["agent_provider"] == pub["style_provider"] == pub["gateway"]["provider"] == "claude"
        claude_pub = next(p for p in pub["providers"] if p["id"] == "claude")
        assert claude_pub["has_key"] and claude_pub["anthropic_endpoint"].startswith("http://127.0.0.1")
        assert "api_key" not in claude_pub and "sk-ant" not in json.dumps(pub)    # Key 不发给网页
        (songlab.SONGS_ROOT / "巷口" / "04_歌词").mkdir(parents=True)
        (songlab.SONGS_ROOT / "巷口" / "00_原曲").mkdir()
        (songlab.SONGS_ROOT / "巷口" / "05_配图").mkdir()
        songlab.save_lyrics("巷口", "巷口灯光忽明忽灭\n手中甜咖啡已冷却")
        md = c.post("/api/songs/巷口/prompts", json={"style": "电影感国风", "count": 3}).json()["markdown"]
    assert "## 01 巷口暖灯" in md
    body = fake.requests[0]["body"]
    assert body["model"] == "claude-opus-5-5" and body["output_config"]["format"]["type"] == "json_schema"
    assert "巷口灯光忽明忽灭" in body["messages"][0]["content"]
