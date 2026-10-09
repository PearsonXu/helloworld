"""内置的 5 个常用剪辑风格。首次启动时写进风格库，用户可以修改、AI 优化，也可以随时恢复成预设。

前两个来自这个项目里做过的歌词 MV（mv_backgrounds/ 下的背景图）；后三个是常见的短视频类型。
"""
from __future__ import annotations

from .profile import (
    Audio, Branding, Captions, ColorGrade, Motion, Pacing, StyleProfile, Transitions,
)

PRESETS: dict[str, StyleProfile] = {
    "guofeng-lyric-mv": StyleProfile(
        name="清新国风歌词MV",
        description="静态风景插画 + 歌曲 + 带时间轴歌词的竖版抒情 MV，清新、留白、有电影感",
        aspect_ratio="9:16", resolution="1080x1920", fps=30,
        pacing=Pacing(avg_shot_seconds=8, cut_on_beat=False, hook_seconds=4,
                      notes="按歌词段落换图：主歌一张、副歌一张；换图点落在乐句开头"),
        captions=Captions(font="思源宋体", size="大", color="#FFFFFF", stroke="柔和黑色阴影，不加硬描边",
                          position="随画面变化", animation="模糊上浮淡入 0.4 秒，淡出 0.3 秒",
                          keyword_highlight=True, max_chars_per_line=10,
                          shine="关键词", shine_color="#FFE9B0", shine_seconds=0.9),
        transitions=Transitions(style="段落之间交叉溶解，副歌进入时用一次缓慢推近", duration_seconds=1.2),
        color_grade=ColorGrade(look="清新通透、低饱和哑光", notes="不压暗、不做复古泛黄；整支 MV 色调统一"),
        audio=Audio(music_ducking=False, target_lufs=-14, fade_in_seconds=1.5, fade_out_seconds=3),
        motion=Motion(ken_burns=True, intensity="轻微"),
        branding=Branding(),
        rules=["歌词避开画面主体（月亮、灯、蝴蝶等），放在留白区域", "副歌第一句关键词放大 1.2–1.3 倍或换成画面主色",
               "一句歌词一屏，语义断行，不拆词", "歌词提前 0.1 秒出现，和演唱对齐"],
        avoid=["闪白、旋转、缩放弹跳等花哨转场", "满屏文字或多行堆叠", "画面里出现水印和 logo"],
    ),
    "cinematic-minimal": StyleProfile(
        name="电影感极简风景",
        description="空镜、风景、氛围类短片：慢节奏、大留白、柔光和胶片颗粒，文字极少",
        aspect_ratio="9:16", resolution="1080x1920", fps=24,
        pacing=Pacing(avg_shot_seconds=5, cut_on_beat=False, hook_seconds=3, notes="镜头之间留呼吸感，不赶节奏"),
        captions=Captions(font="思源宋体", size="中", color="#F2EFE8", stroke="无", position="居中",
                          animation="字距收拢淡入 0.6 秒", keyword_highlight=False, max_chars_per_line=12),
        transitions=Transitions(style="硬切为主，情绪转折处用黑场过渡", duration_seconds=0.8),
        color_grade=ColorGrade(look="电影感哑光、轻微抬黑", notes="加细胶片颗粒和轻微暗角；冷暖对比克制"),
        audio=Audio(music_ducking=True, target_lufs=-16, fade_in_seconds=2, fade_out_seconds=3),
        motion=Motion(ken_burns=True, intensity="轻微"),
        rules=["画面上方或下方留出至少 1/3 空白", "片尾留 2 秒空镜再结束"],
        avoid=["贴纸、表情包、花字", "快速变速"],
    ),
    "talking-head": StyleProfile(
        name="口播知识分享",
        description="单人对着镜头讲解的知识类、观点类口播视频，信息密度高、字幕清楚",
        aspect_ratio="9:16", resolution="1080x1920", fps=30,
        pacing=Pacing(avg_shot_seconds=3, cut_on_beat=False, hook_seconds=3,
                      notes="剪掉口误、停顿超过 0.4 秒的空白和重复句；每 5–8 秒插一次 B-roll 或推近"),
        captions=Captions(font="得意黑", size="大", color="#FFFFFF", stroke="黑色 3px 描边", position="底部",
                          animation="逐句弹出", keyword_highlight=True, max_chars_per_line=14),
        transitions=Transitions(style="硬切；换话题时用 0.2 秒推近", duration_seconds=0.2),
        color_grade=ColorGrade(look="自然肤色、明亮干净"),
        audio=Audio(music_ducking=True, target_lufs=-14, fade_in_seconds=0.5, fade_out_seconds=1),
        motion=Motion(ken_burns=False, intensity="无"),
        rules=["开头 3 秒直接抛出结论或问题", "关键词用黄色 #FFD54A 高亮", "人声降噪，背景音乐比人声低 18 dB 以上"],
        avoid=["长时间不切换画面", "字幕遮挡嘴部"],
    ),
    "beat-montage": StyleProfile(
        name="节奏卡点混剪",
        description="旅行、活动、产品等素材的快节奏卡点混剪，跟着音乐鼓点切换",
        aspect_ratio="9:16", resolution="1080x1920", fps=30,
        pacing=Pacing(avg_shot_seconds=0.8, cut_on_beat=True, hook_seconds=2,
                      notes="主歌每拍一切，副歌半拍一切；高潮前留一个 1–2 秒的长镜头蓄力"),
        captions=Captions(enabled=False),
        transitions=Transitions(style="硬切为主，重拍处用快速推拉或甩镜", duration_seconds=0.15),
        color_grade=ColorGrade(look="高对比、偏鲜艳", notes="所有素材统一色温"),
        audio=Audio(music_ducking=False, target_lufs=-12, fade_in_seconds=0, fade_out_seconds=1.5),
        motion=Motion(ken_burns=True, intensity="适中"),
        rules=["剪切点严格对齐鼓点，误差不超过 1 帧", "同一素材不重复使用"],
        avoid=["慢速溶解转场", "说明性字幕"],
    ),
    "daily-vlog": StyleProfile(
        name="生活Vlog",
        description="日常记录、探店、旅行 Vlog：自然叙事、轻松温暖，保留现场声",
        aspect_ratio="9:16", resolution="1080x1920", fps=30,
        pacing=Pacing(avg_shot_seconds=2.5, cut_on_beat=False, hook_seconds=3, notes="按时间顺序叙事，场景切换时放一个环境空镜"),
        captions=Captions(font="霞鹜文楷", size="中", color="#FFFFFF", stroke="浅灰阴影", position="底部",
                          animation="淡入淡出", keyword_highlight=False, max_chars_per_line=16),
        transitions=Transitions(style="硬切为主，换场景用 0.3 秒交叉溶解", duration_seconds=0.3),
        color_grade=ColorGrade(look="温暖明亮、轻微偏暖"),
        audio=Audio(music_ducking=True, target_lufs=-14, fade_in_seconds=1, fade_out_seconds=2),
        motion=Motion(ken_burns=False, intensity="轻微"),
        rules=["保留有意思的现场声和对话", "开头放当天最好看的一个镜头"],
        avoid=["过多特效和贴纸"],
    ),
}

MAX_STYLES = 5
