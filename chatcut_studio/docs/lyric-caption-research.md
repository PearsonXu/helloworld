# 歌曲 MV 字幕风格：调研与接入可行性

结论先行：**可行，而且第一步几乎不用写新渲染代码。** 组织里已有的 `lyric-mv` 技能在本环境实测可用（11 套中文主题、20 种进场动画），配合 ffmpeg 自带的 libass 做 KTV 逐字字幕，就能覆盖绝大多数中文歌词 MV 风格。只有「故障卡点、3D、音频可视化」这类重效果才需要引入网页渲染引擎。

## 一、找到的现有程序

| 项目 | 技术 | 能做什么 | 许可 | 结论 |
|---|---|---|---|---|
| **lyric-mv**（组织已有技能） | Python + Pillow + ffmpeg | 11 套中文主题；关键词放大换字体、从背景取色；4 种构图（一行、堆叠、错落、侧挂竖排）；20 种进场、8 种停留、11 种出场动画；国风主题带影视字幕规范、自动反色、避让主体、逐帧质检 | 组织内部技能，字体均为 OFL | **首选。** 已实测，见下方样张 |
| [PyonFX](https://github.com/CoffeeStraw/PyonFX) | Python 生成 ASS | 动漫式卡拉OK特效（KFX）：逐字填色、逐帧变换、图形、粒子 | LGPL-3.0（作为库调用没问题） | KTV/特效字幕的进阶方案 |
| ffmpeg + libass（ASS 字幕） | 本机已有 | `\kf` 逐字扫色、`\blur \bord` 发光、`\t` 变换、`\fad` 淡入淡出；渲染极快 | — | **已实测**，做 KTV 和霓虹基础款 |
| [HyperFrames](https://github.com/heygen-com/hyperframes)（HeyGen） | HTML/CSS/GSAP → MP4 | 网页能做的效果都能做：故障、3D、粒子、音频可视化；自带给 AI agent 用的 skill | Apache-2.0 | 重效果风格的候选引擎 |
| [Remotion](https://www.remotion.dev/docs/captions/create-tiktok-style-captions) | React → MP4 | TikTok 式逐词字幕（`createTikTokStyleCaptions`）、成熟生态 | **4 人以上公司需付费**（约 $25/席位/月起） | 能力强，但要注意授权 |
| [pycaps](https://github.com/francozanardi/pycaps) | Python + CSS + Playwright | CSS 写字幕样式；逐词高亮；按语义打标签上色；淡入、弹出、滑入、打字机 | MIT，222★ | 逐词「口播式」字幕好用，中文支持未说明 |
| [lyric-mv-kit](https://github.com/EOMZON/lyric-mv-kit) | Python + Pillow | 8 种风格：编辑留白、专辑胶片、电影宽银幕、动感海报、霓虹、极光、终端故障、万花筒；带中文字体 | MIT（0★，很新） | 可借鉴风格设计和音频能量驱动的思路 |
| [kinetic-typography-template](https://github.com/earlvanze/kinetic-typography-template) | three.js + Headless Chromium | 段落驱动的动感排版（slam、stair、ledger…）、卡拍镜头、辉光、色差 | MIT | 动感卡点风的参考，不支持中文 |
| [kinetic-typography-skills](https://github.com/iart-ai/kinetic-typography-skills) | 给 AI agent 的 skill | 逐字/逐词/逐行错落、拆字、可变字重、路径文字、歌词卡片 | MIT | 可以借它的提示词思路 |
| [Marat-Lyric-Video](https://github.com/TemporariestUsername/Marat-Lyric-Video) | Remotion | timing.json 驱动；**浏览器里点按打拍子定时间轴** | 未核实 | 「手动打点」工具值得借鉴 |
| ChatCut 自带字幕 | ChatCut | 20 多种字幕预设（Netflix 简洁、TikTok 跳动等）、可存为自定义模板；动态图形可做标题动画 | 商业 | 口播类够用；歌词逐字特效能力未知 |

逐字时间轴（做 KTV 风格必需）的工具：[lyrics-transcriber](https://pypi.org/project/lyrics-transcriber)（Whisper → ASS/LRC）、[lyric-align](https://pypi.org/project/lyric-align/)（中日韩文字按单字对齐）、stable-ts、WhisperX，以及先用 Demucs 分离人声以提高准确率。歌声识别的准确率一般，结果需要人工复核。

## 二、风格清单：每种用什么实现

| # | 风格 | 关键效果 | 实现 | 时间轴 |
|---|---|---|---|---|
| 1 | 清新呢喃 | 逐字淡入、模糊上浮、留白 | lyric-mv `fresh` / `whisper` | 逐句 LRC |
| 2 | 国风影视 | 影视字幕规范、自动反色、避让主体、副歌「剑气」扫光 | lyric-mv `guofeng` | 逐句 LRC |
| 3 | 杂志海报 | 关键词巨大、错落或侧挂竖排、字距收拢 | lyric-mv `editorial` / `poster` | 逐句 LRC |
| 4 | 墨韵书法 | 墨迹凝聚、毛笔或行草字体 | lyric-mv `ink` / `cursive` | 逐句 LRC |
| 5 | 夜色霓虹 | 灯光般亮起、外发光 | lyric-mv `neon`，或 ASS `\blur \bord` | 逐句 LRC |
| 6 | **KTV 逐字填色** | 逐字从白色扫成主题色 | ASS `\kf` + ffmpeg（已实测）；进阶用 PyonFX | **逐字** |
| 7 | 故障赛博 | RGB 分离、卡拍抖动 | lyric-mv `glitch` 动画（基础）/ HyperFrames（完整） | 逐句 + 节拍 |
| 8 | 动感卡点大字 | 逐词砸屏、随鼓点缩放、镜头卡拍 | HyperFrames 或 Remotion | 逐字 + 节拍 |
| 9 | 音频可视化 / 3D | 频谱、粒子、立体字 | HyperFrames + three.js | 节拍 / 能量 |

1–5 直接用现成主题；6 已经验证；7–9 要新开发。

## 三、实测（本环境）

- **lyric-mv**：开源字体下载 12 款成功；用我们的背景图一次生成了 11 套主题样张：`samples/lyric_mv_themes_moon.jpg`、`samples/lyric_mv_themes_window.jpg`。分词库 jieba 在这里装不上，不过它是可选依赖，只影响自动断行，手动用 `|` 断行即可。
- **ASS / libass**：ffmpeg 6.1 自带 `ass` 滤镜。逐字扫色和霓虹发光在 4 秒片段上正常，见 `samples/ass_ktv_neon.jpg`（三帧分别是 0.6s、1.2s、2.2s），字幕源文件是 `samples/ass_ktv_neon_sample.ass`。样张里的时间是演示用的假时间。

## 四、怎么接进 ChatCut Studio

```
StyleProfile
 └─ lyrics（新增）: engine = lyric-mv | ass | web | chatcut
                    theme, 进场/停留/出场动画, 关键词强调, 构图, 时间轴粒度（逐句/逐字）
        │
        ▼
本地歌词渲染器（新增，chatcut_studio/render/）
  背景图 + 音频 + LRC/逐字歌词 ──► lyric-mv：整支 MV（mp4）
                                 └► ASS：KTV 字幕层（透明 ProRes 4444 / WebM 透明通道）
        │
        ▼
交给 ChatCut：作为素材导入项目（会上传，界面先确认），在 ChatCut 里继续剪、加片头片尾、导出
```

- **整支 MV vs 透明字幕层**：背景图是我们自己的，所以 lyric-mv 直接出整支 MV 最省事；要在 ChatCut 里换背景、调节奏，就出透明字幕层叠加。lyric-mv 目前只能出合成好的整片，出透明层需要改它的输出部分；ASS 本身就能直接出透明层。
- **AI 优化照样适用**：字幕参数并进风格档案后，「字幕太花」「关键词不够突出」这类反馈，AI 会直接改主题和动画字段（lyric-mv 的反馈对照表可以直接写进优化提示词）。
- **ChatCut 原生字幕**：口播类风格继续用 ChatCut 自带的字幕模板。拿到 ChatCut 工具清单后，再把档案里的字幕字段映射过去。

## 五、风险与注意

1. **时间轴是成败关键**：逐句 LRC 很容易从音乐平台导出；逐字时间轴要么用平台的逐字歌词格式，要么自动对齐后人工复核。建议在界面里加一个「边听边按键打点」的小工具（参考 Marat-Lyric-Video）。
2. **授权**：Remotion 对 4 人以上公司收费；PyonFX 是 LGPL；HyperFrames 是 Apache-2.0；字体用 OFL 开源字体可以商用。公开发布 MV 还需要歌曲和歌词的授权。
3. **lyric-mv 是组织技能**：应从技能目录调用，不要把代码复制进本仓库。
4. **渲染耗时**：lyric-mv 的模糊类动画单核约 4 秒渲染 1 秒视频，一首 4 分钟的歌要 5–15 分钟；ASS 几乎是实时。

## 六、建议的实施顺序

| 阶段 | 内容 | 工作量 |
|---|---|---|
| 1 | 接入 lyric-mv：风格档案加 `lyrics` 字段，5 个内置风格映射到主题，界面加「用我的背景图生成样张」和「渲染 MV」 | 2–3 天 |
| 2 | KTV 逐字：ASS 渲染、透明字幕层输出、支持逐字歌词格式；可选自动对齐和打点工具 | 2–3 天 |
| 3 | 重效果风格（故障、卡点、3D、音频可视化）：评估 HyperFrames，出透明层交给 ChatCut | 1–2 周，可选 |

## 补充：更多开源风格来源

| 类别 | 项目 | 能提供的风格 | 许可 | 怎么用 |
|---|---|---|---|---|
| 歌词播放器 | [AMLL（类 Apple Music 歌词）](https://github.com/amll-dev/applemusic-like-lyrics) | 逐词渐亮、发光，非当前行虚化，弹簧滚动，流体背景；支持 LRC、YRC、QRC、TTML | npm 核心包标注 **AGPL-3.0** | 只借鉴效果，自己实现；配套的逐词歌词库（AMLL TTML DB）和逐词打轴工具（AMLL TTML Tool）可以参考 |
| 歌词播放器 | [SPlayer](https://deepwiki.com/HHCL233/SPlayer/4-lyric-system) | 两套逐字歌词渲染；能读网易云 YRC、QQ 音乐 QRC 逐字歌词 | 未核实 | 参考逐字歌词的获取和解析 |
| 歌词格式 | [Lyricify.Lyrics.Helper](https://www.nuget.org/packages/Lyricify.Lyrics.Helper) | 解析、生成 LRC、QRC、YRC 等逐字格式 | 未核实 | 逐字时间轴的格式转换 |
| 特效字幕 | [Aegisub Karaoke Templater](https://aegisub.org/docs/latest/automation/karaoke_templater/) | 动漫式卡拉OK特效的模板语言 | 随 Aegisub（BSD） | 写好模板后批量生成 ASS |
| 特效字幕 | TCAX 脚本合集（[GitHub aegisub 话题](https://www.github.com/topics/aegisub)） | 287 个特效字幕脚本 | 各脚本不同 | 当效果素材库挑选 |
| 网页动画 | [GSAP 3.13+](https://gsap.com/blog/3-13/) | SplitText 拆字、ScrambleText 乱码解密、各种缓动 | 自有免费许可，可商用，但不能做竞品 | 给 HyperFrames 做动画 |
| 网页动画 | [anime.js v4](https://www.mintlify.com/juliangarnier/anime/api/text-utils) | `text.split()` 按字、词、行拆分，做错落动画 | MIT | 同上 |
| 网页组件 | [React Bits](https://github.com/0xyza/react-bits) | 110 多个组件，文字类有拆字、模糊聚焦、闪光扫过、乱码解密、故障等 | 需核实（各处标注不一） | 挑效果参考或移植 |
| 网页组件 | [Magic UI](https://www.pkgpulse.com/guides/react-bits-vs-aceternity-magic-ui-2026) | 150 多个组件，含多种文字动画 | 开源，具体许可未核实 | 同上 |
| 音频可视化 | [Butterchurn](https://github.com/jberg/butterchurn)（MilkDrop 的 WebGL 版） | 海量迷幻音频可视化预设 | MIT | 做「迷幻 / 电子」风格的动态背景 |
| 音频可视化 | [Remotion 音频可视化](https://www.remotion.dev/docs/audio/visualization) | 频谱条、音波（audiogram 模板） | 受 Remotion 许可约束 | — |
| 打轴工具 | [歌词滚动姬 lrc-maker](https://github.com/magic-akari/lrc-maker) | 网页里边听边按空格打时间轴，导出 LRC | MIT，约 780★ | **可以直接嵌进我们的界面** |

由此可以再加入风格清单的：

| # | 风格 | 效果 | 实现 |
|---|---|---|---|
| 10 | Apple Music 逐词发光 | 当前词逐渐亮起并发光，其他行虚化 | ASS（`\kf` + 发光层）近似实现；网页引擎可完整实现（自己实现，不用 AGPL 代码） |
| 11 | 乱码解密 | 字符先乱跳再定格成歌词 | GSAP ScrambleText；ASS 可逐帧近似 |
| 12 | 闪光扫过 | 一道高光从左扫到右 | **已接入**：`lyrics.py`，ASS `\clip` + `\t` 多层光带（见 `samples/shine_sweep.jpg`） |
| 13 | 迷幻音频背景 | MilkDrop 预设随音乐变化，叠加歌词 | Butterchurn 渲染背景 + 任一字幕引擎 |
