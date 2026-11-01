# ArbDesk：本地 AI 套利 / 机会扫描面板

ArbDesk 是一个本地运行的 Python 程序。它每 5 分钟用代码扫描加密货币和预测市场（Kalshi、Polymarket US）的套利机会，每小时最多一次、且只在有变化时交给 DeepSeek 或 Qwen 的 flash 模型分析，最后由你在本地网页面板里**手动批准**才执行。默认是**模拟盘（paper）**。

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

## 二、Polymarket 模块（默认模拟盘，可开实盘）

只读公开接口（Gamma 和 CLOB），不需要钱包或私钥。每次扫描做三件事：

1. **多结果（negRisk）套利**：在“只会有一个结果成立”的事件里，
   - 所有结果的 YES 卖价加起来 < 1：每个结果各买 1 份 YES，到期固定兑付 $1；
   - 所有结果的 YES 买价加起来 > 1：每个结果各买 1 份 NO，到期固定兑付 $(N−1)。
   会逐档扫描真实订单簿，计算扣除吃单手续费后还能成交多少份，而不是只看最优价。仍在增加选项的 augmented 事件会跳过。
2. **AI 概率评估**：挑出流动性好、1–60 天内到期的市场，让 AI 给出 YES 概率，并全部记录下来。扣除手续费后优势超过 `ai_min_edge` 的，生成待审批建议。
3. **结算与打分**：市场结算后，自动计算模拟仓位的盈亏，并统计 AI 和市场价各自的 **Brier 分数**（越低越准）。AI 的分数长期低于市场之前，不要用真钱跟它的观点。

> 普通的 YES/NO 二元市场没有“YES + NO < 1”的套利：两边共用一个镜像订单簿，买 NO@q 等于卖 YES@(1−q)。

```bash
python -m arbdesk polymarket           # 只跑 Polymarket：结算 → 扫描 → AI 评估
python -m arbdesk polymarket --no-llm
```

---

## 二点五、美国合规预测市场：Kalshi 与 Polymarket US

面板里每个预测市场平台各占一块，各自独立设置模拟盘/实盘、额度上限和 AI 打分。

| | Kalshi | Polymarket US |
|---|---|---|
| 监管 | CFTC 指定合约市场 | CFTC 监管（与 polymarket.com 是两个独立的平台） |
| 南卡州 | 可用 | 可用（州内有一起诉讼尚未判决） |
| 自动多结果套利 | ✅ 只做"买全部 NO"方向（见下） | ❌ 没有互斥事件标记，不自动做 |
| AI 概率评估 | ✅ | ✅ |
| 实盘下单 | ✅ YES 和 NO，全成或全撤（FOK）；**默认连 demo 假钱环境** | ✅ 只买 YES（Long），全成或全撤（FOK） |
| 密钥 | API Key ID + 私钥 PEM 文件 | Key ID + Ed25519 私钥（base64） |

**Kalshi 的多结果套利只做"买全部 NO"：**
- 在标记为互斥的事件里，最多只有一个结果会成真。
- 每个结果各买 1 份 NO，至少兑付 N−1（没有结果成真时兑付 N）。成本扣完手续费后低于 N−1，就是锁定利润。
- 反方向"买全部 YES"并不安全：结果可能不穷尽，也就是可能没有任何一个成真。所以这个方向**从不提议**。

**Kalshi 实盘（建议顺序）：**
1. 在 demo.kalshi.co 注册 demo 账户，在账户设置里创建 API Key，下载私钥 `.pem` 文件。
2. 安装依赖并存入密钥：
   ```bash
   pip install -r requirements-live.txt
   python -m arbdesk vault set KALSHI_API_KEY_ID
   python -m arbdesk vault set-file KALSHI_PRIVATE_KEY 路径/kalshi-key.pem
   ```
   导入后删掉原 `.pem` 文件。
