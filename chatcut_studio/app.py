"""ChatCut Studio 后端。运行：python3 -m chatcut_studio  然后打开 http://127.0.0.1:8765"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from . import chatcut_mcp, envcheck, optimize, settings, styles
from .agent import RunManager, build_prompt, classify
from .presets import MAX_STYLES
from .profile import StyleProfile, render_skill_md
from .providers import ProviderError, list_models

STATIC = Path(__file__).parent / "static"


class UpdateStyleReq(BaseModel):
    profile: StyleProfile
    note: str = "手动修改"


class OptimizeReq(BaseModel):
    feedback: str
    context: str = ""


class RevertReq(BaseModel):
    version: int


class RunReq(BaseModel):
    task: str
    assets: str = ""
    style: str | None = None


class ApproveReq(BaseModel):
    id: str
    allow: bool


def _style_or_404(fn, *args):
    try:
        return fn(*args)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "没有这个风格")
    except styles.LimitReached as e:
        raise HTTPException(409, str(e))


def create_app(manager: RunManager | None = None) -> FastAPI:
    app = FastAPI(title="ChatCut Studio")
    runs = manager or RunManager()
    login_task: dict[str, asyncio.Task | None] = {"t": None}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    # ---------- ① 连接设置 ----------
    @app.get("/api/settings")
    def get_settings():
        return settings.load().public()

    @app.put("/api/settings")
    def put_settings(patch: dict):
        try:
            return settings.update(patch).public()
        except (KeyError, ValueError) as e:
            raise HTTPException(400, f"设置不合法：{e}")

    @app.post("/api/providers/{pid}/models")
    async def provider_models(pid: str):
        try:
            p = settings.load().provider(pid)
            return {"models": await list_models(p)}
        except KeyError:
            raise HTTPException(404, "没有这个服务商")
        except Exception as e:  # 网络错误、Key 错误都直接告诉用户
            raise HTTPException(502, f"获取失败：{e}")

    @app.get("/api/env")
    async def env():
        return {"checks": await asyncio.to_thread(envcheck.check), "login": envcheck.LOGIN_COMMANDS}

    @app.post("/api/env/{action}")
    async def env_action(action: str):
        try:
            return {"results": await asyncio.to_thread(envcheck.do_action, action)}
        except KeyError:
            raise HTTPException(404, "未知操作")

    # ChatCut MCP 直连（API 模型方式）
    @app.get("/api/mcp/status")
    def mcp_status():
        f = chatcut_mcp.flow
        return {"logged_in": chatcut_mcp.FileTokenStorage().logged_in(), "status": f.status,
                "auth_url": f.auth_url, "message": f.message, "redirect_uri": chatcut_mcp.REDIRECT_URI}

    async def _tools() -> list[dict]:
        async with chatcut_mcp.connect(settings.load().mcp) as client:
            return await chatcut_mcp.list_tools(client)

    @app.post("/api/mcp/connect")
    async def mcp_connect():
        """连接并列出工具。OAuth 方式下如果还没登录，会进入浏览器授权流程（前端轮询 status 拿 auth_url）。"""
        f = chatcut_mcp.flow
        if login_task["t"] and not login_task["t"].done():
            return {"started": False}

        async def go():
            f.status, f.message = "connecting", "连接中…"
            try:
                tools = await asyncio.wait_for(_tools(), timeout=360)
                f.tools, f.status, f.message = tools, "ok", f"已连接，共 {len(tools)} 个工具"
            except Exception as e:
                f.status, f.message = "error", f"连接失败：{type(e).__name__}: {e}"

        login_task["t"] = asyncio.get_running_loop().create_task(go())
        return {"started": True}

    @app.get("/api/mcp/tools")
    def mcp_tools():
        tools = chatcut_mcp.flow.tools
        return {"tools": [{"name": t["name"], "description": t["description"],
                           "policy": classify(f"mcp__chatcut__{t['name']}"), "input_schema": t["input_schema"]} for t in tools]}

    @app.post("/api/mcp/logout")
    def mcp_logout():
        chatcut_mcp.FileTokenStorage().clear()
        chatcut_mcp.flow.status, chatcut_mcp.flow.message = "idle", "已退出"
        return {"ok": True}

    @app.get("/oauth/callback")
    def oauth_callback(request: Request):
        q = request.query_params
        if q.get("error"):
            chatcut_mcp.flow.status, chatcut_mcp.flow.message = "error", f"授权被拒绝：{q.get('error')}"
            return HTMLResponse("<p>授权没有完成，可以关闭此页。</p>")
        ok = chatcut_mcp.flow.deliver(q.get("code", ""), q.get("state"), q.get("iss"))
        return HTMLResponse("<p>授权完成，可以关闭此页，回到 ChatCut Studio。</p>" if ok else "<p>没有进行中的授权。</p>")

    # ---------- ② 常用风格 ----------
    @app.get("/api/styles")
    def list_styles():
        return {"styles": styles.list_styles(), "missing_presets": styles.missing_presets(),
                "max": MAX_STYLES, "skills_dir": str(styles.CLAUDE_SKILLS_DIR)}

    @app.get("/api/styles/{slug}")
    def get_style(slug: str):
        return _style_or_404(styles.get, slug)

    @app.put("/api/styles/{slug}")
    def update_style(slug: str, req: UpdateStyleReq):
        return _style_or_404(styles.update, slug, req.profile, req.note)

    @app.post("/api/styles")
    def create_style(req: UpdateStyleReq):
        return {"slug": _style_or_404(styles.create, req.profile, req.note)}

    @app.delete("/api/styles/{slug}")
    def delete_style(slug: str):
        _style_or_404(styles.delete, slug)
        return {"ok": True}

    @app.post("/api/styles/{slug}/restore")
    def restore_style(slug: str):
        return _style_or_404(styles.restore_preset, slug)

    @app.post("/api/styles/{slug}/revert")
    def revert_style(slug: str, req: RevertReq):
        return _style_or_404(styles.revert, slug, req.version)

    @app.post("/api/styles/{slug}/install")
    def install_style(slug: str):
        return {"path": _style_or_404(styles.install, slug)}

    @app.post("/api/styles/{slug}/optimize")
    async def optimize_style(slug: str, req: OptimizeReq):
        if not req.feedback.strip():
            raise HTTPException(400, "请先写下哪里需要改")
        cur = StyleProfile.model_validate(_style_or_404(styles.get, slug)["profile"])
        try:
            proposal, model = await optimize.propose(cur, req.feedback, req.context)
        except (ProviderError, KeyError) as e:
            raise HTTPException(503, str(e))
        except Exception as e:
            raise HTTPException(502, f"模型调用失败：{type(e).__name__}: {e}")
        return {"profile": proposal.profile.model_dump(), "changes": proposal.changes, "model": model,
                "skill_md": render_skill_md(proposal.profile, slug)}

    @app.post("/api/preview")
    def preview(req: UpdateStyleReq):
        return {"skill_md": render_skill_md(req.profile)}

    # ---------- ③ 一键剪辑 ----------
    def _prompt(req: RunReq) -> str:
        skill_md = _style_or_404(styles.get, req.style)["skill_md"] if req.style else ""
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
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/run/{run_id}/approve")
    async def approve(run_id: str, req: ApproveReq):
        run = runs.runs.get(run_id)
        if not run or not run.answer(req.id, req.allow):
            raise HTTPException(404, "没有待确认的操作")
        return {"ok": True}

    return app


app = create_app()
