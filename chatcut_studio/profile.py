"""剪辑风格档案（StyleProfile）与 SKILL.md 模板渲染。

风格档案是界面、Claude 和 SKILL.md 之间的统一数据结构：
问卷 / 从成片学习 都先产出 StyleProfile，再由它生成 SKILL.md。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, Field


class Pacing(BaseModel):
    avg_shot_seconds: float = Field(3.0, description="平均镜头时长（秒）")
    cut_on_beat: bool = Field(True, description="是否按音乐节拍剪切")
    hook_seconds: float = Field(3.0, description="开头抓人片段的时长（秒）")
    notes: str = Field("", description="节奏补充说明")


class Captions(BaseModel):
    enabled: bool = True
    font: str = Field("思源宋体", description="字体")
    size: Literal["小", "中", "大", "特大"] = "中"
    color: str = Field("#FFFFFF", description="主色")
    stroke: str = Field("无", description="描边/阴影，如 '黑色 2px 描边'")
    position: Literal["底部", "居中", "顶部", "左侧竖排", "右侧竖排", "随画面变化"] = "底部"
    animation: str = Field("淡入淡出", description="出入场动画")
    keyword_highlight: bool = Field(False, description="关键词放大 / 换色")
    max_chars_per_line: int = Field(14, description="每行最多字数")
    shine: Literal["无", "关键词", "整句", "仅副歌"] = Field("无", description="闪光扫过：一道高光从左到右扫过文字")
    shine_color: str = Field("#FFE9B0", description="闪光颜色")
    shine_seconds: float = Field(0.8, description="闪光扫过一次的时长（秒）")


class Transitions(BaseModel):
    style: str = Field("硬切为主，段落间交叉溶解", description="转场风格")
    duration_seconds: float = Field(0.5, description="转场时长（秒）")


class ColorGrade(BaseModel):
    look: str = Field("自然通透", description="整体调色风格")
    notes: str = Field("", description="调色补充，如 '暗部偏青、高光偏暖'")


class Audio(BaseModel):
    music_ducking: bool = Field(True, description="人声出现时压低背景音乐")
    target_lufs: float = Field(-14.0, description="目标响度（LUFS）")
    fade_in_seconds: float = 1.0
    fade_out_seconds: float = 2.0


class Motion(BaseModel):
    ken_burns: bool = Field(True, description="静态图缓慢推拉")
    intensity: Literal["无", "轻微", "适中", "强烈"] = "轻微"


class Branding(BaseModel):
    intro: str = Field("", description="片头要求，留空表示无")
    outro: str = Field("", description="片尾要求，留空表示无")
    watermark: str = Field("", description="水印 / logo 要求，留空表示无")


class StyleProfile(BaseModel):
    name: str = Field(..., description="风格名称，如 '清新国风歌词MV'")
    description: str = Field(..., description="一句话说明这个风格适合什么视频、什么时候用")
    aspect_ratio: Literal["9:16", "16:9", "1:1", "4:5"] = "9:16"
    resolution: str = "1080x1920"
    fps: int = 30
    pacing: Pacing = Field(default_factory=Pacing)
    captions: Captions = Field(default_factory=Captions)
    transitions: Transitions = Field(default_factory=Transitions)
    color_grade: ColorGrade = Field(default_factory=ColorGrade)
    audio: Audio = Field(default_factory=Audio)
    motion: Motion = Field(default_factory=Motion)
    branding: Branding = Field(default_factory=Branding)
    rules: list[str] = Field(default_factory=list, description="必须遵守的额外规则")
    avoid: list[str] = Field(default_factory=list, description="明确不要的做法")


def slugify(name: str) -> str:
    """把风格名转成 Skill 目录名（小写字母、数字、连字符）。中文转成拼音太重，这里退回到 style-<hash>。"""
    ascii_part = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_part.lower()).strip("-")
    if len(slug) < 3:
        import hashlib

        slug = "style-" + hashlib.sha1(name.encode()).hexdigest()[:8]
    return slug[:60]


SHINE_SCOPE = {"关键词": "每句的关键词", "整句": "整句歌词", "仅副歌": "副歌歌词"}


def _yn(v: bool) -> str:
    return "是" if v else "否"


SAFETY_RULES = """\
- 花费 ChatCut 积分（AI 生成视频/图片/配乐、转写、翻译）、启动云端渲染/导出、删除项目/素材/时间线、上传私密素材之前，**必须先向用户确认**。
- 每完成一个大步骤，用 ChatCut 的校验工具确认改动在编辑器里可见，再继续下一步。
- 不确定的风格细节，以本 Skill 的参数为准；本 Skill 没写到的，保持 ChatCut 默认，不要自行发挥。"""


def render_skill_md(p: StyleProfile, slug: str | None = None) -> str:
    """确定性模板：同一份风格档案永远生成同样的 SKILL.md，方便比对版本差异。"""
    slug = slug or slugify(p.name)
    desc = p.description.replace("\n", " ").strip()
    c = p.captions
    lines = [
        "---",
        f"name: {slug}",
        f"description: {desc} 在 ChatCut 中按「{p.name}」风格剪辑时使用。",
        "---",
        "",
        f"# {p.name}",
        "",
        desc,
        "",
        "## 风格参数",
        "",
        "| 项目 | 设定 |",
        "|---|---|",
        f"| 画幅 / 分辨率 / 帧率 | {p.aspect_ratio} · {p.resolution} · {p.fps}fps |",
        f"| 平均镜头时长 | {p.pacing.avg_shot_seconds:g} 秒 |",
        f"| 按节拍剪切 | {_yn(p.pacing.cut_on_beat)} |",
        f"| 开头抓人片段 | {p.pacing.hook_seconds:g} 秒 |",
    ]
    if p.pacing.notes:
        lines.append(f"| 节奏补充 | {p.pacing.notes} |")
    if c.enabled:
        lines += [
            f"| 字幕字体 / 字号 | {c.font} · {c.size} |",
            f"| 字幕颜色 / 描边 | {c.color} · {c.stroke} |",
            f"| 字幕位置 | {c.position}，每行不超过 {c.max_chars_per_line} 字 |",
            f"| 字幕动画 | {c.animation} |",
            f"| 关键词突出 | {_yn(c.keyword_highlight)} |",
        ]
        if c.shine != "无":
            lines.append(f"| 闪光扫过 | {SHINE_SCOPE[c.shine]}，{c.shine_color}，每次约 {c.shine_seconds:g} 秒 |")
    else:
        lines.append("| 字幕 | 不加字幕 |")
    lines += [
        f"| 转场 | {p.transitions.style}（约 {p.transitions.duration_seconds:g} 秒） |",
        f"| 调色 | {p.color_grade.look}{'；' + p.color_grade.notes if p.color_grade.notes else ''} |",
        f"| 静态图运动 | {'缓慢推拉，强度' + p.motion.intensity if p.motion.ken_burns else '不加运动'} |",
        f"| 音频 | 目标响度 {p.audio.target_lufs:g} LUFS；人声压低音乐：{_yn(p.audio.music_ducking)}；"
        f"淡入 {p.audio.fade_in_seconds:g}s / 淡出 {p.audio.fade_out_seconds:g}s |",
    ]
    b = p.branding
    for label, v in (("片头", b.intro), ("片尾", b.outro), ("水印", b.watermark)):
        if v:
            lines.append(f"| {label} | {v} |")
    lines += [
        "",
        "## 操作步骤",
        "",
        f"1. 读取当前项目和时间线；如果画幅不是 {p.aspect_ratio}，先把项目设为 {p.aspect_ratio}（{p.resolution}，{p.fps}fps）。",
        "2. 导入用户指定的素材（视频、图片、音频），按用户给的顺序或歌词段落排到主视频轨。",
        f"3. 按平均 {p.pacing.avg_shot_seconds:g} 秒一镜调整片段长度"
        + ("，剪切点对齐音乐节拍" if p.pacing.cut_on_beat else "")
        + f"；前 {p.pacing.hook_seconds:g} 秒放最抓人的画面。",
    ]
    step = 4
    if p.motion.ken_burns:
        lines.append(f"{step}. 给静态图片加缓慢推拉（{p.motion.intensity}），方向交替，避免同向重复。")
        step += 1
    if c.enabled:
        lines.append(
            f"{step}. 添加字幕：{c.font}、{c.size}号、{c.color}、{c.stroke}，位置{c.position}，"
            f"{c.animation}" + ("，关键词放大或换色突出" if c.keyword_highlight else "") + "。断行按语义，不拆词。"
        )
        step += 1
        if c.shine != "无":
            lines.append(
                f"{step}. 闪光扫过：字幕出现后，用一道 {c.shine_color} 的柔和高光从左到右扫过{SHINE_SCOPE[c.shine]}，"
                f"约 {c.shine_seconds:g} 秒，只扫一次；高光带边缘柔化，不要闪白整屏。"
            )
            step += 1
    lines += [
        f"{step}. 转场：{p.transitions.style}。",
        f"{step + 1}. 调色：{p.color_grade.look}。",
        f"{step + 2}. 音频：整体响度对齐 {p.audio.target_lufs:g} LUFS，首尾淡入淡出。",
        f"{step + 3}. 逐段校验画面、字幕、音画同步；列出改动摘要给用户。",
        f"{step + 4}. 用户确认后再导出。",
    ]
    if p.rules:
        lines += ["", "## 必须遵守", ""] + [f"- {r}" for r in p.rules]
    if p.avoid:
        lines += ["", "## 不要这样做", ""] + [f"- {r}" for r in p.avoid]
    lines += ["", "## 安全规则", "", SAFETY_RULES, ""]
    return "\n".join(lines)
