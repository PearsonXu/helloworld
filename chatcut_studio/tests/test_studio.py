import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

from chatcut_studio import agent, settings, styles
from chatcut_studio.app import create_app
from chatcut_studio.optimize import Proposal, propose
from chatcut_studio.presets import MAX_STYLES, PRESETS
from chatcut_studio.profile import StyleProfile, render_skill_md, slugify
from chatcut_studio.providers import ProviderError, chat_json, list_models

from .conftest import tool_call


def profile(**kw):
    return StyleProfile(name=kw.pop("name", "Fresh Guofeng MV"), description="清新国风歌词 MV", **kw)


def use_fake_provider(base_url, model="fake-pro", role="both"):
    patch = {"providers": [{"id": "deepseek", "base_url": base_url, "model": model, "api_key": "sk-test-123456789"}]}
    if role in ("both", "style"):
        patch["style_provider"] = "deepseek"
    if role in ("both", "agent"):
        patch |= {"agent_mode": "api", "agent_provider": "deepseek"}
    settings.update(patch)


# ---------- 风格档案与 SKILL.md ----------
def test_slugify():
    assert slugify("Fresh Guofeng MV") == "fresh-guofeng-mv"
    assert slugify("清新国风").startswith("style-") and slugify("清新国风") == slugify("清新国风")


def test_render_skill_md():
    md = render_skill_md(profile(rules=["歌词不遮挡画面主体"], avoid=["不要用闪白转场"]), "guofeng-lyric-mv")
    head = md.split("---")[1]
    assert "name: guofeng-lyric-mv" in head and "description:" in head
    assert "歌词不遮挡画面主体" in md and "不要用闪白转场" in md and "必须先向用户确认" in md
    p = profile()
    p.captions.enabled = False
    assert "不加字幕" in render_skill_md(p)


@pytest.mark.parametrize("slug", list(PRESETS))
def test_presets_render(slug):
    md = render_skill_md(PRESETS[slug], slug)
    assert md.startswith("---\nname: " + slug)


# ---------- 设置 ----------
def test_settings_mask_and_keep_key(isolated_data):
    settings.update({"providers": [{"id": "deepseek", "api_key": "sk-abcdefghijklmnop", "model": "m1"}]})
    pub = settings.load().public()
    ds = next(p for p in pub["providers"] if p["id"] == "deepseek")
    assert "api_key" not in ds and ds["has_key"] and ds["key_preview"] == "sk-…mnop"
    assert "sk-abcdefghijklmnop" not in json.dumps(pub)
    settings.update({"providers": [{"id": "deepseek", "api_key": "", "model": "m2"}]})  # 空 = 不修改
    assert settings.load().provider("deepseek").api_key == "sk-abcdefghijklmnop"
    settings.update({"providers": [{"id": "deepseek", "api_key": "-"}]})  # "-" = 清除
    assert settings.load().provider("deepseek").api_key == ""
    assert oct(os.stat(isolated_data / "settings.json").st_mode)[-3:] == "600"


def test_settings_env_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env-0000")
    ds = next(p for p in settings.load().public()["providers"] if p["id"] == "deepseek")
    assert ds["has_key"] and ds["key_from_env"]


def test_settings_api_rejects_unknown_provider():
    c = TestClient(create_app())
    assert c.put("/api/settings", json={"providers": [{"id": "nope"}]}).status_code == 400
    assert c.put("/api/settings", json={"agent_mode": "api"}).json()["agent_mode"] == "api"


# ---------- 常用风格库 ----------
def test_seeded_with_presets():
    lst = styles.list_styles()
    assert {s["slug"] for s in lst} == set(PRESETS) and len(lst) == MAX_STYLES
    assert all(s["preset"] and s["versions"] == 1 for s in lst)


def test_limit_versions_revert_restore():
    styles.list_styles()
    with pytest.raises(styles.LimitReached):
        styles.create(profile())
    slug = "talking-head"
    p = StyleProfile.model_validate(styles.get(slug)["profile"])
    p.captions.size = "特大"
    styles.update(slug, p, "字幕加大")
    got = styles.get(slug)
    assert got["profile"]["captions"]["size"] == "特大" and got["history"][0]["note"] == "字幕加大"
    styles.revert(slug, 1)
    assert styles.get(slug)["profile"]["captions"]["size"] == "大" and len(styles.get(slug)["history"]) == 3
    styles.delete("daily-vlog")
    assert styles.missing_presets() == [{"slug": "daily-vlog", "name": "生活Vlog"}]
    new = styles.create(profile(name="My Style"))
    assert new == "my-style"
    with pytest.raises(styles.LimitReached):
        styles.restore_preset("daily-vlog")  # 满了不能加回
    styles.restore_preset("talking-head")  # 已有的可以恢复
    assert styles.get("talking-head")["history"][0]["note"] == "恢复为内置预设"


