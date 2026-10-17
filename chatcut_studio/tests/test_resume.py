"""Studio 被意外结束、重启之后：没做完的任务接着做，做完的不再执行。"""
import asyncio
import json
import time

from fastapi.testclient import TestClient

from chatcut_studio import agent, gateway, settings, songlab
from chatcut_studio.app import create_app


def _wait(cond, seconds=5):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_run_record_kept_when_cancelled_and_removed_when_finished(isolated_data):
    async def forever(run):
        await asyncio.sleep(3600)

    async def quick(run):
        await run.emit({"type": "result", "is_error": False, "cost_usd": 0, "turns": 1, "text": "ok"})

    async def main(runner):
        run = agent.RunManager(runner).start("把视频改成竖屏")
        await asyncio.sleep(0.1)
        return run.id

    unfinished = asyncio.run(main(forever))      # asyncio.run 结束时取消任务 = Studio 关闭
    finished = asyncio.run(main(quick))
    store = isolated_data / "runs"
    assert (store / f"{unfinished}.json").is_file() and not (store / f"{finished}.json").exists()
    assert json.loads((store / f"{unfinished}.json").read_text("utf-8"))["prompt"] == "把视频改成竖屏"


def test_unfinished_run_resumes_on_startup(isolated_data):
    store = isolated_data / "runs"
    store.mkdir()
    (store / "aaa.json").write_text(json.dumps({"id": "aaa", "prompt": "加字幕", "resumes": 0}), "utf-8")
    (store / "bbb.json").write_text(json.dumps({"id": "bbb", "prompt": "导出", "resumes": agent.MAX_RESUMES}), "utf-8")
    seen = []

    async def runner(run):
        seen.append(run.prompt)
        await run.emit({"type": "result", "is_error": False, "cost_usd": 0, "turns": 1, "text": "ok"})

    with TestClient(create_app(agent.RunManager(runner))) as c:
        assert _wait(lambda: not (store / "aaa.json").exists())
        with c.stream("GET", "/api/run/aaa/events") as s:          # id 不变，页面能重新接上
            evs = [json.loads(l[6:]) for l in s.iter_lines() if l.startswith("data: ")]
        assert "自动续跑" in evs[0]["text"] and evs[-1]["type"] == "done"
        assert c.get("/api/run/active").json() == {"id": None}
    assert len(seen) == 1 and seen[0].startswith(agent.RESUME_NOTE) and seen[0].endswith("加字幕")
    assert "不要重复" in seen[0]
    assert not (store / "bbb.json").exists()                     # 续跑次数用完的不再跑


def test_song_jobs_resume_only_when_unfinished(isolated_data, monkeypatch):
    root = isolated_data / "songs"
    calls = []

    def fake_export(name, shift, formant=True):
        calls.append((name, shift))
        out = root / name / songlab.DIRS["shift"]
        for n in (f"混音_{songlab.shift_label(shift)}.wav", f"混音_{songlab.shift_label(shift)}.mp3"):
            (out / n).write_bytes(b"x")
        return ["ok"]

    monkeypatch.setattr(songlab, "export", fake_export)
    for name, status in (("没做完", "running"), ("做完了", "running"), ("出错了", "error")):
        d = root / name
        (d / songlab.DIRS["shift"]).mkdir(parents=True)
        (d / songlab.JOB_FILE).write_text(json.dumps({"kind": "export", "shift": -3, "formant": True, "status": status}), "utf-8")
    lab = songlab.shift_label(-3)
    for n in (f"混音_{lab}.wav", f"混音_{lab}.mp3"):
        (root / "做完了" / songlab.DIRS["shift"] / n).write_bytes(b"x")

    with TestClient(create_app()):
        assert _wait(lambda: not (root / "没做完" / songlab.JOB_FILE).exists())
    assert calls == [("没做完", -3)]
    assert songlab.JOBS["没做完"]["status"] == "done"
    assert not (root / "做完了" / songlab.JOB_FILE).exists()       # 结果已经在了：只清掉记录
    assert (root / "出错了" / songlab.JOB_FILE).is_file()           # 出错停下的不自动重跑


def test_startup_upgrades_old_codex_link(isolated_data):
    """旧版本接入的 Codex 没有重试设置和注意事项：Studio 启动时自动补上。"""
    settings.update({"providers": [{"id": "deepseek", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-pro", "api_key": "sk-x-1234567890"}]})
    gateway.link_codex()
    cfg = gateway.codex_config_path()
    cfg.write_text("".join(l for l in cfg.read_text("utf-8").splitlines(True) if "retries" not in l), "utf-8")
    gateway.agent_guard_files()["Codex"].unlink()
    assert not gateway.codex_resume_ready()
    with TestClient(create_app()) as c:
        assert c.get("/api/gateway").json()["codex_resume"] is True
    assert "stream_max_retries = 20" in cfg.read_text("utf-8")
