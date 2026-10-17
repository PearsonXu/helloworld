"""OpenAI Responses 接口 ↔ Chat Completions 翻译，给 Codex 用。

ChatCut Desktop 的 Agent 按钮里的「ChatGPT」就是本机的 Codex。Codex 现在只会用 Responses 接口（/v1/responses，
wire_api = "responses"），而 DeepSeek 等服务商普遍提供的是 OpenAI 兼容的 /chat/completions。
这里在两者之间翻译：
- 请求：instructions / input 里的消息、函数调用、函数结果、思考内容 → chat messages；
  Codex 的工具（function、custom 自由文本工具如 apply_patch、local_shell）→ chat 的 function 工具；
- 回复：content、tool_calls、DeepSeek 的 reasoning_content → Responses 的 output 项；
  Codex 要流式时，按 Responses 的事件格式（response.created / output_item.added / …delta / output_item.done /
  response.completed）发回。DeepSeek 的思考内容放在 reasoning 项的 encrypted_content 里，Codex 下一轮原样带回，
  翻译时还原成 reasoning_content（DeepSeek 思考模式的工具调用需要它）。
"""
from __future__ import annotations

import base64
import json
import time
import uuid

REASONING_PREFIX = "ccs1:"          # DeepSeek 等：reasoning_content 文本
CLAUDE_PREFIX = "ccsA:"             # Claude：上一轮 assistant 的原始内容块（base64 JSON），原样带回


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return _text(content.get("content") or content.get("text") or content.get("output") or json.dumps(content, ensure_ascii=False))
    out = []
    for p in content:
        if isinstance(p, str):
            out.append(p)
        elif p.get("type") in ("input_text", "output_text", "text", "summary_text", "reasoning_text"):
            out.append(p.get("text", ""))
        elif p.get("type") in ("input_image", "image"):
            out.append("[图片]")
    return "\n".join(out)


def _parts(content) -> str | list:
    """用户消息内容：纯文字合成字符串；带图片时用 chat 的多段格式。"""
    if isinstance(content, str):
        return content
    parts = []
    for p in content or []:
        t = p.get("type")
        if t in ("input_text", "output_text", "text"):
            parts.append({"type": "text", "text": p.get("text", "")})
        elif t == "input_image" and p.get("image_url"):
            parts.append({"type": "image_url", "image_url": {"url": p["image_url"]}})
    if all(x["type"] == "text" for x in parts):
        return "\n".join(x["text"] for x in parts)
    return parts


class Translation:
    """一次请求的翻译结果，记住哪些工具是自由文本工具 / local_shell，回复时要翻回去。"""

    def __init__(self):
        self.custom: set[str] = set()
        self.local_shell = False


