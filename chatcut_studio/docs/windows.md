# 在 Windows 上安装、设置和调试

适用于 Windows 10 / 11。下面的命令都在 **PowerShell** 里、**仓库根目录**（`helloworld`）下运行。

## 一、安装

### 1. 下载代码

```powershell
winget install -e --id Git.Git          # 没有 git 时先装
git clone https://github.com/PearsonXu/helloworld.git
cd helloworld
git checkout claude/song-mv-background-art-wkt2t0
```

### 2. 一键安装

```powershell
powershell -ExecutionPolicy Bypass -File chatcut_studio\windows\setup.ps1
```

脚本会依次：

| 步骤 | 内容 | 缺了会怎样 |
|---|---|---|
| Python 3.11+ | 没有就用 winget 装 3.12（装完要重开 PowerShell 再跑一次脚本） | 无法运行 |
| `.venv` 虚拟环境 + 依赖 | `pip install -r chatcut_studio\requirements.txt` | 无法运行 |
| Chromium | `playwright install chromium` | JIZURA 动态排版渲染不了 |
| ffmpeg（带 libass） | 没有就装 `Gyan.FFmpeg` | 本地渲染歌词视频、闪光扫过都用不了 |
| Claude Code（可选） | 只检查，不自动装 | 只影响「Claude Code 插件」方式 |

手动安装也一样，只是这几步自己做：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r chatcut_studio\requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
winget install -e --id Gyan.FFmpeg
```

### 3. 歌曲工坊的人声分离（可选，约 3 GB）

```powershell
powershell -ExecutionPolicy Bypass -File chatcut_studio\windows\setup.ps1 -Audio
```

装 CUDA 版 PyTorch 和 audio-separator（UVR 同款模型），最后会显示「显卡可用：True NVIDIA GeForce GTX 1660…」。显示 False 时会用 CPU 分离，一首歌要几分钟。第一次分离会下载模型（BS-Roformer 约 600 MB）。

## 二、启动

双击 `chatcut_studio\windows\run.bat`，浏览器会打开 <http://127.0.0.1:8765>。

| 命令 | 用途 |
|---|---|
| `run.bat` | 正常启动 |
| `run.bat demo` | 演示模式：Agent 不调用任何模型、不花钱，用来熟悉界面 |
| `run.bat debug` | 详细日志（看请求、报错） |

或者在 PowerShell 里：`.\.venv\Scripts\python.exe -m chatcut_studio`。换端口：`$env:PORT=8800` 再启动。

第一次用 JIZURA 动态排版时会下载中文字体（约 60 MB，存到 `%USERPROFILE%\.cache\chatcut-studio\fonts`），之后离线可用。

## 三、设置

打开页面后按顺序：

1. **① 连接设置**
   - **Agent 方式**：
     - 只有 API Key（DeepSeek、通义千问、Kimi、智谱、Claude API）→ 选「API 模型直连 ChatCut」，填 Key，点「获取模型列表」选模型。
     - 已装 Claude Code → 选「Claude Code 插件」，点「体检」，按提示「一键安装插件」，然后在 PowerShell 里运行页面给出的登录命令（`claude mcp login …`）。
   - **在 ChatCut Desktop 里用 DeepSeek**：在「模型服务商」里填 DeepSeek 的 base_url（OpenAI 兼容地址）、Key、模型 → 点「测试 DeepSeek」→ 点「接入 Codex」→ 重启 ChatCut Desktop，AI 面板的 Agent 按钮选「ChatGPT」（就是 Codex），新开对话。不对的话点「诊断」。用完点「断开」。
   - **ChatCut 连接**：
     - **本机 ChatCut Desktop**（推荐）：先打开 ChatCut Desktop 和一个项目，点「自动查找」，选中「可连接」的那一项，点「保存并连接」。找不到时展开「手动填写」，填 Desktop 的本机地址或启动命令。
     - **ChatCut 云端**：点「保存并连接」，在弹出的浏览器里登录授权。
     - 连接成功后会列出 ChatCut 的全部工具和每个工具的权限策略。
2. **② 常用风格**：选一个风格，改参数或写反馈让 AI 优化；歌词字幕区可以选「JIZURA 动态排版」、换主题、点「生成样张」预览。
4. **🎵 歌曲工坊**：选原曲 mp4 →「导入」→「开始分离」→ 看建议降几度，试听降 2–6 各档 →「导出降 N」→ 打开 `02_降调` 把混音 mp3 传 Suno → 成品放进 `03_Suno成品` → 粘贴歌词，「用 DeepSeek 生成配图提示词」→ 出好的图放进 `05_配图`。整个歌曲文件夹就是素材包。
3. **③ 一键剪辑**：选风格、写任务、列素材，交给 Agent；花积分 / 上传 / 删除 / 导出的操作会先弹确认。

Key 也可以用环境变量提供（不写进文件）：

```powershell
setx DEEPSEEK_API_KEY "sk-..."      # 设完重开 PowerShell 生效
```

设置保存在 `chatcut_studio\data\settings.json`（已 gitignore）。

## 四、不花钱地测试整条链路

开两个 PowerShell：

```powershell
# 窗口 1：本地假 ChatCut（MCP 服务器）
.\.venv\Scripts\python.exe -m chatcut_studio.fake_chatcut      # http://127.0.0.1:8766/mcp
# 窗口 2：正常启动
chatcut_studio\windows\run.bat
```

在「连接设置」里把 MCP 地址改成 `http://127.0.0.1:8766/mcp`、认证选「不认证」，配一个真实模型 Key，就能跑通「模型 → 工具调用 → ChatCut」整条链路，不消耗 ChatCut 积分。