def test_styles_api_and_install():
    c = TestClient(create_app())
    r = c.get("/api/styles").json()
    assert r["max"] == 5 and len(r["styles"]) == 5
    assert c.post("/api/styles", json={"profile": profile().model_dump()}).status_code == 409
    r = c.post("/api/styles/guofeng-lyric-mv/install").json()
    path, paths = r["path"], r["paths"]
    assert path.endswith("guofeng-lyric-mv/SKILL.md") and os.path.exists(path)
    assert paths["Codex"].endswith(os.path.join(".codex", "skills", "guofeng-lyric-mv", "SKILL.md")) and os.path.exists(paths["Codex"])
    assert open(paths["Codex"], encoding="utf-8").read().startswith("---\nname: guofeng-lyric-mv\n")   # Codex 也认这个 frontmatter
    styles.update("guofeng-lyric-mv", profile(name="清新国风歌词MV v2"), "改名")                                # 装过的风格改了自动更新
    assert "清新国风歌词MV v2" in open(paths["Codex"], encoding="utf-8").read()
    assert c.get("/api/styles/..%2Fetc").status_code == 404
    assert c.post("/api/styles/nope/revert", json={"version": 1}).status_code == 404


# ---------- 模型服务商（OpenAI 兼容，模拟 DeepSeek） ----------
def test_list_models_and_chat_json(fake_llm):
    good = Proposal(profile=profile(), changes=["字幕加大"]).model_dump_json()
    llm = fake_llm([{"content": "这不是 JSON"}, {"content": "```json\n" + good + "\n```"}])
    use_fake_provider(llm.base_url)
    p = settings.load().provider("deepseek")
    assert asyncio.run(list_models(p)) == ["fake-flash", "fake-pro"]
    out = asyncio.run(chat_json(p, "sys", "user", Proposal))
    assert out.changes == ["字幕加大"]
    assert len(llm.requests) == 2 and llm.requests[0]["auth"] == "Bearer sk-test-123456789"
    assert llm.requests[0]["body"]["response_format"] == {"type": "json_object"}
    assert "不符合 Schema" in llm.requests[1]["body"]["messages"][-1]["content"]  # 第二次带着错误重试


def test_chat_json_requires_config():
    with pytest.raises(ProviderError, match="API Key"):
        asyncio.run(chat_json(settings.load().provider("deepseek"), "s", "u", Proposal))


def test_optimize_keeps_name_and_endpoint(fake_llm):
    styles.list_styles()
    cur = StyleProfile.model_validate(styles.get("guofeng-lyric-mv")["profile"])
    changed = cur.model_copy(deep=True)
    changed.name = "模型擅自改名"
    changed.captions.size = "特大"
    llm = fake_llm([{"content": Proposal(profile=changed, changes=["字幕改为特大"]).model_dump_json()}] * 2)
    use_fake_provider(llm.base_url, role="style")
    prop, model = asyncio.run(propose(cur, "字幕再大一点"))
    assert prop.profile.name == cur.name and prop.profile.captions.size == "特大" and "DeepSeek" in model
    c = TestClient(create_app())
    r = c.post("/api/styles/guofeng-lyric-mv/optimize", json={"feedback": "字幕再大一点"}).json()
    assert r["changes"] == ["字幕改为特大"] and "name: guofeng-lyric-mv" in r["skill_md"]
    assert styles.get("guofeng-lyric-mv")["profile"]["captions"]["size"] == "大"  # 建议不会自动保存
    assert c.post("/api/styles/guofeng-lyric-mv/optimize", json={"feedback": " "}).status_code == 400


def test_optimize_without_key_is_503():
    c = TestClient(create_app())
    settings.update({"style_provider": "kimi"})
    r = c.post("/api/styles/guofeng-lyric-mv/optimize", json={"feedback": "x"})
    assert r.status_code == 503 and "API Key" in r.json()["detail"]


# ---------- 权限策略 ----------
@pytest.mark.parametrize("name,expected", [
    ("mcp__plugin_chatcut_chatcut__get_timeline", "allow"),
    ("mcp__plugin_chatcut_chatcut__export_video", "ask"),
    ("mcp__chatcut__generate_video", "ask"),
    ("mcp__chatcut__delete_project", "ask"),
    ("mcp__chatcut__upload_asset", "ask"),
    ("mcp__chatcut__add_caption", "allow"),
    ("Bash", "deny"), ("Write", "deny"), ("mcp__slack__send_message", "deny"), ("Read", "allow"),
])
def test_classify(name, expected):
    assert agent.classify(name) == expected


def test_build_prompt():
    p = agent.build_prompt("做一支 MV", "a.jpg\nsong.mp3", "---\nname: x\n---")
    assert "<style_skill>" in p and "song.mp3" in p and "导出前先问我" in p