def to_chat(body: dict, model: str) -> tuple[dict, Translation]:
    tr = Translation()
    msgs: list[dict] = []
    if body.get("instructions"):
        msgs.append({"role": "system", "content": body["instructions"]})
    items = body.get("input") or []
    if isinstance(items, str):
        items = [{"type": "message", "role": "user", "content": items}]

    pending = {"text": "", "calls": [], "reasoning": "", "claude": None}

    def flush():
        if pending["text"] or pending["calls"]:
            m = {"role": "assistant", "content": pending["text"] or None}
            if pending["calls"]:
                m["tool_calls"] = pending["calls"]
            if pending["reasoning"]:
                m["reasoning_content"] = pending["reasoning"]
            if pending["claude"]:
                m["_anthropic_content"] = pending["claude"]
            msgs.append(m)
        pending.update(text="", calls=[], reasoning="", claude=None)

    for it in items:
        t = it.get("type") or "message"
        if t == "message":
            role = it.get("role", "user")
            if role == "assistant":
                pending["text"] += _text(it.get("content"))
                continue
            flush()
            msgs.append({"role": "system" if role in ("developer", "system") else "user",
                         "content": _text(it.get("content")) if role in ("developer", "system") else _parts(it.get("content"))})
        elif t == "reasoning":
            enc = it.get("encrypted_content") or ""
            if enc.startswith(CLAUDE_PREFIX):
                try:
                    pending["claude"] = json.loads(base64.b64decode(enc[len(CLAUDE_PREFIX):]).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    pass
            else:
                pending["reasoning"] += enc[len(REASONING_PREFIX):] if enc.startswith(REASONING_PREFIX) else ""
        elif t == "function_call":
            pending["calls"].append({"id": it["call_id"], "type": "function",
                                     "function": {"name": it["name"], "arguments": it.get("arguments") or "{}"}})
        elif t == "custom_tool_call":
            pending["calls"].append({"id": it["call_id"], "type": "function",
                                     "function": {"name": it["name"], "arguments": json.dumps({"input": it.get("input", "")}, ensure_ascii=False)}})
        elif t == "local_shell_call":
            a = it.get("action") or {}
            args = {"command": a.get("command") or [], "workdir": a.get("working_directory"), "timeout_ms": a.get("timeout_ms")}
            pending["calls"].append({"id": it.get("call_id") or it.get("id"), "type": "function",
                                     "function": {"name": "local_shell", "arguments": json.dumps({k: v for k, v in args.items() if v}, ensure_ascii=False)}})
        elif t in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
            flush()
            msgs.append({"role": "tool", "tool_call_id": it.get("call_id") or it.get("id"), "content": _text(it.get("output"))})
    flush()

    tools = []
    for tl in body.get("tools") or []:
        kind = tl.get("type")
        if kind == "function":
            tools.append({"type": "function", "function": {"name": tl["name"], "description": (tl.get("description") or "")[:4000],
                                                           "parameters": tl.get("parameters") or {"type": "object", "properties": {}}}})
        elif kind == "custom":       # 自由文本工具（比如 apply_patch）：包成只有一个字符串参数 input 的函数
            tr.custom.add(tl["name"])
            fmt = tl.get("format") or {}
            hint = f"\n\n参数 input 是纯文本{('，格式：' + str(fmt.get('definition'))[:2000]) if fmt.get('definition') else ''}"
            tools.append({"type": "function", "function": {"name": tl["name"], "description": (tl.get("description") or "")[:3000] + hint,
                                                           "parameters": {"type": "object", "properties": {"input": {"type": "string"}},
                                                                          "required": ["input"]}}})
        elif kind == "local_shell":
            tr.local_shell = True
            tools.append({"type": "function", "function": {"name": "local_shell", "description": "在本机执行命令",
                                                           "parameters": {"type": "object", "properties": {
                                                               "command": {"type": "array", "items": {"type": "string"}},
                                                               "workdir": {"type": "string"}, "timeout_ms": {"type": "integer"}},
                                                               "required": ["command"]}}})
        # web_search 等服务端工具：第三方模型没有，跳过

    out: dict = {"model": model, "messages": msgs,
                 "max_tokens": min(int(body.get("max_output_tokens") or 8192), 8192)}
    for k in ("temperature", "top_p"):
        if body.get(k) is not None:
            out[k] = body[k]
    if tools:
        out["tools"] = tools
        tc = body.get("tool_choice")
        if tc in ("auto", "required", "none"):
            out["tool_choice"] = tc
        elif isinstance(tc, dict) and tc.get("name"):
            out["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        if body.get("parallel_tool_calls") is not None:
            out["parallel_tool_calls"] = body["parallel_tool_calls"]
    return out, tr


def from_chat(data: dict, tr: Translation) -> tuple[list[dict], dict]:
    """chat 回复 → Responses 的 output 项列表和 usage。"""
    msg = ((data.get("choices") or [{}])[0].get("message")) or {}
    items: list[dict] = []
    rc = msg.get("reasoning_content") or ""
    if msg.get("_anthropic_content"):
        blob = base64.b64encode(json.dumps(msg["_anthropic_content"], ensure_ascii=False).encode("utf-8")).decode()
        items.append({"type": "reasoning", "id": _id("rs"), "summary": [{"type": "summary_text", "text": rc}] if rc else [],
                      "encrypted_content": CLAUDE_PREFIX + blob})
    elif rc:
        items.append({"type": "reasoning", "id": _id("rs"), "summary": [{"type": "summary_text", "text": rc}],
                      "encrypted_content": REASONING_PREFIX + rc})
    if msg.get("content"):
        items.append({"type": "message", "id": _id("msg"), "role": "assistant", "status": "completed",
                      "content": [{"type": "output_text", "text": msg["content"], "annotations": []}]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name, raw, call_id = fn.get("name", ""), fn.get("arguments") or "{}", tc.get("id") or _id("call")
        if name in tr.custom:
            try:
                inp = json.loads(raw).get("input", "")
            except (ValueError, AttributeError):
                inp = raw
            items.append({"type": "custom_tool_call", "id": _id("ctc"), "call_id": call_id, "name": name, "input": inp, "status": "completed"})
        elif name == "local_shell" and tr.local_shell:
            try:
                a = json.loads(raw)
            except ValueError:
                a = {}
            items.append({"type": "local_shell_call", "id": _id("lsh"), "call_id": call_id, "status": "completed",
                          "action": {"type": "exec", "command": a.get("command") or [], "working_directory": a.get("workdir"),
                                     "timeout_ms": a.get("timeout_ms"), "env": {}}})
        else:
            items.append({"type": "function_call", "id": _id("fc"), "call_id": call_id, "name": name, "arguments": raw, "status": "completed"})
    u = data.get("usage") or {}
    usage = {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0),
             "total_tokens": u.get("total_tokens", u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)),
             "input_tokens_details": {"cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)},
             "output_tokens_details": {"reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)}}
    return items, usage


def response_obj(rid: str, model: str, status: str, output: list, usage: dict | None = None, error: dict | None = None) -> dict:
    return {"id": rid, "object": "response", "created_at": int(time.time()), "status": status, "model": model,
            "output": output, "usage": usage, "error": error, "incomplete_details": None}


class Events:
    """按 Responses 的流式事件格式编码。"""

    def __init__(self):
        self.seq = 0

    def __call__(self, kind: str, **data) -> bytes:
        self.seq += 1
        return f"event: {kind}\ndata: {json.dumps({'type': kind, 'sequence_number': self.seq, **data}, ensure_ascii=False)}\n\n".encode()


def item_events(ev: Events, i: int, it: dict) -> list[bytes]:
    out = []
    t = it["type"]
    if t == "message":
        text = it["content"][0]["text"]
        out.append(ev("response.output_item.added", output_index=i, item={**it, "status": "in_progress", "content": []}))
        out.append(ev("response.content_part.added", item_id=it["id"], output_index=i, content_index=0,
                      part={"type": "output_text", "text": "", "annotations": []}))
        out.append(ev("response.output_text.delta", item_id=it["id"], output_index=i, content_index=0, delta=text))
        out.append(ev("response.output_text.done", item_id=it["id"], output_index=i, content_index=0, text=text))
        out.append(ev("response.content_part.done", item_id=it["id"], output_index=i, content_index=0, part=it["content"][0]))
    elif t == "function_call":
        out.append(ev("response.output_item.added", output_index=i, item={**it, "arguments": "", "status": "in_progress"}))
        out.append(ev("response.function_call_arguments.delta", item_id=it["id"], output_index=i, delta=it["arguments"]))
        out.append(ev("response.function_call_arguments.done", item_id=it["id"], output_index=i, arguments=it["arguments"]))
    elif t == "custom_tool_call":
        out.append(ev("response.output_item.added", output_index=i, item={**it, "input": "", "status": "in_progress"}))
        out.append(ev("response.custom_tool_call_input.delta", item_id=it["id"], output_index=i, delta=it["input"]))
        out.append(ev("response.custom_tool_call_input.done", item_id=it["id"], output_index=i, input=it["input"]))
    elif t == "reasoning" and not it["summary"]:
        out.append(ev("response.output_item.added", output_index=i, item=it))
    elif t == "reasoning":
        out.append(ev("response.output_item.added", output_index=i, item={**it, "summary": []}))
        text = it["summary"][0]["text"]
        out.append(ev("response.reasoning_summary_part.added", item_id=it["id"], output_index=i, summary_index=0,
                      part={"type": "summary_text", "text": ""}))
        out.append(ev("response.reasoning_summary_text.delta", item_id=it["id"], output_index=i, summary_index=0, delta=text))
        out.append(ev("response.reasoning_summary_text.done", item_id=it["id"], output_index=i, summary_index=0, text=text))
        out.append(ev("response.reasoning_summary_part.done", item_id=it["id"], output_index=i, summary_index=0,
                      part={"type": "summary_text", "text": text}))
    else:
        out.append(ev("response.output_item.added", output_index=i, item=it))
    out.append(ev("response.output_item.done", output_index=i, item=it))
    return out
