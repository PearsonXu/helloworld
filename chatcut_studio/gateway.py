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
import time
import uuid
from collections import deque
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
    if p.kind == "anthropic" and "haiku" in small and requested.startswith("claude-"):
        return requested                      # 服务商就是 Claude：后台小任务照用 Claude Code 要的 Haiku，更省钱
    return main


def route(s: settings.Settings) -> tuple[Provider, str]:
    """返回（服务商, 转发方式 anthropic / openai）。"""
    g = s.gateway
    p = s.provider(g.provider)
    if p.kind == "anthropic":           # Claude 永远走它自己的 Messages API；「转发方式」只对 DeepSeek 这类服务商生效
        return p, "anthropic"
    url = p.anthropic_endpoint()
    if g.mode == "anthropic" or (g.mode == "auto" and url):
        if not url:
            raise ValueError(f"{p.label} 没有 Anthropic 兼容接口，请把转发方式改成「OpenAI 兼容」")
        return p, "anthropic"
    if not p.base_url:
        raise ValueError(f"{p.label} 还没有填写 base_url")
    return p, "openai"


RECENT: deque[dict] = deque(maxlen=30)     # 最近收到的请求（诊断用：Claude Code 到底有没有走网关）


async def handle(body: dict, headers) -> Response:
    """/gateway/v1/messages：记一笔再处理。"""
    rec = {"time": time.strftime("%H:%M:%S"), "requested": body.get("model", ""), "client": (headers.get("user-agent") or "")[:80],
           "stream": bool(body.get("stream"))}
    r = await _handle(body, headers)
    rec["status"] = r.status_code
    if not rec.get("model"):
        try:
            s = settings.load()
            rec["model"] = pick_model(s.gateway, s.provider(s.gateway.provider), rec["requested"])
        except KeyError:
            rec["model"] = ""
    RECENT.append(rec)
    return r


async def _handle(body: dict, headers) -> Response:
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
        if r.status_code >= 400:
            try:
                ok = bool((json.loads(data).get("error") or {}).get("message"))
            except (ValueError, AttributeError):
                ok = False
            if not ok:                       # 对方没给出可读的错误：补上状态码和地址，方便排查
                return error(r.status_code, f"{p.label}：HTTP {r.status_code}，{target} 没有返回错误说明"
                                            f"（{data[:200].decode('utf-8', 'replace') or '空'}）。检查 base_url 和 Key")
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
    url = p.base_url.rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(url, headers={"Authorization": f"Bearer {p.key()}"}, json=req)
    try:
        data = r.json()
    except ValueError:
        data = {"error": {"message": r.text[:500]}}
    if r.status_code >= 400 and not (isinstance(data, dict) and (data.get("error") or {}).get("message")):
        data = {"error": {"message": f"HTTP {r.status_code}，{url} 没有返回错误说明（地址可能不对，或者不是 OpenAI 兼容接口）"}}
    return r.status_code, data


async def _upstream(p: Provider, req: dict) -> tuple[int, dict]:
    """chat 格式请求发给服务商：Claude 走官方 SDK（Messages API），其他走 OpenAI 兼容 /chat/completions。"""
    if p.kind == "anthropic":
        from . import claude_chat
        return await claude_chat.complete(p, req)
    return await _openai(p, req)


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
    set_guard("Claude Code", True)
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
    set_guard("Claude Code", False)
    return status()


def refresh_if_linked() -> None:
    """在 Studio 里换了模型：已接入时顺手更新 Claude Code / Codex 里的模型名。"""
    if linked():
        link()
    if codex_linked():
        link_codex()


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
    import shutil
    return {"url": gateway_url(), "linked": linked(s), "claude_settings": str(claude_settings_path()),
            "codex_linked": codex_linked(s), "codex_config": str(codex_config_path()), "codex_resume": codex_resume_ready(),
            "codex_installed": bool(shutil.which("codex")), "claude_installed": bool(shutil.which("claude")),
            "provider": g.provider, "provider_label": p.label if p else "", "how": how, "model": model,
            "small_model": g.small_model, "mode": g.mode, "problem": problem}