3. `python -m arbdesk check kalshi`：检查连接和余额，不下单。
4. `config.toml` 里设置 `[kalshi] mode = "live"`，保留 `environment = "demo"`，用假钱把下单、成交、结算完整跑几次。
5. 确认一切正常后，在 kalshi.com 正式账户创建新的 API Key，替换保险库里的两项，再把 `environment` 改成 `"prod"`。

**Polymarket US 实盘：**
1. 在 Polymarket US App 里完成 KYC 并入金。
2. 到 polymarket.us/developer 创建 API Key。私钥只显示一次，立刻存入保险库：
   ```bash
   python -m arbdesk vault set POLYMARKET_US_KEY_ID
   python -m arbdesk vault set POLYMARKET_US_SECRET_KEY
   ```
3. `python -m arbdesk check polymarket_us`，然后设置 `[polymarket_us] mode = "live"`。
4. Polymarket US 没有 demo 环境，**第一笔只下 1–2 份**，然后到 App 里核对成交和扣款。

**常用命令：**
```bash
python -m arbdesk venue kalshi --no-llm          # 只跑 Kalshi：结算 → 扫描（不调用 AI）
python -m arbdesk venue polymarket_us --no-llm
```

---

## 二点六、自动运行：代码每 5 分钟扫描，AI 每小时只看变化

| | 频率 | 花 token 吗 | 做什么 |
|---|---|---|---|
| 代码扫描 | 每 5 分钟（`scan.interval_minutes`） | 不花 | 加密货币价差和资金费率、Kalshi 互斥事件 NO 组合、跨平台已确认事件对的实时价格 |
| AI（flash 模型） | 最多每 60 分钟（`llm.interval_minutes`） | 花，而且有上限 | 只在有变化时调用（见下） |

**AI 只在这些情况下调用：**
- **加密货币：** 机会列表有变化时分析；没有机会时，一天最多给一次行情观点。
- **概率评估：** 只评估价格变动超过 5 个百分点、或距上次评估超过 24 小时的市场。
- **跨平台匹配：** 只判断新出现的候选事件对，每次最多 15 对；判断结果永久缓存，同一对不会重复花 token。

**Token 控制：**
- 每次调用都会把 API 返回的真实用量（含缓存命中）记到本地数据库。
- 用到每日 token 预算（默认 30 万）、每月美元上限（默认 $5）或每日调用上限时，AI 自动暂停，代码扫描照常进行。
- 每次调用都限制了输出长度。
- 面板顶部显示今日 token 和本月花费，鼠标悬停可以看各用途的明细。
- `python -m arbdesk check llm` 只发一个约 20 token 的请求，用来确认模型名和 key 是否可用。
- 默认使用 flash 模型（`deepseek-flash` / `qwen-flash`）。模型名称会变，以 `check llm` 的结果为准，需要的话在 `.env` 里用 `LLM_MODEL=` 指定。
- `[llm]` 里的价格只用来估算花费和执行月度上限，请按你的供应商价格页填写准确数字。

**其他改动：**
- **待审批建议去重：** 同一个机会持续存在时，只刷新原来那条建议的价格，不会每次都新增。
- **AI 打分不重复计数：** Brier 分数里每个市场只计一次（取最后一次评估），反复评估不会让打分失真。
- **自动清理：** 两天前、而且什么都没扫到的扫描记录会自动删除，数据库不会无限增长。

**跨平台套利（Kalshi ↔ Polymarket US）：**
- 两个平台上的同一事件，一边买 YES、一边买 NO，合计成本低于 $1 就是锁定利润。
- 流程分三步：
  1. 代码按标题词语和到期日粗筛候选（不花 token）；
  2. AI 判断两个市场是否"在完全相同的情况下结算为 YES"；
  3. 代码用两边的实时订单簿计价。
- 风险在第 2 步：两个平台的结算规则可能有细微差别。所以只采用高置信度的"同一事件"判断，要求更高的利润门槛（默认 1%），**批准前请自己对照两边的规则原文。**
- 资金要分别存在两个平台。

