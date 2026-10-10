"""歌曲工坊：原曲 mp4 → 提取音频 → 分离人声 / 伴奏 → 试听选降几度 → 导出降调文件 → 歌词配图提示词 → 一个素材文件夹。

每首歌一个文件夹（默认 ~/ChatCutStudio/歌曲/<歌名>/），所有中间文件按步骤编号放好，最后整个文件夹就是素材包：

    00_原曲/        原曲.mp4、原曲.wav
    01_人声分离/    人声.wav、伴奏.wav            （audio-separator，和 UVR 同一套模型，有 NVIDIA 显卡就用显卡）
    02_降调/        混音_降4.mp3/.wav、人声_降4.wav、伴奏_降4.wav   （Rubber Band，保留共振峰）
    03_Suno成品/    你从 Suno 下载的改编版放这里
    04_歌词/        歌词.txt
    05_配图/        配图提示词.md（DeepSeek 按歌词写的 3–5 个画面），出好的图也放这里
    素材清单.txt
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

SONGS_ROOT = Path(os.environ.get("CHATCUT_STUDIO_SONGS") or Path.home() / "ChatCutStudio" / "歌曲")
MODEL_DIR = Path(os.environ.get("CHATCUT_STUDIO_SEP_MODELS") or Path.home() / ".cache" / "chatcut-studio" / "separator-models")
DIRS = {"src": "00_原曲", "stems": "01_人声分离", "shift": "02_降调", "suno": "03_Suno成品", "lyrics": "04_歌词", "images": "05_配图"}
MODELS = {   # UVR 里同名的模型；第一次用会自动下载到 MODEL_DIR
    "model_bs_roformer_ep_317_sdr_12.9755.ckpt": "BS-Roformer（人声最干净，推荐；1660 约 1–2 分钟一首）",
    "UVR-MDX-NET-Voc_FT.onnx": "MDX-Net Voc FT（更快，UVR 经典模型）",
}
DEFAULT_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
PREVIEW_SHIFTS = [0, -2, -3, -4, -5, -6]
NOTE = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}

JOBS: dict[str, dict] = {}       # 歌名 → 正在跑的后台任务（人声分离 / 导出）


class SongError(Exception):
    pass


# ---------------------------------------------------------------- 工具
def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise SongError("找不到 ffmpeg，请先安装（winget install Gyan.FFmpeg）")
    return exe


def _run(cmd: list[str], timeout: int = 1800) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if p.returncode != 0:
        raise SongError((p.stderr or p.stdout).strip()[-800:])


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name).strip().strip(".")
    if not name:
        raise SongError("歌名不能为空")
    return name[:60]


def song_dir(name: str) -> Path:
    d = SONGS_ROOT / safe_name(name)
    if not d.is_dir():
        raise SongError(f"没有这首歌：{name}")
    return d


def note_name(hz: float) -> str:
    if not hz or hz <= 0:
        return "-"
    m = round(69 + 12 * math.log2(hz / 440))
    return f"{NOTE[m % 12]}{m // 12 - 1}"


def shift_label(n: int) -> str:
    return "原调" if n == 0 else (f"降{-n}" if n < 0 else f"升{n}")


def load_mono(path: Path, sr: int = 16000) -> np.ndarray:
    """用 ffmpeg 解码成单声道 float32。"""
    p = subprocess.run([_ffmpeg(), "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True, timeout=600)
    if p.returncode != 0:
        raise SongError(p.stderr.decode("utf-8", "replace")[-500:])
    return np.frombuffer(p.stdout, dtype=np.float32)


# ---------------------------------------------------------------- 新建 / 状态
def create(name: str, src: Path) -> dict:
    """新建一首歌：把原曲（mp4 / 音频）放进 00_原曲，并提取成 wav。"""
    name = safe_name(name)
    d = SONGS_ROOT / name
    for sub in DIRS.values():
        (d / sub).mkdir(parents=True, exist_ok=True)
    dst = d / DIRS["src"] / ("原曲" + src.suffix.lower())
    if src.resolve() != dst.resolve():
        shutil.copyfile(src, dst)
    tmp = d / DIRS["src"] / "原曲.tmp.wav"         # 原曲本身是 wav 时输入输出同名，先写临时文件
    _run([_ffmpeg(), "-y", "-v", "error", "-i", str(dst), "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", "-f", "wav", str(tmp)])
    os.replace(tmp, d / DIRS["src"] / "原曲.wav")
    write_manifest(name)
    return info(name)


def list_songs() -> list[dict]:
    if not SONGS_ROOT.is_dir():
        return []
    out = []
    for d in sorted(SONGS_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if d.is_dir() and (d / DIRS["src"]).is_dir():
            out.append({"name": d.name, "stage": info(d.name)["stage"]})
    return out


def _files(d: Path, exts: set[str]) -> list[str]:
    return sorted(p.name for p in d.iterdir() if p.is_file() and p.suffix.lower() in exts) if d.is_dir() else []


def info(name: str) -> dict:
    d = song_dir(name)
    stems = d / DIRS["stems"]
    analysis = json.loads((d / "analysis.json").read_text("utf-8")) if (d / "analysis.json").is_file() else None
    lyrics = (d / DIRS["lyrics"] / "歌词.txt")
    prompts = (d / DIRS["images"] / "配图提示词.md")
    st = {
        "name": d.name, "dir": str(d),
        "has_audio": (d / DIRS["src"] / "原曲.wav").is_file(),
        "has_video": any((d / DIRS["src"]).glob("原曲.mp4")),
        "has_stems": (stems / "人声.wav").is_file() and (stems / "伴奏.wav").is_file(),
        "analysis": analysis,
        "exports": _files(d / DIRS["shift"], AUDIO_EXT),
        "suno": _files(d / DIRS["suno"], AUDIO_EXT),
        "lyrics": lyrics.read_text("utf-8") if lyrics.is_file() else "",
        "prompts": prompts.read_text("utf-8") if prompts.is_file() else "",
        "images": _files(d / DIRS["images"], IMAGE_EXT),
        "job": JOBS.get(d.name),
        "dirs": DIRS,
    }
    st["stage"] = ("配图" if st["images"] else "歌词" if st["prompts"] or st["lyrics"] else "Suno" if st["suno"]
                   else "降调完成" if st["exports"] else "已分离" if st["has_stems"] else "已导入")
    return st


def write_manifest(name: str) -> None:
    d = song_dir(name)
    i = info(name)
    a = i["analysis"] or {}
    lines = [f"歌曲：{i['name']}", f"生成时间：{time.strftime('%Y-%m-%d %H:%M')}", ""]
    if a:
        lines += [f"原曲调性（估计）：{a.get('key', '-')}",
                  f"原唱人声音域：{a.get('low_note')} – {a.get('high_note')}（中位 {a.get('mid_note')}）",
                  f"建议：{a.get('advice', '')}", ""]
    for key, sub in DIRS.items():
        files = sorted(p.name for p in (d / sub).iterdir() if p.is_file()) if (d / sub).is_dir() else []
        lines.append(f"{sub}/")
        lines += [f"  {f}" for f in files] or ["  （空）"]
    lines += ["", "下一步：把 02_降调 里的文件传到 Suno 改编，下载的成品放进 03_Suno成品；",
              "在 Studio 里粘贴歌词、生成配图提示词，出好的图放进 05_配图；整个文件夹交给 ChatCut 做 MV。"]
    (d / "素材清单.txt").write_text("\n".join(lines) + "\n", "utf-8")


# ---------------------------------------------------------------- 人声分离
def separator_ready() -> tuple[bool, str]:
    try:
        import audio_separator  # noqa: F401
    except ImportError:
        return False, "还没有安装人声分离组件：运行 chatcut_studio\\windows\\setup.ps1 -Audio（或 pip install \"audio-separator[gpu]\"）"
    return True, ""


async def separate(name: str, model: str = DEFAULT_MODEL) -> None:
    """后台任务：在独立进程里跑 audio-separator（出错 / 显存不够不会拖垮 Studio）。"""
    d = song_dir(name)
    job = JOBS[d.name] = {"kind": "separate", "status": "running", "progress": 0, "message": "准备中（第一次会下载模型）…"}
    ok, why = separator_ready()
    if not ok:
        job.update(status="error", message=why)
        return
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "chatcut_studio.songlab", "separate", str(d / DIRS["src"] / "原曲.wav"),
           str(d / DIRS["stems"]), model, str(MODEL_DIR)]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    tail: list[str] = []
    buf = b""
    while True:
        chunk = await proc.stdout.read(4096)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for raw in parts:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            tail = (tail + [line])[-30:]
            if m := re.search(r"(\d{1,3})%\|", line):
                job["progress"] = int(m.group(1))
                job["message"] = "分离中…" if "Download" not in line and "download" not in line else "下载模型中…"
            elif line.startswith("STATUS "):
                job["message"] = line[7:]
    code = await proc.wait()
    if code == 0 and (d / DIRS["stems"] / "人声.wav").is_file():
        job.update(status="running", progress=100, message="分析音域和调性…")
        try:
            await asyncio.to_thread(analyze, d.name)
            job.update(status="done", message="完成")
        except Exception as e:      # 分析失败不影响分离结果
            job.update(status="done", message=f"分离完成；音域分析失败：{e}")
        write_manifest(d.name)
    else:
        job.update(status="error", message="人声分离失败：" + " / ".join(tail[-4:]))


def _separate_main(src: str, out_dir: str, model: str, model_dir: str) -> None:
    """子进程入口：python -m chatcut_studio.songlab separate <wav> <输出目录> <模型> <模型目录>"""
    from audio_separator.separator import Separator
    print("STATUS 加载模型（第一次会下载）…", flush=True)
    sep = Separator(output_dir=out_dir, model_file_dir=model_dir, output_format="WAV")
    sep.load_model(model_filename=model)
    print("STATUS 分离中…", flush=True)
    files = sep.separate(src)
    out = Path(out_dir)
    for f in files:                 # 输出文件名带模型名，统一改成 人声.wav / 伴奏.wav
        p = Path(f) if Path(f).is_absolute() else out / f
        low = p.name.lower()
        target = "人声.wav" if "vocal" in low and "instrument" not in low else "伴奏.wav" if ("instrument" in low or "other" in low) else None
        if target:
            os.replace(p, out / target)
    print("STATUS 完成", flush=True)


# ---------------------------------------------------------------- 分析：人声音域 + 调性 → 建议降几度
KRUMHANSL_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KRUMHANSL_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def vocal_f0(y: np.ndarray, sr: int = 16000, frame: int = 1024, hop: int = 320, fmin: float = 70, fmax: float = 1000):
    """YIN 基频估计（向量化）。返回（每帧基频，无声为 0；每帧能量）。"""
    n = 1 + max(0, (len(y) - frame) // hop)
    if n <= 1:
        return np.zeros(0), np.zeros(0)
    idx = np.arange(frame)[None, :] + hop * np.arange(n)[:, None]
    frames = y[idx] * np.hanning(frame)[None, :].astype(np.float32)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    w = frame // 2
    spec = np.fft.rfft(frames, 2 * frame, axis=1)
    acf = np.fft.irfft(spec * np.conj(spec), axis=1)[:, :w]
    energy = np.cumsum(frames ** 2, axis=1)
    e0 = energy[:, w - 1:w]                                  # 前 w 个采样的能量
    e_tau = energy[:, w - 1 + np.arange(w)] - np.concatenate([np.zeros((n, 1)), energy[:, :w - 1]], axis=1)
    diff = e0 + e_tau - 2 * acf
    diff[:, 0] = 0
    cum = np.cumsum(diff[:, 1:], axis=1) / np.arange(1, w)[None, :]
    cmnd = np.ones_like(diff)
    cmnd[:, 1:] = diff[:, 1:] / np.maximum(cum, 1e-9)
    lo, hi = int(sr / fmax), min(w - 1, int(sr / fmin))
    seg = cmnd[:, lo:hi]
    tau = np.argmin(seg, axis=1) + lo
    best = seg[np.arange(n), tau - lo]
    # 抛物线插值求小数周期：高音区周期只有几十个采样点，取整会差半个半音
    t = np.clip(tau, 1, w - 2)
    a_, b_, c_ = cmnd[np.arange(n), t - 1], cmnd[np.arange(n), t], cmnd[np.arange(n), t + 1]
    den = a_ - 2 * b_ + c_
    frac = np.where(np.abs(den) > 1e-9, 0.5 * (a_ - c_) / np.where(np.abs(den) > 1e-9, den, 1), 0.0)
    tau_f = t + np.clip(frac, -0.5, 0.5)
    voiced = (best < 0.2) & (rms > max(1e-4, np.percentile(rms, 60) * 0.3))
    f0 = np.where(voiced, sr / tau_f, 0.0)
    return f0, rms


def detect_key(y: np.ndarray, sr: int = 16000) -> str:
    frame, hop = 4096, 2048
    n = 1 + max(0, (len(y) - frame) // hop)
    chroma = np.zeros(12)
    freqs = np.fft.rfftfreq(frame, 1 / sr)
    band = (freqs > 60) & (freqs < 2000)
    pcs = (np.round(12 * np.log2(freqs[band] / 440)) + 9) % 12
    win = np.hanning(frame)
    for k in range(0, n, 2):                                # 隔帧取样就够估调
        mag = np.abs(np.fft.rfft(y[k * hop:k * hop + frame] * win))[band]
        np.add.at(chroma, pcs.astype(int), mag)
    if not chroma.any():
        return "-"
    best, name = -2.0, "-"
    for i in range(12):
        for prof, kind in ((KRUMHANSL_MAJOR, "大调"), (KRUMHANSL_MINOR, "小调")):
            r = np.corrcoef(chroma, np.roll(prof, i))[0, 1]
            if r > best:
                best, name = r, f"{NOTE[i]} {kind}"
    return name


def suggest_shift(p10: float, p50: float, p90: float) -> tuple[int, str]:
    """女转男：让常用的高音（第 90 百分位）落到 A4 左右，男声比较舒服；限制在降 0–7 个半音。"""
    if p90 <= 0:
        return -4, "没测出人声音高，按女转男的常见值降 4 个半音"
    n = int(round(12 * math.log2(440 / p90)))
    n = max(-7, min(0, n))
    if n == 0:
        return 0, f"原唱常用高音约 {note_name(p90)}，男声原调就能唱，不用降"
    return n, (f"原唱常用高音约 {note_name(p90)}，降 {-n} 个半音后约 {note_name(p90 * 2 ** (n / 12))}，"
               f"整体在 {note_name(p10 * 2 ** (n / 12))}–{note_name(p90 * 2 ** (n / 12))}，男声比较舒服"
               "（女转男一般降 4–5 个半音，建议试听 ±1 档再定）")


def analyze(name: str) -> dict:
    d = song_dir(name)
    sr = 16000
    voc = load_mono(d / DIRS["stems"] / "人声.wav", sr)
    f0, rms = vocal_f0(voc, sr)
    v = f0[f0 > 0]
    p10, p50, p90 = (np.percentile(v, [10, 50, 90]).tolist() if len(v) > 20 else (0.0, 0.0, 0.0))
    shift, advice = suggest_shift(p10, p50, p90)
    inst_path = d / DIRS["stems"] / "伴奏.wav"
    key = detect_key(load_mono(inst_path if inst_path.is_file() else d / DIRS["src"] / "原曲.wav", sr), sr)
    # 试听片段：人声最饱满的 30 秒（多半是副歌）
    hop_s = 320 / sr
    win = max(1, int(30 / hop_s))
    if len(rms) > win:
        c = np.convolve(rms, np.ones(win), "valid")
        start = float(np.argmax(c) * hop_s)
    else:
        start = 0.0
    a = {"low_hz": p10, "mid_hz": p50, "high_hz": p90, "low_note": note_name(p10), "mid_note": note_name(p50),
         "high_note": note_name(p90), "key": key, "suggest": shift, "advice": advice,
         "preview_start": round(start, 1), "duration": round(len(voc) / sr, 1)}
    if key != "-" and shift:
        i = NOTE.index(key.split()[0])
        a["key_after"] = f"{NOTE[(i + shift) % 12]} {key.split()[1]}"
    (d / "analysis.json").write_text(json.dumps(a, ensure_ascii=False, indent=1), "utf-8")
    write_manifest(name)
    return a


# ---------------------------------------------------------------- 降调：试听片段 + 导出
def _rubberband(shift: int, formant: bool) -> str:
    ratio = 2 ** (shift / 12)
    return f"rubberband=pitch={ratio:.6f}:pitchq=quality:transients=smooth" + (":formant=preserved" if formant else "")


def track_path(d: Path, track: str) -> Path:
    return {"mix": d / DIRS["src"] / "原曲.wav", "vocals": d / DIRS["stems"] / "人声.wav",
            "inst": d / DIRS["stems"] / "伴奏.wav"}[track]


def preview(name: str, shift: int, track: str = "mix", formant: bool = True, start: float | None = None, seconds: int = 30) -> Path:
    """试听片段（mp3，缓存）：同一套 Rubber Band 参数，所以听到的就是导出的效果。"""
    d = song_dir(name)
    src = track_path(d, track)
    if not src.is_file():
        raise SongError("还没有这条音轨" + ("（先分离人声）" if track != "mix" else ""))
    if start is None:
        a = info(name)["analysis"] or {}
        start = float(a.get("preview_start", 30.0))
    out = d / ".preview" / f"{track}_{shift}_{'f' if formant else 'n'}_{int(start)}.mp3"
    if not out.is_file():
        out.parent.mkdir(exist_ok=True)
        af = [] if shift == 0 else ["-af", _rubberband(shift, formant)]
        _run([_ffmpeg(), "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", str(seconds), "-i", str(src), *af,
              "-c:a", "libmp3lame", "-b:a", "192k", str(out)], timeout=300)
    return out


def export(name: str, shift: int, formant: bool = True) -> list[str]:
    """导出整首：混音（mp3 320k + wav）、人声、伴奏（wav）。"""
    d = song_dir(name)
    out_dir = d / DIRS["shift"]
    out_dir.mkdir(exist_ok=True)
    lab, made = shift_label(shift), []
    tracks = [("混音", "mix")] + ([("人声", "vocals"), ("伴奏", "inst")] if info(name)["has_stems"] else [])
    for cn, tr in tracks:
        af = [] if shift == 0 else ["-af", _rubberband(shift, formant and tr != "inst")]   # 伴奏没有共振峰的问题
        wav = out_dir / f"{cn}_{lab}.wav"
        _run([_ffmpeg(), "-y", "-v", "error", "-i", str(track_path(d, tr)), *af, "-c:a", "pcm_s16le", str(wav)])
        made.append(wav.name)
        if tr == "mix":             # Suno 上传用 mp3 更方便
            mp3 = out_dir / f"{cn}_{lab}.mp3"
            _run([_ffmpeg(), "-y", "-v", "error", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", "320k", str(mp3)])
            made.append(mp3.name)
    write_manifest(name)
    return made


async def export_job(name: str, shift: int, formant: bool) -> None:
    d = song_dir(name)
    job = JOBS[d.name] = {"kind": "export", "status": "running", "progress": 0, "message": f"导出 {shift_label(shift)} …"}
    try:
        files = await asyncio.to_thread(export, name, shift, formant)
        job.update(status="done", progress=100, message="已导出：" + "、".join(files))
    except Exception as e:
        job.update(status="error", message=f"导出失败：{e}")


# ---------------------------------------------------------------- 歌词 → 配图提示词
def save_lyrics(name: str, text: str) -> None:
    d = song_dir(name)
    (d / DIRS["lyrics"]).mkdir(exist_ok=True)
    (d / DIRS["lyrics"] / "歌词.txt").write_text(text.strip() + "\n", "utf-8")
    write_manifest(name)


def prompts_markdown(name: str, style: str, scenes: list) -> str:
    out = [f"# {name} · 配图提示词", "", f"风格：{style}", "", "每张图：9:16 竖版（1080×1920），画面里不要出现文字、水印、logo。", ""]
    for i, s in enumerate(scenes, 1):
        out += [f"## {i:02d} {s.title}", "", f"对应歌词：{s.lyrics}", "", f"画面：{s.description}", "",
                "英文提示词（给 Midjourney / SD / Flux 等用）：", "", "```", s.prompt_en, "```", ""]
    return "\n".join(out)


async def make_prompts(name: str, style: str, count: int = 4) -> str:
    from pydantic import BaseModel, Field

    from . import settings
    from .providers import chat_json

    lyrics = info(name)["lyrics"].strip()
    if not lyrics:
        raise SongError("先粘贴歌词并保存")

    class Scene(BaseModel):
        title: str = Field(description="画面标题，4–8 个字")
        lyrics: str = Field(description="这张图对应的歌词段落（原文摘录）")
        description: str = Field(description="中文画面描述：主体、环境、时间与光线、色调、镜头景别，60–120 字")
        prompt_en: str = Field(description="英文图像生成提示词，逗号分隔的短语，包含风格、构图、光线、画幅 9:16，末尾加 no text, no watermark")

    class Plan(BaseModel):
        scenes: list[Scene]

    s = settings.load()
    p = s.provider(s.agent_provider)
    system = ("你是 MV 美术指导。根据歌词为竖版歌词 MV 设计背景图：按歌曲段落顺序，每张对应一段歌词，"
              "画面贴合歌词意象和情绪，风景为主、人物不露正脸，整组色调统一、可以连成一个故事。不要在画面里放文字。")
    plan = await chat_json(p, system, f"风格：{style}\n张数：{count}\n\n歌词：\n{lyrics}", Plan)
    md = prompts_markdown(song_dir(name).name, style, plan.scenes[:max(3, min(5, count))])
    (song_dir(name) / DIRS["images"] / "配图提示词.md").write_text(md, "utf-8")
    write_manifest(name)
    return md


def open_folder(name: str, sub: str = "") -> None:
    d = song_dir(name)
    target = d / sub if sub in DIRS.values() else d
    if sys.platform == "win32":
        os.startfile(str(target))          # noqa: S606（本机程序，打开用户自己的文件夹）
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "separate":
        _separate_main(*sys.argv[2:6])
