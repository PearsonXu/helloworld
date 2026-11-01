// All dynamic text (including AI output) is escaped before insertion.
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v, d = 2) => (v == null || isNaN(v) ? "—" : Number(v).toFixed(d));

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  if (r.status === 401) { $("login").style.display = "block"; throw new Error("unauthorized"); }
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}

const TYPE = { cross_exchange: "跨所价差", funding_carry: "资金费率套利", funding_spread: "跨所费率差", triangular: "三角套利" };

function oppDetail(o) {
  switch (o.type) {
    case "cross_exchange": return `${o.buy_exchange} 买 ${o.buy_price} → ${o.sell_exchange} 卖 ${o.sell_price}`;
    case "funding_carry": return `${o.exchange} 费率 ${o.funding_rate_pct}%/期，往返手续费 ${o.round_trip_fee_pct}%`;
    case "funding_spread": return `多 ${o.long_perp_exchange} / 空 ${o.short_perp_exchange}`;
    case "triangular": return o.path;
    default: return "";
  }
}
function oppReturn(o) {
  if (o.net_pct != null) return `${o.net_pct}%`;
  if (o.apr_pct != null) return `年化 ${o.apr_pct}%`;
  if (o.apr_spread_pct != null) return `年化差 ${o.apr_spread_pct}%`;
  return "";
}