## 五、调试

### 跑测试

```powershell
.\.venv\Scripts\python.exe -m pytest chatcut_studio\tests -q            # 全部（75 个）
.\.venv\Scripts\python.exe -m pytest chatcut_studio\tests\test_jizura.py -q -x   # 只测动态排版，遇错即停
.\.venv\Scripts\python.exe -m pytest chatcut_studio\tests -q -k lyrics   # 按名字筛选
```

没装 Chromium / ffmpeg / 字体时，相关测试会自动跳过（显示为 `s`），不算失败。

### 用 VS Code 断点调试

1. 安装 VS Code 和「Python」扩展，打开 `helloworld` 文件夹，右下角选解释器 `.venv`。
2. 新建 `.vscode\launch.json`：

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "ChatCut Studio",
      "type": "debugpy",
      "request": "launch",
      "module": "chatcut_studio",
      "env": { "PYTHONUTF8": "1", "LOG_LEVEL": "debug" },
      "justMyCode": true
    },
    {
      "name": "ChatCut Studio（演示模式）",
      "type": "debugpy",
      "request": "launch",
      "module": "chatcut_studio",
      "env": { "PYTHONUTF8": "1", "CHATCUT_STUDIO_DEMO": "1" }
    },
    {
      "name": "测试",
      "type": "debugpy",
      "request": "launch",
      "module": "pytest",
      "args": ["chatcut_studio/tests", "-q", "-x"],
      "env": { "PYTHONUTF8": "1" }
    }
  ]
}
```

3. 按 F5 启动，在 `agent.py`（Agent 执行、权限确认）、`providers.py`（模型调用）、`chatcut_mcp.py`（ChatCut 连接）、`jizura.py`（动态排版渲染）里打断点。

### 看动态排版渲染出了什么

每次本地渲染都会在输出视频旁边写一个 `*.plan.json`，里面是每个镜头用了哪个版式、入场、出场、配色，可以对照画面找问题。

前端问题按 F12 打开浏览器开发者工具，看 Console 和 Network（`/api/...` 请求）。

## 六、常见问题

| 现象 | 原因 / 解决 |
|---|---|
| 运行脚本提示「禁止运行脚本」 | 用 `powershell -ExecutionPolicy Bypass -File ...` 运行，或 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `py` / `ffmpeg` / `claude` 找不到 | 刚用 winget 安装的程序要**重开 PowerShell** 才在 PATH 里 |
| 「找不到 ffmpeg」或闪光扫过渲染失败 | 装 `winget install Gyan.FFmpeg`；用 `ffmpeg -filters \| findstr ass` 确认带 libass |
| 「无法启动 Chromium」 | `.\.venv\Scripts\python.exe -m playwright install chromium` |
| 中文乱码 | 用 `run.bat` 启动（已设 `PYTHONUTF8=1` 和 UTF-8 代码页）；自己启动时先 `$env:PYTHONUTF8=1` |
| 字体下载失败 | 需要能访问 GitHub；也可以手动把字体放到 `%USERPROFILE%\.cache\chatcut-studio\fonts`（页面会列出缺哪些） |
| 端口被占用 | `$env:PORT=8800` 后再启动 |
| 降调导出报 `No such filter: 'rubberband'` | 你的 ffmpeg 没带这个滤镜。`git pull` 后运行 `.\.venv\Scripts\python.exe -m pip install -r chatcut_studio\requirements.txt`（会装 Python 版 Rubber Band，需要 Python 3.12+），重启 Studio |
| Desktop 的 Agent 按钮里没有「Claude」 | 本机没装 Claude Code。用「ChatGPT」（Codex）接入 DeepSeek 即可，不需要 Claude Code |
| 杀毒软件拦截 | 把 `helloworld\.venv` 和 `%LOCALAPPDATA%\ms-playwright` 加入信任 |
