"""连接本机的 ChatCut Desktop。

ChatCut Desktop 自带一个本机 MCP 连接，让同一台电脑上的 Agent 直接编辑 Desktop 里当前打开的项目
（官方文档：chatcut.io/docs/desktop-app）。文档没有公开它的地址和端口，所以这里自动查找：

1. Desktop 安装后会把自己登记到本机 Agent 的 MCP 配置里：读 Claude Code（~/.claude.json）、
   Claude Desktop、Codex（~/.codex/config.toml）、Cursor 的配置，挑出名字 / 地址 / 命令里带 chatcut、
   而且是本机的那一项（云端 api.chatcut.io 的不算）。
2. 找正在运行的 ChatCut Desktop 进程，看它在本机监听哪些端口，逐个试 MCP 路径。

找到的每个候选都会真的握手一次（列出工具），能连上的才算数。也可以在界面里手动填地址或命令。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
MCP_PATHS = ("/api/external-mcp/mcp", "/mcp")      # 云端 ChatCut 的路径是前者，本机版先试同一个


class Endpoint(BaseModel):
    """一个可连接的 MCP 端点：本机 HTTP 地址，或一条启动本机 MCP 程序的命令（stdio）。"""
    transport: str = "http"                           # http / stdio
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    name: str = ""
    source: str = ""                                  # 从哪里找到的（显示给用户）

    def label(self) -> str:
        return self.url if self.transport == "http" else " ".join([self.command, *self.args])

    def sdk_config(self) -> dict:
        """给 Claude Agent SDK 的 mcp_servers 用。"""
        if self.transport == "http":
            return {"type": "http", "url": self.url, **({"headers": self.headers} if self.headers else {})}
        return {"type": "stdio", "command": self.command, "args": self.args, **({"env": self.env} if self.env else {})}


def _home() -> Path:
    return Path(os.environ.get("CHATCUT_STUDIO_HOME") or Path.home())


def config_files() -> list[tuple[str, Path, str]]:
    """（显示名, 路径, 格式）。格式：claude（~/.claude.json，含各项目的 mcpServers）、json（mcpServers）、codex（toml）。"""
    h = _home()
    if sys.platform == "win32":
        claude_desktop = Path(os.environ.get("APPDATA", h / "AppData" / "Roaming")) / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "darwin":
        claude_desktop = h / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    else:
        claude_desktop = h / ".config" / "Claude" / "claude_desktop_config.json"
    return [
        ("Claude Code", h / ".claude.json", "claude"),
        ("Claude Code", h / ".claude" / "mcp.json", "json"),
        ("Claude Desktop", claude_desktop, "json"),
        ("Codex", h / ".codex" / "config.toml", "codex"),
        ("Cursor", h / ".cursor" / "mcp.json", "json"),
    ]


def _is_local_chatcut(name: str, e: Endpoint) -> bool:
    text = " ".join([name, e.url, e.command, *e.args]).lower()
    if "chatcut" not in text or "api.chatcut.io" in text:
        return False
    if e.transport == "http":
        return (urlparse(e.url).hostname or "") in LOCAL_HOSTS
    return bool(e.command)


def _entry(name: str, d: dict, source: str) -> Endpoint | None:
    if not isinstance(d, dict):
        return None
    url = d.get("url") or d.get("serverUrl") or ""
    headers = {k: str(v) for k, v in (d.get("headers") or d.get("http_headers") or {}).items()}
    if d.get("bearer_token_env_var") and os.environ.get(d["bearer_token_env_var"]):        # Codex 的写法
        headers["Authorization"] = "Bearer " + os.environ[d["bearer_token_env_var"]]
    if url:
        e = Endpoint(transport="http", url=url, headers=headers, name=name, source=source)
    elif d.get("command"):
        e = Endpoint(transport="stdio", command=str(d["command"]), args=[str(a) for a in d.get("args") or []],
                     env={k: str(v) for k, v in (d.get("env") or {}).items()}, name=name, source=source)
    else:
        return None
    return e if _is_local_chatcut(name, e) else None


def from_configs() -> list[Endpoint]:
    found: list[Endpoint] = []
    for label, path, fmt in config_files():
        try:
            if not path.is_file():
                continue
            if fmt == "codex":
                servers = [(n, d, f"{label}（{path}）") for n, d in (tomllib.loads(path.read_text("utf-8")).get("mcp_servers") or {}).items()]
            else:
                data = json.loads(path.read_text("utf-8"))
                servers = [(n, d, f"{label}（{path}）") for n, d in (data.get("mcpServers") or {}).items()]
                if fmt == "claude":     # 只对某个项目生效的配置
                    for proj, pd in (data.get("projects") or {}).items():
                        servers += [(n, d, f"{label}（项目 {proj}）") for n, d in ((pd or {}).get("mcpServers") or {}).items()]
        except Exception:
            continue                      # 配置文件损坏 / 格式不认识：跳过，不影响别的来源
        for n, d, src in servers:
            if (e := _entry(n, d, src)) and all(e.label() != f.label() for f in found):
                found.append(e)
    return found


def from_processes() -> list[Endpoint]:
    """正在运行的 ChatCut Desktop 进程在本机监听的端口 → 候选地址（还没验证）。"""
    try:
        import psutil
    except ImportError:
        return []
    ports: set[int] = set()
    me = os.getpid()
    for p in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            info = p.info
            text = " ".join([info.get("name") or "", info.get("exe") or "", *(info.get("cmdline") or [])]).lower()
            if p.pid == me or "chatcut" not in text or "chatcut_studio" in text or "python" in (info.get("name") or "").lower():
                continue
            conns = p.net_connections(kind="inet") if hasattr(p, "net_connections") else p.connections(kind="inet")
            ports |= {c.laddr.port for c in conns if c.status == psutil.CONN_LISTEN and c.laddr.ip in LOCAL_HOSTS}
        except Exception:
            continue                      # 没权限 / 进程已退出
    return [Endpoint(transport="http", url=f"http://127.0.0.1:{port}{path}", name="ChatCut Desktop",
                     source=f"正在运行的 ChatCut Desktop（端口 {port}）") for port in sorted(ports) for path in MCP_PATHS]


async def probe(e: Endpoint, timeout: float = 6) -> tuple[bool, str, int]:
    """真的连一次并列出工具。返回（是否成功, 说明, 工具数）。"""
    from . import chatcut_mcp
    try:
        async def go():
            async with chatcut_mcp.open_endpoint(e) as client:
                return await chatcut_mcp.list_tools(client)
        tools = await asyncio.wait_for(go(), timeout)
        return True, f"可以连接，{len(tools)} 个工具", len(tools)
    except Exception as ex:
        return False, f"{type(ex).__name__}: {ex}"[:300], 0


async def discover(probe_all: bool = True) -> list[dict]:
    """查找并验证。返回 [{endpoint, ok, message, tools}]，能连上的排前面。"""
    cands = from_configs()
    cands += [e for e in from_processes() if all(e.label() != c.label() for c in cands)]
    if not probe_all:
        return [{"endpoint": e.model_dump(), "ok": None, "message": "", "tools": 0} for e in cands]
    results = await asyncio.gather(*(probe(e) for e in cands))
    out = [{"endpoint": e.model_dump(), "ok": ok, "message": msg, "tools": n} for e, (ok, msg, n) in zip(cands, results)]
    # 进程端口是猜的：只留下真能连上的；配置文件里写明的即使暂时连不上也列出来（多半是 Desktop 没打开）
    out = [r for r in out if r["ok"] or not r["endpoint"]["source"].startswith("正在运行")]
    return sorted(out, key=lambda r: not r["ok"])


async def first_working() -> Endpoint | None:
    for r in await discover():
        if r["ok"]:
            return Endpoint(**r["endpoint"])
    return None
