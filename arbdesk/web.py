"""Local web panel. Binds to 127.0.0.1 and requires a random access token."""
from __future__ import annotations

import ipaddress
import os
import secrets
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .config import Settings
from .executor import Executor
from .service import Desk
from .storage import Store

STATIC = Path(__file__).parent / "static"
COOKIE = "arbdesk_token"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def load_or_create_token(data_dir: Path) -> str:
    path = data_dir / "panel_token"
    if path.exists():
        return path.read_text().strip()
    token = secrets.token_urlsafe(24)
    path.write_text(token)
    if os.name == "posix":
        os.chmod(path, 0o600)
    return token


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_app(settings: Settings, store: Store, desk: Desk, executor: Executor, token: str) -> FastAPI:
    app = FastAPI(title="ArbDesk", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Reject requests addressed to a non-local Host header (DNS-rebinding defence).
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in LOCAL_HOSTS:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'"
        return resp

    def auth(request: Request) -> None:
        if not secrets.compare_digest(request.cookies.get(COOKIE, ""), token):
            raise HTTPException(401, "open the URL printed in the terminal to log in")

    def json_only(request: Request) -> None:
        # Cross-site HTML forms cannot send application/json without a CORS preflight.
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "application/json required")

    @app.get("/")
    def index(request: Request):
        given = request.query_params.get("token")
        if given:
            if not secrets.compare_digest(given, token):
                raise HTTPException(401, "bad token")
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=30 * 86400)
            return resp
        return FileResponse(STATIC / "index.html")

    @app.get("/app.js")
    def js():
        return FileResponse(STATIC / "app.js", media_type="application/javascript")

    @app.get("/api/state", dependencies=[Depends(auth)])
    def state():
        return {
            "mode": settings.risk.mode,
            "risk": settings.risk.__dict__,
            "scan_times": settings.scan.times,
            "exchanges": settings.scan.exchanges,
            "keyed_exchanges": sorted(settings.secrets.exchange_keys),  # names only
            "llm": {"configured": desk.analyst.available, "provider": desk.analyst.provider,
                    "model": desk.analyst.model, "calls_today": store.llm_calls_today(),
                    "max_calls_per_day": settings.llm.max_calls_per_day},
            "today": store.today_stats(),
            "running": desk.running,
            "polymarket_mode": settings.polymarket.mode,
            "halted": store.halted(),
            "secrets_source": settings.secrets.source,
        }

    @app.get("/api/scans", dependencies=[Depends(auth)])
    def scans(limit: int = 10):
        return store.scans(min(limit, 50), source="crypto")

    @app.get("/api/polymarket", dependencies=[Depends(auth)])
    def polymarket():
        latest = store.scans(1, source="polymarket")
        return {"enabled": settings.polymarket.enabled, "scan": latest[0] if latest else None,
                "forecasts": store.forecasts(50), "scores": store.forecast_scores()}

    @app.get("/api/analyses", dependencies=[Depends(auth)])
    def analyses(limit: int = 10):
        return store.analyses(min(limit, 50))

    @app.get("/api/proposals", dependencies=[Depends(auth)])
    def proposals():
        return store.proposals()

    @app.get("/api/trades", dependencies=[Depends(auth)])
    def trades():
        return store.trades()

    @app.post("/api/run", dependencies=[Depends(auth), Depends(json_only)])
    async def run(request: Request):
        body = await request.json()
        if desk.running:
            return {"started": False, "detail": "already running"}
        threading.Thread(target=desk.run_cycle, kwargs={"use_llm": bool(body.get("use_llm", True))},
                         daemon=True).start()
        return {"started": True}

    @app.post("/api/proposals/{pid}/approve", dependencies=[Depends(auth), Depends(json_only)])
    async def approve(pid: int, request: Request):
        body = await request.json()
        # Real money requires the user to have typed LIVE in the confirmation dialog.
        confirm = isinstance(body, dict) and body.get("confirm_live") == "LIVE"
        try:
            return await run_in_threadpool(executor.execute, pid, confirm)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/halt", dependencies=[Depends(auth), Depends(json_only)])
    def halt():
        executor.halt("halted from panel")
        return {"ok": True}

    @app.post("/api/resume", dependencies=[Depends(auth), Depends(json_only)])
    def resume():
        executor.resume()
        return {"ok": True}

    @app.post("/api/proposals/{pid}/reject", dependencies=[Depends(auth), Depends(json_only)])
    def reject(pid: int):
        p = store.proposal(pid)
        if not p or p["status"] != "pending":
            raise HTTPException(400, "not pending")
        store.set_proposal_status(pid, "rejected", "rejected by user")
        return {"ok": True}

    return app