async function refresh() {
  const s = await api("/api/state");
  $("mode").textContent = s.mode === "live" ? "加密货币 实盘 LIVE" : "加密货币 模拟";
  $("mode").className = "pill " + s.mode;
  const L = s.llm;
  $("llm").textContent = L.configured
    ? `AI ${L.model} · 今日 ${(L.tokens_today / 1000).toFixed(1)}k/${(L.daily_token_budget / 1000).toFixed(0)}k token · 本月 $${num(L.month_usd, 3)}/$${num(L.monthly_usd_cap)}` +
      (L.ok ? "" : ` · 已暂停：${L.reason}`)
    : "AI 未配置";
  $("llm").className = "pill" + (L.configured && !L.ok ? " live" : "");
  $("llm").title = (L.by_purpose || []).map((p) => `${p.purpose}: ${p.calls} 次, 输入 ${p.input} (缓存 ${p.cached}), 输出 ${p.output}, $${num(p.usd, 4)}`).join("\n");
  $("times").textContent = `代码扫描每 ${s.intervals.scan_minutes} 分钟 · AI 每 ${s.intervals.ai_minutes} 分钟（有变化才调用）`;
  $("venuePills").innerHTML = Object.values(s.venues).map((v) => {
    const live = v.mode === "live";
    const env = v.environment === "demo" && live ? "（demo 假钱）" : "";
    return `<span class="pill ${live ? "live" : "paper"}">${esc(v.label)} ${live ? "实盘" + env : "模拟"}</span>`;
  }).join("");
  $("secsrc").textContent = { vault: "密钥：加密保险库", "vault+env": "密钥：保险库 + .env（建议迁移）", env: "密钥：.env 明文" }[s.secrets_source] || s.secrets_source;
  $("haltBanner").style.display = s.halted ? "" : "none";
  $("haltReason").textContent = s.halted ? `原因：${s.halted}` : "";
  $("halt").style.display = s.halted ? "none" : "";
  window.__live = { crypto: s.mode === "live", venues: s.venues };
  $("run").disabled = $("runScan").disabled = s.running;
  $("run").textContent = s.running ? "运行中…" : "立即扫描 + AI 分析";
  const pnlCls = s.today.pnl_usd >= 0 ? "good" : "bad";
  $("stats").innerHTML = `
    <div class="stat"><b>${s.today.trades} / ${s.risk.max_trades_per_day}</b><span>今日交易</span></div>
    <div class="stat"><b class="${pnlCls}">${num(s.today.pnl_usd)}</b><span>今日已实现 PnL $</span></div>
    <div class="stat"><b>${num(s.today.fees_usd)}</b><span>今日手续费 $</span></div>
    <div class="stat"><b>${esc(s.keyed_exchanges.join(", ") || "无")}</b><span>已配置密钥的交易所</span></div>`;
  $("riskline").textContent = `风控：总资金上限 $${s.risk.max_capital_usd} · 单笔 ≤ $${s.risk.max_per_trade_usd} · ` +
    `日亏损上限 $${s.risk.daily_loss_limit_usd} · 滑点 ≤ ${s.risk.max_slippage_pct}% · 实盘永续 ${s.risk.allow_live_swap ? "允许" : "禁止"}`;

  const [scans, analyses, proposals, trades] = await Promise.all([
    api("/api/scans?limit=1"), api("/api/analyses?limit=5"), api("/api/proposals"), api("/api/trades"),
  ]);

  const scan = scans[0];
  if (scan) {
    const errs = Object.keys(scan.summary.errors || {});
    $("scanmeta").textContent = `${scan.ts} · ${scan.opportunities.length} 个机会` + (errs.length ? ` · 失败: ${errs.join(", ")}` : "");
    $("opps").innerHTML = scan.opportunities.map((o) => `<tr>
      <td>${esc(TYPE[o.type] || o.type)}</td><td>${esc(o.symbol || "")}</td><td>${esc(oppDetail(o))}</td>
      <td>${esc(oppReturn(o))}</td><td class="muted">${esc(o.note)}</td></tr>`).join("")
      || `<tr><td colspan="5" class="muted">本次没有超过阈值的机会（这很正常）</td></tr>`;
  }

  const a = analyses.find((x) => x.content.kind !== "pm_forecast");
  if (a) {
    const c = a.content;
    $("analysis").innerHTML = `<p class="muted">${esc(a.ts)} · ${esc(a.provider)}/${esc(a.model)}</p>
      <p>${esc(c.market_view)}</p>
      ${(c.ranked || []).map((r) => `<div>#${esc(r.ref)} <b class="${r.verdict === "actionable" ? "good" : r.verdict === "watch" ? "warn" : "muted"}">${esc(r.verdict)}</b> ${esc(r.reason)}</div>`).join("")}
      ${(c.risks || []).length ? `<p class="warn">风险：${(c.risks || []).map(esc).join("；")}</p>` : ""}`;
  }

  $("proposals").innerHTML = proposals.map((p) => {
    const isPred = (l) => l.token_id !== undefined;
    const legs = (p.payload.legs || []).map((l) => isPred(l)
      ? `${esc(VENUE_LABEL[l.exchange] || l.exchange)} 买 ${esc(l.outcome)} · ${esc(l.label)} · ${esc(l.shares)} 份 @ ≤${esc(l.limit_price)}`
      : `${esc(l.exchange)} ${esc(l.market || "spot")} ${esc(l.side)} ${esc(l.symbol)} $${esc(l.usd)}`).join("<br>");
    const first = (p.payload.legs || [])[0] || {};
    const venue = window.__live.venues[first.exchange];
    const live = venue ? venue.mode === "live" : window.__live.crypto;
    const cost = (p.payload.legs || []).reduce((t, l) => t + (isPred(l) ? l.shares * l.limit_price : +l.usd || 0), 0);
    const demo = venue && venue.environment === "demo" && first.exchange === "kalshi";
    const liveLabel = demo ? "批准（Kalshi demo 假钱）" : "批准（真钱）";
    const costLabel = `约 $${cost.toFixed(2)}${demo ? "，Kalshi demo 假钱" : "，真钱"}`;
    const actions = p.status === "pending"
      ? `<button data-act="approve" data-id="${p.id}" data-live="${live ? 1 : 0}" data-cost="${costLabel}" class="${live ? "danger" : ""}">${live ? liveLabel : "批准执行"}</button> <button class="danger" data-act="reject" data-id="${p.id}">拒绝</button>`
      : `<span class="${p.status === "partial" ? "bad" : "muted"}">${esc(p.status)}</span>`;
    return `<div class="card"><div class="row"><b>#${p.id} ${esc(p.payload.title)}</b>
      <span class="muted">置信度 ${esc(p.payload.confidence)}</span><span style="flex:1"></span>${actions}</div>
      <div>${esc(p.payload.rationale)}</div><div class="muted">${legs}</div>
      <div class="muted">退出：${esc(p.payload.exit_plan)}</div>
      ${p.note ? `<div class="warn">${esc(p.note)}</div>` : ""}</div>`;
  }).join("") || `<p class="muted">暂无</p>`;

  await renderVenues();
  await renderCross();
  await renderTail();
  await renderAlerts();
  await renderDip();
  await renderSim();
  await renderStrategies();

  $("trades").innerHTML = trades.map((t) => `<tr><td>${esc(t.ts)}</td><td>${esc(t.mode)}</td><td>${esc(t.exchange)}</td>
    <td>${esc(t.symbol)}</td><td>${esc(t.side)}</td><td>${num(t.amount, 6)}</td><td>${num(t.price, 4)}</td>
    <td>${num(t.cost_usd)}</td><td>${num(t.fee_usd, 4)}</td><td>${num(t.pnl_usd, 4)}</td></tr>`).join("");
}

const VENUE_LABEL = { polymarket: "Polymarket（国际）", kalshi: "Kalshi", polymarket_us: "Polymarket US" };

async function renderVenues() {
  const venues = await api("/api/venues");
  $("venues").innerHTML = venues.map((v) => {
    const sc = v.scores;
    const better = sc.ai_brier != null && sc.ai_brier < sc.market_brier;
    const s = v.scan;
    const errs = s ? Object.keys(s.summary.errors || {}) : [];
    const meta = s ? `${s.ts} · 事件 ${s.summary.events ?? "—"} · 互斥事件 ${s.summary.negrisk_events ?? "—"} · 初筛 ${s.summary.screened ?? "—"}` +
      (errs.length ? ` · 失败: ${errs.join(", ")}` : "") : "尚未扫描";
    const mode = v.mode === "live" ? (v.environment === "demo" ? "（实盘 · Kalshi demo 假钱）" : "（实盘：真钱）") : "（模拟盘）";
    const opps = s ? s.opportunities.map((o) => `<tr>
      <td><a href="${esc(o.url || "#")}" target="_blank" rel="noopener noreferrer">${esc(o.title)}</a></td>
      <td>买全部 ${o.direction === "buy_yes" ? "YES" : "NO"}（${esc(o.outcomes)} 个）</td><td>${num(o.shares)}</td>
      <td>${num(o.cost_usd)}</td><td>${o.direction === "buy_no" && v.name === "kalshi" ? "≥" : ""}${num(o.payout_usd)}</td>
      <td class="good">$${num(o.profit_usd, 3)} (${esc(o.net_pct)}%)</td></tr>`).join("") : "";
    const noArb = v.name === "polymarket_us"
      ? "Polymarket US 没有互斥事件标记，不自动做多结果套利；这里只做 AI 概率评估"
      : "本次没有扣费后仍有利润的组合（大部分时候都是这样）";
    const fc = v.forecasts.map((f) => `<tr><td>${esc(f.ts.slice(0, 16))}</td>
      <td>${esc(f.question)}</td><td>${num(f.market_prob)}</td><td>${num(f.ai_prob)}</td><td>${num(f.confidence)}</td>
      <td>${f.outcome == null ? '<span class="muted">未结算</span>' : f.outcome ? "YES" : "NO"}</td>
      <td class="muted">${esc(f.rationale)}</td></tr>`).join("") || `<tr><td colspan="7" class="muted">暂无</td></tr>`;
    return `<section>
      <h2>${esc(v.label)} <span class="muted">${mode}</span> <span class="muted">${esc(meta)}</span></h2>
      <div class="stats">
        <div class="stat"><b>${sc.resolved}</b><span>已结算的 AI 预测</span></div>
        <div class="stat"><b class="${sc.ai_brier == null ? "" : better ? "good" : "bad"}">${num(sc.ai_brier, 4)}</b><span>AI Brier（越低越准）</span></div>
        <div class="stat"><b>${num(sc.market_brier, 4)}</b><span>市场价 Brier</span></div>
      </div>
      <h3 style="font-size:14px;margin:14px 0 6px">多结果套利</h3>
      <table><thead><tr><th>事件</th><th>方向</th><th>份数</th><th>成本$</th><th>兑付$</th><th>利润</th></tr></thead>
        <tbody>${opps || `<tr><td colspan="6" class="muted">${noArb}</td></tr>`}</tbody></table>
      <h3 style="font-size:14px;margin:14px 0 6px">AI 概率评估记录</h3>
      <table><thead><tr><th>时间</th><th>问题</th><th>市场价</th><th>AI 概率</th><th>置信度</th><th>结果</th><th>理由</th></tr></thead>
        <tbody>${fc}</tbody></table>
    </section>`;
  }).join("");
}

async function renderStrategies() {
  const d = await api("/api/strategies");
  $("stratSection").style.display = d.enabled.length ? "" : "none";
  const pct = (v) => v == null ? "—" : `<span class="${v >= 0 ? "good" : "bad"}">${v >= 0 ? "+" : ""}${num(v)}%</span>`;
  $("stratrows").innerHTML = d.rows.map((r) => {
    const bt = r.backtest || {};
    const days = bt.from ? Math.round((bt.to - bt.from) / 86400000) : 0;
    return `<tr><td>${esc(r.label)}</td><td>${esc(r.symbol)}</td><td>${esc(r.position)}${r.note ? `<br><span class="muted">${esc(r.note)}</span>` : ""}</td>
      <td>$${num(r.equity)}</td><td>${pct(r.return_pct)}</td><td>${pct(r.buy_hold_pct)}</td>
      <td>${num(r.max_dd_pct, 1)}%</td><td>${r.trades}</td>
      <td>${bt.return_pct == null ? "—" : `${pct(bt.return_pct)} vs 持有 ${pct(bt.buy_hold_pct)} · 回撤 ${num(bt.max_drawdown_pct, 1)}% · ${bt.trades} 笔 · ${days} 天`}
        ${bt.trades_per_week != null ? `<br><span class="muted">每周 ${num(bt.trades_per_week, 1)} 笔 · 胜率 ${bt.win_rate_pct == null ? "—" : num(bt.win_rate_pct, 0) + "%"} · 平均持有 ${bt.avg_hold_days ?? "—"} 天 · 手续费占本金 ${num(bt.fees_pct_of_capital, 1)}%</span>` : ""}
        ${bt.without_lppls ? `<br><span class="muted">不加 LPPLS：${pct(bt.without_lppls.return_pct)} · 回撤 ${num(bt.without_lppls.max_drawdown_pct, 1)}% · ${bt.without_lppls.trades} 笔</span>` : ""}</td></tr>`;
  }).join("") || `<tr><td colspan="9" class="muted">下一次扫描时自动开始（每个策略每个币种 $25 虚拟资金）</td></tr>`;
  const lp = Object.entries(d.lppls || {});
  $("stratlppls").innerHTML = lp.length ? "LPPLS 泡沫置信度（日线，昨日收盘）：" + lp.map(([sym, v]) => {
    const cls = v.bubble >= 0.5 ? "bad" : v.bubble >= 0.25 ? "warn" : "good";
    return `${esc(sym)} <span class="${cls}">${num(v.bubble * 100, 0)}%</span>`
      + ` <span class="muted">(30 天最高 ${num(v.max_30d * 100, 0)}% · 反泡沫 ${num(v.anti * 100, 0)}%${v.tc_days != null ? ` · 临界点约 ${num(v.tc_days, 0)} 天` : ""})</span>`;
  }).join(" · ") + `<br><span class="muted">≥25% 收紧移动止损，≥50% 不开新仓，≥75% 离场。只作用于"定制策略1"（快速趋势 + LPPLS，BTC）。</span>` : "";
  $("stratnote").textContent = `手续费 ${d.fee_pct}% + 滑点 ${d.slippage_pct}%/笔；信号只用已收盘的 K 线。回测用同一套规则跑最近的历史行情，只代表过去；"同期持有"是同一时段一直持有不动的收益，用来对比策略有没有价值。`;
}

async function renderSim() {
  const d = await api("/api/sim");
  const r = d.snapshot;
  $("simStart").style.display = d.running ? "none" : "";
  $("simStop").style.display = d.running ? "" : "none";
  if (!r) {
    $("simmeta").textContent = "尚未开始";
    $("simstats").innerHTML = "";
    $("simline").textContent = `点"开始模拟"：默认 $${d.defaults.capital}、${d.defaults.hours} 小时。模拟期间通过风控的模拟盘建议会自动执行（实盘永远不会自动执行）。`;
    $("simstrats").innerHTML = ""; $("simpos").innerHTML = "";
    return;
  }
  const end = new Date(r.finished_ts || r.end_ts);
  const left = Math.max(0, end - new Date());
  const hh = Math.floor(left / 3600000), mm = Math.floor((left % 3600000) / 60000);
  $("simmeta").textContent = d.running
    ? `#${r.id} 进行中 · 剩余 ${hh} 小时 ${mm} 分`
    : `#${r.id} 已结束 · ${(r.finished_ts || "").slice(0, 16)} UTC`;
  const cls = (v) => (v >= 0 ? "good" : "bad");
  $("simstats").innerHTML = `
    <div class="stat"><b>$${num(r.capital)} → <span class="${cls(r.pnl)}">$${num(r.equity)}</span></b><span>初始资金 → 账户总值</span></div>
    <div class="stat"><b class="${cls(r.pnl)}">${r.return_pct >= 0 ? "+" : ""}${num(r.return_pct)}%</b><span>收益率</span></div>
    <div class="stat"><b>$${num(r.cash)}</b><span>现金（其中挂单冻结 $${num(r.reserved)}）</span></div>
    <div class="stat"><b>$${num(r.positions_value)}</b><span>持仓市值</span></div>
    <div class="stat"><b class="${cls(r.net_after_ai)}">${r.net_after_ai >= 0 ? "+" : ""}${num(r.net_after_ai)}</b><span>扣除 AI 花费后净盈亏</span></div>`;
  const pts = d.curve.map((c) => c.value);
  $("simline").textContent = `已实现 ${num(r.realized)} · 浮动 ${num(r.unrealized)} · 手续费 ${num(r.fees)} · AI 花费 $${num(r.ai_cost_usd, 4)}（${r.ai_tokens} token）` +
    (pts.length ? ` · 期间最高 $${num(Math.max(...pts))} / 最低 $${num(Math.min(...pts))}` : "") +
    (r.unpriced_positions ? ` · ${r.unpriced_positions} 个持仓暂无报价，按成本估值` : "") +
    (r.reason ? ` · 结束原因：${r.reason}` : "") +
    (r.as_of ? ` · 估值时间 ${r.as_of.slice(11, 16)} UTC（每次扫描更新）` : "");
  $("simstrats").innerHTML = r.by_strategy.map((b) => `<tr><td>${esc(b.label)}</td><td>${b.trades}</td>
    <td>${num(b.spent)}</td><td class="${cls(b.realized)}">${num(b.realized)}</td>
    <td class="${cls(b.unrealized || 0)}">${num(b.unrealized || 0)}</td><td>${num(b.fees)}</td></tr>`).join("")
    || `<tr><td colspan="6" class="muted">还没有成交（很多时候一天内都不会有机会，这本身也是结果）</td></tr>`;
  $("simpos").innerHTML = r.positions.map((p) => `<tr><td>${esc(VENUE_LABEL[p.venue] || p.venue)}</td>
    <td>${esc(p.label)}</td><td>${num(p.qty, 4)}</td><td>${num(p.cost)}</td>
    <td>${p.price == null ? "—" : num(p.price, 4)}</td><td>${num(p.value)}</td>
    <td class="${cls(p.unrealized)}">${num(p.unrealized)}</td></tr>`).join("")
    || `<tr><td colspan="7" class="muted">无持仓</td></tr>`;
}

async function renderDip() {
  const d = await api("/api/dip");
  window.__dip = d;
  $("dipSection").style.display = d.enabled ? "" : "none";
  if (!d.enabled) return;
  $("dipmeta").textContent = `${d.mode === "live" ? "实盘" : "模拟盘"} · ${d.exchange}`;
  $("dipstats").innerHTML = `
    <div class="stat"><b>$${num(d.open_usd)} / $${num(d.max_open_usd)}</b><span>挂单中金额</span></div>
    <div class="stat"><b>$${num(d.filled_quarter_usd)} / $${num(d.quarterly_budget_usd)}</b><span>本季度已成交 / 季度预算</span></div>`;
  $("diprows").innerHTML = d.symbols.map((r) => {
    const open = r.open.map((o) => `${num(o.level_pct, 0)}% @ ${num(o.price, 2)}`).join("<br>") || '<span class="muted">无</span>';
    const pnl = r.unrealized == null ? "—" : `<span class="${r.unrealized >= 0 ? "good" : "bad"}">${num(r.unrealized)}</span>`;
    return `<tr><td>${esc(r.symbol)}</td><td>${r.price == null ? "—" : num(r.price, 2)}</td><td>${open}</td>
      <td>${r.filled.length} 笔</td><td>${r.position_qty ? "$" + num(r.position_cost) : "—"}</td><td>${pnl}</td></tr>`;
  }).join("");
}

async function renderAlerts() {
  const a = await api("/api/alerts");
  const list = a.unacked || [];
  $("alertBanner").style.display = a.enabled && list.length ? "" : "none";
  const em = a.email || {};
  $("emailStatus").textContent = !em.enabled ? "邮件提醒：未开启（见 README「邮件提醒」）"
    : em.problems.length ? `邮件提醒：配置不完整 — ${em.problems.join("；")}`
    : `邮件提醒：已开启` + (em.last ? ` · 最近一封 ${em.last.ts.slice(0, 16)} ${em.last.ok ? "成功" : "失败：" + (em.last.error || "")}` : "");
  const color = { critical: "bad", warning: "warn", info: "muted" };
  $("alertList").innerHTML = list.map((x) => `<div class="card"><div class="row">
      <b class="${color[x.level] || ""}">${x.level === "critical" ? "严重" : x.level === "warning" ? "注意" : "提示"} · ${esc(x.title)}</b>
      <span class="muted">${esc(x.ts.slice(0, 16))}</span><span style="flex:1"></span>
      <button class="ghost" data-ack="${x.id}">已读</button></div>
      <div class="muted">${esc(x.detail)}</div></div>`).join("");
}

async function renderTail() {
  const t = await api("/api/tail");
  $("tailSection").style.display = t.enabled ? "" : "none";
  if (!t.enabled) return;
  const sc = t.scan;
  const errs = sc ? Object.keys(sc.summary.errors || {}) : [];
  $("tailmeta").textContent = sc ? `${sc.ts} · 合约 ${sc.summary.contracts} · 已定价 ${sc.summary.priced}` +
    (errs.length ? ` · 失败: ${errs.join(", ")}` : "") : "尚未扫描";
  const left = t.budget_quarter - t.spent_quarter;
  $("tailstats").innerHTML = `
    <div class="stat"><b class="${left > 0 ? "" : "bad"}">$${num(t.spent_quarter)} / $${num(t.budget_quarter)}</b><span>本季度黑天鹅预算已用</span></div>
    <div class="stat"><b>$${num(t.max_usd_per_bet)}</b><span>单注上限</span></div>
    <div class="stat"><b>${sc ? sc.opportunities.length : 0}</b><span>当前便宜的彩票</span></div>`;
  const rows = sc ? sc.summary.rows || [] : [];
  const strike = (r) => r.kind === "between" ? `${r.lo}–${r.hi}` : (r.lo ?? r.hi);
  const kindLabel = { above: "高于", below: "低于", between: "区间", touch_above: "触及≥", touch_below: "触及≤" };
  $("tailrows").innerHTML = rows.map((r) => `<tr class="${r.cheap ? "" : "muted"}">
    <td>${esc(VENUE_LABEL[r.venue] || r.venue)}</td>
    <td>${esc(r.currency)} ${esc(kindLabel[r.kind] || r.kind)} ${esc(strike(r))}<br><span class="muted">${esc(r.label)}</span></td>
    <td>${esc(r.expiry.slice(0, 16))}</td><td>${esc(r.side)}</td><td>${num(r.price * 100, 1)}¢</td>
    <td>${num(r.implied * 100, 1)}%</td><td class="${r.cheap ? "good" : ""}">${num(r.ratio, 2)}×</td>
    <td>${num(r.edge * 100, 1)}¢</td></tr>`).join("")
    || `<tr><td colspan="8" class="muted">暂无可定价的合约</td></tr>`;
}

async function renderCross() {
  const c = await api("/api/crossvenue");
  $("crossSection").style.display = c.enabled ? "" : "none";
  if (!c.enabled) return;
  const sc = c.scan;
  $("crossmeta").textContent = sc ? `${sc.ts} · 已确认同一事件 ${sc.summary.matched_pairs ?? 0} 对` : "尚未扫描";
  $("crossopps").innerHTML = (sc ? sc.opportunities : []).map((o) => `<tr>
    <td>${esc(o.title)}<br><span class="muted">${esc(o.title_b)}</span></td>
    <td>${esc(VENUE_LABEL[o.venue_a])} ${esc(o.legs[0].outcome)} + ${esc(VENUE_LABEL[o.venue_b])} ${esc(o.legs[1].outcome)}</td>
    <td>${num(o.shares, 0)}</td><td>${num(o.cost_usd)}</td><td>${num(o.payout_usd)}</td>
    <td class="good">$${num(o.profit_usd, 3)} (${esc(o.net_pct)}%)</td></tr>`).join("")
    || `<tr><td colspan="6" class="muted">当前没有扣费后仍有利润的跨平台组合</td></tr>`;
  $("crosspairs").innerHTML = c.pairs.map((p) => `<tr>
    <td>${esc(p.title_a)}</td><td>${esc(p.title_b)}</td><td>${num(p.similarity)}</td>
    <td class="${p.verdict === "same" ? "good" : "muted"}">${p.verdict === "same" ? "同一事件" : "不同"}</td>
    <td>${num(p.confidence)}</td><td class="muted">${esc(p.reason)}</td></tr>`).join("")
    || `<tr><td colspan="6" class="muted">暂无（AI 每次只判断少量新候选，结果永久缓存）</td></tr>`;
}

document.addEventListener("click", async (e) => {
  const ack = e.target.closest("button[data-ack]");
  if (ack) { await api("/api/alerts/ack", { id: Number(ack.dataset.ack) }); return refresh(); }
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const { act, id } = b.dataset;
  const isLive = b.dataset.live === "1";
  let body = {};
  if (act === "approve") {
    if (isLive) {
      const typed = prompt(`⚠️ 建议 #${id} 将实际下单（${b.dataset.cost}）。\n确认请输入大写 LIVE：`);
      if (typed !== "LIVE") return;
      body = { confirm_live: "LIVE" };
    } else if (!confirm(`确认执行建议 #${id}（模拟）？`)) return;
  }
  b.disabled = true;
  try {
    const r = await api(`/api/proposals/${id}/${act}`, body);
    if (r.errors && r.errors.length) alert(r.errors.join("\n"));
  } catch (err) { alert(err.message); }
  refresh();
});
$("run").onclick = () => api("/api/run", { use_llm: true }).then(refresh);
$("propRefresh").onclick = async () => {
  const b = $("propRefresh");
  b.disabled = true; b.textContent = "检查中…";
  try { await api("/api/proposals/revalidate", {}); await refresh(); }
  catch (e) { alert(e.message); }
  finally { b.disabled = false; b.textContent = "刷新"; }
};
$("stratReset").onclick = () => confirm("清空所有策略的模拟账本，从下一次扫描重新开始？") && api("/api/strategies/reset", {}).then(refresh);
$("simStart").onclick = async () => {
  const d = await api("/api/sim");
  const capital = prompt("模拟初始资金（美元）：", d.defaults.capital);
  if (capital === null) return;
  const hours = prompt("模拟时长（小时）：", d.defaults.hours);
  if (hours === null) return;
  try { await api("/api/sim/start", { capital: Number(capital), hours: Number(hours) }); } catch (e) { alert(e.message); }
  refresh();
};
$("simStop").onclick = () => confirm("提前结束当前模拟并生成报告？") && api("/api/sim/stop", {}).then(refresh);
$("dipArm").onclick = async () => {
  const live = window.__dip && window.__dip.mode === "live";
  let body = {};
  if (live) {
    const typed = prompt("⚠️ 将在交易所挂出真钱限价买单（会一直挂着，直到成交或撤销）。\n确认请输入大写 LIVE：");
    if (typed !== "LIVE") return;
    body = { confirm_live: "LIVE" };
  } else if (!confirm("按现价重新布置模拟挂单？")) return;
  const r = await api("/api/dip/arm", body);
  if (r.errors && r.errors.length) alert(r.errors.join("\n"));
  refresh();
};
$("dipCancel").onclick = () => confirm("撤销全部抄底挂单？") && api("/api/dip/cancel", {}).then(refresh);
$("ackAll").onclick = () => api("/api/alerts/ack", {}).then(refresh);
$("halt").onclick = () => confirm("立即停止所有实盘下单？") && api("/api/halt", {}).then(refresh);
$("resume").onclick = () => confirm("确认已在交易所 / 预测市场检查过持仓，恢复实盘？") && api("/api/resume", {}).then(refresh);
$("runScan").onclick = () => api("/api/run", { use_llm: false }).then(refresh);

refresh().catch(() => {});
setInterval(() => refresh().catch(() => {}), 10000);
