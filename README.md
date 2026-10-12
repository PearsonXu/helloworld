# ArbDesk：本地 AI 套利 / 机会扫描面板

ArbDesk 是一个本地运行的 Python 程序。它每天定时扫描几次加密货币市场的套利机会，交给 DeepSeek 或 Qwen 分析并给出建议，最后由你在本地网页面板里**手动批准**才执行。默认是**模拟盘（paper）**。

> ⚠️ 本项目不是投资建议。100–200 美元的本金下，手续费、最小下单量和滑点会吃掉大部分理论价差，请先用模拟盘跑至少几周再考虑实盘。

---

## 一、开源框架调研结论（2026-10）

| 项目 | 定位 | 适合本需求吗 |
|---|---|---|
| [CCXT](https://docs.ccxt.com/) | 100+ 交易所统一 API 库（行情和下单） | ✅ **直接用作底层**。本项目的行情和下单都基于它 |
| [Hummingbot](https://github.com/hummingbot/hummingbot) | 做市 / 跨所套利机器人，自带连接器 | 可选。套利执行最成熟，但较重，接 LLM 不方便 |
| [Freqtrade](https://github.com/freqtrade/freqtrade) | 方向性策略机器人（回测、FreqAI），底层用 CCXT | 做“投资/趋势”策略时可用，不是为套利设计的 |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) | LangGraph 多智能体 LLM 研究框架，原生支持 DeepSeek / Qwen | 适合借鉴“分析师→研究员→风控”的提示词思路。偏股票，且是研究用途 |
| TradingAgents-CryptoA（社区 fork） | 上面那个项目加 CCXT 和链上数据 | 维护状态不明，不建议直接依赖 |

**结论**：不直接套用大框架，而是 **CCXT（数据和交易）+ OpenAI 兼容 SDK（DeepSeek/Qwen）+ 自己的风控和面板**。这样代码量小，所有逻辑可审计，密钥只经过本地代码。以后需要严肃的套利执行时，可以再把 Hummingbot 接进来当执行引擎。

### 关于 AI 计划
- DeepSeek 和 Qwen 都提供 **OpenAI 兼容接口**，本项目只需在 `.env` 里切换 `LLM_PROVIDER`。
- Qwen 的 “Coding Plan” 类订阅通常**只允许在编程工具中使用**，用在本程序里可能违反条款。建议用按量计费的 Model Studio / DashScope API key。具体以阿里云控制台的当前条款为准。
- 每天扫描 3 次、每次 1 个请求，token 成本通常每月不到 1 美元。

### 小资金下各类套利的现实情况
| 类型 | 小资金可行性 | 说明 |
|---|---|---|
| 资金费率期现套利（现货多 + 永续空） | ⭐ 最现实 | 年化常见 5–30%，费率会翻负；$150 本金一年大约赚十几到几十美元 |
| 跨交易所价差 | 低 | 要在两边预存资金，主流币价差扣掉手续费后很少为正 |
| 三角套利 | 很低 | 毫秒级机会，手动审批基本赶不上，仅作观察 |

---

## 二、安全设计

- **密钥只在本地**：`.env`（已加入 git 忽略，启动时检查是否为 `chmod 600`）。日志会自动打码密钥。
- **数据只在本地**：扫描、AI 分析、建议、成交记录都存在 `data/arbdesk.db`（SQLite，权限 600，git 忽略）。
- **发给 AI 的只有公开行情和风控上限**，不发密钥、余额和交易记录（有单元测试保证）。
- **AI 只能提议，不能执行**：执行必须你在面板里点“批准”，之后还要通过代码里的硬性风控：单笔上限、总资金上限、日亏损上限、每日交易次数、交易所和币种白名单、滑点上限。实盘永续默认禁止。
- **面板只监听 127.0.0.1**：非回环地址会拒绝启动。访问需要随机 token（HttpOnly + SameSite=Strict cookie），并校验 Host 头（防 DNS rebinding），POST 只接受 JSON（防 CSRF），设置了 CSP，AI 输出全部转义（防 XSS）。
- **程序不调用任何提币或划转接口。**

**交易所 API key 务必这样创建**：只开“交易”权限，**关闭提币**，绑定本机 IP 白名单。最好单独开一个子账户，只放 100–200 美元。

---

## 三、使用

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env && chmod 600 .env        # 填 LLM_API_KEY（实盘时再填交易所 key）
cp config.example.toml config.toml            # 调整币种、定时、风控

python -m arbdesk panel        # 启动面板 + 定时器，打开终端打印的带 token 链接
python -m arbdesk scan         # 手动跑一次扫描 + AI 分析
python -m arbdesk scan --no-llm
python -m arbdesk proposals    # 列出建议
python -m arbdesk approve 3    # 命令行批准执行
```

上实盘的步骤：先模拟盘跑几周，确认建议质量；然后在 `config.toml` 设置 `risk.mode = "live"`，并在 `.env` 填对应交易所的 key。实盘下单使用带滑点上限的限价单，并检查交易所的最小下单量。

> 部分交易所在某些地区访问受限。扫描失败的交易所会显示在面板的“最新扫描”里，其他交易所照常工作。

## 四、结构

```
arbdesk/
  config.py    配置与密钥加载、权限检查、日志打码
  scanners.py  跨所价差 / 资金费率 / 三角套利（纯函数）+ 公开行情抓取
  llm.py       DeepSeek / Qwen 分析（JSON 输出）
  risk.py      硬性风控
  executor.py  模拟盘 / 实盘执行（需人工批准）
  service.py   扫描 → 分析 → 生成建议；每日定时
  storage.py   本地 SQLite
  web.py + static/  本地面板
tests/         单元测试：pytest
```

## 五、后续可以做的

- 资金费率套利的持仓跟踪和自动平仓提醒（费率翻负时）
- 接入 Freqtrade 做趋势类“投资”策略回测，让 AI 只负责筛选和解释
- 历史机会统计，用来评估阈值和 AI 建议的命中率