async def self_test() -> dict:
    """在 Studio 里试一次：走和 Claude Code 完全一样的网关路径，发一句话。"""
    s = settings.load()
    body = {"model": "claude-sonnet", "max_tokens": 200, "stream": False,
            "messages": [{"role": "user", "content": "请只回复：连接成功"}]}
    r = await handle(body, {"x-api-key": s.gateway.token, "user-agent": "ChatCut Studio 自测"})
    raw = getattr(r, "body", b"") or b""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {"error": {"message": raw[:300].decode("utf-8", "replace")}}
    if r.status_code >= 400 or data.get("type") == "error":
        msg = (data.get("error") or {}).get("message") if isinstance(data, dict) else ""
        return {"ok": False, "message": msg or f"HTTP {r.status_code}（没有错误说明）"}
    return {"ok": True, "message": _text_of(data.get("content")) or "（模型返回为空）", "model": data.get("model", "")}


# ---------------------------------------------------------------- Codex（ChatCut Desktop 里的「ChatGPT」Agent）
async def handle_responses(body: dict, headers) -> Response:
    """/gateway/openai/v1/responses：Codex 用的 Responses 接口，翻译成 DeepSeek 的 /chat/completions。"""
    from . import responses_api as ra
    rec = {"time": time.strftime("%H:%M:%S"), "requested": body.get("model", ""), "client": (headers.get("user-agent") or "Codex")[:80],
           "stream": bool(body.get("stream"))}
    r = await _handle_responses(body, headers, rec)
    rec["status"] = r.status_code
    RECENT.append(rec)
    return r


def _resp_error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "invalid_request_error" if status < 500 else "server_error", "code": None}},
                        status_code=status)


async def _handle_responses(body: dict, headers, rec: dict) -> Response:
    from . import responses_api as ra
    s = settings.load()
    if not authorized(headers, s.gateway.token):
        return _resp_error(401, "ChatCut Studio 网关：Token 不对。请在 Studio「连接设置」里重新点「接入 Codex」。")
    try:
        p = s.provider(s.gateway.provider)
    except KeyError as e:
        return _resp_error(400, f"ChatCut Studio 网关：没有服务商 {e}")
    if not p.key() or (p.kind != "anthropic" and not p.base_url):
        return _resp_error(400, f"ChatCut Studio 网关：{p.label} 的 API Key{'' if p.kind == 'anthropic' else ' 或 base_url'} 还没填")
    model = s.gateway.model or p.model
    if not model:
        return _resp_error(400, f"ChatCut Studio 网关：{p.label} 还没有选择模型")
    rec["model"] = model
    req, tr = ra.to_chat(body, model)
    rec["tools"] = len(req.get("tools") or [])
    rec["skipped"] = tr.skipped
    # 连接检查用：Agent 这次带来了哪些 ChatCut 工具、哪些 skill（Codex 在说明里列出每个 skill 的 SKILL.md 路径）
    rec["chatcut_tools"] = [t["function"]["name"] for t in req.get("tools") or []
                            if t.get("type") == "function" and "chatcut" in t["function"]["name"].lower()]
    rec["chatcut_results"] = _chatcut_results(body)
    rec["skills"] = sorted(set(re.findall(r"[\\/]+([^\\/\s\"'`()<>]+)[\\/]+SKILL\.md", json.dumps(body, ensure_ascii=False))))
    rid = "resp_" + uuid.uuid4().hex[:24]

    if not body.get("stream"):
        try:
            status, data = await _upstream(p, req)
        except httpx.HTTPError as e:
            return _resp_error(502, f"连不上 {p.label}：{e}")
        if status >= 400:
            return _resp_error(status, f"{p.label}：{(data.get('error') or {}).get('message') or data}")
        items, usage = ra.from_chat(data, tr)
        rec["calls"] = _calls(items)
        return JSONResponse(ra.response_obj(rid, model, "completed", items, usage))

    async def stream():
        ev = ra.Events()
        yield ev("response.created", response=ra.response_obj(rid, model, "in_progress", []))
        yield ev("response.in_progress", response=ra.response_obj(rid, model, "in_progress", []))
        task = asyncio.ensure_future(_upstream(p, req))
        while not task.done():                      # 上游不是流式：等待期间发 SSE 注释保活
            await asyncio.wait({task}, timeout=10)
            if not task.done():
                yield b": keepalive\n\n"
        try:
            status, data = task.result()
        except httpx.HTTPError as e:
            status, data = 502, {"error": {"message": f"连不上 {p.label}：{e}"}}
        if status >= 400:
            msg = f"{p.label}：{(data.get('error') or {}).get('message') or data}"
            yield ev("response.failed", response=ra.response_obj(rid, model, "failed", [], None, {"code": str(status), "message": msg}))
            return
        items, usage = ra.from_chat(data, tr)
        rec["calls"] = _calls(items)
        for i, it in enumerate(items):
            for b in ra.item_events(ev, i, it):
                yield b
        yield ev("response.completed", response=ra.response_obj(rid, model, "completed", items, usage))
    return StreamingResponse(stream(), media_type="text/event-stream")