**电脑需要一直开着：**
- 面板运行时，程序会让 Windows 不进入睡眠（屏幕可以关）。但笔记本合盖仍然会睡眠，需要在电源设置里改成"合盖不做任何操作"。
- 开机自动启动：按 Win+R，输入 `shell:startup`，在打开的文件夹里新建快捷方式，目标填：
  ```
  powershell.exe -NoExit -ExecutionPolicy Bypass -File "C:\Users\你的用户名\arbdesk\scripts\start_arbdesk.ps1"
  ```
  登录后会弹出窗口让你输入主密码。主密码不会保存在任何地方，所以每次启动都需要手动输入。

---

## 二点七、黑天鹅定价：预测市场合约 vs 期权反推概率（不是套利）

**做什么：** 对 Kalshi 和 Polymarket US 上每一个"BTC/ETH 到期价格高于 / 低于 / 位于某区间"的合约，用 Deribit 期权链反推同一价位、同一日期的概率，再和合约售价比较。
- 期权反推概率：用 N(d2) 加上波动率曲线倾斜（skew）修正；两个到期日之间按总方差插值；不在报价范围外做外推。
- 某一边只卖几美分，而期权反推的概率明显更高时，就是"便宜的黑天鹅彩票"。
- 默认的"便宜"标准：价格 ≤15¢、反推概率 ≥ 价格的 1.5 倍、扣费后优势 ≥2¢。
- 满足条件的合约会生成待审批建议（`kind = "tail"`）。

**数据来源：**
- **Kalshi：** 用 `strike_type`、`floor_strike`、`cap_strike` 字段，按 `kalshi_series` 里列出的系列拉取。
- **Polymarket US：** 只解析措辞简单、不含歧义的标题，例如 "above $150,000"、"reach $10k"、"dip to $55k"。
  - 标题里出现两个数字、同时提到两种币、或者是百分比的，一律跳过，不猜。
  - "reach / hit" 这类"到期前任何时刻触及"的合约，用 2 倍到期概率近似（这是无漂移情况下的近似）。
- **Deribit：** 只读取公开行情，不需要账户。

**为什么"比期权还便宜"才有意义：** 期权反推的是风险中性概率，通常已经包含尾部溢价，也就是比真实概率偏高。合约价格连这个都不到，才算真的便宜。

**局限：**
- 两边结算用的指数不同：Kalshi 用 CF Benchmarks BRTI 的最后一分钟均价，Deribit 期权按 08:00 UTC 结算。
- 结算时间点也不一定一致。
- 最重要的一点：**大多数彩票会到期归零**，命中的那一次才可能赚回很多倍。

**资金纪律（代码强制执行，模拟盘和实盘都适用）：**
- 单注 ≤ $5（`tail.max_usd_per_bet`）。
- 每个自然季度合计 ≤ $30（`tail.quarterly_budget_usd`），超出部分会被风控拒绝。
- 面板显示本季度已用金额。

**运行方式：** 每 5 分钟和代码扫描一起运行，不花 AI token。也可以手动运行一次：
```bash
python -m arbdesk tail      # 列出定价结果，* 表示便宜的票
```

---

## 二点八、极端行情提醒（只提醒，不下单）

每 5 分钟随代码扫描检查一次，不花 AI token：

| 提醒 | 默认条件 | 级别 |
|---|---|---|
| 价格急变 | BTC/ETH 1 小时内变动 ≥5%；24 小时内变动 ≥12% | 1 小时内 ≥12% 为**严重** |
| 波动率飙升 | 近月平值隐含波动率 ≥100%，或比 24 小时前高 20 个点 | 注意 |
| 资金费率极端 | 永续资金费率超过 ±0.15%/8 小时（年化约 160%），说明多头或空头过度拥挤 | 注意 |
| 稳定币脱锚 | USDT/USDC 偏离 $1 超过 0.5% | 超过 2% 为**严重** |
| 数据源中断 | 原本正常的数据源连续 3 次失败 | 注意 |

**数据来源（都是公开数据）：**
- 价格指数和资金费率来自 Deribit；
- 隐含波动率复用黑天鹅定价已经拉取的期权链，不额外请求；
- 稳定币价格来自 Kraken 的公开行情。

