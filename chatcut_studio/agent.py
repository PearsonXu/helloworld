"""剪辑 Agent：驱动 ChatCut MCP 执行剪辑，花积分 / 删除 / 上传 / 导出前在界面上弹确认。

三种执行器（设置页里选）：
- claude_code：Claude Agent SDK，复用本机 Claude Code 里已安装、已登录的 ChatCut 插件。
- api：任意 API 模型（Claude API、DeepSeek、通义千问、Kimi、GLM……）+ 本程序直连 ChatCut MCP。
- demo：演示模式，不连 ChatCut、不花钱（CHATCUT_STUDIO_DEMO=1）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

MODEL = os.environ.get("CHATCUT_STUDIO_MODEL", "claude-opus-5-5")
MAX_TURNS = int(os.environ.get("CHATCUT_STUDIO_MAX_TURNS", "60"))

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


DESKTOP_SERVER = "chatcut_desktop"   # Claude Code 方式下本机 Desktop 端点的名字 → 工具名 mcp__chatcut_desktop__*

SYSTEM_PROMPT = """你是 ChatCut 视频剪辑助手，只能通过提供的 ChatCut 工具操作用户的剪辑项目。
- 先读取项目和时间线，弄清现状再动手；每完成一个大步骤，用读取/校验类工具确认改动生效。
- 严格遵守用户给出的风格 Skill；Skill 没写到的保持 ChatCut 默认。
- 会花积分、上传素材、删除内容或导出的操作，系统会先征求用户同意；被拒绝时换一种做法或停下来说明。
- 用中文简短汇报进度，最后列出改动摘要和需要用户确认的事项。"""


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

    # 连本机 ChatCut Desktop 时：把找到的本机端点直接交给 Claude Code，并说明只用它（不用云端插件）
    from . import desktop, settings
    extra = {}
    s = settings.load()
    if s.mcp.target == "desktop":
        e = s.mcp.desktop or await desktop.first_working()
        if not e:
            raise RuntimeError("没有找到 ChatCut Desktop 的本机连接：请先打开 ChatCut Desktop 和一个项目，再在「连接设置」里点「自动查找」")
        await run.emit({"type": "text", "text": f"连接本机 ChatCut Desktop：{e.label()}"})
        extra = {"mcp_servers": {DESKTOP_SERVER: e.sdk_config()},
                 "system_prompt": {"type": "preset", "preset": "claude_code",
                                   "append": f"只用 mcp__{DESKTOP_SERVER}__ 开头的工具操作 ChatCut Desktop 里当前打开的项目，"
                                             "不要用其他 ChatCut 插件或云端项目。"}}

    options = ClaudeAgentOptions(
        **extra,
        model=MODEL,
        effort="high",
        permission_mode="default",
        can_use_tool=can_use_tool,
        disallowed_tools=["Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch"],
        max_turns=MAX_TURNS,
        setting_sources=["user"],
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


async def api_runner(run: Run) -> None:
    """API 模型 + 直连 ChatCut MCP 的工具调用循环。"""
    from . import chatcut_mcp, settings
    from .providers import make_adapter

    s = settings.load()
    p = s.provider(s.agent_provider)
    adapter = make_adapter(p)
    await run.emit({"type": "text", "text": f"使用 {p.label}（{p.model}），正在连接 ChatCut…"})
    async with chatcut_mcp.connect(s.mcp) as client:
        tools = await chatcut_mcp.list_tools(client)
        await run.emit({"type": "text", "text": f"已连接，ChatCut 提供 {len(tools)} 个工具。"})
        messages = adapter.start(SYSTEM_PROMPT, run.prompt)
        for turn in range(1, MAX_TURNS + 1):
            step = await adapter.step(messages, tools)
            if step.text.strip():
                await run.emit({"type": "text", "text": step.text})
            if not step.calls:
                await run.emit({"type": "result", "is_error": False, "cost_usd": None, "turns": turn, "text": step.text})
                return
            results = []
            for call in step.calls:
                await run.emit({"type": "tool", "tool": call.name, "input": call.args})
                if call.bad_args:
                    results.append((call, f"参数不是合法 JSON：{call.bad_args[:200]}", True))
                    continue
                decision = classify(f"mcp__chatcut__{call.name}")
                if decision == "ask":
                    decision = "allow" if await run.ask(call.name, call.args) else "deny"
                await run.emit({"type": "decision", "tool": call.name, "decision": decision})
                if decision != "allow":
                    results.append((call, "用户拒绝了这个操作。请换一种不需要它的做法，或停下来向用户说明。", True))
                    continue
                results.append((call, *await chatcut_mcp.call_tool(client, call.name, call.args)))
            adapter.add_results(messages, results)
        await run.emit({"type": "result", "is_error": True, "cost_usd": None, "turns": MAX_TURNS,
                        "text": f"达到最大轮数 {MAX_TURNS}，已停止"})


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


def pick_runner() -> Runner:
    if os.environ.get("CHATCUT_STUDIO_DEMO") == "1":
        return demo_runner
    from . import settings

    return api_runner if settings.load().agent_mode == "api" else sdk_runner


MAX_RESUMES = 3      # 同一个任务最多自动续跑几次（防止任务本身每次都把 Studio 弄崩、无限重来）
RESUME_NOTE = ("【续跑】上次执行这个任务时 ChatCut Studio 被意外结束，任务没有做完。"
               "先读取当前打开的项目和时间线，弄清楚哪些步骤已经做过：做过的不要重复（尤其不要重复生成、导出、上传，"
               "那会重复花积分），只把没完成的部分做完；如果检查后发现任务其实已经全部完成，直接说明即可，不要做任何改动。"
               "\n\n原任务如下：\n\n")


class RunManager:
    """运行中的任务同时记在 <数据目录>/runs/<id>.json：正常结束（完成或出错）就删掉；
    Studio 被意外结束时记录留着，下次启动 resume() 自动续跑。"""

    def __init__(self, runner: Runner | None = None, store: Path | None = None):
        self.runner = runner  # None = 每次运行时按当前设置选择
        self.runs: dict[str, Run] = {}
        self._store = store

    @property
    def store(self) -> Path:
        if self._store:
            return self._store
        from . import settings

        return settings.DATA_ROOT / "runs"

    def _save(self, rec: dict) -> None:
        self.store.mkdir(parents=True, exist_ok=True)
        (self.store / f"{rec['id']}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")

    def start(self, prompt: str, *, run_id: str | None = None, task_prompt: str | None = None, resumes: int = 0) -> Run:
        runner = self.runner or pick_runner()
        run = Run(id=run_id or uuid.uuid4().hex[:12], prompt=prompt)
        self.runs[run.id] = run
        self._save({"id": run.id, "prompt": task_prompt or prompt, "resumes": resumes})

        async def _go():
            finished = False
            try:
                await runner(run)
                finished = True
            except Exception as e:  # 把错误送回界面，而不是让后台任务静默失败
                finished = True
                await run.emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                # 跑完 / 出错 = 结束了，删记录；被取消（Studio 关闭）= 没做完，记录留着下次续跑
                if finished:
                    (self.store / f"{run.id}.json").unlink(missing_ok=True)
                run.done = True
                for fut in run.pending.values():
                    if not fut.done():
                        fut.set_result(False)
                await run.emit({"type": "done"})

        asyncio.get_running_loop().create_task(_go())
        return run

    def resume(self) -> list[Run]:
        """Studio 启动时调用：把上次没做完的任务接着做；已经完成的任务没有记录，不会再执行。"""
        if not self.store.is_dir():
            return []
        out = []
        for f in sorted(self.store.glob("*.json"), key=lambda x: x.stat().st_mtime):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                run_id, prompt, n = rec["id"], rec["prompt"], int(rec.get("resumes", 0))
            except (OSError, ValueError, KeyError):
                f.unlink(missing_ok=True)
                continue
            if run_id in self.runs:
                continue
            if n >= MAX_RESUMES:
                f.unlink(missing_ok=True)
                continue
            run = self.start(RESUME_NOTE + prompt, run_id=run_id, task_prompt=prompt, resumes=n + 1)
            run.history.insert(0, {"type": "text", "text": f"（Studio 重启了，这个任务上次没做完，正在自动续跑：第 {n + 1} 次）"})
            out.append(run)
        return out

    def active(self) -> Run | None:
        """最近一个还没结束的任务（页面刷新 / Studio 重启后让界面重新接上）。"""
        live = [r for r in self.runs.values() if not r.done]
        return live[-1] if live else None