TOOL_FAILED = re.compile(r"requires approval|rejected|denied|cancell?ed|not found|unknown tool|error|失败|拒绝", re.I)


def _chatcut_results(body: dict) -> list[dict]:
    """这次请求里带回来的 ChatCut 工具结果（上一轮调用的）：工具真的执行成功了没有。"""
    items = body.get("input") if isinstance(body.get("input"), list) else []
    names = {it.get("call_id"): (it.get("namespace") or "").removeprefix("mcp__") + "/" + it.get("name", "")
             for it in items if it.get("type") == "function_call" and "chatcut" in ((it.get("namespace") or "") + it.get("name", "")).lower()}
    out = []
    for it in items:
        if it.get("type") == "function_call_output" and it.get("call_id") in names:
            o = it.get("output")
            text = o if isinstance(o, str) else " ".join(str(x.get("text", "")) for x in o or [] if isinstance(x, dict))
            text = re.sub(r"^Wall time:[^\n]*\n(Output:\s*)?", "", text.strip())      # Codex 给工具结果加的耗时说明
            out.append({"tool": names[it["call_id"]], "ok": not TOOL_FAILED.search(text[:300]), "text": text[:200]})
    return out


def _calls(items: list[dict]) -> list[str]:
    """模型这一轮要调用的工具（MCP 工具写成「服务器/工具名」）。"""
    return [(it["namespace"].removeprefix("mcp__") + "/" if it.get("namespace") else "") + it["name"]
            for it in items if it.get("type") == "function_call"]


CODEX_PROVIDER = "chatcut_studio"
_TABLE = re.compile(r"^\s*\[")
_TOP_KEY = re.compile(r"^\s*(model_provider|model)\s*=")


def codex_config_path() -> Path:
    from .desktop import _home
    return Path(os.environ.get("CODEX_HOME") or _home() / ".codex") / "config.toml"


def _codex_read() -> str:
    f = codex_config_path()
    return f.read_text("utf-8") if f.is_file() else ""


def _codex_parts(text: str) -> tuple[list[str], list[str]]:
    """（顶层键的行, 从第一个表头开始的其余行）。TOML 的顶层键必须写在所有 [表] 之前。"""
    lines = text.splitlines()
    i = next((k for k, l in enumerate(lines) if _TABLE.match(l)), len(lines))
    return lines[:i], lines[i:]


def _drop_our_table(lines: list[str]) -> list[str]:
    out, skip = [], False
    for l in lines:
        if _TABLE.match(l):
            skip = l.strip() == f"[model_providers.{CODEX_PROVIDER}]"
        if not skip:
            out.append(l)
    while out and not out[-1].strip():
        out.pop()
    return out


def codex_linked(s: settings.Settings | None = None) -> bool:
    s = s or settings.load()
    try:
        import tomllib
        data = tomllib.loads(_codex_read())
    except (OSError, ValueError):
        return False
    prov = (data.get("model_providers") or {}).get(CODEX_PROVIDER) or {}
    return (data.get("model_provider") == CODEX_PROVIDER and prov.get("base_url") == gateway_url() + "/openai/v1"
            and prov.get("experimental_bearer_token") == s.gateway.token)


def _write_codex(text: str) -> None:
    f = codex_config_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".toml.tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, f)


