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

没有 ChatCut 账号、也不想花钱时，可以这样体验：

```bash
CHATCUT_STUDIO_DEMO=1 python3 -m chatcut_studio     # 演示 Agent，不调用任何模型
python3 -m chatcut_studio.fake_chatcut              # 本地假 ChatCut（http://127.0.0.1:8766/mcp）
```

用假 ChatCut 时，在「连接设置」里把 MCP 地址改成 `http://127.0.0.1:8766/mcp`，认证选「不认证」，再配一个真实的模型 Key，就能测试「API 模型直连」整条链路。

测试：`python3 -m pytest chatcut_studio/tests -q`（43 个，含「模拟 DeepSeek + 模拟 ChatCut」走真实 HTTP 的端到端测试）

## 闪光扫过（`lyrics.py`）

![闪光扫过](docs/samples/shine_sweep.jpg)

左列：只扫关键词（「清新国风歌词MV」的默认设置）；右列：扫整句。从上到下依次是扫光前、扫到 1/4、1/2、3/4、扫完。

做法：LRC 歌词 → ASS 字幕 → ffmpeg + libass 渲染。在正文上面叠 4 层排版完全相同的「高光字」，每层用一个随时间从左移到右的矩形裁剪（`\clip` + `\t`）只露出一条光带；4 层宽度递增、越外越透明，叠出来接近柔和的渐变，没有硬边。只扫关键词时，高光层里非关键词的字设为全透明，光带也只在关键词范围内走。

歌词写法：`[00:12.50]巷口{灯光}忽明忽灭`，`{词}` 标关键词；行末加 ` #chorus` 表示副歌。

需要带 libass 的 ffmpeg（`ffmpeg -filters | grep ass`）。字体从 `chatcut_studio/fonts/` 或 `~/.cache/lyric-mv/fonts/` 读取，找不到时由系统字体回退。

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
  agent.py         三种执行器（Claude Code / API / 演示）、权限策略、运行与确认
  presets.py       5 个内置风格
  styles.py        风格库：上限 5 个、版本历史、恢复预设、安装
  optimize.py      AI 优化风格（按反馈生成修改建议）
  lyrics.py        歌词字幕渲染：LRC → ASS（含闪光扫过）→ ffmpeg
  profile.py       风格档案结构 + SKILL.md 模板
  envcheck.py      Claude Code 方式的体检与安装
  fake_chatcut.py  本地假 ChatCut MCP 服务器
  static/index.html
  tests/
```
