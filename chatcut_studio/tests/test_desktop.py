"""连接本机 ChatCut Desktop：自动查找、两种连接方式（HTTP / stdio）、设置里的 Token 打码、Agent 端到端。"""
import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chatcut_studio import agent, chatcut_mcp, desktop, settings
from chatcut_studio.app import create_app

from .conftest import tool_call
from .test_studio import use_fake_provider

STDIO = {"command": sys.executable, "args": ["-m", "chatcut_studio.fake_chatcut", "--stdio"]}


def write_configs(home: Path, http_url: str):
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "chatcut-desktop": {"type": "http", "url": http_url, "headers": {"Authorization": "Bearer local-secret"}},
            "chatcut": {"type": "http", "url": "https://api.chatcut.io/api/external-mcp/mcp"},   # 云端：不算
            "github": {"type": "http", "url": "http://127.0.0.1:9/mcp"},                         # 不是 ChatCut：不算
        },
        "projects": {"C:/work/mv": {"mcpServers": {"chatcut-local": STDIO}}},
    }), "utf-8")
    (home / ".codex").mkdir(exist_ok=True)
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.chatcut_desktop]\nurl = "http://127.0.0.1:1/api/external-mcp/mcp"\n', "utf-8")


def test_from_configs_picks_local_chatcut_only(isolated_data):
    write_configs(isolated_data / "home", "http://127.0.0.1:5/mcp")
    found = {e.label(): e for e in desktop.from_configs()}
    assert set(found) == {"http://127.0.0.1:5/mcp", " ".join([STDIO["command"], *STDIO["args"]]),
                          "http://127.0.0.1:1/api/external-mcp/mcp"}
    assert found["http://127.0.0.1:5/mcp"].headers == {"Authorization": "Bearer local-secret"}
    assert "项目 C:/work/mv" in next(e for e in found.values() if e.transport == "stdio").source


def test_discover_probes_http_and_stdio(isolated_data, fake_chatcut):
    url, _ = fake_chatcut
    write_configs(isolated_data / "home", url)
    res = asyncio.run(desktop.discover())
    ok = {r["endpoint"]["transport"] for r in res if r["ok"]}
    assert ok == {"http", "stdio"}, res
    assert not res[-1]["ok"] and res[0]["ok"]                    # 能连上的排前面；端口 1 那个连不上
    assert all(r["tools"] >= 5 for r in res if r["ok"])


def test_connect_desktop_target(isolated_data, fake_chatcut):
    url, state = fake_chatcut

    async def tools(cfg):
        async with chatcut_mcp.connect(cfg) as c:
            return [t["name"] for t in await chatcut_mcp.list_tools(c)]

    cfg = settings.McpConfig(target="desktop", desktop=desktop.Endpoint(transport="stdio", **STDIO))
    assert "export_video" in asyncio.run(tools(cfg))
    with pytest.raises(chatcut_mcp.DesktopNotFound):           # 没登记、没在运行：给出明确提示
        asyncio.run(tools(settings.McpConfig(target="desktop")))
    write_configs(isolated_data / "home", url)                  # 不固定端点时自动查找
    assert "get_project" in asyncio.run(tools(settings.McpConfig(target="desktop")))


def test_settings_mask_desktop_headers(isolated_data):
    write_configs(isolated_data / "home", "http://127.0.0.1:5/mcp")
    with TestClient(create_app()) as c:
        found = c.get("/api/mcp/desktop/discover").json()["found"]
        ep = next(r["endpoint"] for r in found if r["endpoint"]["url"] == "http://127.0.0.1:5/mcp")
        assert ep["headers"] == {"Authorization": "***"}         # Token 不发给前端
        pub = c.put("/api/settings", json={"mcp": {"target": "desktop", "desktop": ep}}).json()
        assert pub["mcp"]["target"] == "desktop" and pub["mcp"]["desktop"]["headers"] == {"Authorization": "***"}
    assert settings.load().mcp.desktop.headers == {"Authorization": "Bearer local-secret"}   # 保存的是真值
    settings.update({"mcp": {"desktop": None}})
    assert settings.load().mcp.desktop is None


def test_api_agent_edits_desktop_project(fake_llm):
    """API 模型 → 本机 ChatCut Desktop（以 stdio 方式登记的那种）。"""
    llm = fake_llm([{"content": None, "tool_calls": [tool_call("c1", "update_project_settings", {"aspect_ratio": "9:16"}),
                                                     tool_call("c2", "get_project", {})]},
                    lambda body: {"content": "好了" if "9:16" in json.dumps(body["messages"], ensure_ascii=False) else "?"}])
    use_fake_provider(llm.base_url, role="agent")
    settings.update({"mcp": {"target": "desktop", "desktop": {"transport": "stdio", **STDIO}}})

    async def main():
        run = agent.RunManager().start("改成竖屏")
        async for _ in run.events_from(0):
            pass
        return run.history

    hist = asyncio.run(main())
    assert "error" not in [e["type"] for e in hist], hist
    assert hist[-2]["type"] == "result" and hist[-2]["text"] == "好了"


def test_claude_code_mode_gets_desktop_server(monkeypatch):
    """Claude Code 方式：把本机 Desktop 端点作为 mcp_servers 交给 Claude Code，工具名走权限策略。"""
    import claude_agent_sdk

    seen = {}

    class FakeClient:
        def __init__(self, options):
            seen["options"] = options
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def query(self, prompt):
            pass
        async def receive_response(self):
            yield claude_agent_sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                                 num_turns=1, session_id="s", total_cost_usd=0, result="ok")

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeClient)
    settings.update({"mcp": {"target": "desktop", "desktop": {"transport": "http", "url": "http://127.0.0.1:5/mcp"}}})

    async def main():
        run = agent.Run("x", "做点什么")
        await agent.sdk_runner(run)
        return run

    asyncio.run(main())
    o = seen["options"]
    assert o.mcp_servers == {agent.DESKTOP_SERVER: {"type": "http", "url": "http://127.0.0.1:5/mcp"}}
    assert agent.DESKTOP_SERVER in o.system_prompt["append"]
    assert agent.classify(f"mcp__{agent.DESKTOP_SERVER}__export_video") == "ask"
    assert agent.classify(f"mcp__{agent.DESKTOP_SERVER}__get_timeline") == "allow"
