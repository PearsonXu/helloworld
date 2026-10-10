import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chatcut_studio import jizura, lyrics
from chatcut_studio.app import BG_DIR, create_app
from chatcut_studio.presets import PRESETS
from chatcut_studio.profile import Jizura, render_skill_md


def _chromium_ok():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


needs_browser = pytest.mark.skipif(not (shutil.which("ffmpeg") and _chromium_ok()), reason="需要 Playwright Chromium 和 ffmpeg")
needs_fonts = pytest.mark.skipif(bool(jizura.ensure_fonts(download=True)), reason="中文字体下载失败")


def run_js(js, arg=None):
    async def go():
        async with jizura._Page() as page:
            return await page.evaluate(js, arg)
    return asyncio.run(go())


def test_vendored_source_and_license():
    root = jizura.SRC_DIR.parent
    assert "MIT License" in (root / "LICENSE").read_text()
    assert "852wa/JIZURA" in (root / "UPSTREAM.md").read_text()
    assert len(list(jizura.SRC_DIR.glob("*.js"))) > 30


def test_chinese_themes():
    assert Jizura(theme="叠字海报").theme == "叠字国潮" and Jizura(theme="日系文字PV").theme == "宋韵留白"  # 旧名兼容
    js = jizura.HARNESS_JS
    for name, t in jizura.THEMES.items():
        assert all(p.startswith("cn_") for p in t["palettes"]), name
    assert "columnsBig" not in jizura.THEMES["叠字国潮"]["allow"]["layout"]      # 大字居中已去掉
    for key in ("cnPoem", "cnStagger", "cnEcho", "cnFrame", "cnMoon"):           # 我们新加的中式版式
        assert f"reg('{key}'" in js and any(key in t["allow"]["layout"] for t in jizura.THEMES.values())
    assert "window.CN_KW = PLAN.lines.map" in js and "J.mainDraw = (env, it)" in js   # {关键词} 传到版式里突出
    for face in ("dela", "reggae", "potta", "round", "pop", "kiwi", "klee"):  # 卡通 / 日文字体都换成宋、楷、书法
        assert face + ":" in js.replace(" ", "")


def test_skill_md_mentions_overlay():
    md = render_skill_md(PRESETS["cinematic-minimal"], "cinematic-minimal")
    assert "JIZURA「宋韵留白」" in md and "透明歌词层" in md and "不要用 ChatCut 自带字幕" in md


@needs_browser
def test_theme_whitelists_exist_in_jizura():
    """白名单里写的部件名在 JIZURA 里都存在（升级 JIZURA 后部件改名会在这里报出来）。"""
    reg = run_js("() => Object.fromEntries(J.GROUP_KEYS.map(g => [g, J.order(g)]))")
    for name, th in jizura.THEMES.items():
        for g, pats in th["allow"].items():
            exact = [p for p in pats if not p.endswith("*")]
            missing = [p for p in exact if p not in reg[g]]
            assert not missing, f"{name}/{g} 缺少部件：{missing}"
        for pal in th["palettes"]:
            assert pal in jizura.PALETTES
    assert set(run_js("() => J.STYLE_ORDER.concat(Object.keys(J.STYLES))")) >= set(jizura.PALETTES)


@needs_browser
def test_chinese_chunking_and_wrapping():
    got = run_js("""() => ({
      chunks: ['手中甜咖啡已冷却', '你看啊仲夏的弯月', '倒挂天际的笑靥', '海与山风和月', '在没有你的世界'].map(s => CS.chunkZh(s)),
      wrap: J.splitLines('手中甜咖啡已冷却', 5),
      chunkText: J.chunkText('倒挂天际的笑靥'),
      latin: J.splitLines('hello wonderful world of lyrics', 10),
    })""")
    assert got["chunks"] == [["手中", "甜咖啡", "已冷却"], ["你看啊", "仲夏的", "弯月"], ["倒挂", "天际的", "笑靥"],
                             ["海与山", "风和月"], ["在没有", "你的世界"]]
    assert got["wrap"] == "手中甜咖啡\n已冷却"
    assert got["chunkText"] == ["倒挂", "天际的", "笑靥"]
    assert "\n" in got["latin"] and "wonder\nful" not in got["latin"]  # 英文仍走 JIZURA 原逻辑


