"""歌词字幕渲染：LRC → ASS 字幕（含「闪光扫过」）→ ffmpeg + libass 出视频。

闪光扫过的做法：在正文下面叠两层同样排版的「高光字」，用一个随时间从左移到右的
矩形裁剪（\\clip 配合 \\t 动画）只露出窄窄一条，看起来就是一道光扫过文字：
  - 光芯层：窄带、接近白色、轻微模糊；
  - 光晕层：宽带、闪光色、半透明、大模糊。
只扫关键词时，高光层里非关键词的字设为全透明；排版和正文完全一致，所以光只落在关键词上。

歌词写法（与 lyric-mv 技能一致）：
  [00:12.50]巷口{灯光}忽明忽灭        {词} 标记关键词
  [00:20.00]你看啊春日的{蝴蝶} #chorus  行末 #chorus 表示副歌
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .profile import Captions

W, H = 1080, 1920
FONT_DIRS = [Path(os.environ.get("CHATCUT_STUDIO_FONTS", Path(__file__).parent / "fonts")),
             Path.home() / ".cache" / "lyric-mv" / "fonts"]
# 界面里的中文字体名 → 字体文件里的英文族名（libass 按族名找字体，找不到时由 fontconfig 回退）
FONT_FAMILY = {"思源宋体": "Noto Serif SC", "思源黑体": "Noto Sans SC", "得意黑": "Smiley Sans",
               "霞鹜文楷": "LXGW WenKai", "站酷小薇": "ZCOOL XiaoWei", "站酷快乐体": "ZCOOL KuaiLe",
               "马善政": "Ma Shan Zheng", "龙藏体": "Long Cang"}
FONT_SIZE = {"小": 56, "中": 72, "大": 92, "特大": 116}
Y_POS = {"底部": 0.78, "居中": 0.5, "顶部": 0.24, "左侧竖排": 0.5, "右侧竖排": 0.5, "随画面变化": 0.5}
IN_MS, OUT_MS = 350, 250
TAG = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


@dataclass
class Line:
    start: float
    end: float
    text: str  # 去掉标记后的纯文本
    keywords: list[str] = field(default_factory=list)
    chorus: bool = False
    raw: str = ""  # 保留 {关键词} 标记


def parse_lrc(text: str, tail: float = 5.0) -> list[Line]:
    """解析 LRC。支持一行多个时间标签、{关键词}、行末 #chorus；元信息行（[ar:]等）忽略。"""
    items: list[tuple[float, str]] = []
    for row in text.splitlines():
        stamps = TAG.findall(row)
        body = TAG.sub("", row).strip()
        if not stamps or not body:
            continue
        for m, s in stamps:
            items.append((int(m) * 60 + float(s), body))
    items.sort(key=lambda x: x[0])
    lines = []
    for i, (t, body) in enumerate(items):
        chorus = bool(re.search(r"#chorus\s*$", body))
        raw = re.sub(r"\s*#\w+\s*$", "", body)
        end = items[i + 1][0] if i + 1 < len(items) else t + tail
        lines.append(Line(t, end, raw.replace("{", "").replace("}", ""), re.findall(r"\{([^}]+)\}", raw), chorus, raw))
    return lines


def sample_lines(text: str, seconds_per_line: float = 3.0) -> list[Line]:
    """样张用：没有时间标签的几行歌词，按固定间隔排时间。"""
    rows = [r.strip() for r in text.splitlines() if r.strip()]
    lrc = "\n".join(f"[00:{i * seconds_per_line:05.2f}]{r}" for i, r in enumerate(rows))
    return parse_lrc(lrc, tail=seconds_per_line)