**提醒怎么出现：**
- 面板顶部的提醒栏，可以逐条或全部标为已读；
- Windows 桌面弹窗（需要 `plyer`，`requirements.txt` 在 Windows 上会自动安装）；
- 程序窗口里的日志。

**避免重复提醒：** 同一条提醒 6 小时内不会重复出现；但如果情况升级（比如从"注意"变成"严重"），会立刻再提醒一次。

**一直连不上的数据源不报中断：** 例如被地区限制的交易所从来没有成功过，就不会产生"中断"提醒。

**严重级别会自动停止实盘：** 当前有任何实盘模式开着时（加密货币或任意预测市场），严重提醒会自动按下紧急停止，原因写在面板顶部。只用模拟盘时不会触发停止。

**手动检查一次：**
```bash
python -m arbdesk alerts          # 检查一次，并列出最近的提醒
python -m arbdesk alerts --ack    # 全部标为已读
```

---

## 三、安全设计

### 1. 加密保险库（`data/vault.json`）
- **加密方式**：AES-256-GCM 加密，密钥由主密码经 scrypt（约 128 MB 内存）派生，暴力猜密码很慢。文件头（KDF 参数）也纳入认证，被篡改会直接解密失败。
- **主密码**：不保存在任何地方，每次启动时手动输入。至少 12 位，**忘记就无法恢复**。
- **写入安全**：先写临时文件，自检能解密后再原子替换；文件权限 600。
- **Polymarket 钱包私钥只能放在保险库**：写在 `.env` 或环境变量里会被忽略并报警告。
- **保护范围**：保险库保护的是存在磁盘上的密钥（硬盘、备份、云同步、电脑丢失）。程序运行时密钥在内存里，如果电脑本身中了木马，它保护不了。所以实盘钱包里只放你愿意亏掉的钱。

### 2. 实盘下单的保护
- **两次确认**：先在面板点“批准（真钱）”，再输入大写 `LIVE`。服务器端也会检查，跳过前端没用。命令行需要 `approve ID --live` 再输入 `LIVE`。
- **地区检查**：每次下单前调用 Polymarket 官方接口 `polymarket.com/api/geoblock`。所在地区受限，或者检查失败，**都拒绝下单，没有绕过开关**。
- **下单前预检**：所有腿先对照实时订单簿和 pUSD 余额检查，任何一项不满足就一单都不发。
- **全成或全撤（FOK）**：每单要么在限价内全部成交，要么完全不成交，不会在订单簿上留下挂单。
- **多腿顺序**：多结果套利按订单簿最薄的先下，降低“一半成交一半没成交”的概率。如果真的只成交了一部分，建议会标成 `partial`，并**自动触发紧急停止**，等你人工检查。
- **额度上限**：每日实盘额度（默认 $20）、未结算总敞口（默认 $50）、单市场上限，在代码里硬性检查。
- **紧急停止**：面板右上角按钮，或者 `python -m arbdesk halt`。停止后所有实盘下单都会被拒绝；`resume` 恢复。
- **不会做的事**：程序不发任何提币、划转或授权交易，只签订单。

### 3. 其他
- **数据只在本地**：`data/arbdesk.db`，权限 600，已加入 git 忽略。
- **发给 AI 的只有公开行情和风控上限。** 面板只监听 127.0.0.1，访问需要 token，有 CSRF、XSS 和 DNS rebinding 防护。日志自动打码密钥。
- **交易所 API key**：只开“交易”权限，关闭提币，绑定 IP 白名单，用子账户。

---

## 四、使用

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt               # Polymarket 实盘再加：pip install -r requirements-live.txt
cp config.example.toml config.toml

# 密钥放进加密保险库（推荐，Polymarket 私钥必须这样放）
python -m arbdesk vault init                  # 设主密码
python -m arbdesk vault set LLM_API_KEY       # 输入时不回显
python -m arbdesk vault list                  # 只显示名字，不显示值
#（已经有 .env 的话：python -m arbdesk vault import-env，然后删掉 .env 里的密钥行）