@needs_browser
def test_build_lyrics_syntax():
    lines = lyrics.parse_lrc("[00:00.50]巷口{灯光}忽明忽灭\n[00:03.50]你看啊仲夏的{弯月} #chorus\n[00:06]嘴角/不经意/泄露想念\n[00:09]Hello *world*!")
    arg = [[{"start": l.start, "raw": l.raw, "chorus": l.chorus} for l in lines], True]
    out = run_js("([l, c]) => CS.buildLyrics(l, c)", arg).splitlines()
    assert out[0] == "[00:00.50]巷口/*灯光*/忽明忽灭"
    assert out[1] == "[00:03.50]你看啊/仲夏的/*弯月*!"
    assert out[2] == "[00:06.00]嘴角/不经意/泄露想念"
    assert out[3] == "[00:09.00]Hello world"  # 用户文本里的 * 和 ! 不会被误当成 JIZURA 语法


@needs_browser
@needs_fonts
def test_contrast_follows_background():
    """亮背景自动配深色字，暗背景配浅色字：每个镜头的文字与背景对比度都 ≥ 3。"""
    lines = lyrics.sample_lines("巷口{灯光}忽明忽灭\n手中甜咖啡已冷却\n你看啊仲夏的弯月", 3.5)
    js = """async (a) => { const info = await CS.setup(a);
      const rl = h => { const v = [0,2,4].map(i => parseInt(h.slice(1).slice(i, i + 2), 16) / 255).map(c => c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)); return 0.2126*v[0]+0.7152*v[1]+0.0722*v[2]; };
      const L = a.bgLums[0][1], cr = f => (Math.max(rl(f), L) + 0.05) / (Math.min(rl(f), L) + 0.05);
      return { palette: info.palette, min: Math.min(...PLAN.cuts.map(c => cr(PLAN.style.schemes[c.scheme % PLAN.style.schemes.length].fg))) }; }"""
    for theme in jizura.THEMES:
        for lum in (0.85, 0.03):
            a = jizura.project_args(lines, Jizura(theme=theme, seed=2), 0.1, 30, None)
            a["bgLums"] = [[0.0, lum]]
            r = run_js(js, a)
            assert r["min"] >= 3, (theme, lum, r)


@needs_browser
@needs_fonts
def test_render_video_and_overlay(tmp_path):
    lines = lyrics.sample_lines("巷口{灯光}忽明忽灭\n你看啊仲夏的{弯月} #chorus", 3)
    bgs = sorted(BG_DIR.glob("*.jpg"))[:2]
    out, info = asyncio.run(jizura.render(lines, Jizura(theme="叠字国潮", seed=4), tmp_path / "v", bgs, None, 0.25))
    assert out.suffix == ".mp4" and out.stat().st_size > 10_000 and not info.get("warnings")
    assert info["frames"] == pytest.approx(info["duration"] * 12, abs=2)  # 一拍两：每秒只渲染 12 张不重复的画面
    assert json.loads(out.with_suffix(".plan.json").read_text())["cuts"]
    mov, _ = asyncio.run(jizura.render(lines[:1], Jizura(theme="清新诗意"), tmp_path / "o", None, None, 0.2, overlay=True))
    fmt = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", str(mov)],
                         capture_output=True, text=True).stdout
    assert mov.suffix == ".mov" and "yuva" in fmt


@needs_browser
@needs_fonts
def test_preview_api_jizura():
    c = TestClient(create_app())
    meta = c.get("/api/jizura/meta").json()
    assert [t["name"] for t in meta["themes"]] == ["宋韵留白", "水墨书法", "叠字国潮", "清新诗意"]
    assert not any(p["key"].startswith("hr") for p in meta["palettes"])
    profile = PRESETS["cinematic-minimal"].model_dump()
    bg = c.get("/api/lyrics/backgrounds").json()["backgrounds"][0]
    r = c.post("/api/lyrics/preview", json={"profile": profile, "text": "巷口{灯光}忽明忽灭\n手中甜咖啡已冷却", "background": bg}).json()
    assert r["engine"] == "jizura" and len(r["frames"]) == 3 and r["cuts"] and r["palette"] in jizura.PALETTES
    assert c.get(r["video"]).status_code == 200
