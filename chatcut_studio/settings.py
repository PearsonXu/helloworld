"""本地设置：模型服务商（API Key）、Agent 连接方式、ChatCut MCP 连接。

保存在 data/settings.json（已 gitignore，权限 600）。API Key 永远不会原样返回给前端。
模型名变化很快，这里只预填 base_url；模型请在界面里点「获取模型列表」后选择。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

DATA_ROOT = Path(os.environ.get("CHATCUT_STUDIO_DATA", Path(__file__).parent / "data"))
CHATCUT_MCP_URL = "https://api.chatcut.io/api/external-mcp/mcp"


class Provider(BaseModel):
    id: str
    label: str
    kind: Literal["anthropic", "openai"]  # openai = OpenAI 兼容的 /chat/completions
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    key_env: str = ""  # 没填 api_key 时，从这个环境变量读取
    hint: str = ""

    def key(self) -> str:
        return self.api_key or (os.environ.get(self.key_env, "") if self.key_env else "")

    def public(self) -> dict:
        k = self.key()
        d = self.model_dump(exclude={"api_key"})
        d["has_key"] = bool(k)
        d["key_preview"] = (k[:3] + "…" + k[-4:]) if len(k) > 10 else ("已设置" if k else "")
        d["key_from_env"] = bool(not self.api_key and k)
        return d


PRESETS: list[Provider] = [
    Provider(id="claude", label="Claude（Anthropic API）", kind="anthropic", model="claude-opus-5-5",
             key_env="ANTHROPIC_API_KEY", hint="https://platform.claude.com 申请 API Key"),
    Provider(id="deepseek", label="DeepSeek", kind="openai", base_url="https://api.deepseek.com",
             key_env="DEEPSEEK_API_KEY", hint="https://platform.deepseek.com 申请；deepseek-chat 等旧名已于 2026/07/24 停用"),
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
    url: str = CHATCUT_MCP_URL
    auth: Literal["oauth", "token", "none"] = "oauth"
    token: str = ""


class Settings(BaseModel):
    providers: list[Provider] = Field(default_factory=lambda: [p.model_copy() for p in PRESETS])
    style_provider: str = "claude"  # 风格优化用哪个模型
    agent_mode: Literal["claude_code", "api"] = "claude_code"  # 剪辑 Agent 用 Claude Code 插件，还是 API 模型直连 ChatCut MCP
    agent_provider: str = "deepseek"
    mcp: McpConfig = Field(default_factory=McpConfig)

    def provider(self, pid: str) -> Provider:
        for p in self.providers:
            if p.id == pid:
                return p
        raise KeyError(pid)

    def public(self) -> dict:
        d = self.model_dump(exclude={"providers", "mcp"})
        d["providers"] = [p.public() for p in self.providers]
        d["mcp"] = {"url": self.mcp.url, "auth": self.mcp.auth, "has_token": bool(self.mcp.token)}
        return d


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
        for k in ("base_url", "model"):
            if k in pp:
                setattr(p, k, pp[k].strip())
        if pp.get("api_key"):
            p.api_key = "" if pp["api_key"] == "-" else pp["api_key"].strip()
    for k in ("style_provider", "agent_mode", "agent_provider"):
        if k in patch:
            setattr(s, k, patch[k])
    if m := patch.get("mcp"):
        if "url" in m:
            s.mcp.url = m["url"].strip()
        if "auth" in m:
            s.mcp.auth = m["auth"]
        if m.get("token"):
            s.mcp.token = "" if m["token"] == "-" else m["token"].strip()
    s = Settings.model_validate(s.model_dump())
    save(s)
    return s
