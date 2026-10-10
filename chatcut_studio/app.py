"""ChatCut Studio 后端。运行：python3 -m chatcut_studio  然后打开 http://127.0.0.1:8765"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from . import chatcut_mcp, envcheck, gateway, jizura, lyrics, optimize, settings, styles
from .agent import RunManager, build_prompt, classify
from .presets import MAX_STYLES
from .profile import StyleProfile, render_skill_md
from .providers import ProviderError, list_models

STATIC = Path(__file__).parent / "static"
BG_DIR = Path(__file__).resolve().parent.parent / "mv_backgrounds" / "vertical_1080x1920"
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}


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


class LyricPreviewReq(BaseModel):
    profile: StyleProfile
    text: str
    background: str = ""


class LyricRenderReq(BaseModel):
    style: str
    lrc: str
    backgrounds: list[str] = []
    audio: str = ""
    overlay: bool = False


def _backgrounds() -> list[str]:
    return sorted(p.name for p in BG_DIR.glob("*") if p.suffix.lower() in IMG_EXT) if BG_DIR.is_dir() else []


def _bg_path(name: str) -> Path:
    if name not in _backgrounds():
        raise HTTPException(400, f"没有这张背景图：{name}")
    return BG_DIR / name


def _render_dir() -> Path:
    d = settings.DATA_ROOT / "renders" / uuid.uuid4().hex[:10]
    d.mkdir(parents=True, exist_ok=True)
    return d


def _url(p: Path) -> str:
    return f"/renders/{p.parent.name}/{p.name}"


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
            s = settings.update(patch).public()
            if "gateway" in patch or "providers" in patch:
                gateway.refresh_if_linked()      # 已接入 Claude Code 时，顺手更新它显示的模型名
            return s
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

    # ---------- 本机模型网关：Claude Code / ChatCut Desktop 用 Studio 里的模型 ----------
    @app.post("/gateway/v1/messages")
    async def gw_messages(req: Request):
        try:
            body = await req.json()
        except ValueError:
            return gateway.error(400, "请求不是 JSON", "invalid_request_error")
        return await gateway.handle(body, req.headers)

    @app.post("/gateway/v1/messages/count_tokens")
    async def gw_count(req: Request):
        if not gateway.authorized(req.headers, settings.load().gateway.token):
            return gateway.error(401, "Token 不对", "authentication_error")
        return gateway.count_tokens(await req.json())

    @app.get("/api/gateway")
    def gw_status():
        return gateway.status()

    @app.post("/api/gateway/{action}")
    async def gw_action(action: str):
        try:
            if action == "link":
                return gateway.link()
            if action == "unlink":
                return gateway.unlink()
            if action == "test":
                return await gateway.self_test()
        except (KeyError, ValueError, OSError) as e:
            raise HTTPException(400, f"操作失败：{e}")
        raise HTTPException(404, "未知操作")

    @app.get("/api/mcp/desktop/discover")
    async def desktop_discover():
        """查找本机 ChatCut Desktop 的 MCP 连接，并逐个试连。"""
        from . import desktop
        found = await desktop.discover()
        for r in found:
            r["endpoint"] = settings.mask_endpoint(r["endpoint"])
        return {"found": found, "searched": [f"{n}：{p}" for n, p, _ in desktop.config_files()]}

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

    # ---------- 歌词字幕（本地渲染，含闪光扫过） ----------
    @app.get("/api/jizura/meta")
    def jizura_meta():
        return {"themes": [{"name": k, "desc": v["desc"]} for k, v in jizura.THEMES.items()],
                "palettes": [{"key": k, "name": v} for k, v in jizura.PALETTES.items() if not k.startswith("hr")],
                "missing_fonts": jizura.ensure_fonts(download=False)}

    @app.get("/api/lyrics/backgrounds")
    def lyric_backgrounds():
        return {"backgrounds": _backgrounds()}

    @app.get("/api/lyrics/backgrounds/{name}")
    def lyric_background_file(name: str):
        return FileResponse(_bg_path(name))

    @app.post("/api/lyrics/preview")
    async def lyric_preview(req: LyricPreviewReq):
        """用当前（可以是未保存的）字幕参数，渲染几句样张：半分辨率小视频 + 扫光前/中/后三帧。"""
        lines = lyrics.sample_lines(req.text)
        if not lines:
            raise HTTPException(400, "请输入至少一句歌词")
        c = req.profile.captions
        bgs = [_bg_path(req.background)] if req.background else []
        d = _render_dir()
        if c.engine == "JIZURA 动态排版":
            lines = lyrics.sample_lines(req.text, seconds_per_line=3.5)  # 动态排版一句会切成几个镜头，给长一点
            try:
                video, info = await jizura.render(lines, c.jizura, d / "preview", bgs, None, 0.5)
                shots = await asyncio.to_thread(lyrics.frames, video, jizura.preview_times(info, 3), d)
            except lyrics.RenderError as e:
                raise HTTPException(500, str(e))
            return {"video": _url(video), "frames": [_url(p) for p in shots], "engine": "jizura",
                    "palette": info.get("palette"), "palette_name": jizura.PALETTES.get(info.get("palette"), ""),
                    "cuts": [{"text": x["text"], "layout": x["layout"], "treat": x["treat"]} for x in info["cuts"]]}
        try:
            video = await asyncio.to_thread(lyrics.render, lines, c, d / "preview", bgs, None, 0.5)
            target = next((ln for ln in lines if c.shine != "无" and lyrics.shine_applies(ln, c)), lines[0])
            shots = await asyncio.to_thread(lyrics.frames, video, lyrics.sweep_times(target, c), d)
        except lyrics.RenderError as e:
            raise HTTPException(500, str(e))
        return {"video": _url(video), "frames": [_url(p) for p in shots],
                "shine_line": target.text if c.shine != "无" and lyrics.shine_applies(target, c) else ""}

    @app.post("/api/lyrics/render")
    async def lyric_render(req: LyricRenderReq):
        """按风格渲染整首歌：整支视频（背景图 + 字幕 + 音频），或透明字幕层（.mov，导入 ChatCut 叠加）。"""
        profile = StyleProfile.model_validate(_style_or_404(styles.get, req.style)["profile"])
        lines = lyrics.parse_lrc(req.lrc)
        if not lines:
            raise HTTPException(400, "没有解析到带时间标签的歌词，格式如 [00:12.50]歌词")
        audio = None
        if req.audio.strip():
            audio = Path(req.audio.strip()).expanduser()
            if not audio.is_file():
                raise HTTPException(400, f"找不到音频文件：{audio}")
        bgs = [_bg_path(n) for n in req.backgrounds]
        d = _render_dir()
        if profile.captions.engine == "JIZURA 动态排版":
            # 透明层模式下背景图不画进去，只用来判断亮度、挑配色
            try:
                out, info = await jizura.render(lines, profile.captions.jizura, d / "lyrics", bgs, audio, 1.0, 30, req.overlay)
            except lyrics.RenderError as e:
                raise HTTPException(500, str(e))
            return {"url": _url(out), "path": str(out), "lines": len(lines), "duration": round(info["duration"], 1),
                    "engine": "jizura", "palette": info.get("palette"), "bpm": info.get("bpm"), "cuts": len(info["cuts"])}
        bgs = [] if req.overlay else bgs
        try:
            out = await asyncio.to_thread(lyrics.render, lines, profile.captions, d / "lyrics", bgs, audio,
                                          1.0, 30, req.overlay)
        except lyrics.RenderError as e:
            raise HTTPException(500, str(e))
        return {"url": _url(out), "path": str(out), "lines": len(lines), "duration": round(lines[-1].end, 1)}

    @app.get("/renders/{rid}/{name}")
    def render_file(rid: str, name: str):
        root = (settings.DATA_ROOT / "renders").resolve()
        f = (root / rid / name).resolve()
        if root not in f.parents or not f.is_file():
            raise HTTPException(404, "没有这个文件")
        return FileResponse(f)

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