# ---------- 端到端：DeepSeek（模拟）+ ChatCut MCP（模拟），走真实 HTTP ----------
def test_api_agent_end_to_end(fake_llm, fake_chatcut):
    mcp_url, state = fake_chatcut

    def final(body):  # 第三轮：确认上一步工具结果已经回传给模型
        tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
        assert any("已导出" in m["content"] for m in tool_msgs)
        assert {t["function"]["name"] for t in body["tools"]} >= {"get_project", "export_video"}
        return {"content": "完成：已改成 9:16 并导出。"}

    llm = fake_llm([
        {"content": "先看看项目。", "tool_calls": [tool_call("c1", "get_project", {}),
                                                 tool_call("c2", "update_project_settings", {"aspect_ratio": "9:16"})]},
        {"content": None, "tool_calls": [tool_call("c3", "export_video", {"format": "mp4"})]},
        final,
    ])
    use_fake_provider(llm.base_url, role="agent")
    settings.update({"mcp": {"url": mcp_url, "auth": "none"}})

    async def main():
        run = agent.RunManager().start(agent.build_prompt("改成竖屏并导出", "", ""))
        seen = []
        async for ev in run.events_from(0):
            seen.append(ev)
            if ev["type"] == "approval":
                assert ev["tool"] == "export_video"
                run.answer(ev["id"], True)
        return seen

    seen = asyncio.run(main())
    types = [e["type"] for e in seen]
    assert "error" not in types, seen
    assert types.count("approval") == 1 and types[-2:] == ["result", "done"]
    assert state["aspect_ratio"] == "9:16" and state["exports"] == 1
    # 第二轮请求里，两个工具结果都按 OpenAI 格式回传了
    second = llm.requests[1]["body"]["messages"]
    assert [m["tool_call_id"] for m in second if m["role"] == "tool"] == ["c1", "c2"]


def test_api_agent_denied_export_not_executed(fake_llm, fake_chatcut):
    mcp_url, state = fake_chatcut
    llm = fake_llm([{"content": None, "tool_calls": [tool_call("c1", "export_video", {})]},
                    lambda body: {"content": "好的，不导出。" if "拒绝" in body["messages"][-1]["content"] else "?"}])
    use_fake_provider(llm.base_url, role="agent")
    settings.update({"mcp": {"url": mcp_url, "auth": "none"}})

    async def main():
        run = agent.RunManager().start("导出")
        async for ev in run.events_from(0):
            if ev["type"] == "approval":
                run.answer(ev["id"], False)
        return run.history

    hist = asyncio.run(main())
    assert state["exports"] == 0
    assert hist[-2]["type"] == "result" and hist[-2]["text"] == "好的，不导出。"


def test_mcp_connect_lists_tools_with_policy(fake_chatcut):
    mcp_url, _ = fake_chatcut
    with TestClient(create_app()) as c:
        c.put("/api/settings", json={"mcp": {"url": mcp_url, "auth": "none"}})
        assert c.post("/api/mcp/connect").json()["started"]
        for _ in range(100):
            st = c.get("/api/mcp/status").json()
            if st["status"] in ("ok", "error"):
                break
            asyncio.run(asyncio.sleep(0.05))
        assert st["status"] == "ok", st
        tools = {t["name"]: t["policy"] for t in c.get("/api/mcp/tools").json()["tools"]}
    assert tools["get_project"] == "allow" and tools["export_video"] == "ask" and tools["add_clip"] == "allow"


# ---------- 运行与确认（与执行器无关的部分） ----------
async def _fake_runner(run: agent.Run):
    await run.emit({"type": "text", "text": "开始"})
    allowed = await run.ask("export_video", {"format": "mp4"})
    await run.emit({"type": "result", "is_error": False, "cost_usd": 0, "turns": 1, "text": f"allowed={allowed}"})


@pytest.mark.parametrize("allow", [True, False])
def test_approval_roundtrip(allow):
    async def main():
        run = agent.RunManager(_fake_runner).start("x")
        seen = []
        async for ev in run.events_from(0):
            seen.append(ev)
            if ev["type"] == "approval":
                assert not run.answer("wrong-id", True) and run.answer(ev["id"], allow)
        return seen

    seen = asyncio.run(main())
    assert [e["type"] for e in seen] == ["text", "approval", "result", "done"]
    assert seen[2]["text"] == f"allowed={allow}"


def test_runner_error_and_sse_replay():
    async def broken(run):
        raise RuntimeError("ChatCut MCP 未连接")

    with TestClient(create_app(agent.RunManager(broken))) as c:
        run_id = c.post("/api/run", json={"task": "x"}).json()["id"]
        for _ in range(2):  # 第二次订阅 = 断线重连，能拿到完整历史
            with c.stream("GET", f"/api/run/{run_id}/events") as s:
                evs = [json.loads(l[6:]) for l in s.iter_lines() if l.startswith("data: ")]
            assert [e["type"] for e in evs] == ["error", "done"] and "未连接" in evs[0]["message"]
        assert c.post(f"/api/run/{run_id}/approve", json={"id": "nope", "allow": True}).status_code == 404
        assert c.post("/api/prompt", json={"task": " "}).status_code == 400
        assert c.post("/api/env/rm-rf").status_code == 404