python -m arbdesk panel        # 启动面板 + 自动扫描（会先问主密码；Windows 可用 scripts\start_arbdesk.ps1）
python -m arbdesk scan         # 手动跑一次扫描 + AI 分析
python -m arbdesk proposals    # 列出建议
python -m arbdesk approve 3    # 命令行批准（模拟）
```

### 开启 Polymarket 实盘（建议先模拟盘跑 4–8 周）
1. 先确认你所在地区允许使用 Polymarket（程序也会自动检查）。
2. **新建一个专用钱包**，只充入你愿意亏掉的金额，比如 $50。不要用存了其他资产的钱包。
3. 在 polymarket.com 用这个钱包登录，先手动完成一次小额交易，让网站帮你完成 pUSD 和授权设置。
4. 存入密钥：
   ```bash
   pip install -r requirements-live.txt
   python -m arbdesk vault set POLYMARKET_PRIVATE_KEY
   python -m arbdesk vault set POLYMARKET_FUNDER          # polymarket.com 上显示的钱包地址
   python -m arbdesk vault set POLYMARKET_SIGNATURE_TYPE  # 1 = 邮箱登录，2 = 浏览器钱包，0 = 普通 EOA
   ```
5. `python -m arbdesk pm-check`：检查地区、签名、余额，**不会下单**。
6. 在 `config.toml` 的 `[polymarket]` 里设置 `mode = "live"`，重启面板。之后每个建议都需要你点“批准（真钱）”并输入 `LIVE`。
7. 市场结算后，赢的份额需要在 polymarket.com 上点 “Claim” 领取。程序会在本地记录盈亏，但不会替你领取。

交易所实盘：设置 `risk.mode = "live"`，把交易所 key 存进保险库（`vault set EXCHANGE_OKX_API_KEY` 等）。

> 部分交易所和 Polymarket 在某些地区受限。请遵守当地法律和平台条款，程序不会帮助绕过任何地区限制。

## 五、结构

```
arbdesk/
  config.py    配置与密钥加载、权限检查、日志打码
  scanners.py  跨所价差 / 资金费率 / 三角套利（纯函数）+ 公开行情抓取
  polymarket.py  Polymarket 数据、多结果套利计算、预测候选、结算
  llm.py       DeepSeek / Qwen 分析（JSON 输出）
  risk.py      硬性风控
  executor.py  模拟盘 / 实盘执行（需人工批准，实盘需输入 LIVE）
  vault.py     加密保险库（AES-256-GCM + scrypt）
  pm_live.py   Polymarket 实盘下单（CLOB V2 SDK，FOK，地区检查）
  kalshi.py    Kalshi：数据、互斥事件 NO 组合套利、签名、FOK 下单
  pmus.py      Polymarket US：数据、AI 候选、FOK 下单（官方 SDK）
  venues.py    三个预测市场的统一接口
  crossvenue.py  跨平台同一事件匹配（代码粗筛 + AI 判断 + 缓存）与计价
  deribit.py   Deribit 公开期权链 → 波动率曲线 → 到期价格概率（含 skew 修正）
  tail.py      BTC/ETH 价格合约提取（Kalshi 行权价字段 / Polymarket US 标题）与黑天鹅定价
  alerts.py    极端行情提醒（价格、波动率、资金费率、稳定币、数据源）
scripts/start_arbdesk.ps1  Windows 启动脚本（可放进开机启动）
  service.py   代码扫描（每 5 分钟）+ AI（每小时、有变化才调用）→ 生成建议
  storage.py   本地 SQLite
  web.py + static/  本地面板
tests/         单元测试：pytest
```

## 六、后续可以做的

- 资金费率套利的持仓跟踪和自动平仓提醒（费率翻负时）
- 接入 Freqtrade 做趋势类“投资”策略回测，让 AI 只负责筛选和解释
- 历史机会统计，用来评估阈值和 AI 建议的命中率
