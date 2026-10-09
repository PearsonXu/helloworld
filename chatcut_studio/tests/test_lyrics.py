import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from chatcut_studio import lyrics
from chatcut_studio.app import BG_DIR, create_app
from chatcut_studio.presets import PRESETS
from chatcut_studio.profile import Captions, render_skill_md

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="需要 ffmpeg")


def cap(**kw):
    return Captions(**({"shine": "整句", "shine_seconds": 0.8} | kw))


def test_parse_lrc():
    lines = lyrics.parse_lrc("[ar:某人]\n[00:01.00]巷口{灯光}忽明忽灭\n\n[00:04.50][01:00.00]你看啊{弯月} #chorus\n[00:03.00]手中甜咖啡已冷却")
    assert [round(l.start, 2) for l in lines] == [1.0, 3.0, 4.5, 60.0]
    assert lines[0].text == "巷口灯光忽明忽灭" and lines[0].keywords == ["灯光"] and lines[0].end == 3.0
    assert lines[2].chorus and lines[2].text == "你看啊弯月" and lines[3].end == 65.0


def test_scope():
    kw, plain, chorus = lyrics.sample_lines("a{b}c\nabc\nabc #chorus")
    assert [lyrics.shine_applies(l, cap(shine="关键词")) for l in (kw, plain, chorus)] == [True, False, False]
    assert [lyrics.shine_applies(l, cap(shine="仅副歌")) for l in (kw, plain, chorus)] == [False, False, True]
    assert all(lyrics.shine_applies(l, cap(shine="整句")) for l in (kw, plain, chorus))


def _events(ass):
    return [l for l in ass.splitlines() if l.startswith("Dialogue")]


def test_ass_shine_layers():
    lines = lyrics.sample_lines("巷口{灯光}忽明忽灭\n手中甜咖啡已冷却")
    ev = _events(lyrics.build_ass(lines, cap(shine="关键词", keyword_highlight=True)))
    assert len(ev) == 1 + 4 + 1  # 第一句：正文 + 4 条光带；第二句没有关键词：只有正文
    shine = ev[1:5]
    assert all("\\clip(" in e and "\\t(" in e for e in shine)
    # 只露关键词：非关键词全透明，关键词恢复该层透明度
    assert all("{\\1a&HFF&\\3a&HFF&}巷口" in e and "忽明忽灭" in e for e in shine)
    # 光带从左往右走
    x0 = [int(m) for m in re.findall(r"\\clip\((-?\d+),", shine[0])]
    assert x0[1] > x0[0]
    assert len(_events(lyrics.build_ass(lines, cap(shine="无")))) == 2


def test_ass_keyword_restores_style_and_stroke():
    ass = lyrics.build_ass(lyrics.sample_lines("巷口{灯光}忽明"), cap(shine="无", keyword_highlight=True,
                                                                     stroke="柔和黑色阴影，不加硬描边"))
    body = _events(ass)[0]
    assert "\\r" not in body and "{\\fscx100\\fscy100\\1c&HFFFFFF&}忽明" in body
    assert "\\3a&HA0&" in body  # 「阴影」优先于「描边」


def test_long_line_wraps_without_splitting_keyword():
    rows = lyrics._wrap("我用尽所有字眼去描写{无法留你}片刻停歇", 10)
    assert len(rows) == 2 and all(r.count("{") == r.count("}") for r in rows)


def test_skill_md_mentions_shine():
    md = render_skill_md(PRESETS["guofeng-lyric-mv"], "guofeng-lyric-mv")
    assert "闪光扫过" in md and "#FFE9B0" in md


@needs_ffmpeg
def test_render_shine_moves(tmp_path):
    """真实渲染：扫光中途的高光位置在扫光前后都不存在，且随时间向右移动。"""
    c = cap(shine="整句", shine_seconds=1.0, size="大", color="#FFFFFF", shine_color="#FFD060")
    lines = lyrics.sample_lines("你看啊仲夏的弯月", seconds_per_line=3)
    video = lyrics.render(lines, c, tmp_path / "v", None, None, 0.5)
    s = lines[0].start + (lyrics.IN_MS + 80) / 1000

    def warm_x(t):  # 偏暖（高光色）像素的平均横坐标
        p = tmp_path / f"f{t}.png"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", str(p)], check=True)
        a = np.asarray(Image.open(p).convert("RGB")).astype(int)
        mask = (a[..., 0] - a[..., 2] > 60) & (a[..., 0] > 150)
        return mask.sum(), (np.nonzero(mask)[1].mean() if mask.any() else None)

    n_before, _ = warm_x(s - 0.1)
    n1, x1 = warm_x(s + 0.3)
    n2, x2 = warm_x(s + 0.65)
    n_after, _ = warm_x(s + 1.3)
    assert n1 > 50 and n2 > 50 and n_before < 10 and n_after < 10
    assert x2 > x1 + 40


@needs_ffmpeg
def test_overlay_has_alpha(tmp_path):
    out = lyrics.render(lyrics.sample_lines("透明字幕层"), cap(), tmp_path / "o", overlay=True, scale=0.25)
    assert out.suffix == ".mov"
    fmt = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", str(out)],
                         capture_output=True, text=True).stdout
    assert "yuva" in fmt


@needs_ffmpeg
@pytest.mark.skipif(not BG_DIR.is_dir(), reason="需要 mv_backgrounds")
def test_preview_and_render_api(tmp_path):
    c = TestClient(create_app())
    bgs = c.get("/api/lyrics/backgrounds").json()["backgrounds"]
    assert len(bgs) >= 1
    profile = PRESETS["guofeng-lyric-mv"].model_dump()
    r = c.post("/api/lyrics/preview", json={"profile": profile, "text": "巷口{灯光}忽明\n手中咖啡", "background": bgs[0]}).json()
    assert r["shine_line"] == "巷口灯光忽明" and len(r["frames"]) == 3
    assert c.get(r["video"]).status_code == 200 and c.get(r["frames"][1]).headers["content-type"] == "image/jpeg"
    assert c.post("/api/lyrics/preview", json={"profile": profile, "text": "x", "background": "../../etc/passwd"}).status_code == 400
    assert c.get("/renders/..%2F..%2F/settings.json").status_code == 404
    lrc = "[00:00.50]巷口{灯光}忽明忽灭\n[00:02.50]你看啊{弯月} #chorus"
    r = c.post("/api/lyrics/render", json={"style": "guofeng-lyric-mv", "lrc": lrc, "backgrounds": bgs[:2]}).json()
    assert r["lines"] == 2 and Path(r["path"]).suffix == ".mp4" and Path(r["path"]).exists()
    assert c.post("/api/lyrics/render", json={"style": "guofeng-lyric-mv", "lrc": "没有时间标签"}).status_code == 400
    assert c.post("/api/lyrics/render", json={"style": "guofeng-lyric-mv", "lrc": lrc, "audio": "/nope.mp3"}).status_code == 400
