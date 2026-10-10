"""诊断：为什么 ChatCut Desktop 里的 Agent 没有用上 Studio 里的 DeepSeek。

Studio 的「接入」只改 Claude Code 的用户级设置（~/.claude/settings.json）。Desktop 里仍然是别的模型（比如 Qwen）时，
可能是：
1. Desktop 里用的是它自带的 Agent，没有切换到「Claude Code」；
2. 有比用户级设置优先的配置还指向别的模型：系统 / 用户环境变量、Claude Code 的本地设置或企业托管设置；
3. ChatCut Desktop 启动 Claude Code 时自己传了一套模型设置。
这里把能在本机查到的都查一遍，并在本机实际调用一次 Claude Code，看网关有没有收到请求。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import gateway, settings
from .desktop import _home

WATCH_KEYS = re.compile(r"^(ANTHROPIC_|CLAUDE_CODE_USE_|DASHSCOPE|QWEN|OPENAI_BASE_URL|OPENAI_API_BASE|CODEX_)", re.I)
SECRET_KEYS = re.compile(r"(TOKEN|KEY|SECRET)", re.I)
OTHER_MODEL = re.compile(r"qwen|dashscope|aliyuncs|bailian|百炼|通义", re.I)


def mask(key: str, value: str) -> str:
    v = str(value)
    if SECRET_KEYS.search(key) or re.match(r"^(sk-|sk_)", v):
        return (v[:4] + "…" + v[-4:]) if len(v) > 12 else "***"
    return v


def env_vars() -> list[dict]:
    """Studio 进程看到的环境变量，以及 Windows 注册表里的用户 / 系统环境变量（新开的程序会读到这些）。"""
    out = [{"source": "当前环境变量（启动 Studio 的窗口）", "key": k, "value": v} for k, v in os.environ.items() if WATCH_KEYS.match(k)]
    if sys.platform == "win32":
        import winreg
        for label, root, sub in (("Windows 用户环境变量", winreg.HKEY_CURRENT_USER, r"Environment"),
                                 ("Windows 系统环境变量", winreg.HKEY_LOCAL_MACHINE,
                                  r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
            try:
                with winreg.OpenKey(root, sub) as k:
                    i = 0
                    while True:
                        try:
                            name, value, _ = winreg.EnumValue(k, i)
                        except OSError:
                            break
                        i += 1
                        if WATCH_KEYS.match(name):
                            out.append({"source": label, "key": name, "value": str(value)})
            except OSError:
                continue
    return out


def claude_settings_files() -> list[tuple[str, Path]]:
    h = _home()
    files = [("Claude Code 用户设置", h / ".claude" / "settings.json"),
             ("Claude Code 用户本地设置", h / ".claude" / "settings.local.json")]
    if sys.platform == "win32":
        files += [("Claude Code 托管设置（优先级最高）", Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ClaudeCode" / "managed-settings.json"),
                  ("Claude Code 托管设置（优先级最高）", Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "ClaudeCode" / "managed-settings.json")]
    elif sys.platform == "darwin":
        files += [("Claude Code 托管设置（优先级最高）", Path("/Library/Application Support/ClaudeCode/managed-settings.json"))]
    else:
        files += [("Claude Code 托管设置（优先级最高）", Path("/etc/claude-code/managed-settings.json"))]
    return files


def settings_entries() -> list[dict]:
    out = []
    for label, f in claude_settings_files():
        if not f.is_file():
            continue
        try:
            data = json.loads(f.read_text("utf-8") or "{}")
        except (OSError, ValueError) as e:
            out.append({"source": f"{label}（{f}）", "key": "（读取失败）", "value": str(e)})
            continue
        for k, v in (data.get("env") or {}).items():
            if WATCH_KEYS.match(k):
                out.append({"source": f"{label}（{f}）", "key": k, "value": str(v)})
        if data.get("model"):
            out.append({"source": f"{label}（{f}）", "key": "model", "value": str(data["model"])})
    return out


def chatcut_desktop_configs(limit: int = 40) -> list[dict]:
    """在 ChatCut Desktop 的配置目录里找提到 Qwen / 模型地址的配置（只读小的文本配置文件）。"""
    h = _home()
    roots = [Path(os.environ.get("APPDATA", h / "AppData" / "Roaming")), Path(os.environ.get("LOCALAPPDATA", h / "AppData" / "Local")),
             h / "Library" / "Application Support", h / ".config", h]
    dirs = []
    for r in roots:
        try:
            dirs += [d for d in r.iterdir() if d.is_dir() and "chatcut" in d.name.lower() and "studio" not in d.name.lower()]
        except OSError:
            continue
    hits = []
    pat = re.compile(r"qwen|dashscope|aliyuncs|ANTHROPIC_BASE_URL|ANTHROPIC_MODEL|base_?url|\"model\"", re.I)
    for d in dict.fromkeys(dirs):
        for f in d.rglob("*"):
            if len(hits) >= limit:
                return hits
            try:
                if (not f.is_file() or f.suffix.lower() not in (".json", ".toml", ".yaml", ".yml", ".ini", ".env", ".conf", ".cfg")
                        or f.stat().st_size > 2_000_000 or len(f.relative_to(d).parts) > 5):
                    continue
                for line in f.read_text("utf-8", errors="replace").splitlines():
                    if pat.search(line):
                        line = re.sub(r"(sk-[A-Za-z0-9_-]{4})[A-Za-z0-9_-]+", r"\1…", line.strip())
                        hits.append({"file": str(f), "line": line[:200]})
                        if len(hits) >= limit:
                            return hits
            except (OSError, ValueError):
                continue
    return hits


def run_claude_once(timeout: int = 120) -> dict:
    """在本机实际调用一次 Claude Code（和 Desktop 启动的是同一个），看网关有没有收到请求。"""
    exe = shutil.which("claude")
    if not exe:
        return {"ran": False, "message": "本机找不到 claude 命令：没有安装 Claude Code，ChatCut Desktop 也就不会出现 Claude Code 这个选项"}
    before = len(gateway.RECENT) and gateway.RECENT[-1]
    try:
        p = subprocess.run([exe, "-p", "请只回复两个字：收到", "--output-format", "json"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, cwd=str(_home()))
    except subprocess.TimeoutExpired:
        return {"ran": True, "via_gateway": bool(gateway.RECENT and gateway.RECENT[-1] is not before), "message": f"Claude Code {timeout} 秒没有回复"}
    via = bool(gateway.RECENT and gateway.RECENT[-1] is not before and gateway.RECENT[-1]["client"] != "ChatCut Studio 自测")
    try:
        data = json.loads(p.stdout)
        models = list((data.get("modelUsage") or {}).keys())
        msg = (data.get("result") or "")[:200]
        err = data.get("is_error")
    except ValueError:
        models, msg, err = [], (p.stdout + p.stderr).strip()[-400:], p.returncode != 0
    return {"ran": True, "via_gateway": via, "models": models, "message": msg, "error": bool(err)}


def run_codex_once(timeout: int = 120) -> dict:
    """在本机实际调用一次 Codex（Desktop 里的「ChatGPT」），看网关有没有收到 Responses 请求。"""
    exe = shutil.which("codex")
    if not exe:
        return {"ran": False, "message": "本机找不到 codex 命令"}
    before = gateway.RECENT[-1] if gateway.RECENT else None
    try:
        p = subprocess.run([exe, "exec", "--skip-git-repo-check", "请只回复两个字：收到"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, cwd=str(_home()))
        out = (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        out = f"Codex {timeout} 秒没有回复"
    via = bool(gateway.RECENT and gateway.RECENT[-1] is not before and "Studio 自测" not in gateway.RECENT[-1]["client"])
    return {"ran": True, "via_gateway": via, "models": [], "message": out[-300:], "error": not via}


def _points_elsewhere(e: dict) -> bool:
    return e["key"].upper() == "ANTHROPIC_BASE_URL" and e["value"].rstrip("/") != gateway.gateway_url()


async def diagnose(run_claude: bool = True) -> dict:
    st = gateway.status()
    envs, sets = env_vars(), settings_entries()
    desk = await asyncio.to_thread(chatcut_desktop_configs)
    claude = await asyncio.to_thread(run_claude_once) if run_claude and st["linked"] else None
    codex = await asyncio.to_thread(run_codex_once) if run_claude and st["codex_linked"] else None
    findings = []

    def add(level, text):
        findings.append({"level": level, "text": text})

    if not st["codex_installed"] and not st["claude_installed"]:
        add("fail", "本机既没有 Codex 也没有 Claude Code，ChatCut Desktop 的 Agent 按钮里就不会有它们。")
    if not st["linked"] and not st["codex_linked"]:
        add("fail", "还没有接入：Desktop 的 Agent 按钮里是「ChatGPT」就点「接入 Codex」，是「Claude」就点「接入 Claude Code」。")
    if st["codex_installed"] and not st["codex_linked"]:
        add("warn", "本机有 Codex（Desktop 里显示为「ChatGPT」），但还没接入：点「接入 Codex」。")
    if st["problem"]:
        add("fail", f"DeepSeek 配置不完整：{st['problem']}")
    for e in envs:
        if _points_elsewhere(e) or OTHER_MODEL.search(e["value"]) or e["key"].upper() in ("ANTHROPIC_MODEL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            add("fail" if _points_elsewhere(e) or OTHER_MODEL.search(e["value"]) else "warn",
                f"{e['source']}里有 {e['key']} = {mask(e['key'], e['value'])}。环境变量会和 Studio 的设置冲突"
                + ("（看起来是 Qwen / 阿里云的配置）" if OTHER_MODEL.search(e["value"]) else "")
                + "：Windows 搜索「编辑系统环境变量」→ 环境变量，删掉这一项，然后重启 ChatCut Desktop 和 Studio。")
    for e in sets:
        if "用户设置（" in e["source"] and e["key"].upper() != "MODEL":
            continue                                   # 用户设置是 Studio 写的
        if _points_elsewhere(e) or OTHER_MODEL.search(e["value"]) or e["key"] == "model":
            add("fail", f"{e['source']} 里有 {e['key']} = {mask(e['key'], e['value'])}，优先级比 Studio 写的设置高"
                + ("，而且指向 Qwen / 阿里云" if OTHER_MODEL.search(e["value"]) else "") + "：删掉这一项后重启 ChatCut Desktop。")
    qwen_desk = [d for d in desk if OTHER_MODEL.search(d["line"])]
    if qwen_desk:
        add("warn", f"ChatCut Desktop 自己的配置里提到了 Qwen（{len(qwen_desk)} 处，见下方明细）："
                    "Desktop 自带的 Agent 用的是它自己的模型，要在 AI 输入框的 Agent 选择里切换到「Claude Code」才会用 DeepSeek；"
                    "如果 Desktop 在设置里能给 Claude Code 单独选模型或填地址，也要改成默认 / 留空。")
    if claude:
        if not claude["ran"]:
            add("fail", claude["message"])
        elif claude["via_gateway"]:
            add("ok", "本机直接运行 Claude Code 时已经通过网关用上了 DeepSeek（网关收到了请求）。"
                      "所以如果 ChatCut Desktop 里还是 Qwen，说明 Desktop 用的不是 Claude Code：在 AI 输入框的 Agent 选择里切到「Claude Code」，新开一个对话。")
        else:
            add("fail", "本机直接运行 Claude Code，网关没有收到请求：Claude Code 被别的配置覆盖了（见上面的环境变量 / 设置文件），"
                        f"它用的模型是 {', '.join(claude.get('models') or []) or '未知'}。")
    if codex:
        if codex["via_gateway"]:
            add("ok", "本机直接运行 Codex 时已经通过网关用上了 DeepSeek。如果 ChatCut Desktop 里还不是，"
                      "重启 ChatCut Desktop，在 Agent 按钮里选「ChatGPT」，新开一个对话。")
        else:
            add("fail", "本机直接运行 Codex，网关没有收到请求：Codex 被别的配置覆盖了（环境变量、项目里的 .codex/config.toml，"
                        "或者 Desktop 启动它时指定了别的模型）。Codex 的输出见明细。")
    if not any(f["level"] == "fail" for f in findings) and not gateway.RECENT:
        add("warn", "网关还没有收到过 Desktop 的请求。在 ChatCut Desktop 里把 Agent 切到「ChatGPT」（或「Claude」），新开对话发一句话后再诊断一次。")
    return {"status": st, "findings": findings,
            "env": [{**e, "value": mask(e["key"], e["value"])} for e in envs],
            "settings": [{**e, "value": mask(e["key"], e["value"])} for e in sets],
            "desktop_configs": desk, "claude": claude, "codex": codex, "recent": list(gateway.RECENT)[-10:]}
