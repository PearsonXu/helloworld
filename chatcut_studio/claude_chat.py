"""Chat Completions 格式 ↔ Claude Messages API（官方 Anthropic SDK），给网关的 Codex 路径用。

Codex（ChatCut Desktop 里的「ChatGPT」）发来的 Responses 请求，先由 responses_api.py 翻译成 chat 格式；
服务商选的是 Claude 时，这里再用 Anthropic SDK 调 Messages API，把回复转回 chat 格式（content / tool_calls /
finish_reason / usage），后面的流程和 DeepSeek 完全一样。

思考块：Claude Opus 5.5 等模型的思考一直开着，下一轮要把上一轮 assistant 的内容块（thinking 块带签名、fallback 块等）
原样带回。回复里把原始内容块放在 `_anthropic_content`，responses_api 把它塞进 reasoning 项的 encrypted_content，
Codex 下一轮原样送回来，这里再原样放回 assistant 消息——不改动任何一个块。
"""
from __future__ import annotations

import json
import re
from typing import Any

import anthropic

from .providers import _anthropic, _claude_extra
from .settings import Provider

MAX_TOKENS = 32000           # 走流式，长回复也不会超时


def _image_block(url: str) -> dict | None:
    m = re.match(r"data:(image/[\w.+-]+);base64,(.+)", url or "", re.S)
    if m:
        return {"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}}
    if url.startswith("http"):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _user_blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    out = []
    for p in content or []:
        if p.get("type") == "text" and p.get("text"):
            out.append({"type": "text", "text": p["text"]})
        elif p.get("type") == "image_url" and (b := _image_block((p.get("image_url") or {}).get("url", ""))):
            out.append(b)
    return out


def to_messages(req: dict) -> tuple[str, list[dict], list[dict], dict | None]:
    """chat 请求 → (system, messages, tools, tool_choice)。"""
    def as_text(c) -> str:
        return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)

    # 只有开头的系统说明进顶层 system；对话中途插入的（Codex 会补环境信息等）留在原位置，变成一段 user 文字。
    # 这样已经发过的内容只增不改：Claude 的思考块和「system + tools + 之前的消息」绑定，改了前面就会失效
    all_msgs = req.get("messages", [])
    lead = 0
    while lead < len(all_msgs) and all_msgs[lead]["role"] == "system":
        lead += 1
    system = "\n\n".join(as_text(m["content"]) for m in all_msgs[:lead])
    msgs: list[dict] = []

    def add(role: str, blocks: list[dict]):
        if not blocks:
            return
        if msgs and msgs[-1]["role"] == role and role == "user":     # 工具结果和后面的文字合成一条 user 消息
            msgs[-1]["content"] += blocks
        else:
            msgs.append({"role": role, "content": blocks})

    for m in all_msgs[lead:]:
        role = m["role"]
        if role == "system":
            add("user", [{"type": "text", "text": "[系统说明]\n" + as_text(m["content"])}])
            continue
        if role == "user":
            add("user", _user_blocks(m.get("content")))
        elif role == "tool":
            add("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": str(m.get("content") or "")}])
        elif role == "assistant":
            if m.get("_anthropic_content"):            # 上一轮 Claude 的原始内容块：原样带回（思考块不能改）
                add("assistant", list(m["_anthropic_content"]))
                continue
            blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {"_raw": fn.get("arguments")}
                blocks.append({"type": "tool_use", "id": tc["id"], "name": fn.get("name", ""),
                               "input": args if isinstance(args, dict) else {"value": args}})
            add("assistant", blocks)

    tools = [{"name": t["function"]["name"], "description": t["function"].get("description") or "",
              "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
             for t in req.get("tools") or [] if t.get("type") == "function"]
    # Claude Opus 5.5 / Sonnet 5.5 不支持强制调用某个工具（any / tool 会 400），统一用 auto，none 照传
    tc = req.get("tool_choice")
    tool_choice = {"type": "none"} if tc == "none" else ({"type": "auto"} if tools else None)
    return system, msgs, tools, tool_choice


def _dump(block: Any) -> dict:
    return block.model_dump(mode="json", exclude_none=True) if hasattr(block, "model_dump") else dict(block)


def from_message(msg: Any) -> dict:
    """Claude 回复 → chat completions 格式的回复（含原始内容块）。"""
    raw = [_dump(b) for b in msg.content]
    text = "".join(b.get("text", "") for b in raw if b.get("type") == "text")
    thinking = "\n".join(b.get("thinking", "") for b in raw if b.get("type") == "thinking" and b.get("thinking"))
    calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
             for b in raw if b.get("type") == "tool_use"]
    finish = {"tool_use": "tool_calls", "max_tokens": "length", "refusal": "content_filter"}.get(msg.stop_reason or "", "stop")
    if msg.stop_reason == "refusal" and not text:
        text = "（Claude 拒绝了这次请求）"
    u = msg.usage
    cached = (getattr(u, "cache_read_input_tokens", 0) or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
    message: dict = {"role": "assistant", "content": text or None, "_anthropic_content": raw}
    if thinking:
        message["reasoning_content"] = thinking
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": (u.input_tokens or 0) + cached, "completion_tokens": u.output_tokens or 0,
                      "prompt_tokens_details": {"cached_tokens": getattr(u, "cache_read_input_tokens", 0) or 0}},
            "model": msg.model}


async def complete(p: Provider, req: dict) -> tuple[int, dict]:
    """返回 (HTTP 状态, chat completions 格式的回复)，出错时和 OpenAI 接口一样返回 {"error": {...}}。"""
    system, msgs, tools, tool_choice = to_messages(req)
    kwargs: dict[str, Any] = {"model": req["model"], "max_tokens": MAX_TOKENS, "messages": msgs,
                              "output_config": {"effort": "high"}, **_claude_extra(p)}
    if system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice
    try:
        async with _anthropic(p).beta.messages.stream(**kwargs) as stream:
            msg = await stream.get_final_message()
    except anthropic.APIStatusError as e:
        return e.status_code, {"error": {"message": f"Claude：{e.message}"}}
    except anthropic.APIConnectionError as e:
        return 502, {"error": {"message": f"连不上 Claude（{p.base_url or 'api.anthropic.com'}）：{e}"}}
    return 200, from_message(msg)
