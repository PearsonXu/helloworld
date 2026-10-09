"""AI 优化风格：用户用大白话说哪里不满意，模型给出修改后的风格档案和改动清单，用户确认后才保存。

可以用设置里任意一个模型服务商（Claude、DeepSeek、通义千问、Kimi、GLM……）。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from . import settings
from .profile import StyleProfile
from .providers import chat_json


class Proposal(BaseModel):
    profile: StyleProfile = Field(..., description="修改后的完整风格档案")
    changes: list[str] = Field(..., description="逐条说明改了什么、为什么，每条一句话")


SYSTEM = """你是资深短视频剪辑指导，负责根据用户反馈优化一份剪辑风格档案。
规则：
- 只改和反馈相关的字段，其他字段原样保留；风格名称 name 不要改。
- 数值要具体（秒、字号档位、LUFS、颜色值），不要写"适当""合适"。
- 反馈里提到的新要求，能对应到具体字段就改字段，对应不上就写进 rules（必须遵守）或 avoid（不要这样做）。
- rules / avoid 每条都是可执行的短句；删掉和新反馈矛盾的旧条目。
- changes 用中文逐条说明改动，没有改动就说明原因。"""


def build_user_prompt(profile: StyleProfile, feedback: str, context: str = "") -> str:
    parts = [f"当前风格档案（JSON）：\n{profile.model_dump_json(indent=2)}", f"用户反馈：\n{feedback.strip()}"]
    if context.strip():
        parts.append(f"这次剪辑的背景（供参考）：\n{context.strip()}")
    return "\n\n".join(parts)


async def propose(profile: StyleProfile, feedback: str, context: str = "") -> tuple[Proposal, str]:
    s = settings.load()
    p = s.provider(s.style_provider)
    result = await chat_json(p, SYSTEM, build_user_prompt(profile, feedback, context), Proposal)
    result.profile.name = profile.name  # 名称不允许被模型改掉，避免风格库错乱
    return result, f"{p.label} · {p.model}"
