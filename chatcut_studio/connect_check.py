"""连接检查：Desktop + Agent 这条路上的每一环，分开测。

1. API：通过网关调一次「用哪个模型」里选的模型（和 Desktop 里的 Agent 走同一条路）。
2. ChatCut Desktop：Studio 自己连一次 Desktop 的本机 MCP，列出工具（Desktop 和项目有没有打开）。
3. Agent ↔ Desktop：
   - 静态：Codex 配置里有没有登记 ChatCut Desktop 的本机 MCP；~/.codex/skills 里装了哪些 skill。
   - 网关记录：Desktop 里最近的对话，Codex 带来了几个 ChatCut 工具、哪些 skill，模型有没有调用 ChatCut 工具。
   - 现场测试：在本机跑一次 codex exec，让它用只读工具读取当前项目，看工具调用有没有真的发生。
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess

from . import desktop, gateway, styles

AGENT_PROMPT = ("这是 ChatCut Studio 的连接测试，不要修改任何东西。"
                "调用 ChatCut Desktop 本机 MCP 里一个只读工具（比如 get_project、get_active_project 或 get_timeline），"
                "读取当前打开的项目，用一句话回答项目名和画幅；然后列出你现在能用的 skill 名称。")


async def check_api() -> dict:
    r = await gateway.self_test()
    st = gateway.status()
    return {"ok": bool(r.get("ok")), "message": r.get("message", ""), "model": st.get("model"), "provider": st.get("provider_label")}


async def check_desktop() -> dict:
    found = await desktop.discover()
    ok = [r for r in found if r["ok"]]
    if ok:
        e = ok[0]
        return {"ok": True, "message": f"已连上 ChatCut Desktop 本机 MCP：{e['message']}", "endpoint": e["endpoint"]["url"] or e["endpoint"]["command"],
                "tools": e["tools"]}
    why = "；".join(f"{r['endpoint']['source']}：{r['message']}" for r in found)[:400]
    return {"ok": False, "message": "连不上 ChatCut Desktop 的本机 MCP：先打开 ChatCut Desktop 和一个项目"
                                    + (f"（{why}）" if why else "（本机配置里也没有找到它登记的连接）"), "tools": 0}


def installed_skills() -> list[str]:
    d = styles.codex_skills_dir()
    if not d.is_dir():
        return []
    return sorted(x.name for x in d.iterdir() if (x / "SKILL.md").is_file())


def _summary(recs: list[dict]) -> dict:
    tools = sorted({t for r in recs for t in r.get("chatcut_tools") or []})
    calls = [c for r in recs for c in r.get("calls") or [] if "chatcut" in c.lower()]
    skills = sorted({k for r in recs for k in r.get("skills") or []})
    results = [x for r in recs for x in r.get("chatcut_results") or []]
    return {"requests": len(recs), "chatcut_tools": tools, "chatcut_calls": calls, "skills": skills,
            "results_ok": [x for x in results if x["ok"]], "results_failed": [x for x in results if not x["ok"]],
            "errors": [f"{r.get('time')} HTTP {r.get('status')}" for r in recs if (r.get("status") or 200) >= 400]}


def check_agent_static() -> dict:
    st = gateway.status()
    mcp = [e.label() for e in desktop.from_configs() if e.source.startswith("Codex")]
    # Desktop 里的对话（不算 Studio 自测，也不算「现场测试」自己跑的 codex exec）
    recent = [r for r in gateway.RECENT if "chatcut_tools" in r and not re.search(r"Studio|codex_exec", r.get("client") or "")]
    return {"codex_installed": st["codex_installed"], "codex_linked": st["codex_linked"], "codex_mcp": mcp,
            "skills_installed": installed_skills(), "skills_dir": str(styles.codex_skills_dir()),
            "recent": _summary(recent[-10:]), "recent_time": recent[-1]["time"] if recent else ""}


def run_codex(timeout: int = 240) -> dict:
    """在本机真的跑一次 Codex：模型走网关，工具走 ChatCut Desktop 的本机 MCP。"""
    exe = shutil.which("codex")
    if not exe:
        return {"ran": False, "ok": False, "message": "本机找不到 codex 命令"}
    n = len(gateway.RECENT)
    marker = gateway.RECENT[-1] if gateway.RECENT else None
    try:
        p = subprocess.run([exe, "exec", "--skip-git-repo-check", "-s", "read-only", *_readonly_overrides(), AGENT_PROMPT], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                           cwd=str(desktop._home()))
        out = (p.stdout or "").strip() or (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        out = f"Codex {timeout} 秒没有做完"
    recs = list(gateway.RECENT)
    if marker is not None and marker in recs:
        recs = recs[recs.index(marker) + 1:]
    else:
        recs = recs[n:] if len(recs) > n else recs[-max(1, len(recs) - n):]
    recs = [r for r in recs if "chatcut_tools" in r or r.get("status", 200) >= 400]
    s = _summary(recs)
    return {"ran": True, "ok": bool(s["results_ok"]), "output": out[-600:], **s}


def _readonly_overrides() -> list[str]:
    """现场测试只放行只读的 ChatCut 工具：在这次运行里，Codex 配置里登记的 ChatCut Desktop 服务器
    只开放读取类工具，并且免确认（命令行没人点「允许」）；改动、导出、花积分的工具这次根本看不到。"""
    from .agent import COSTLY, READ_ONLY
    from .chatcut_mcp import list_tools, open_endpoint

    async def names(e):
        async with open_endpoint(e) as client:
            return [t["name"] if isinstance(t, dict) else t.name for t in await list_tools(client)]

    args = []
    for e in desktop.from_configs():
        if not e.source.startswith("Codex") or not re.fullmatch(r"[A-Za-z0-9_-]+", e.name or ""):
            continue
        try:
            tools = asyncio.run(asyncio.wait_for(names(e), 15))
        except Exception:
            continue
        ro = [t for t in tools if READ_ONLY.search(t) and not COSTLY.search(t)]
        if ro:
            args += ["-c", f'mcp_servers.{e.name}.default_tools_approval_mode="approve"',
                     "-c", f"mcp_servers.{e.name}.enabled_tools={json.dumps(ro, ensure_ascii=False)}"]
    return args


async def check_agent(live: bool = True) -> dict:
    static = check_agent_static()
    live_r = await asyncio.to_thread(run_codex) if live and static["codex_linked"] else None
    findings = []

    def add(level, text):
        findings.append({"level": level, "text": text})

    if not static["codex_installed"]:
        add("fail", "本机没有 Codex（Desktop 里的「ChatGPT」）。")
    elif not static["codex_linked"]:
        add("fail", "Codex 还没接入 Studio：在上面点「接入 Codex」。")
    if static["codex_mcp"]:
        add("ok", "Codex 的配置里登记了 ChatCut Desktop 的本机 MCP：" + "、".join(static["codex_mcp"]))
    else:
        add("warn", "Codex 的配置（~/.codex/config.toml）里没有 ChatCut Desktop 的本机 MCP。Desktop 启动 Codex 时可能会自己带上；"
                    "以下面「Desktop 最近的对话」为准。")
    sk = static["skills_installed"]
    add("ok" if sk else "warn", f"Codex 的 skill 目录里有 {len(sk)} 个 skill：" + "、".join(sk) if sk
        else f"Codex 的 skill 目录（{static['skills_dir']}）里还没有 skill：在「更多功能 → 常用风格」里点「安装为 Agent 的 skill」。")
    r = static["recent"]
    if r["requests"]:
        if r["chatcut_tools"]:
            add("ok", f"Desktop 最近的对话（{static['recent_time']}）带来了 {len(r['chatcut_tools'])} 个 ChatCut 工具"
                      + (f"，模型调用过 {len(r['chatcut_calls'])} 次" if r["chatcut_calls"] else "，模型还没调用过") + "。")
        else:
            add("fail", f"Desktop 最近的对话（{static['recent_time']}）里没有 ChatCut 工具：Agent 看不到 Desktop 的项目。"
                        "确认 Desktop 里打开了项目，在 AI 面板新开一个对话再试。")
        if r["results_failed"] and not r["results_ok"]:
            x = r["results_failed"][-1]
            add("warn", f"Desktop 最近的对话里，{x['tool']} 没有执行成功：{x['text'][:150]}")
        missing = [k for k in sk if k not in r["skills"]]
        if r["skills"] or sk:
            add("ok" if not missing else "warn", f"最近的对话里 Agent 看到的 skill：{'、'.join(r['skills']) or '（无）'}"
                + (f"；没看到：{'、'.join(missing)}（新装的 skill 要新开对话才生效）" if missing else ""))
    else:
        add("warn", "网关还没收到过 Desktop 里 Agent 的请求（Studio 重启后会清空）：在 Desktop 里选「ChatGPT」发一句话，再检查一次。")
    if live_r:
        if not live_r["ran"]:
            add("fail", live_r["message"])
        elif live_r["ok"]:
            x = live_r["results_ok"][0]
            add("ok", f"现场测试通过：Codex 通过网关用上了模型，调用 ChatCut Desktop 的 {x['tool']} 成功，读到：{x['text'][:80]}")
        elif live_r["results_failed"]:
            x = live_r["results_failed"][0]
            add("fail", f"现场测试：模型调用了 {x['tool']}，但工具没有执行成功：{x['text'][:150]}")
        elif not live_r["requests"]:
            add("fail", "现场测试：网关没收到 Codex 的请求，Codex 没走 Studio（见输出）。")
        elif not live_r["chatcut_tools"]:
            add("warn", "现场测试：命令行里的 Codex 没带 ChatCut 工具。Desktop 可能是在启动 Codex 时才把本机 MCP 交给它的，"
                        "以「Desktop 最近的对话」为准。")
        else:
            add("fail", f"现场测试：Codex 带了 {len(live_r['chatcut_tools'])} 个 ChatCut 工具，但模型没有调用（见输出）。")
    ok = not any(f["level"] == "fail" for f in findings) and (
        bool(live_r and live_r["ok"]) or bool(r["chatcut_calls"]) or bool(r["chatcut_tools"]))
    return {"ok": ok, "findings": findings, "static": static, "live": live_r}
