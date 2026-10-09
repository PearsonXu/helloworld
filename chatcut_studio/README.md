# ChatCut Studio（原型）

给 [ChatCut](https://chatcut.io/) 套一层简易操作界面，把三件繁琐的事放到一个页面里：

| 页面 | 做什么 | 怎么实现 |
|---|---|---|
| ① 环境与 Agent | 体检 Claude Code、ChatCut 插件、MCP 登录、Claude API 凭据；一键安装 / 更新插件；给出登录命令和 Desktop 里选 agent 的步骤 | 把 [官方安装文档](https://github.com/ChatCut-Inc/agent-plugin/blob/main/docs/claude-code-install.md) 里的命令做成白名单按钮 |
| ② 风格记录器 | 填问卷，或粘贴一个满意项目的时间线让 Claude 反推，得到「风格档案」，再生成 `SKILL.md` | Claude API（`claude-opus-5-5`，结构化输出）；没有凭据时用离线模板 |
| 风格库 | 保存、编辑、删除风格；一键安装到 `~/.claude/skills/<slug>/SKILL.md` | 本地文件 `chatcut_studio/data/styles/` |
| ③ 一键剪辑 | 选风格 + 写任务 + 列素材，交给 Agent 在 ChatCut 里执行；会花积分 / 上传 / 删除 / 导出的操作先在页面上弹确认。也可以只复制提示词，粘到 ChatCut Desktop 的 AI 面板 | Claude Agent SDK + ChatCut MCP，`can_use_tool` 权限回调 |

## 运行

```bash
pip install -r chatcut_studio/requirements.txt
python3 -m chatcut_studio            # 打开 http://127.0.0.1:8765
```

先体验界面（不连 ChatCut、不花钱）：

```bash
CHATCUT_STUDIO_DEMO=1 python3 -m chatcut_studio
```

测试：`python3 -m pytest chatcut_studio/tests -q`

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Claude API 凭据（也可以用 `ant auth login`）。没有它，风格生成走离线模板，「从成片学习」不可用 |
| `CHATCUT_STUDIO_MODEL` | `claude-opus-5-5` | 风格生成和 Agent 用的模型 |
| `CHATCUT_MCP_MODE` | `plugin` | `plugin`：复用本机 Claude Code 里已登录的 ChatCut 插件；`http`：直连托管 MCP（需 `CHATCUT_MCP_TOKEN`，能否这样用待验证） |
| `CHATCUT_STUDIO_AUTO_EDIT` | `1` | 普通时间线编辑是否自动放行；设为 `0` 则每一步都要确认 |
| `CHATCUT_STUDIO_MAX_TURNS` | `60` | Agent 最多轮数 |
| `CHATCUT_STUDIO_DEMO` | — | 设为 `1` 使用演示 Agent |
| `CHATCUT_STUDIO_DATA` / `CHATCUT_STUDIO_SKILLS_DIR` | `chatcut_studio/data` / `~/.claude/skills` | 风格库和 Skill 安装位置 |

## 权限策略（`agent.py` 里的 `classify`）

- ChatCut 的只读工具（get / list / read / verify…）：自动放行
- 会花积分、不可逆或会上传素材的工具（generate / export / render / upload / import / delete / transcribe / translate…）：**弹窗确认**
- 普通时间线编辑：默认放行（可改成逐步确认）
- 本机 shell、写文件、其他服务：一律拒绝

这一版是**按工具名关键词**判断的。拿到 ChatCut MCP 的真实工具清单后，应改成逐个工具的明确列表（见下文第 1 步）。

## 目录

```
chatcut_studio/
  app.py          FastAPI 后端
  profile.py      风格档案数据结构 + SKILL.md 离线模板
  llm.py          Claude API：生成 SKILL.md、从时间线反推风格
  envcheck.py     环境体检 / 一键安装（白名单命令）
  styles.py       本地风格库、安装到 ~/.claude/skills
  agent.py        Agent SDK 执行器、权限策略、演示模式
  static/index.html  单页界面
  tests/          27 个测试
```
