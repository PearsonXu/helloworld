"""调用 Claude API：把风格档案润色成 SKILL.md，以及从 ChatCut 时间线反推风格档案。

没有配置 API 凭据时，调用方应退回到 profile.render_skill_md 的离线模板。
"""
from __future__ import annotations

import json
import os

import anthropic

from .profile import SAFETY_RULES, StyleProfile, render_skill_md, slugify

MODEL = os.environ.get("CHATCUT_STUDIO_MODEL", "claude-opus-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMUnavailable(RuntimeError):
    pass


def has_credentials() -> bool:
    return any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")) or os.path.exists(
        os.path.expanduser("~/.config/anthropic")
    )


def _client() -> anthropic.Anthropic:
    if not has_credentials():
        raise LLMUnavailable("未配置 Claude API 凭据（ANTHROPIC_API_KEY 或 `ant auth login`）")
    return anthropic.Anthropic()


def _check(resp) -> None:
    if resp.stop_reason == "refusal":
        raise LLMUnavailable("模型拒绝了这次请求")
    if resp.stop_reason == "max_tokens":
        raise LLMUnavailable("输出被截断，请缩短输入后重试")


SKILL_SYSTEM = f"""你是资深短视频剪辑指导，负责把一份结构化的剪辑风格档案写成 ChatCut 剪辑 agent 能直接执行的 Agent Skill（SKILL.md）。

要求：
- 输出完整的 SKILL.md 文本，不要任何解释，不要包在代码块里。
- 开头是 YAML frontmatter，只有 name 和 description 两个字段；name 必须原样使用给定的 slug；description 一句话说明这个风格是什么、什么时候用。
- 正文用中文：风格概述、参数表、按顺序编号的操作步骤（对应 ChatCut 时间线上的具体动作）、必须遵守、不要这样做、验收清单。
- 参数要具体到数值，避免"适当""合适"这类模糊词；档案里没有的信息不要编造。
- 末尾原样保留以下安全规则：
{SAFETY_RULES}"""


def generate_skill_md(p: StyleProfile) -> str:
    client = _client()
    draft = render_skill_md(p)
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        system=SKILL_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"slug: {slugify(p.name)}\n\n风格档案（JSON）：\n{p.model_dump_json(indent=2)}\n\n"
                    f"下面是按模板机械生成的草稿，请在它的基础上改写得更清楚、更可执行：\n\n{draft}"
                ),
            }
        ],
    )
    _check(resp)
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text.startswith("---"):
        raise LLMUnavailable("模型输出缺少 frontmatter")
    return text + "\n"


LEARN_SYSTEM = """你是资深短视频剪辑指导。用户会给你一份 ChatCut 时间线数据（通过 ChatCut MCP 读取，JSON），
以及可选的文字说明。请从中归纳出可复用的剪辑风格档案：
- 镜头时长取实际片段时长的中位数，按节拍剪切看剪切点是否与音乐节拍/字幕节奏对齐；
- 字幕、转场、调色、运动、音频参数尽量从数据里读出具体值；读不出来的保持默认值，不要猜；
- rules / avoid 写成可执行的短句。"""


def learn_profile(timeline: str, notes: str = "") -> StyleProfile:
    client = _client()
    try:
        json.loads(timeline)
    except json.JSONDecodeError as e:
        raise ValueError(f"时间线不是合法 JSON：{e}") from e
    resp = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        system=LEARN_SYSTEM,
        messages=[{"role": "user", "content": f"时间线：\n{timeline}\n\n补充说明：{notes or '（无）'}"}],
        output_format=StyleProfile,
    )
    _check(resp)
    if resp.parsed_output is None:
        raise LLMUnavailable("模型没有返回有效的风格档案")
    return resp.parsed_output
