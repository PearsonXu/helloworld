"""模型服务商适配：Claude（Anthropic SDK）和 OpenAI 兼容接口（DeepSeek、通义千问、Kimi、GLM 等）。

提供三件事：
- list_models：拉取服务商当前可用的模型列表
- chat_json：要求模型按 pydantic 结构返回 JSON（风格优化用）
- make_adapter：给剪辑 Agent 用的「带工具调用的一步对话」
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, TypeVar

import anthropic
import httpx
from pydantic import BaseModel, ValidationError

from .settings import Provider

T = TypeVar("T", bound=BaseModel)

# 这些 Claude 模型支持服务端拒答兜底（fallbacks: "default"）
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# 思考块和产生它时的 system / tools / 之前的消息绑定；对不上时（比如 Codex 中途增减了工具）丢掉旧思考块继续，而不是 400
BINDING_BETA = "thinking-binding-controls-2026-08-01"
TIMEOUT = httpx.Timeout(300, connect=15)


class ProviderError(RuntimeError):
    pass


def _require(p: Provider) -> str:
    key = p.key()
    if not key:
        raise ProviderError(f"{p.label} 还没有设置 API Key")
    if not p.model:
        raise ProviderError(f"{p.label} 还没有选择模型")
    if p.kind == "openai" and not p.base_url:
        raise ProviderError(f"{p.label} 还没有填写 base_url")
    return key


def _anthropic(p: Provider) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=p.key(), **({"base_url": p.base_url} if p.base_url else {}))


def _claude_extra(p: Provider) -> dict:
    extra: dict[str, Any] = {"thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
                             "betas": [BINDING_BETA]}
    if p.model in FALLBACK_MODELS:
        extra["betas"].append(FALLBACK_BETA)
        extra["fallbacks"] = "default"
    return extra


async def _openai_post(p: Provider, path: str, body: dict | None = None, method: str = "POST") -> dict:
    url = p.base_url.rstrip("/") + path
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.request(method, url, headers={"Authorization": f"Bearer {p.key()}"}, json=body)
    if r.status_code >= 400:
        raise ProviderError(f"{p.label} 返回 {r.status_code}：{r.text[:300]}")
    return r.json()


# ---------------------------------------------------------------- 模型列表
async def list_models(p: Provider) -> list[str]:
    if not p.key():
        raise ProviderError(f"{p.label} 还没有设置 API Key")
    if p.kind == "anthropic":
        client = _anthropic(p)
        return [m.id async for m in client.models.list()]
    data = await _openai_post(p, "/models", method="GET")
    return sorted(m["id"] for m in data.get("data", []))


# ---------------------------------------------------------------- 结构化输出
async def chat_json(p: Provider, system: str, user: str, schema: type[T]) -> T:
    _require(p)
    if p.kind == "anthropic":
        resp = await _anthropic(p).beta.messages.parse(
            model=p.model, max_tokens=16000, system=system,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": user}], output_format=schema, **_claude_extra(p),
        )
        if resp.stop_reason == "refusal":
            raise ProviderError("模型拒绝了这次请求")
        if resp.parsed_output is None:
            raise ProviderError("模型没有返回有效结果")
        return resp.parsed_output

    # OpenAI 兼容：JSON 模式 + 本地校验，失败时带着错误再试一次
    sys = (system + "\n\n只输出一个 JSON 对象，不要任何其他文字。JSON Schema：\n"
           + json.dumps(schema.model_json_schema(), ensure_ascii=False))
    messages = [{"role": "system", "content": sys}, {"role": "user", "content": user}]
    last_err = ""
    for _ in range(2):
        body = {"model": p.model, "messages": messages, "response_format": {"type": "json_object"}}
        try:
            data = await _openai_post(p, "/chat/completions", body)
        except ProviderError as e:  # 有的模型不支持 response_format，去掉再试
            if "response_format" not in str(e) and "400" not in str(e):
                raise
            body.pop("response_format")
            data = await _openai_post(p, "/chat/completions", body)
        text = (data["choices"][0]["message"].get("content") or "").strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            return schema.model_validate_json(text)
        except ValidationError as e:
            last_err = str(e)[:800]
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"输出不符合 Schema：{last_err}\n请修正后重新输出完整 JSON。"}]
    raise ProviderError(f"模型输出两次都不符合格式：{last_err}")


# ---------------------------------------------------------------- Agent 用的工具调用
@dataclass
class ToolCall:
    id: str
    name: str
    args: dict
    bad_args: str = ""  # 参数不是合法 JSON 时的原文


@dataclass
class Step:
    text: str
    calls: list[ToolCall] = field(default_factory=list)
    stop: str = ""


class AnthropicAdapter:
    def __init__(self, p: Provider):
        _require(p)
        self.p, self.client = p, _anthropic(p)

    def start(self, system: str, prompt: str) -> list:
        self.system = system
        return [{"role": "user", "content": prompt}]

    async def step(self, messages: list, tools: list[dict]) -> Step:
        resp = await self.client.beta.messages.create(
            model=self.p.model, max_tokens=16000, system=self.system, output_config={"effort": "high"},
            tools=[{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tools],
            messages=messages, **_claude_extra(self.p),
        )
        if resp.stop_reason == "refusal":
            raise ProviderError("模型拒绝了这次请求")
        messages.append({"role": "assistant", "content": resp.content})  # 原样回传（含 thinking 块）
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, b.input) for b in resp.content if b.type == "tool_use"]
        return Step(text, calls, resp.stop_reason or "")

    def add_results(self, messages: list, results: list[tuple[ToolCall, str, bool]]) -> None:
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": out, "is_error": err} for c, out, err in results]})


class OpenAIAdapter:
    def __init__(self, p: Provider):
        _require(p)
        self.p = p

    def start(self, system: str, prompt: str) -> list:
        return [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

    async def step(self, messages: list, tools: list[dict]) -> Step:
        body = {"model": self.p.model, "messages": messages, "tools": [
            {"type": "function", "function": {"name": t["name"], "description": t["description"][:1000],
                                              "parameters": t["input_schema"]}} for t in tools]}
        data = await _openai_post(self.p, "/chat/completions", body)
        choice = data["choices"][0]
        msg = {k: v for k, v in choice["message"].items() if v is not None}
        messages.append(msg)  # 原样回传（DeepSeek 思考模式需要带回 reasoning_content）
        calls = []
        for tc in msg.get("tool_calls", []):
            raw = tc["function"].get("arguments") or "{}"
            try:
                args, bad = json.loads(raw), ""
            except json.JSONDecodeError:
                args, bad = {}, raw
            calls.append(ToolCall(tc["id"], tc["function"]["name"], args if isinstance(args, dict) else {}, bad))
        return Step(msg.get("content") or "", calls, choice.get("finish_reason", ""))

    def add_results(self, messages: list, results: list[tuple[ToolCall, str, bool]]) -> None:
        for c, out, err in results:
            messages.append({"role": "tool", "tool_call_id": c.id, "content": ("错误：" if err else "") + out})


def make_adapter(p: Provider):
    return AnthropicAdapter(p) if p.kind == "anthropic" else OpenAIAdapter(p)
