"""ChatCut Studio 后端。运行：python3 -m chatcut_studio  然后打开 http://127.0.0.1:8765"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import envcheck, llm, styles
from .agent import RunManager, build_prompt
from .profile import StyleProfile, render_skill_md

STATIC = Path(__file__).parent / "static"


class GenerateReq(BaseModel):
    profile: StyleProfile
    use_ai: bool = True


class LearnReq(BaseModel):
    timeline: str
    notes: str = ""


class SaveReq(BaseModel):
    profile: StyleProfile
    skill_md: str


class RunReq(BaseModel):
    task: str
    assets: str = ""
    style: str | None = None


class ApproveReq(BaseModel):
    id: str
    allow: bool


def create_app(manager: RunManager | None = None) -> FastAPI:
    app = FastAPI(title="ChatCut Studio")
    runs = manager or RunManager()

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    # ---------- ① 环境与 Agent 设置 ----------
    @app.get("/api/env")
    async def env():
        return {"checks": await asyncio.to_thread(envcheck.check), "login": envcheck.LOGIN_COMMANDS}

    @app.post("/api/env/{action}")
    async def env_action(action: str):
        try:
            return {"results": await asyncio.to_thread(envcheck.do_action, action)}
        except KeyError:
            raise HTTPException(404, "未知操作")

    # ---------- ② 风格记录器 ----------
    @app.get("/api/profile/default")
    def default_profile():
        return StyleProfile(name="我的风格", description="").model_dump()

    @app.post("/api/styles/generate")
    async def generate(req: GenerateReq):
        if req.use_ai and llm.has_credentials():
            try:
                return {"skill_md": await asyncio.to_thread(llm.generate_skill_md, req.profile), "source": "claude"}
            except llm.LLMUnavailable as e:
                return {"skill_md": render_skill_md(req.profile), "source": "template", "warning": str(e)}
        warning = "未配置 Claude API 凭据" if req.use_ai else None
        return {"skill_md": render_skill_md(req.profile), "source": "template", "warning": warning}

    @app.post("/api/styles/learn")
    async def learn(req: LearnReq):
        try:
            p = await asyncio.to_thread(llm.learn_profile, req.timeline, req.notes)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except llm.LLMUnavailable as e:
            raise HTTPException(503, str(e))
        return {"profile": p.model_dump()}

    @app.get("/api/styles")
    def list_styles():
        return {"styles": styles.list_styles(), "skills_dir": str(styles.CLAUDE_SKILLS_DIR)}

    @app.post("/api/styles")
    def save_style(req: SaveReq):
        return {"slug": styles.save(req.profile, req.skill_md)}

    @app.get("/api/styles/{slug}")
    def get_style(slug: str):
        try:
            return styles.get(slug)
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, "没有这个风格")

    @app.delete("/api/styles/{slug}")
    def delete_style(slug: str):
        try:
            styles.delete(slug)
        except ValueError:
            raise HTTPException(404, "没有这个风格")
        return {"ok": True}

    @app.post("/api/styles/{slug}/install")
    def install_style(slug: str):
        try:
            return {"path": styles.install(slug)}
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, "没有这个风格")

    # ---------- ③ 一键剪辑 ----------
    def _prompt(req: RunReq) -> str:
        skill_md = ""
        if req.style:
            try:
                skill_md = styles.get(req.style)["skill_md"]
            except (FileNotFoundError, ValueError):
                raise HTTPException(404, "没有这个风格")
        if not req.task.strip():
            raise HTTPException(400, "请填写剪辑任务")
        return build_prompt(req.task, req.assets, skill_md)

    @app.post("/api/prompt")
    def prompt(req: RunReq):
        return {"prompt": _prompt(req)}

    @app.post("/api/run")
    async def start_run(req: RunReq):
        return {"id": runs.start(_prompt(req)).id}

    @app.get("/api/run/{run_id}/events")
    async def run_events(run_id: str):
        run = runs.runs.get(run_id)
        if not run:
            raise HTTPException(404, "没有这次运行")

        async def stream():
            async for ev in run.events_from(0):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/run/{run_id}/approve")
    async def approve(run_id: str, req: ApproveReq):
        run = runs.runs.get(run_id)
        if not run or not run.answer(req.id, req.allow):
            raise HTTPException(404, "没有待确认的操作")
        return {"ok": True}

    return app


app = create_app()
