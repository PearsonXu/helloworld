import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from chatcut_studio import agent, styles
from chatcut_studio.app import create_app
from chatcut_studio.profile import StyleProfile, render_skill_md, slugify


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(styles, "DATA_DIR", tmp_path / "styles")
    monkeypatch.setattr(styles, "CLAUDE_SKILLS_DIR", tmp_path / "skills")
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("chatcut_studio.llm.has_credentials", lambda: False)


def profile(**kw):
    return StyleProfile(name=kw.pop("name", "Fresh Guofeng MV"), description="清新国风歌词 MV", **kw)


# ---------- 模板 ----------
def test_slugify():
    assert slugify("Fresh Guofeng MV") == "fresh-guofeng-mv"
    assert slugify("清新国风").startswith("style-")
    assert slugify("清新国风") == slugify("清新国风")


def test_render_skill_md_frontmatter_and_content():
    md = render_skill_md(profile(rules=["歌词不遮挡画面主体"], avoid=["不要用闪白转场"]))
    head = md.split("---")[1]
    assert "name: fresh-guofeng-mv" in head
    assert "description:" in head
    assert "9:16" in md and "思源宋体" in md
    assert "歌词不遮挡画面主体" in md and "不要用闪白转场" in md
    assert "必须先向用户确认" in md


def test_render_without_captions():
    p = profile()
    p.captions.enabled = False
    md = render_skill_md(p)
    assert "不加字幕" in md and "添加字幕" not in md


# ---------- 权限策略 ----------
@pytest.mark.parametrize("name,expected", [
    ("mcp__plugin_chatcut_chatcut__get_timeline", "allow"),
    ("mcp__plugin_chatcut_chatcut__list_assets", "allow"),
    ("mcp__plugin_chatcut_chatcut__export_video", "ask"),
    ("mcp__plugin_chatcut_chatcut__generate_video", "ask"),
    ("mcp__chatcut__delete_project", "ask"),
    ("mcp__chatcut__upload_asset", "ask"),
    ("mcp__chatcut__add_caption", "allow"),
    ("Bash", "deny"),
    ("Write", "deny"),
    ("mcp__slack__send_message", "deny"),
    ("Read", "allow"),
])
def test_classify(name, expected):
    assert agent.classify(name) == expected


def test_classify_manual_edit_mode(monkeypatch):
    monkeypatch.setenv("CHATCUT_STUDIO_AUTO_EDIT", "0")
    assert agent.classify("mcp__chatcut__add_caption") == "ask"


def test_build_prompt_includes_skill():
    p = agent.build_prompt("做一支 MV", "a.jpg\nsong.mp3", "---\nname: x\n---\n# X")
    assert "<style_skill>" in p and "做一支 MV" in p and "song.mp3" in p and "导出前先问我" in p


def test_mcp_options(monkeypatch):
    assert agent.mcp_options() == {"setting_sources": ["user"]}
    monkeypatch.setenv("CHATCUT_MCP_MODE", "http")
    monkeypatch.setenv("CHATCUT_MCP_TOKEN", "t0k")
    cfg = agent.mcp_options()["mcp_servers"]["chatcut"]
    assert cfg["url"] == agent.CHATCUT_MCP_URL and cfg["headers"]["Authorization"] == "Bearer t0k"


# ---------- API ----------
def test_generate_save_install_flow():
    c = TestClient(create_app())
    p = profile().model_dump()
    r = c.post("/api/styles/generate", json={"profile": p, "use_ai": True}).json()
    assert r["source"] == "template" and r["warning"]
    slug = c.post("/api/styles", json={"profile": p, "skill_md": r["skill_md"]}).json()["slug"]
    assert slug == "fresh-guofeng-mv"
    lst = c.get("/api/styles").json()["styles"]
    assert lst[0]["slug"] == slug and not lst[0]["installed"]
    path = c.post(f"/api/styles/{slug}/install").json()["path"]
    assert path.endswith(f"{slug}/SKILL.md")
    assert c.get("/api/styles").json()["styles"][0]["installed"]
    assert c.get(f"/api/styles/{slug}").json()["skill_md"] == r["skill_md"]
    assert c.delete(f"/api/styles/{slug}").status_code == 200
    assert c.get(f"/api/styles/{slug}").status_code == 404


