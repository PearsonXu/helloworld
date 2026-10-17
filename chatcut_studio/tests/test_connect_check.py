"""连接检查：API / ChatCut Desktop / Agent 是否拿到 Desktop 的工具和本地 skill。"""
from fastapi.testclient import TestClient

from chatcut_studio import gateway, settings
from chatcut_studio.app import create_app

from .conftest import tool_call


def _codex_request(tok, c):
    body = {"model": "gpt-5", "stream": True,
            "instructions": r"## Skills\n- 歌词MV: 竖屏歌词视频 (file: C:\\Users\\me\\.codex\\skills\\歌词MV\\SKILL.md)",
            "tools": [{"type": "namespace", "name": "mcp__chatcut_desktop",
                       "tools": [{"type": "function", "name": "get_project", "parameters": {"type": "object"}}]}],
            "input": "读项目"}
    return c.post("/gateway/openai/v1/responses", json=body, headers={"Authorization": f"Bearer {tok}"})


def test_agent_check_sees_desktop_tools_and_skills(isolated_data, fake_llm, fake_chatcut):
    url, _ = fake_chatcut
    llm = fake_llm([{"content": "连接成功"}, {"content": None, "tool_calls": [tool_call("c1", "mcp__chatcut_desktop__get_project", {})]},
                    {"content": "16:9"}])
    settings.update({"providers": [{"id": "deepseek", "base_url": llm.base_url, "model": "deepseek-v4-pro", "api_key": "sk-c-1234567890"}],
                     "gateway": {"provider": "deepseek"}})
    gateway.link_codex()
    cfg = gateway.codex_config_path()
    cfg.write_text(cfg.read_text("utf-8") + f'\n[mcp_servers.chatcut_desktop]\nurl = "{url}"\n', "utf-8")
    skill = cfg.parent / "skills" / "歌词MV"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: 歌词MV\n---\n", "utf-8")
    gateway.RECENT.clear()
    tok = settings.load().gateway.token
    with TestClient(create_app()) as c:
        api = c.post("/api/check/api").json()
        assert api["ok"] and "连接成功" in api["message"]
        desk = c.post("/api/check/desktop").json()
        assert desk["ok"] and desk["tools"] > 0
        before = c.post("/api/check/agent?live=false").json()
        assert not before["ok"] and any("还没收到" in f["text"] for f in before["findings"])
        _codex_request(tok, c)                                       # Desktop 里的 Codex 发来一轮
        body2 = {"model": "gpt-5", "input": [{"type": "function_call", "call_id": "c1", "name": "get_project", "namespace": "mcp__chatcut_desktop", "arguments": "{}"},
                                             {"type": "function_call_output", "call_id": "c1", "output": "Wall time: 0.01 seconds\nOutput:\n{\"aspect_ratio\": \"16:9\"}"},
                                             {"type": "function_call", "call_id": "c2", "name": "get_timeline", "namespace": "mcp__chatcut_desktop", "arguments": "{}"},
                                             {"type": "function_call_output", "call_id": "c2", "output": "MCP tool call requires approval, but approval policy is never"}]}
        rec = gateway.RECENT[-1]
        assert rec["chatcut_tools"] == ["mcp__chatcut_desktop__get_project"] and rec["calls"] == ["chatcut_desktop/get_project"]
        assert rec["skills"] == ["歌词MV"]
        c.post("/gateway/openai/v1/responses", json=body2, headers={"Authorization": f"Bearer {tok}"})
        assert gateway.RECENT[-1]["chatcut_results"] == [
            {"tool": "chatcut_desktop/get_project", "ok": True, "text": '{"aspect_ratio": "16:9"}'},
            {"tool": "chatcut_desktop/get_timeline", "ok": False, "text": "MCP tool call requires approval, but approval policy is never"}]
        after = c.post("/api/check/agent?live=false").json()
    assert after["ok"], after["findings"]
    texts = " ".join(f["text"] for f in after["findings"])
    assert "登记了 ChatCut Desktop" in texts and "1 个 ChatCut 工具" in texts and "调用过 1 次" in texts and "歌词MV" in texts


def test_desktop_check_explains_when_not_open(isolated_data):
    with TestClient(create_app()) as c:
        r = c.post("/api/check/desktop").json()
    assert not r["ok"] and "先打开 ChatCut Desktop" in r["message"]
