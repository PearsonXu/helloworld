# ChatCut Studio（原型）

给 [ChatCut](https://chatcut.io/) 套一层简易操作界面：连接好模型，选一个常用剪辑风格，交给 Agent 在 ChatCut 里剪。

| 页面 | 做什么 |
|---|---|
| ① 连接设置 | 选择剪辑 Agent 的驱动方式：**Claude Code 插件**，或 **API 模型直连 ChatCut**（Claude API、DeepSeek、通义千问、Kimi、智谱 GLM、任意 OpenAI 兼容服务）。填 API Key、一键获取模型列表；连接 ChatCut MCP 并列出全部工具及其权限策略；Claude Code 方式的体检和插件安装 |
| ② 常用风格 | 内置 5 个常用风格（上限 5 个）。可以手动改参数，也可以用大白话写反馈，让 AI 给出修改建议，确认后保存为新版本；支持版本历史回退、恢复预设、复制为新风格、安装到 Claude Code |
| 歌词字幕 | 字幕参数里可以设「闪光扫过」（作用于关键词 / 整句 / 仅副歌，颜色和时长可调）；风格页可以用当前参数在背景图上「生成样张」；一键剪辑页可以按风格**在本地渲染整首歌**：整支视频（mp4），或透明字幕层（ProRes 4444 .mov，导入 ChatCut 叠加） |
| ③ 一键剪辑 | 选风格 + 写任务 + 列素材，交给 Agent 执行；会花积分 / 上传 / 删除 / 导出的操作先在页面上弹确认。剪完可以直接写反馈，回到 ② 优化这次用的风格 |

## 内置风格

| 风格 | 来源 |
|---|---|
| 清新国风歌词MV | 本项目做过的歌词 MV（`mv_backgrounds/`） |
| 电影感极简风景 | 本项目做过的电影感背景图风格 |
| 口播知识分享 | 常见类型 |
| 节奏卡点混剪 | 常见类型 |
| 生活Vlog | 常见类型 |

内容在 `presets.py`。界面里改过之后，随时可以「恢复预设」。

## 运行

```bash
pip install -r chatcut_studio/requirements.txt
python3 -m chatcut_studio            # 打开 http://127.0.0.1:8765
```

**Windows**：运行 `powershell -ExecutionPolicy Bypass -File chatcut_studio\windows\setup.ps1` 一键安装，双击 `chatcut_studio\windows\run.bat` 启动。详细的安装、设置、调试步骤见 [docs/windows.md](docs/windows.md)。

没有 ChatCut 账号、也不想花钱时，可以这样体验：

```bash
CHATCUT_STUDIO_DEMO=1 python3 -m chatcut_studio     # 演示 Agent，不调用任何模型
python3 -m chatcut_studio.fake_chatcut              # 本地假 ChatCut（http://127.0.0.1:8766/mcp）
```

用假 ChatCut 时，在「连接设置」里把 MCP 地址改成 `http://127.0.0.1:8766/mcp`，认证选「不认证」，再配一个真实的模型 Key，就能测试「API 模型直连」整条链路。

测试：`python3 -m pytest chatcut_studio/tests -q`（65 个，含「模拟 DeepSeek + 模拟 ChatCut」走真实 HTTP 的端到端测试）

## JIZURA 动态排版（`jizura.py`）

![JIZURA 四套主题](docs/samples/jizura_themes.jpg)

从上到下：宋韵留白、水墨书法、叠字国潮、清新诗意（背景是我们自己的插画）。

字幕参数里把「本地渲染引擎」选成 **JIZURA 动态排版**，就会用 [JIZURA](https://github.com/852wa/JIZURA)（MIT）渲染歌词：
它有 600 多个部件（184 种版式、125 种入场、109 种出场、62 种文字处理、130 种装饰……），按随机种子编排，
「换一种编排」就是换种子。源码原样放在 `third_party/jizura/src`（版本见 `UPSTREAM.md`），我们没有改它，在外面做了这些融合和优化：

| 优化 | 说明 |
|---|---|
| 服务器端渲染 | 无头 Chromium 里逐帧调用 JIZURA 的 `Renderer.frame()` 画透明歌词层，ffmpeg 叠到我们的背景图上、合上音频；或直接出透明 .mov 给 ChatCut。一拍两（每秒 12 张）的主题只渲染不重复的画面，由 ffmpeg 补帧 |
| 中文分镜 | JIZURA 按日文规则切分，会把中文拆在词中间（「笑｜靥」「咖｜啡」）。改为按词切：连续单字两两成词、「与／和」连接左右、助词语气词向前黏、落单的字向后黏，关键词不拆；多行换行和版式内部拆词也按词来。拿不准的句子可以在歌词里用 `/` 手动断开 |
| 四套中文主题 | 从部件库挑出白名单：**宋韵留白**（诗笺、错落、窗格、宋体大字、留白）、**水墨书法**（诗笺、月轮、窗格，书法字、墨晕 / 笔顺入场）、**叠字国潮**（叠影、错落、窗格，新中式金色）、**清新诗意**（诗笺、月轮、错落，楷体、「」引号、萤火、花瓣）。只保留干净的版式：一屏一句、一个视觉焦点。去掉了恐怖部件、故障风、霓虹、日文装饰、HUD、「No.04」这类编号，以及显得混乱的部件：巨字裁切出画、关键字拆行、同一句重复多遍（vcols / zoomRepeat）、描边回声 / 多重阴影、挂轴、横穿文字的笔刷、大字水印 |
| 新中式版式 | 用 JIZURA 已有的部件（竖排、印章、细线、残影、圆、版框）组合出 5 个新版式，注册进 JIZURA，入场 / 出场动画照常可用：**诗笺**（竖排正文，多列从右往左，一侧发丝线，列尾朱印）、**错落**（第一个词放大，其余小字错开落在下方，朱色短线相连）、**叠影**（同一句放大成淡淡的底纹，正文压在下半部分）、**窗格**（古籍版框式双线框，四边从角上画出，右下角压印）、**月轮**（一轮淡圆，竖排正文贴着圆的一侧或横排落在圆下） |
| 关键词突出 | 歌词里 `{词}` 标出的关键词会传进每个版式，**每个词只出现一次**：所有版式里关键词逐字换成强调色（整句本来是强调色时，正文退回正常色）；**诗笺**在关键词右侧加着重号；**错落**把关键词放大，前半句小字在左上、后半句小字在右下，按阅读顺序排。为了不重复歌词，去掉了大字分行（columnsBig，会把整句再写一遍小字）、刻歌词字的印章和竖排小字装饰，center 的小字副标题和 quote 的落款也关掉了；我们的印章改成不刻字的闲章 |
| 中文审美 | 换掉 JIZURA 偏日系 / 卡通的字体和配色：圆体、波普、手写体统一映射到思源宋体 Heavy、霞鹜文楷、马善政书法；新增 5 套中国色配色（墨与朱砂、夜色鎏金、青绿山水、月白黛蓝、胭脂），每套有浅字、深字两种方案。背景巨字和残影的颜色按背景图平均色来混，和画面同色调，不会出现浑浊的色块 |
| 跟背景对比 | 渲染前计算每张背景图的亮度，自动挑对比度够的配色；对比度不足 3 的镜头换成同一配色里对比度最高的方案 |
| 本地字体 | 思源黑体 / 宋体、站酷三款、马善政从 Google Fonts 仓库、霞鹜文楷从 GitHub 发布页下载到 `~/.cache/chatcut-studio/fonts`，渲染不依赖外网 |
| 节拍 | 给了音频时用 JIZURA 自己的节拍分析，切镜头对齐鼓点 |
| 我们的歌词语法 | `{关键词}` → JIZURA 的强调，`#chorus` → 副歌「重击」（闪光 + 震动，可关） |

需要 `pip install playwright` 并 `playwright install chromium`（或系统已有 Chromium）。升级 JIZURA：覆盖 `third_party/jizura/src`，跑 `pytest chatcut_studio/tests/test_jizura.py`——白名单里的部件改了名会直接报出来。

## 闪光扫过（`lyrics.py`）

![闪光扫过](docs/samples/shine_sweep.jpg)

左列：只扫关键词（「清新国风歌词MV」的默认设置）；右列：扫整句。从上到下依次是扫光前、扫到 1/4、1/2、3/4、扫完。

做法：LRC 歌词 → ASS 字幕 → ffmpeg + libass 渲染。在正文上面叠 4 层排版完全相同的「高光字」，每层用一个随时间从左移到右的矩形裁剪（`\clip` + `\t`）只露出一条光带；4 层宽度递增、越外越透明，叠出来接近柔和的渐变，没有硬边。只扫关键词时，高光层里非关键词的字设为全透明，光带也只在关键词范围内走。

歌词写法：`[00:12.50]巷口{灯光}忽明忽灭`，`{词}` 标关键词；行末加 ` #chorus` 表示副歌。

需要带 libass 的 ffmpeg（`ffmpeg -filters | grep ass`）。字体从 `chatcut_studio/fonts/` 或 `~/.cache/lyric-mv/fonts/` 读取，找不到时由系统字体回退。

## 在 ChatCut Desktop 里用 DeepSeek（`gateway.py`）

ChatCut Desktop 的 AI 输入框选「Claude Code」时，Desktop 会启动本机的 Claude Code。Studio 内置一个**本机模型网关**（`http://127.0.0.1:8765/gateway`，Anthropic Messages 格式），「连接设置 → 在 ChatCut Desktop 里用 DeepSeek → 接入 Claude Code」会把 Claude Code 指向它，于是 Desktop 里的 Agent 用的就是 Studio 里配置的 DeepSeek：

```
ChatCut Desktop AI 输入框 → Claude Code → Studio 网关 → DeepSeek（OpenAI 兼容接口）
```

| | 说明 |
|---|---|
| 转发方式 | 自动：官方 `api.deepseek.com` 走 DeepSeek 的 Anthropic 兼容接口原样转发；中转等其他 OpenAI 兼容地址把请求翻译成 `/chat/completions`（工具调用、图片、思考内容 `reasoning_content` 都来回转换；Claude Code 要流式时按 Anthropic 事件格式分段发回）。也可以手动指定 |
| 换模型 / 换 Key | 在 Studio 里改，下一次请求立即生效，不用动 Claude Code |
| 写了什么 | 只在 `~/.claude/settings.json` 的 `env` 里写网关地址、本机 Token（不是 DeepSeek 的 Key）、显示用的模型名、上下文长度；原值先备份，「断开」时原样恢复 |
| 安全 | 网关只监听本机，要带 Studio 生成的 Token 才能用 |
| 注意 | 接入期间本机所有 Claude Code 会话都用 DeepSeek，Studio 要保持运行 |

已用真实的 Claude Code 验证：只靠 Studio 写入的设置，Claude Code 经网关调用（模拟的）OpenAI 兼容 DeepSeek，完成「模型发起读文件 → Claude Code 执行 → 结果和思考内容回传 → 回答」的完整一轮。

**Desktop 里还是别的模型（比如 Qwen）？** 点「诊断：Desktop 里为什么不是 DeepSeek」（`diagnose.py`）：检查网关有没有收到过 Claude Code 的请求；查系统 / 用户环境变量、Claude Code 的本地和托管设置里有没有优先级更高、指向别的模型的配置；扫 ChatCut Desktop 自己的配置目录里提到 Qwen 的地方；并在本机实际调用一次 Claude Code，看它有没有走网关。结论会说明是该在 Desktop 里把 Agent 切到「Claude Code」，还是该删掉哪一项冲突配置。

在 Studio 里剪也一样用 DeepSeek：「剪辑 Agent 用什么驱动」选 API 模型（默认 DeepSeek），「ChatCut 连接」选本机 ChatCut Desktop，到「③ 一键剪辑」下任务，改的就是 Desktop 里当前打开的项目。

## 连接本机 ChatCut Desktop（`desktop.py`）

「连接设置 → ChatCut 连接」里选 **本机 ChatCut Desktop**，Agent 就直接编辑 Desktop 里当前打开的项目（素材用本机文件），Claude Code 和 API 模型两种方式都能用。

ChatCut Desktop 自带本机 MCP 连接（[官方说明](https://chatcut.io/docs/desktop-app)），但没有公开地址，所以「自动查找」会：

1. 读本机 Agent 的 MCP 配置，挑出名字 / 地址 / 命令里带 chatcut、而且是本机的那一项（云端 `api.chatcut.io` 的不算）：Claude Code `~/.claude.json`（含各项目的配置）、Claude Desktop、Codex `~/.codex/config.toml`、Cursor `~/.cursor/mcp.json`；
2. 找正在运行的 ChatCut Desktop 进程，看它在本机监听的端口，逐个试 `/api/external-mcp/mcp`、`/mcp`；
3. 每个候选都真的连一次、列出工具，能连上的标「可连接」。

找不到时可以「手动填写」本机地址（HTTP）或启动命令（stdio）。也可以选「不固定，每次连接时自动查找」（Desktop 每次启动端口可能变化时有用）。
Claude Code 方式下，找到的端点以 `chatcut_desktop` 的名字交给 Claude Code，并要求它只用这一组工具，不去碰云端插件。配置里带的 Token 只保存在本机，不会发给网页。

## 两种 Agent 方式的区别

| | Claude Code 插件 | API 模型直连 ChatCut |
|---|---|---|
| 需要安装 | Claude Code + ChatCut 官方插件 | 不需要 |
| 模型 | Claude（Claude Code 的账号） | 任意：Claude API、DeepSeek、通义千问、Kimi、GLM… |
| 连接 ChatCut | 官方插件（已验证可用的路径） | 本程序作为 MCP 客户端直连 `api.chatcut.io`（**登录方式待验证**） |
| 工具调用循环 | Claude Agent SDK | `agent.py` 里的 `api_runner` |

## 配置

都在界面里设置，保存在 `chatcut_studio/data/settings.json`（已 gitignore，文件权限 600，API Key 不会返回给前端）。
也可以用环境变量提供 Key：`ANTHROPIC_API_KEY`、`DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`、`MOONSHOT_API_KEY`、`ZHIPUAI_API_KEY`。

| 环境变量 | 说明 |
|---|---|
| `CHATCUT_STUDIO_AUTO_EDIT` | 默认 `1`：普通时间线编辑自动执行；设为 `0` 则每一步都要确认 |
| `CHATCUT_STUDIO_MAX_TURNS` | Agent 最多轮数，默认 60 |
| `CHATCUT_STUDIO_DATA` / `CHATCUT_STUDIO_SKILLS_DIR` | 数据目录 / Skill 安装目录（默认 `~/.claude/skills`） |
| `PORT` | 端口，默认 8765（OAuth 回调地址随之变化） |

## 权限策略（`agent.py` 的 `classify`）

- ChatCut 只读工具（get / list / read / verify…）：自动执行
- 会花积分、不可逆或会上传素材的（generate / export / render / upload / import / delete / transcribe / translate…）：**先确认**
- 普通时间线编辑：默认自动执行
- 其他一切（本机 shell、写文件、其他服务）：禁止

目前按工具名关键词判断。连接真实 ChatCut 后，「连接设置」页会列出每个工具的策略，据此改成逐个工具的明确清单。

## 目录

```
chatcut_studio/
  app.py           FastAPI 后端
  settings.py      模型服务商、Agent 方式、ChatCut 连接设置
  providers.py     Claude / OpenAI 兼容接口：模型列表、结构化输出、工具调用
  chatcut_mcp.py   直连 ChatCut MCP（OAuth / Token）
  desktop.py       查找并连接本机 ChatCut Desktop（Agent 配置 / 进程端口，HTTP / stdio）
  gateway.py       本机模型网关：Claude Code（ChatCut Desktop 里的 Agent）通过它用 DeepSeek
  diagnose.py      诊断 Desktop 里为什么没用上 DeepSeek（环境变量、设置优先级、Desktop 配置、实测）
  agent.py         三种执行器（Claude Code / API / 演示）、权限策略、运行与确认
  presets.py       5 个内置风格
  styles.py        风格库：上限 5 个、版本历史、恢复预设、安装
  optimize.py      AI 优化风格（按反馈生成修改建议）
  lyrics.py        歌词字幕渲染：LRC → ASS（含闪光扫过）→ ffmpeg
  jizura.py        JIZURA 动态排版融合层：无头渲染、中文分镜、主题、对比度、字体
  third_party/jizura/  JIZURA 源码（MIT，未改动）
  profile.py       风格档案结构 + SKILL.md 模板
  envcheck.py      Claude Code 方式的体检与安装
  fake_chatcut.py  本地假 ChatCut MCP 服务器
  static/index.html
  tests/
```