# ---------------------------------------------------------------- ASS
def ass_color(hex_color: str) -> str:
    h = hex_color.strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        h = "FFFFFF"
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def _ts(sec: float) -> str:
    cs = max(0, int(round(sec * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _wrap(raw: str, max_chars: int) -> list[str]:
    """超长时在中间附近断成两行（优先在空格和标点处），不拆开 {关键词}。"""
    plain = raw.replace("{", "").replace("}", "")
    if len(plain) <= max_chars:
        return [raw]
    tokens = re.findall(r"\{[^}]+\}|.", raw)
    best, pos, mid = None, 0, len(plain) / 2
    for i, tok in enumerate(tokens[:-1]):
        pos += len(tok.strip("{}"))
        score = abs(pos - mid) - (3 if tok in " ，,、。！？!?" else 0)
        if best is None or score < best[0]:
            best = (score, i + 1)
    cut = best[1]
    return ["".join(tokens[:cut]).strip(), "".join(tokens[cut:]).strip()]


def _segments(raw: str) -> list[tuple[str, bool]]:
    """把 '巷口{灯光}忽明' 拆成 [('巷口', False), ('灯光', True), ('忽明', False)]。"""
    return [(m.group(1) or m.group(2), m.group(1) is not None) for m in re.finditer(r"\{([^}]+)\}|([^{]+)", raw)]


def _rich(rows: list[str], key_on: str, key_off: str, hide: str = "", show: str = "") -> str:
    """生成行内文本。key_on/key_off：关键词开始/结束时的样式（结束时显式恢复，不用 \\r，免得清掉整行设置）。
    hide/show：高光层只露关键词时，非关键词用 hide 设为全透明，之后用 show 恢复该层自己的透明度。"""
    out = []
    for row in rows:
        parts = []
        for seg, is_key in _segments(row):
            seg = seg.replace("\\", "＼")
            if is_key and key_on:
                parts.append("{" + key_on + "}" + seg + "{" + key_off + "}")
            elif not is_key and hide:
                parts.append("{" + hide + "}" + seg + "{" + show + "}")
            else:
                parts.append(seg)
        out.append("".join(parts))
    return "\\N".join(out)


def shine_applies(line: Line, c: Captions) -> bool:
    if c.shine == "关键词":
        return bool(line.keywords)
    if c.shine == "仅副歌":
        return line.chorus
    return c.shine == "整句"


def build_ass(lines: list[Line], c: Captions, w: int = W, h: int = H) -> str:
    k = w / 1080
    fs = round(FONT_SIZE[c.size] * k)
    family = FONT_FAMILY.get(c.font, c.font)
    stroke = c.stroke or ""
    if "阴影" in stroke:  # 先判断阴影：「柔和阴影，不加硬描边」这类写法里也含「描边」二字
        bord, border_alpha, blur = fs * 0.07, "&HA0&", fs * 0.09
    elif "描边" in stroke:
        bord, border_alpha, blur = max(1, fs * 0.035), "&H00&", 0.8
    else:
        bord, border_alpha, blur = 0, "&HFF&", 0.6
    big, normal = ("\\fscx118\\fscy118", "\\fscx100\\fscy100") if c.keyword_highlight else ("", "")
    key_color = ass_color(c.shine_color if c.shine != "无" else c.color)
    key_on = big + (f"\\1c{key_color}" if c.keyword_highlight else "")
    key_off = normal + (f"\\1c{ass_color(c.color)}" if c.keyword_highlight else "")
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Lyric,{family},{fs},{ass_color(c.color)},&H00FFFFFF,&H00141414,&H00000000,0,0,0,0,100,100,{round(fs * 0.06)},0,1,0,0,5,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    x = w / 2
    for ln in lines:
        rows = _wrap(ln.raw, c.max_chars_per_line)
        y = h * Y_POS[c.position]
        rise = round(fs * 0.15)
        move = f"\\an5\\move({x:.0f},{y + rise:.0f},{x:.0f},{y:.0f},0,{IN_MS})"
        fade = f"\\fad({IN_MS},{OUT_MS})"
        start, end = _ts(ln.start), _ts(ln.end)
        body = f"{{{move}{fade}\\bord{bord:.1f}\\3a{border_alpha}\\blur{blur:.1f}}}" + _rich(rows, key_on, key_off)
        events.append(f"Dialogue: 0,{start},{end},Lyric,,0,0,0,,{body}")
        if c.shine == "无" or not shine_applies(ln, c):
            continue
        # 高光带扫过的范围：按字宽估算（libass 下中文字宽约 0.77 倍字号，再加字距）
        adv = fs * 0.77 + round(fs * 0.06)
        key_scale = 1.18 if c.keyword_highlight else 1.0
        widths = [sum(len(s) * adv * (key_scale if k else 1) for s, k in _segments(r)) for r in rows]
        half_h = len(rows) * fs * 0.75 + fs * 0.5
        top, bot = y - half_h, y + half_h
        left, right = x - max(widths) / 2 - fs * 0.3, x + max(widths) / 2 + fs * 0.3
        if c.shine == "关键词" and len(rows) == 1:
            # 只扫关键词时，光带只在关键词范围内走，整段时长都落在关键词上
            pos, l0 = x - widths[0] / 2, None
            for s_, k in _segments(rows[0]):
                wseg = len(s_) * adv * (key_scale if k else 1)
                if k and l0 is None:
                    l0, r0 = pos, pos + wseg
                pos += wseg
            left, right = l0 - fs * 0.25, r0 + fs * 0.25
        dur_ms = (ln.end - ln.start) * 1000
        s = IN_MS + 80
        d = max(200, min(c.shine_seconds * 1000, dur_ms - s - OUT_MS - 50))
        if d <= 200 and dur_ms < s + OUT_MS + 300:
            continue  # 这句太短，扫不完就不扫
        only_key = c.shine == "关键词"
        color = ass_color(c.shine_color)
        # 光芯的颜色要和字本身有反差：关键词已经是闪光色时用白光，否则用闪光色给字「染」一下
        core = "&HFFFFFF&" if (only_key and c.keyword_highlight) else color
        # 同一中心、宽度递增、越外越透明的几条光带叠在一起，近似高斯渐变，避免矩形硬边
        layers = [
            (fs * 0.30, core, "&H00&", "&HFFFFFF&", "&H60&", fs * 0.03, fs * 0.04),
            (fs * 0.70, core, "&H50&", color, "&H90&", fs * 0.06, fs * 0.10),
            (fs * 1.20, color, "&HA0&", color, "&HC0&", fs * 0.08, fs * 0.14),
            (fs * 1.80, color, "&HD0&", color, "&HE0&", fs * 0.10, fs * 0.18),
        ]
        widest = layers[-1][0]
        p0, p1 = left - widest / 2, right + widest / 2  # 光带中心的起止位置
        for band, c1, a1, c3, a3, bd, bl in layers:
            show = f"\\1a{a1}\\3a{a3}"
            tags = f"\\1c{c1}\\3c{c3}\\bord{bd:.1f}\\blur{bl:.1f}" + show
            clip0 = f"\\clip({p0 - band / 2:.0f},{top:.0f},{p0 + band / 2:.0f},{bot:.0f})"
            clip1 = f"\\clip({p1 - band / 2:.0f},{top:.0f},{p1 + band / 2:.0f},{bot:.0f})"
            anim = f"\\t({s},{s + d:.0f},{clip1})"
            text = _rich(rows, big, normal, "\\1a&HFF&\\3a&HFF&" if only_key else "", show if only_key else "")
            events.append(f"Dialogue: 1,{start},{end},Lyric,,0,0,0,,{{{move}{fade}{tags}{clip0}{anim}}}{text}")
    return head + "\n".join(events) + "\n"


# ---------------------------------------------------------------- 渲染
class RenderError(RuntimeError):
    pass


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RenderError("找不到 ffmpeg，请先安装（需要带 libass）")
    return exe


def _fontsdir() -> str:
    for d in FONT_DIRS:
        if d.is_dir():
            return str(d)
    return ""


def _ass_filter(ass_path: Path) -> str:
    esc = lambda p: str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    f = f"ass='{esc(ass_path)}'"
    if fd := _fontsdir():
        f += f":fontsdir='{esc(fd)}'"
    return f


def _run(cmd: list[str], timeout: int) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RenderError("ffmpeg 失败：" + p.stderr.strip()[-800:])


def background_schedule(lines: list[Line], bgs: list[Path]) -> list[float]:
    """每张背景图开始显示的时间：换图点卡在歌词句与句之间（按句数平均分给每张图）。"""
    per = max(1, len(lines) // max(1, len(bgs)))
    return [lines[min(i * per, len(lines) - 1)].start if i else 0.0 for i in range(len(bgs))]


def background_input(lines: list[Line], bgs: list[Path], duration: float, w: int, h: int, fps: int, out: Path) -> list[str]:
    """背景图的 ffmpeg 输入参数：多张图轮换；没有图时用纯色。"""
    if not bgs:
        return ["-f", "lavfi", "-i", f"color=c=0x1d2b44:s={w}x{h}:r={fps}:d={duration:.2f}"]
    cuts = background_schedule(lines, bgs) + [duration]
    listing = out.with_suffix(".bg.txt")
    listing.write_text("".join(
        f"file '{Path(b).resolve()}'\nduration {max(0.1, cuts[i + 1] - cuts[i]):.3f}\n" for i, b in enumerate(bgs))
        + f"file '{Path(bgs[-1]).resolve()}'\n", "utf-8")
    return ["-f", "concat", "-safe", "0", "-i", str(listing)]


def fit_filter(w: int, h: int, fps: int) -> str:
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={fps}"


def render(lines: list[Line], c: Captions, out: Path, backgrounds: list[Path] | None = None,
           audio: Path | None = None, scale: float = 1.0, fps: int = 30, overlay: bool = False,
           timeout: int = 1800) -> Path:
    """渲染视频。overlay=True 时输出透明背景的字幕层（ProRes 4444 .mov），可导入 ChatCut 叠在画面上。"""
    if not lines:
        raise RenderError("没有可渲染的歌词行")
    w, h = int(W * scale) // 2 * 2, int(H * scale) // 2 * 2
    duration = lines[-1].end + 0.5
    out.parent.mkdir(parents=True, exist_ok=True)
    ass_path = out.with_suffix(".ass")
    ass_path.write_text(build_ass(lines, c, w, h), "utf-8")
    cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
    if overlay:
        cmd += ["-f", "lavfi", "-i", f"color=c=black@0.0:s={w}x{h}:r={fps}:d={duration:.2f},format=rgba"]
        vf = f"{_ass_filter(ass_path)},format=yuva444p10le"
        enc = ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le"]
        out = out.with_suffix(".mov")
    else:
        cmd += background_input(lines, backgrounds or [], duration, w, h, fps, out)
        vf = f"{fit_filter(w, h, fps)},{_ass_filter(ass_path)},format=yuv420p"
        enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
        out = out.with_suffix(".mp4")
    if audio:
        cmd += ["-i", str(audio)]
    cmd += ["-vf", vf, *enc, "-t", f"{duration:.2f}"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"] if audio else ["-an"]
    _run(cmd + [str(out)], timeout)
    return out


def frames(video: Path, times: list[float], out_dir: Path) -> list[Path]:
    paths = []
    for i, t in enumerate(times):
        p = out_dir / f"frame_{i}.jpg"
        _run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video),
              "-frames:v", "1", "-q:v", "3", str(p)], 60)
        paths.append(p)
    return paths


def sweep_times(line: Line, c: Captions) -> list[float]:
    """样张取帧时间：扫光前、扫到一半、扫完之后。"""
    s = line.start + (IN_MS + 80) / 1000
    d = min(c.shine_seconds, max(0.2, line.end - line.start - (IN_MS + OUT_MS + 130) / 1000))
    return [s - 0.05, s + d * 0.45, s + d + 0.25]