def link_codex() -> dict:
    """让 Codex（Desktop 里的「ChatGPT」）通过网关用 DeepSeek：只改 config.toml 的 model_provider / model 两行，加一个
    [model_providers.chatcut_studio] 表；原来的两行先备份，断开时恢复。"""
    s = settings.load()
    p = s.provider(s.gateway.provider)
    model = s.gateway.model or p.model or "deepseek"
    top, rest = _codex_parts(_codex_read())
    if not codex_linked(s):
        settings.set_gateway_backup({m.group(1): l for l in top if (m := _TOP_KEY.match(l))}, "codex_backup")
    top = [l for l in top if not _TOP_KEY.match(l)]
    while top and not top[0].strip():
        top.pop(0)
    lines = [f'model_provider = "{CODEX_PROVIDER}"   # ChatCut Studio：Codex 通过本机网关用 Studio 里选的模型',
             f'model = "{model}"', *top, *_drop_our_table(rest), "",
             f"[model_providers.{CODEX_PROVIDER}]", f'name = "ChatCut Studio · {p.label}"',
             f'base_url = "{gateway_url()}/openai/v1"', 'wire_api = "responses"',
             # Studio 被意外结束、自动重启的那几秒里多重试几次，Desktop 里的任务接着做，不用重来
             "request_max_retries = 20", "stream_max_retries = 20",
             f'experimental_bearer_token = "{s.gateway.token}"']
    _write_codex("\n".join(lines) + "\n")
    set_guard("Codex", True)
    return status()


def codex_resume_ready() -> bool:
    """接入的 Codex 是否已经带上「掉线续跑」：重试设置 + 不许结束 Studio 的注意事项（旧版本接入的没有）。"""
    f = agent_guard_files()["Codex"]
    return ("stream_max_retries" in _codex_read()
            and f.is_file() and GUARD_BEGIN in f.read_text(encoding="utf-8", errors="replace"))


def unlink_codex() -> dict:
    s = settings.load()
    top, rest = _codex_parts(_codex_read())
    top = [l for l in top if not _TOP_KEY.match(l)]
    restored = [s.gateway.codex_backup[k] for k in ("model_provider", "model") if s.gateway.codex_backup.get(k)]
    text = "\n".join([*restored, *top, *_drop_our_table(rest)]).strip()
    _write_codex(text + "\n" if text else "")
    settings.set_gateway_backup({}, "codex_backup")
    set_guard("Codex", False)
    return status()


# ---------------------------------------------------------------- 给 Agent 的注意事项（全局说明文件）
GUARD_BEGIN = "<!-- ChatCut Studio 注意事项：开始（接入时由 Studio 写入，断开时删除） -->"
GUARD_END = "<!-- ChatCut Studio 注意事项：结束 -->"


def guard_text() -> str:
    port = os.environ.get("PORT", "8765")
    return "\n".join([
        GUARD_BEGIN,
        "## 本机进程注意事项（ChatCut Studio）",
        "",
        f"- 本机运行着 ChatCut Studio（命令行 `python -m chatcut_studio`，监听 127.0.0.1:{port}），它是你连接模型的网关。"
        "结束它，你自己的会话就会断开。",
        "- 不要按名字批量结束进程：不要用 `Stop-Process -Name python`、`taskkill /IM python.exe`、`pkill python`、`killall python` 这类命令。",
        "- 只结束你自己启动的进程：启动时记下进程号（例如 `$p = Start-Process python -ArgumentList ... -PassThru` 后用 `$p.Id`），"
        "需要停止时用 `Stop-Process -Id <进程号>`。",
        "- 不确定是哪个进程时先列出来：`Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Select-Object ProcessId, CommandLine`，"
        "跳过命令行里带 chatcut_studio 的进程。",
        GUARD_END,
    ])


def agent_guard_files() -> dict[str, Path]:
    return {"Codex": codex_config_path().parent / "AGENTS.md", "Claude Code": claude_settings_path().parent / "CLAUDE.md"}


def set_guard(which: str, on: bool) -> None:
    """在 Agent 的全局说明文件里写入 / 删除注意事项（只动标记之间的内容，用户自己写的不碰）。"""
    f = agent_guard_files()[which]
    text = f.read_text("utf-8") if f.is_file() else ""
    if GUARD_BEGIN in text and GUARD_END in text:
        a, rest = text.split(GUARD_BEGIN, 1)
        text = a.rstrip("\n") + rest.split(GUARD_END, 1)[1]
    text = text.strip("\n")
    if on:
        text = (text + "\n\n" if text else "") + guard_text()
    if text or f.is_file():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text + "\n" if text else "", "utf-8")