def test_bad_slug_rejected():
    c = TestClient(create_app())
    assert c.get("/api/styles/..%2F..%2Fetc").status_code == 404
    assert c.post("/api/styles/Bad_Slug/install").status_code == 404


def test_learn_without_credentials_returns_503():
    c = TestClient(create_app())
    assert c.post("/api/styles/learn", json={"timeline": "{}"}).status_code == 503


def test_prompt_requires_task():
    c = TestClient(create_app())
    assert c.post("/api/prompt", json={"task": " "}).status_code == 400
    assert "做 MV" in c.post("/api/prompt", json={"task": "做 MV"}).json()["prompt"]


def test_env_action_whitelist():
    c = TestClient(create_app())
    assert c.post("/api/env/rm-rf").status_code == 404


async def _fake_runner(run: agent.Run):
    """模拟 Agent：一次需要确认的导出，确认后继续完成。"""
    await run.emit({"type": "text", "text": "开始剪辑"})
    tool = "mcp__chatcut__export_video"
    allowed = await run.ask(tool, {"format": "mp4"}) if agent.classify(tool) == "ask" else True
    await run.emit({"type": "result", "is_error": False, "cost_usd": 0.01, "turns": 3, "text": f"allowed={allowed}"})


@pytest.mark.parametrize("allow", [True, False])
def test_approval_roundtrip(allow):
    async def main():
        mgr = agent.RunManager(_fake_runner)
        run = mgr.start("导出成片")
        seen = []
        async for ev in run.events_from(0):
            seen.append(ev)
            if ev["type"] == "approval":
                assert not run.answer("wrong-id", True)
                assert run.answer(ev["id"], allow)
        return seen

    seen = asyncio.run(main())
    assert [e["type"] for e in seen] == ["text", "approval", "result", "done"]
    assert seen[2]["text"] == f"allowed={allow}"


def test_pending_approval_denied_when_run_ends():
    async def runner(run):
        task = asyncio.create_task(run.ask("mcp__chatcut__delete_project", {}))
        await asyncio.sleep(0)
        raise RuntimeError("中途断开")

    async def main():
        run = agent.RunManager(runner).start("x")
        return [e async for e in run.events_from(0)]

    types = [e["type"] for e in asyncio.run(main())]
    assert types == ["approval", "error", "done"]


def test_sse_replays_history_over_http():
    with TestClient(create_app(agent.RunManager(lambda run: run.emit({"type": "text", "text": "hi"})))) as c:
        run_id = c.post("/api/run", json={"task": "x"}).json()["id"]
        for _ in range(2):  # 第二次订阅模拟断线重连
            with c.stream("GET", f"/api/run/{run_id}/events") as s:
                evs = [json.loads(l[6:])["type"] for l in s.iter_lines() if l.startswith("data: ")]
            assert evs == ["text", "done"]
        assert c.post(f"/api/run/{run_id}/approve", json={"id": "nope", "allow": True}).status_code == 404


def test_runner_error_is_reported():
    async def broken(run):
        raise RuntimeError("ChatCut MCP 未连接")

    with TestClient(create_app(agent.RunManager(broken))) as c:
        run_id = c.post("/api/run", json={"task": "x"}).json()["id"]
        with c.stream("GET", f"/api/run/{run_id}/events") as s:
            evs = [json.loads(l[6:]) for l in s.iter_lines() if l.startswith("data: ")]
    assert evs[0]["type"] == "error" and "未连接" in evs[0]["message"]
    assert evs[-1]["type"] == "done"
