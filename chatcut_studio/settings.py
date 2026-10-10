"""本地设置：模型服务商（API Key）、Agent 连接方式、ChatCut MCP 连接。

保存在 data/settings.json（已 gitignore，权限 600）。API Key 永远不会原样返回给前端。
模型名变化很快，这里只预填 base_url；模型请在界面里点「获取模型列表」后选择。
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from .desktop import Endpoint

DATA_ROOT = Path(os.environ.get("CHATCUT_STUDIO_DATA", Path(__file__).parent / "data"))
CHATCUT_MCP_URL = "https://api.chatcut.io/api/external-mcp/mcp"
# 各服务商的 Anthropic 兼容接口（给 Claude Code 用，见 gateway.py）。界面里可以改。
ANTHROPIC_COMPAT = {
    "deepseek": "https://api.deepseek.com/anthropic",
    "kimi": "https://api.moonshot.cn/anthropic",
    "glm": "https://open.bigmodel.cn/api/anthropic",
    "qwen": "https://dashscope.aliyuncs.com/apps/anthropic",
}


class Provider(BaseModel):
    id: str
    label: str
    kind: Literal["anthropic", "openai"]  # openai = OpenAI 兼容的 /chat/completions
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    key_env: str = ""  # 没填 api_key 时，从这个环境变量读取
    hint: str = ""
    anthropic_url: str = ""  # Anthropic 兼容接口；空 = 用 ANTHROPIC_COMPAT 里的默认值

    def key(self) -> str:
        return self.api_key or (os.environ.get(self.key_env, "") if self.key_env else "")

    def anthropic_endpoint(self) -> str:
        if self.kind == "anthropic":
            return self.base_url or "https://api.anthropic.com"
        if self.anthropic_url:
            return self.anthropic_url
        # 默认的 Anthropic 兼容接口只对官方地址有效；填的是中转 / 其他 OpenAI 兼容地址时走翻译
        preset = next((p for p in PRESETS if p.id == self.id), None)
        same_host = preset and urlparse(preset.base_url).hostname == urlparse(self.base_url).hostname
        return ANTHROPIC_COMPAT.get(self.id, "") if same_host else ""

    def public(self) -> dict:
        k = self.key()
        d = self.model_dump(exclude={"api_key"})
        d["anthropic_endpoint"] = self.anthropic_endpoint()
        d["has_key"] = bool(k)
        d["key_preview"] = (k[:3] + "…" + k[-4:]) if len(k) > 10 else ("已设置" if k else "")
        d["key_from_env"] = bool(not self.api_key and k)
        return d


PRESETS: list[Provider] = [
    Provider(id="claude", label="Claude（Anthropic API）", kind="anthropic", model="claude-opus-5-5",
             key_env="ANTHROPIC_API_KEY", hint="https://platform.claude.com 申请 API Key"),
    Provider(id="deepseek", label="DeepSeek", kind="openai", base_url="https://api.deepseek.com",
             key_env="DEEPSEEK_API_KEY", hint="填 OpenAI 兼容地址：官方是 https://api.deepseek.com，用中转服务就填中转地址；Key 在 platform.deepseek.com 申请"),
    Provider(id="qwen", label="通义千问（阿里云百炼）", kind="openai",
             base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", model="qwen-plus",
             key_env="DASHSCOPE_API_KEY", hint="阿里云百炼控制台申请"),
    Provider(id="kimi", label="Kimi（月之暗面）", kind="openai", base_url="https://api.moonshot.cn/v1",
             key_env="MOONSHOT_API_KEY", hint="国内 Key 用 api.moonshot.cn，海外 Key 用 api.moonshot.ai"),
    Provider(id="glm", label="智谱 GLM", kind="openai", base_url="https://open.bigmodel.cn/api/paas/v4",
             key_env="ZHIPUAI_API_KEY", hint="智谱开放平台申请"),
    Provider(id="custom", label="其他 OpenAI 兼容服务", kind="openai", hint="任何兼容 /chat/completions 的服务"),
]


class McpConfig(BaseModel):
    target: Literal["cloud", "desktop"] = "cloud"     # 连 ChatCut 云端，还是本机的 ChatCut Desktop
    url: str = CHATCUT_MCP_URL
    auth: Literal["oauth", "token", "none"] = "oauth"
    token: str = ""
    desktop: Endpoint | None = None                   # 选定的 Desktop 端点；None = 每次自动查找


class GatewayConfig(BaseModel):
    """本机模型网关（gateway.py）：Claude Code / ChatCut Desktop 通过它使用 Studio 里的模型。"""
    provider: str = "deepseek"
    model: str = ""                  # 空 = 用这个服务商在上面选好的模型
    small_model: str = ""            # Claude Code 后台小任务用的快速模型；空 = 同一个
    mode: Literal["auto", "anthropic", "openai"] = "auto"
    context_tokens: int = 128000     # 告诉 Claude Code 这个模型的上下文长度（它不认识第三方模型名）
    token: str = Field(default_factory=lambda: secrets.token_urlsafe(24))   # 本机访问网关的 Token
    backup: dict[str, str | None] = Field(default_factory=dict)            # 接入前 Claude Code 里的原值
    codex_backup: dict[str, str] = Field(default_factory=dict)              # 接入前 Codex config.toml 里 model / model_provider 的原行


class Settings(BaseModel):
    providers: list[Provider] = Field(default_factory=lambda: [p.model_copy() for p in PRESETS])
    style_provider: str = "deepseek"  # 风格优化用哪个模型
    agent_mode: Literal["claude_code", "api"] = "api"  # 剪辑 Agent 用 Claude Code 插件，还是 API 模型直连 ChatCut MCP
    agent_provider: str = "deepseek"
    mcp: McpConfig = Field(default_factory=McpConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)

    def provider(self, pid: str) -> Provider:
        for p in self.providers:
            if p.id == pid:
                return p
        raise KeyError(pid)

    def public(self) -> dict:
        d = self.model_dump(exclude={"providers", "mcp", "gateway"})
        d["gateway"] = self.gateway.model_dump(exclude={"token", "backup", "codex_backup"})
        d["providers"] = [p.public() for p in self.providers]
        d["mcp"] = {"target": self.mcp.target, "url": self.mcp.url, "auth": self.mcp.auth, "has_token": bool(self.mcp.token),
                    "desktop": mask_endpoint(self.mcp.desktop.model_dump()) if self.mcp.desktop else None}
        return d


MASK = "***"


def mask_endpoint(d: dict) -> dict:
    """端点里的请求头可能带 Token，给前端看时只留名字。"""
    return {**d, "headers": {k: MASK for k in d.get("headers") or {}}}


def _unmask(d: dict) -> dict:
    """前端回传的端点里请求头是 ***：从本机配置里按同一个地址取回真实值。"""
    if not any(v == MASK for v in (d.get("headers") or {}).values()):
        return d
    from .desktop import from_configs
    for e in from_configs():
        if e.transport == d.get("transport") and e.url == d.get("url"):
            return {**d, "headers": e.headers}
    return {**d, "headers": {k: v for k, v in d["headers"].items() if v != MASK}}


def _path() -> Path:
    return DATA_ROOT / "settings.json"


def load() -> Settings:
    f = _path()
    s = Settings.model_validate_json(f.read_text("utf-8")) if f.exists() else Settings()
    known = {p.id for p in s.providers}  # 新版本加的预设服务商补进旧设置
    s.providers += [p.model_copy() for p in PRESETS if p.id not in known]
    return s


def save(s: Settings) -> None:
    f = _path()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(s.model_dump_json(indent=2), "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(f)


def update(patch: dict) -> Settings:
    """合并前端提交的修改。api_key / token 为空字符串表示「不修改」，"-" 表示清除。"""
    s = load()
    for pp in patch.get("providers", []):
        p = s.provider(pp["id"])
        for k in ("base_url", "model", "anthropic_url"):
            if k in pp:
                setattr(p, k, pp[k].strip())
        if pp.get("api_key"):
            p.api_key = "" if pp["api_key"] == "-" else pp["api_key"].strip()
    for k in ("style_provider", "agent_mode", "agent_provider"):
        if k in patch:
            setattr(s, k, patch[k])
    if m := patch.get("mcp"):
        if "target" in m:
            s.mcp.target = m["target"]
        if "desktop" in m:            # None = 每次自动查找
            s.mcp.desktop = Endpoint(**_unmask(m["desktop"])) if m["desktop"] else None
        if "url" in m:
            s.mcp.url = m["url"].strip()
        if "auth" in m:
            s.mcp.auth = m["auth"]
        if m.get("token"):
            s.mcp.token = "" if m["token"] == "-" else m["token"].strip()
    if g := patch.get("gateway"):
        for k in ("provider", "model", "small_model", "mode"):
            if k in g:
                setattr(s.gateway, k, g[k].strip() if isinstance(g[k], str) else g[k])
        s.provider(s.gateway.provider)
    s = Settings.model_validate(s.model_dump())
    save(s)
    return s


def set_gateway_backup(backup: dict, field: str = "backup") -> None:
    s = load()
    setattr(s.gateway, field, backup)
    save(s)
