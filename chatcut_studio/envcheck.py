"""Claude Code 方式的环境体检与一键安装：把 ChatCut 官方安装文档里的命令包装成白名单动作。

参考：https://github.com/ChatCut-Inc/agent-plugin/blob/main/docs/claude-code-install.md
"""
from __future__ import annotations

import shutil
import subprocess

MARKETPLACE_URL = "https://github.com/ChatCut-Inc/agent-plugin.git"
PLUGIN = "chatcut@chatcut-inc"
MCP_ID = "plugin:chatcut:chatcut"

# 只允许运行这里列出的命令，界面不能传任意命令进来
ACTIONS: dict[str, list[list[str]]] = {
    "install": [
        ["claude", "plugin", "marketplace", "add", MARKETPLACE_URL],
        ["claude", "plugin", "install", PLUGIN],
    ],
    "update": [
        ["claude", "plugin", "marketplace", "update", "chatcut-inc"],
        ["claude", "plugin", "update", PLUGIN],
    ],
}

# 登录要打开浏览器完成 OAuth，并且需要交互式终端，只能让用户自己在终端里跑
LOGIN_COMMANDS = {
    "claude": f"claude mcp login {MCP_ID}",
    "codex": "codex mcp login chatcut",
}


def run(cmd: list[str], timeout: int = 60) -> dict:
    exe = shutil.which(cmd[0])
    if not exe:
        return {"cmd": " ".join(cmd), "ok": False, "output": f"找不到命令 {cmd[0]}"}
    try:   # 用 which 找到的完整路径：Windows 上 npm 装的是 claude.cmd，直接写 "claude" 启动不了
        p = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return {"cmd": " ".join(cmd), "ok": p.returncode == 0, "output": (p.stdout + p.stderr).strip()[-4000:]}
    except subprocess.TimeoutExpired:
        return {"cmd": " ".join(cmd), "ok": False, "output": f"超时（{timeout}s）"}


def check() -> list[dict]:
    """返回一组检查项：id、名称、状态（ok / warn / fail）、详情、修复建议。"""
    items = []
    claude = run(["claude", "--version"], 20)
    items.append({
        "id": "claude", "name": "Claude Code CLI", "status": "ok" if claude["ok"] else "fail",
        "detail": claude["output"], "fix": "安装 Claude Code：https://code.claude.com/docs",
    })
    codex = run(["codex", "--version"], 20)
    items.append({
        "id": "codex", "name": "Codex CLI（可选）", "status": "ok" if codex["ok"] else "warn",
        "detail": codex["output"], "fix": "可选：只用 Claude Code 时可以忽略",
    })
    if claude["ok"]:
        plugin = run(["claude", "plugin", "details", PLUGIN], 30)
        items.append({
            "id": "plugin", "name": "ChatCut 插件", "status": "ok" if plugin["ok"] else "fail",
            "detail": plugin["output"], "fix": "点击「一键安装插件」",
        })
        mcp = run(["claude", "mcp", "get", MCP_ID], 30)
        connected = mcp["ok"] and "connected" in mcp["output"].lower()
        items.append({
            "id": "mcp", "name": "ChatCut MCP 登录", "status": "ok" if connected else ("warn" if mcp["ok"] else "fail"),
            "detail": mcp["output"], "fix": f"在终端运行：{LOGIN_COMMANDS['claude']}，然后在浏览器里授权",
        })
    items.append({
        "id": "desktop", "name": "ChatCut Desktop 里的 Agent 选择", "status": "manual",
        "detail": "Desktop 没有公开接口，只能手动设置",
        "fix": "打开 ChatCut Desktop → AI 面板 → 点输入框旁边的 agent 按钮 → Agent Selection 里选 Claude Code",
    })
    return items


def do_action(name: str) -> list[dict]:
    if name not in ACTIONS:
        raise KeyError(name)
    results = []
    for cmd in ACTIONS[name]:
        r = run(cmd, 180)
        results.append(r)
        if not r["ok"]:
            break
    return results
