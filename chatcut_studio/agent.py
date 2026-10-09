"""用 Claude Agent SDK 驱动 ChatCut MCP 执行剪辑，花积分 / 删除 / 上传 / 导出前在界面上弹确认。

两种连接方式（环境变量 CHATCUT_MCP_MODE）：
- plugin（默认）：复用本机 Claude Code 里已安装、已登录的 ChatCut 插件（setting_sources=["user"]）。
- http：直接连 ChatCut 托管 MCP，需要自己提供 CHATCUT_MCP_TOKEN（OAuth access token，能否这样用待验证）。
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

CHATCUT_MCP_URL = "https://api.chatcut.io/api/external-mcp/mcp"
MODEL = os.environ.get("CHATCUT_STUDIO_MODEL", "claude-opus-5-5")

# 工具名里出现这些词，视为会花积分、不可逆或会把素材传到云端，需要用户确认
COSTLY = re.compile(r"generat|gen_|video_gen|music|voice|tts|transcri|translat|render|export|upload|import|delete|remove|purchase|credit|publish", re.I)
READ_ONLY = re.compile(r"(^|_)(get|list|read|search|inspect|view|status|verify|describe|preview|find)", re.I)
SAFE_LOCAL_TOOLS = {"Read", "Glob", "Grep", "TodoWrite", "Skill", "ToolSearch"}


def classify(tool_name: str) -> str:
    """返回 'allow' / 'ask' / 'deny'。"""
    if tool_name in SAFE_LOCAL_TOOLS:
        return "allow"
    if not (tool_name.startswith("mcp__") and "chatcut" in tool_name.lower()):
        return "deny"  # 只让它操作 ChatCut，不碰本机 shell / 文件写入 / 其他服务
    action = tool_name.split("__")[-1]
    if COSTLY.search(action):
        return "ask"
    if READ_ONLY.search(action):
        return "allow"
    return "allow" if os.environ.get("CHATCUT_STUDIO_AUTO_EDIT", "1") == "1" else "ask"


def build_prompt(task: str, assets: str, skill_md: str) -> str:
    parts = ["请使用 ChatCut 完成下面的剪辑任务。先读取当前打开的项目和时间线，再动手。"]
    if skill_md:
        parts.append("严格按照下面这份剪辑风格 Skill 执行：\n\n<style_skill>\n" + skill_md.strip() + "\n</style_skill>")
    parts.append("任务：\n" + task.strip())
    if assets.strip():
        parts.append("素材（按顺序）：\n" + assets.strip())
    parts.append("完成后列出你做了哪些改动、还有哪些需要我确认。导出前先问我。")
    return "\n\n".join(parts)


def mcp_options() -> dict[str, Any]:
    if os.environ.get("CHATCUT_MCP_MODE", "plugin") == "http":
        headers = {}
        if tok := os.environ.get("CHATCUT_MCP_TOKEN"):
            headers["Authorization"] = f"Bearer {tok}"
        return {"mcp_servers": {"chatcut": {"type": "http", "url": CHATCUT_MCP_URL, "headers": headers}}}
    return {"setting_sources": ["user"]}


@dataclass
class Run:
    id: str
    prompt: str
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    done: bool = False
    _cond: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def emit(self, ev: dict) -> None:
        async with self._cond:
            self.history.append(ev)
            self._cond.notify_all()

    async def events_from(self, start: int = 0):
        """从第 start 个事件开始依次产出，直到 done；多个页面同时订阅、断线重连都不会丢事件。"""
        i = start
        while True:
            async with self._cond:
                await self._cond.wait_for(lambda: len(self.history) > i)
                batch = self.history[i:]
            for ev in batch:
                i += 1
                yield ev
                if ev["type"] == "done":
                    return

    async def ask(self, tool_name: str, tool_input: dict) -> bool:
        req = uuid.uuid4().hex[:8]
        fut = asyncio.get_running_loop().create_future()
        self.pending[req] = fut
        await self.emit({"type": "approval", "id": req, "tool": tool_name, "input": tool_input})
        try:
            return await fut
        finally:
            self.pending.pop(req, None)

    def answer(self, req: str, allow: bool) -> bool:
        fut = self.pending.get(req)
        if fut and not fut.done():
            fut.set_result(allow)
            return True
        return False


Runner = Callable[[Run], Awaitable[None]]


async def sdk_runner(run: Run) -> None:
    """真实执行：Claude Agent SDK + ChatCut MCP。"""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        PermissionResultAllow,
        PermissionResultDeny,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
    )

    async def can_use_tool(tool_name, tool_input, context):
        decision = classify(tool_name)
        if decision == "ask":
            decision = "allow" if await run.ask(tool_name, tool_input) else "deny"
        await run.emit({"type": "decision", "tool": tool_name, "decision": decision})
        if decision == "allow":
            return PermissionResultAllow()
        return PermissionResultDeny(message="用户或策略拒绝了这个操作，请换一种不需要它的做法，或停下来向用户说明。")

    options = ClaudeAgentOptions(
        model=MODEL,
        effort="high",
        permission_mode="default",
        can_use_tool=can_use_tool,
        disallowed_tools=["Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch"],
        max_turns=int(os.environ.get("CHATCUT_STUDIO_MAX_TURNS", "60")),
        **mcp_options(),
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query(run.prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock) and b.text.strip():
                        await run.emit({"type": "text", "text": b.text})
                    elif isinstance(b, ToolUseBlock):
                        await run.emit({"type": "tool", "tool": b.name, "input": b.input})
            elif isinstance(msg, ResultMessage):
                await run.emit({"type": "result", "is_error": msg.is_error, "cost_usd": msg.total_cost_usd,
                                "turns": msg.num_turns, "text": msg.result or ""})


async def demo_runner(run: Run) -> None:
    """演示模式（CHATCUT_STUDIO_DEMO=1）：不连 ChatCut、不花钱，用来体验界面和确认流程。"""
    steps = [
        ("text", "先读取当前项目和时间线。"),
        ("tool", "mcp__chatcut__get_timeline"),
        ("text", "项目是 16:9，按风格改成 9:16，并把 5 张背景图按歌词段落排到主轨。"),
        ("tool", "mcp__chatcut__update_project_settings"),
        ("tool", "mcp__chatcut__add_clips"),
        ("tool", "mcp__chatcut__add_captions"),
        ("tool", "mcp__chatcut__export_video"),
    ]
    for kind, val in steps:
        await asyncio.sleep(0.6)
        if kind == "text":
            await run.emit({"type": "text", "text": val})
            continue
        await run.emit({"type": "tool", "tool": val, "input": {}})
        decision = classify(val)
        if decision == "ask":
            decision = "allow" if await run.ask(val, {"format": "mp4", "resolution": "1080x1920"}) else "deny"
        await run.emit({"type": "decision", "tool": val, "decision": decision})
    await run.emit({"type": "result", "is_error": False, "cost_usd": 0.0, "turns": len(steps), "text": "演示完成"})


class RunManager:
    def __init__(self, runner: Runner | None = None):
        if runner is None:
            runner = demo_runner if os.environ.get("CHATCUT_STUDIO_DEMO") == "1" else sdk_runner
        self.runner = runner
        self.runs: dict[str, Run] = {}

    def start(self, prompt: str) -> Run:
        run = Run(id=uuid.uuid4().hex[:12], prompt=prompt)
        self.runs[run.id] = run

        async def _go():
            try:
                await self.runner(run)
            except Exception as e:  # 把错误送回界面，而不是让后台任务静默失败
                await run.emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                run.done = True
                for fut in run.pending.values():
                    if not fut.done():
                        fut.set_result(False)
                await run.emit({"type": "done"})

        asyncio.get_running_loop().create_task(_go())
        return run
