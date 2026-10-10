"""歌曲工坊：音高 / 调性分析、降调精度、素材文件夹、接口。"""
import json
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from chatcut_studio import settings, songlab
from chatcut_studio.app import create_app

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="需要 ffmpeg")
SR = 44100


def tone(midi_notes, sec=0.8, harmonics=8, amp=0.25):
    out = []
    for m in midi_notes:
        f = 440 * 2 ** ((m - 69) / 12)
        t = np.arange(int(SR * sec)) / SR
        ph = 2 * np.pi * f * t + 0.3 * np.sin(2 * np.pi * 5 * t)
        out.append(amp * sum(np.sin(k * ph) / k for k in range(1, harmonics + 1)) * np.minimum(1, t * 20) * np.minimum(1, (sec - t) * 20))
    return np.concatenate(out)


def write_wav(path: Path, y: np.ndarray):
    path.parent.mkdir(parents=True, exist_ok=True)
    x = (np.stack([y, y], 1) * 30000).clip(-32767, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(x.tobytes())


def test_note_and_suggestion():
    assert songlab.note_name(440) == "A4" and songlab.note_name(261.6) == "C4"
    n, why = songlab.suggest_shift(392, 466, 587)          # 常用高音 D5 → 降 5 到 A4
    assert n == -5 and "降 5" in why
    assert songlab.suggest_shift(150, 200, 300)[0] == 0      # 本来就低：不用降
    assert songlab.suggest_shift(600, 900, 1200)[0] == -7    # 最多降 7
    assert songlab.shift_label(-4) == "降4" and songlab.safe_name(' a/b:c? ') == "a b c"


@needs_ffmpeg
def test_f0_and_key(tmp_path):
    melody = [69, 72, 74, 76, 74, 72, 69, 67] * 2             # A 小调旋律，A4–E5
    write_wav(tmp_path / "v.wav", tone(melody))
    f0, _ = songlab.vocal_f0(songlab.load_mono(tmp_path / "v.wav"))
    v = f0[f0 > 0]
    assert abs(69 + 12 * np.log2(np.median(v) / 440) - 73) < 1.2   # 这段旋律的中位音在 C5–D5 之间
    chords = np.concatenate([sum(tone([r + d], sec=1.0, harmonics=4, amp=0.15) for d in (0, 3, 7)) for r in (57, 60, 55, 57)])
    write_wav(tmp_path / "c.wav", np.tile(chords, 3))
    assert songlab.detect_key(songlab.load_mono(tmp_path / "c.wav")) in ("A 小调", "C 大调")   # 关系大小调都算对


@needs_ffmpeg
def test_song_pipeline_without_separation(isolated_data):
    """新建（mp4）→ 放入分离结果 → 分析 → 试听 → 导出降 4，降调后音高正好低 4 个半音，时长不变。"""
    melody = [74, 76, 79, 81, 79, 76] * 3                    # D5–A5，偏高的女声
    write_wav(isolated_data / "in.wav", tone(melody))
    mp4 = isolated_data / "原曲.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x90:d=15", "-i", str(isolated_data / "in.wav"),
                    "-shortest", "-c:v", "libx264", "-c:a", "aac", str(mp4)], check=True)
    st = songlab.create("测试/歌", mp4)
    d = Path(st["dir"])
    assert st["name"] == "测试 歌" and st["has_video"] and (d / "00_原曲" / "原曲.wav").is_file() and (d / "素材清单.txt").is_file()
    shutil.copy(d / "00_原曲" / "原曲.wav", d / "01_人声分离" / "人声.wav")
    write_wav(d / "01_人声分离" / "伴奏.wav", np.zeros(SR * 14))
    a = songlab.analyze(st["name"])
    assert a["suggest"] <= -4 and a["high_note"].startswith(("G5", "G#5", "A5", "A#5"))
    assert songlab.preview(st["name"], -4, "vocals").stat().st_size > 10_000
    files = songlab.export(st["name"], -4)
    assert set(files) == {"混音_降4.wav", "混音_降4.mp3", "人声_降4.wav", "伴奏_降4.wav"}
    before = songlab.vocal_f0(songlab.load_mono(d / "01_人声分离" / "人声.wav"))[0]
    after = songlab.vocal_f0(songlab.load_mono(d / "02_降调" / "人声_降4.wav"))[0]
    shift = 12 * np.log2(np.median(after[after > 0]) / np.median(before[before > 0]))
    assert abs(shift + 4) < 0.35, shift
    dur = lambda p: wave.open(str(p)).getnframes()
    assert abs(dur(d / "02_降调" / "人声_降4.wav") - dur(d / "01_人声分离" / "人声.wav")) < SR * 0.05
    manifest = (d / "素材清单.txt").read_text("utf-8")
    assert "人声_降4.wav" in manifest and "建议" in manifest


@needs_ffmpeg
def test_songs_api_upload_files_and_prompts(isolated_data, fake_llm):
    write_wav(isolated_data / "in.wav", tone([69, 72, 76] * 2))
    data = (isolated_data / "in.wav").read_bytes()
    llm = fake_llm([lambda body: {"content": json.dumps({"scenes": [
        {"title": "巷口暖灯", "lyrics": "巷口灯光忽明忽灭", "description": "雨后小巷，暖黄路灯", "prompt_en": "rainy alley, warm lamp, 9:16, no text"},
        {"title": "冷掉的咖啡", "lyrics": "手中甜咖啡已冷却", "description": "窗边的咖啡杯", "prompt_en": "coffee cup by window, no text"},
        {"title": "月", "lyrics": "你看啊仲夏的弯月", "description": "湖面弯月", "prompt_en": "crescent moon over lake, no text"}]}, ensure_ascii=False)}])
    settings.update({"providers": [{"id": "deepseek", "base_url": llm.base_url, "model": "deepseek-v4-pro", "api_key": "sk-test-1234567890"}],
                     "agent_provider": "deepseek"})
    with TestClient(create_app()) as c:
        r = c.post("/api/songs?name=巷口暖灯", content=data, headers={"X-Filename": "song.wav"})
        assert r.status_code == 200 and r.json()["has_audio"], r.text
        assert c.get("/api/songs").json()["songs"][0]["name"] == "巷口暖灯"
        assert c.get("/api/songs/巷口暖灯/file", params={"path": "00_原曲/原曲.wav"}).status_code == 200
        assert c.get("/api/songs/巷口暖灯/file", params={"path": "../../settings.json"}).status_code == 404   # 不能读文件夹外面
        assert c.get("/api/songs/巷口暖灯/preview", params={"shift": -3}).headers["content-type"] == "audio/mpeg"
        assert c.put("/api/songs/巷口暖灯/lyrics", json={"text": "巷口灯光忽明忽灭\n手中甜咖啡已冷却"}).json()["ok"]
        md = c.post("/api/songs/巷口暖灯/prompts", json={"style": "电影感国风", "count": 3}).json()["markdown"]
    assert "## 01 巷口暖灯" in md and "rainy alley" in md
    d = songlab.SONGS_ROOT / "巷口暖灯"
    assert (d / "05_配图" / "配图提示词.md").is_file() and (d / "04_歌词" / "歌词.txt").read_text("utf-8").startswith("巷口")
    assert "巷口灯光忽明忽灭" in llm.requests[0]["body"]["messages"][-1]["content"]
