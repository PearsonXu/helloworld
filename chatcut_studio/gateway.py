"""本机模型网关：把 Studio 里配置的模型包装成 Anthropic Messages 接口，给 Claude Code 用。

ChatCut Desktop 的 AI 输入框选「Claude Code」时，Desktop 会启动本机的 Claude Code。
把 Claude Code 指向这里（`ANTHROPIC_BASE_URL=http://127.0.0.1:8765/gateway`）之后，
Desktop 里的 Agent 用的就是 Studio 当前选的模型（DeepSeek、通义千问、Kimi、GLM、Claude API、任意 OpenAI 兼容服务）；
在 Studio 里换模型、换 Key，下一次请求立即生效，不用再改 Claude Code。

两种转发方式：
- 服务商有 Anthropic 兼容接口（Claude API、DeepSeek、Kimi、GLM、通义千问）：原样转发，只换模型名和 Key，支持流式；
- 只有 OpenAI 兼容接口：把请求翻译成 /chat/completions（含工具调用），回来再翻译成 Anthropic 格式，
  Claude Code 要流式时按 Anthropic 的事件格式分段发回。
网关只监听本机，并且要带 Studio 生成的本机 Token 才能用，别的程序拿不到你的 API Key。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import uuid
from pathlib import Path

import httpx
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import settings
from .settings import Provider

TIMEOUT = httpx.Timeout(600, connect=20)
SIGNATURE = "chatcut-studio"   # 翻译出来的 thinking 块没有真正的签名，占个位
STOP = {"stop": "end_turn", "tool_calls": "tool_use", "function_call": "tool_use", "length": "max_tokens",
        "content_filter": "refusal"}


def error(status: int, message: str, kind: str = "api_error") -> JSONResponse:
    return JSONResponse({"type": "error", "error": {"type": kind, "message": message}}, status_code=status)


def authorized(headers, token: str) -> bool:
    key = headers.get("x-api-key") or ""
    auth = headers.get("authorization") or ""
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    return any(v and secrets.compare_digest(v, token) for v in (key, bearer))


def pick_model(g: settings.GatewayConfig, p: Provider, requested: str) -> str:
    """Claude Code 的后台小任务（标题、摘要等）请求 haiku 档位：有「快速模型」就用它。"""
    main = g.model or p.model
    small = (requested or "").lower()
    if g.small_model and ("haiku" in small or requested == g.small_model):
        return g.small_model
    return main


def route(s: settings.Settings) -> tuple[Provider, str]:
    """返回（服务商, 转发方式 anthropic / openai）。"""
    g = s.gateway
    p = s.provider(g.provider)
    url = p.anthropic_endpoint()
    if g.mode == "anthropic" or (g.mode == "auto" and url):
        if not url:
            raise ValueError(f"{p.label} 没有 Anthropic 兼容接口，请把转发方式改成「OpenAI 兼容」")
        return p, "anthropic"
    if not p.base_url:
        raise ValueError(f"{p.label} 还没有填写 base_url")
    return p, "openai"


async def handle(body: dict, headers) -> Response:
    """/gateway/v1/messages"""
    s = settings.load()
    if not authorized(headers, s.gateway.token):
        return error(401, "ChatCut Studio 网关：Token 不对。请在 Studio「连接设置」里重新点「接入 Claude Code」。", "authentication_error")
    try:
        p, how = route(s)
    except (KeyError, ValueError) as e:
        return error(400, f"ChatCut Studio 网关：{e}", "invalid_request_error")
    if not p.key():
        return error(400, f"ChatCut Studio 网关：还没有填写 {p.label} 的 API Key", "authentication_error")
    model = pick_model(s.gateway, p, body.get("model", ""))
    if not model:
        return error(400, f"ChatCut Studio 网关：{p.label} 还没有选择模型", "invalid_request_error")
    if how == "anthropic":
        return await passthrough(body, headers, p, model)
    return await via_openai(body, p, model)


# ---------------------------------------------------------------- 原样转发（Anthropic 兼容接口）
async def passthrough(body: dict, headers, p: Provider, model: str) -> Response:
    body = {**body, "model": model}
    key = p.key()
    h = {"content-type": "application/json", "anthropic-version": headers.get("anthropic-version") or "2023-06-01"}
    if p.kind == "anthropic":
        h["x-api-key"] = key
        if beta := headers.get("anthropic-beta"):
            h["anthropic-beta"] = beta
    else:   # 第三方兼容接口：两种认证头都带；metadata.user_id 只留字母数字（DeepSeek 会拒绝别的字符）
        h["x-api-key"], h["authorization"] = key, f"Bearer {key}"
        if isinstance(body.get("metadata"), dict) and "user_id" in body["metadata"]:
            body["metadata"] = {"user_id": re.sub(r"[^A-Za-z0-9_-]", "_", str(body["metadata"]["user_id"]))[:64]}
    target = p.anthropic_endpoint().rstrip("/") + "/v1/messages"
    client = httpx.AsyncClient(timeout=TIMEOUT)
    try:
        r = await client.send(client.build_request("POST", target, headers=h, json=body), stream=True)
    except httpx.HTTPError as e:
        await client.aclose()
        return error(502, f"ChatCut Studio 网关：连不上 {p.label}（{target}）：{e}")
    if r.status_code >= 400 or not body.get("stream"):
        data = await r.aread()
        await r.aclose(); await client.aclose()
        return Response(data, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))

    async def relay():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose(); await client.aclose()
    return StreamingResponse(relay(), media_type="text/event-stream")


# ---------------------------------------------------------------- 翻译（OpenAI 兼容接口）
def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if b.get("type") == "text":
            out.append(b.get("text", ""))
        elif b.get("type") == "image":
            out.append("[图片]")
    return "\n".join(out)


def to_openai(body: dict, model: str) -> dict:
    msgs: list[dict] = []
    if system := body.get("system"):
        msgs.append({"role": "system", "content": _text_of(system)})
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            msgs.append({"role": m["role"], "content": c})
            continue
        if m["role"] == "assistant":
            text = "".join(b.get("text", "") for b in c if b.get("type") == "text")
            reasoning = "".join(b.get("thinking", "") for b in c if b.get("type") == "thinking")
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
                     for b in c if b.get("type") == "tool_use"]
            msg = {"role": "assistant", "content": text or None}
            if reasoning:     # DeepSeek 思考模式：工具调用的多轮里要把思考内容带回去
                msg["reasoning_content"] = reasoning
            if calls:
                msg["tool_calls"] = calls
            msgs.append(msg)
            continue
        parts = []
        for b in c:   # 工具结果要紧跟在 assistant 的 tool_calls 后面，先发；其余内容合成一条 user 消息
            t = b.get("type")
            if t == "tool_result":
                msgs.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                             "content": ("错误：" if b.get("is_error") else "") + _text_of(b.get("content"))})
            elif t == "text":
                parts.append({"type": "text", "text": b.get("text", "")})
            elif t == "image" and (b.get("source") or {}).get("type") == "base64":
                src = b["source"]
                parts.append({"type": "image_url", "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
        if parts:
            only_text = all(x["type"] == "text" for x in parts)
            msgs.append({"role": "user", "content": "\n".join(x["text"] for x in parts) if only_text else parts})
    out: dict = {"model": model, "messages": msgs, "max_tokens": min(int(body.get("max_tokens") or 4096), 8192)}
    for a, b in (("temperature", "temperature"), ("top_p", "top_p"), ("stop_sequences", "stop")):
        if body.get(a) is not None:
            out[b] = body[a]
    tools = [t for t in body.get("tools") or [] if t.get("type") in (None, "custom") and t.get("input_schema")]
    if tools:
        out["tools"] = [{"type": "function", "function": {"name": t["name"], "description": (t.get("description") or "")[:4000],
                                                          "parameters": t["input_schema"]}} for t in tools]
        tc = body.get("tool_choice") or {}
        if tc.get("type") == "any":
            out["tool_choice"] = "required"
        elif tc.get("type") == "tool":
            out["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        elif tc.get("type") == "none":
            out["tool_choice"] = "none"
    return out


def from_openai(data: dict, model: str) -> dict:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = []
    if msg.get("reasoning_content"):   # DeepSeek 的思考内容 → Anthropic thinking 块，Claude Code 下一轮会原样带回
        content.append({"type": "thinking", "thinking": msg["reasoning_content"], "signature": SIGNATURE})
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_raw": fn.get("arguments")}
        content.append({"type": "tool_use", "id": tc.get("id") or "toolu_" + uuid.uuid4().hex[:24],
                        "name": fn.get("name", ""), "input": args if isinstance(args, dict) else {"value": args}})
    stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else STOP.get(choice.get("finish_reason"), "end_turn")
    usage = data.get("usage") or {}
    return {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant", "model": model,
            "content": content if any(b["type"] != "thinking" for b in content) else content + [{"type": "text", "text": ""}], "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)}}


async def _openai(p: Provider, req: dict) -> tuple[int, dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(p.base_url.rstrip("/") + "/chat/completions", headers={"Authorization": f"Bearer {p.key()}"}, json=req)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"error": {"message": r.text[:500]}}


def _sse(name: str, data: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def sse_events(msg: dict) -> list[bytes]:
    """把一条完整的 Anthropic 消息拆成流式事件（message_start 已单独发过）。"""
    out = []
    for i, b in enumerate(msg["content"]):
        if b["type"] == "thinking":
            out.append(_sse("content_block_start", {"type": "content_block_start", "index": i,
                                                    "content_block": {"type": "thinking", "thinking": "", "signature": ""}}))
            out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": b["thinking"]}}))
            out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": b["signature"]}}))
        elif b["type"] == "text":
            out.append(_sse("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}))
            out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b["text"]}}))
        else:
            out.append(_sse("content_block_start", {"type": "content_block_start", "index": i,
                                                    "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}}))
            out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"], ensure_ascii=False)}}))
        out.append(_sse("content_block_stop", {"type": "content_block_stop", "index": i}))
    out.append(_sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None},
                                      "usage": {"output_tokens": msg["usage"]["output_tokens"]}}))
    out.append(_sse("message_stop", {"type": "message_stop"}))
    return out


async def via_openai(body: dict, p: Provider, model: str) -> Response:
    req = to_openai(body, model)
    if not body.get("stream"):
        try:
            status, data = await _openai(p, req)
        except httpx.HTTPError as e:
            return error(502, f"ChatCut Studio 网关：连不上 {p.label}：{e}")
        if status >= 400:
            return error(status, f"{p.label}：{(data.get('error') or {}).get('message') or data}")
        return JSONResponse(from_openai(data, model))

    async def stream():
        head = {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant", "model": model, "content": [],
                "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}
        yield _sse("message_start", {"type": "message_start", "message": head})
        task = asyncio.ensure_future(_openai(p, req))
        while not task.done():                       # 上游不是流式：等待期间发 ping，连接不会被当成卡死
            await asyncio.wait({task}, timeout=10)
            if not task.done():
                yield _sse("ping", {"type": "ping"})
        try:
            status, data = task.result()
        except httpx.HTTPError as e:
            status, data = 502, {"error": {"message": f"连不上 {p.label}：{e}"}}
        if status >= 400:
            yield _sse("error", {"type": "error", "error": {"type": "api_error",
                                                           "message": f"{p.label}：{(data.get('error') or {}).get('message') or data}"}})
            return
        for ev in sse_events(from_openai(data, model)):
            yield ev
    return StreamingResponse(stream(), media_type="text/event-stream")


def count_tokens(body: dict) -> dict:
    """粗略估算（第三方接口大多没有 count_tokens）：中文约 1 字 1 token，英文约 4 字符 1 token。"""
    text = json.dumps({k: body.get(k) for k in ("system", "messages", "tools")}, ensure_ascii=False)
    han = len(re.findall(r"[㐀-鿿]", text))
    return {"input_tokens": han + (len(text) - han) // 4}


# ---------------------------------------------------------------- 接入 / 断开 Claude Code
ENV_KEYS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS")


def gateway_url() -> str:
    return f"http://127.0.0.1:{os.environ.get('PORT', '8765')}/gateway"


def claude_settings_path() -> Path:
    from .desktop import _home
    return _home() / ".claude" / "settings.json"


def _read_claude() -> dict:
    f = claude_settings_path()
    if not f.is_file():
        return {}
    text = f.read_text("utf-8").strip()
    return json.loads(text) if text else {}


def _write_claude(data: dict) -> None:
    f = claude_settings_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(tmp, f)


def claude_env(s: settings.Settings) -> dict[str, str]:
    g, p = s.gateway, s.provider(s.gateway.provider)
    model = g.model or p.model or "studio"
    return {"ANTHROPIC_BASE_URL": gateway_url(), "ANTHROPIC_AUTH_TOKEN": g.token,
            # 模型名只用于 Claude Code 里显示；真正用哪个模型由网关按 Studio 当前设置决定
            "ANTHROPIC_MODEL": model, "ANTHROPIC_DEFAULT_OPUS_MODEL": model, "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": g.small_model or model,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            # Claude Code 不认识第三方模型名时按 20 万 token 估上下文；按 12.8 万估，宁可早点压缩，不超出模型上限
            **({} if p.kind == "anthropic" else {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(g.context_tokens)})}


def linked(s: settings.Settings | None = None) -> bool:
    s = s or settings.load()
    try:
        env = _read_claude().get("env") or {}
    except (OSError, ValueError):
        return False
    return env.get("ANTHROPIC_BASE_URL") == gateway_url() and env.get("ANTHROPIC_AUTH_TOKEN") == s.gateway.token


def link() -> dict:
    """把 Claude Code 指向本机网关。第一次接入时备份原来的这几项设置，断开时恢复。"""
    s = settings.load()
    s.provider(s.gateway.provider)                     # 服务商不存在时直接报错
    data = _read_claude()
    env = data.setdefault("env", {})
    if not linked(s):
        settings.set_gateway_backup({k: env.get(k) for k in ENV_KEYS})
    env.update(claude_env(s))
    _write_claude(data)
    return status()


def unlink() -> dict:
    s = settings.load()
    data = _read_claude()
    env = data.setdefault("env", {})
    for k in ENV_KEYS:
        old = s.gateway.backup.get(k)
        if old is None:
            env.pop(k, None)
        else:
            env[k] = old
    if not env:
        data.pop("env")
    _write_claude(data)
    settings.set_gateway_backup({})
    return status()


def refresh_if_linked() -> None:
    """在 Studio 里换了模型：已接入时顺手更新 Claude Code 里显示的模型名。"""
    if linked():
        link()


def status() -> dict:
    s = settings.load()
    g = s.gateway
    try:
        p, how = route(s)
        problem = "" if p.key() else f"还没有填写 {p.label} 的 API Key"
        model = g.model or p.model
        if not model:
            problem = problem or f"{p.label} 还没有选择模型"
    except (KeyError, ValueError) as e:
        p, how, model, problem = None, "", "", str(e)
    return {"url": gateway_url(), "linked": linked(s), "claude_settings": str(claude_settings_path()),
            "provider": g.provider, "provider_label": p.label if p else "", "how": how, "model": model,
            "small_model": g.small_model, "mode": g.mode, "problem": problem}


async def self_test() -> dict:
    """在 Studio 里试一次：走和 Claude Code 完全一样的网关路径，发一句话。"""
    s = settings.load()
    body = {"model": "claude-sonnet", "max_tokens": 200, "stream": False,
            "messages": [{"role": "user", "content": "请只回复：连接成功"}]}
    r = await handle(body, {"x-api-key": s.gateway.token})
    data = json.loads(r.body) if hasattr(r, "body") else {}
    if r.status_code >= 400 or data.get("type") == "error":
        msg = (data.get("error") or {}).get("message") if isinstance(data, dict) else ""
        return {"ok": False, "message": msg or f"HTTP {r.status_code}"}
    return {"ok": True, "message": _text_of(data.get("content")) or "（模型返回为空）", "model": data.get("model", "")}
