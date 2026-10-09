"""直接连接 ChatCut MCP（不经过 Claude Code），给 DeepSeek 等 API 模型使用。

认证三选一（设置页里选）：
- oauth：标准 MCP OAuth（动态注册 + 浏览器授权），回调地址 http://127.0.0.1:<端口>/oauth/callback。
  ChatCut 是否允许第三方客户端这样登录还没有验证，这是下一步要确认的事。
- token：手动粘贴一个 access token。
- none：不认证（本地测试用的假服务器）。
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager

from mcp import Client
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from .settings import DATA_ROOT, McpConfig

PORT = int(os.environ.get("PORT", "8765"))
REDIRECT_URI = f"http://127.0.0.1:{PORT}/oauth/callback"


class FileTokenStorage:
    """OAuth token 和动态注册得到的 client 信息存在 data/chatcut_oauth.json。"""

    def __init__(self, path=None):
        self.path = path or DATA_ROOT / "chatcut_oauth.json"

    def _read(self) -> dict:
        return json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}

    def _write(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d), "utf-8")
        os.chmod(self.path, 0o600)

    async def get_tokens(self) -> OAuthToken | None:
        t = self._read().get("tokens")
        return OAuthToken.model_validate(t) if t else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._write(self._read() | {"tokens": tokens.model_dump(mode="json")})

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        c = self._read().get("client")
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        self._write(self._read() | {"client": info.model_dump(mode="json")})

    def logged_in(self) -> bool:
        return bool(self._read().get("tokens"))

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class OAuthFlow:
    """把 MCP SDK 的「打开授权页 / 等回调」接到网页上：前端轮询 auth_url 打开它，回调路由交回 code。"""

    def __init__(self):
        self.auth_url: str | None = None
        self.status = "idle"  # idle / connecting / waiting（等用户在浏览器授权）/ ok / error
        self.message = ""
        self.tools: list[dict] = []  # 最近一次连接列出的工具
        self._code: asyncio.Future | None = None

    async def redirect_handler(self, url: str) -> None:
        self.auth_url, self.status = url, "waiting"
        self._code = asyncio.get_running_loop().create_future()

    async def callback_handler(self) -> AuthorizationCodeResult:
        if self._code is None:
            raise RuntimeError("没有进行中的授权")
        try:
            return await asyncio.wait_for(self._code, timeout=300)
        finally:
            self.auth_url = None

    def deliver(self, code: str, state: str | None, iss: str | None = None) -> bool:
        if self._code and not self._code.done():
            self._code.set_result(AuthorizationCodeResult(code=code, state=state, iss=iss))
            return True
        return False


flow = OAuthFlow()


def _auth(cfg: McpConfig, storage: FileTokenStorage):
    if cfg.auth == "oauth":
        return OAuthClientProvider(
            server_url=cfg.url,
            client_metadata=OAuthClientMetadata(
                client_name="ChatCut Studio", redirect_uris=[REDIRECT_URI],
                grant_types=["authorization_code", "refresh_token"], response_types=["code"],
            ),
            storage=storage, redirect_handler=flow.redirect_handler, callback_handler=flow.callback_handler,
        )
    return None


@asynccontextmanager
async def connect(cfg: McpConfig, storage: FileTokenStorage | None = None):
    storage = storage or FileTokenStorage()
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.auth == "token" and cfg.token else None
    http = create_mcp_http_client(headers=headers, auth=_auth(cfg, storage))
    async with http, Client(streamable_http_client(cfg.url, http_client=http)) as client:
        yield client


async def list_tools(client: Client) -> list[dict]:
    tools, cursor = [], None
    while True:
        res = await client.list_tools(cursor=cursor)
        tools += [{"name": t.name, "description": t.description or t.title or t.name,
                   "input_schema": t.input_schema or {"type": "object", "properties": {}}} for t in res.tools]
        cursor = res.next_cursor
        if not cursor:
            return tools


async def call_tool(client: Client, name: str, args: dict, limit: int = 20000) -> tuple[str, bool]:
    """返回（给模型看的文本，是否出错）。结果太长时截断，避免撑爆上下文。"""
    try:
        r = await client.call_tool(name, args)
    except Exception as e:  # 网络或协议错误也交还给模型，让它决定怎么办
        return f"{type(e).__name__}: {e}", True
    parts = []
    for c in r.content:
        parts.append(getattr(c, "text", None) or f"[{getattr(c, 'type', 'content')}]")
    if not parts and r.structured_content is not None:
        parts.append(json.dumps(r.structured_content, ensure_ascii=False))
    out = "\n".join(parts) or "(无输出)"
    if len(out) > limit:
        out = out[:limit] + f"\n…（已截断，原长 {len(out)} 字）"
    return out, bool(r.is_error)
