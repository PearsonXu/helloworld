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
  $("mode").textContent = s.mode === "live" ? "实盘 LIVE" : "模拟 PAPER";
  $("mode").className = "pill " + s.mode;
  $("llm").textContent = s.llm.configured
    ? `AI ${s.llm.provider}/${s.llm.model} · 今日 ${s.llm.calls_today}/${s.llm.max_calls_per_day}`
    : "AI 未配置";
  $("times").textContent = "定时 " + s.scan_times.join(" / ");
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
    api("/api/scans?limit=1"), api("/api/analyses?limit=1"), api("/api/proposals"), api("/api/trades"),
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

  const a = analyses[0];
  if (a) {
    const c = a.content;
    $("analysis").innerHTML = `<p class="muted">${esc(a.ts)} · ${esc(a.provider)}/${esc(a.model)}</p>
      <p>${esc(c.market_view)}</p>
      ${(c.ranked || []).map((r) => `<div>#${esc(r.ref)} <b class="${r.verdict === "actionable" ? "good" : r.verdict === "watch" ? "warn" : "muted"}">${esc(r.verdict)}</b> ${esc(r.reason)}</div>`).join("")}
      ${(c.risks || []).length ? `<p class="warn">风险：${(c.risks || []).map(esc).join("；")}</p>` : ""}`;
  }

  $("proposals").innerHTML = proposals.map((p) => {
    const legs = (p.payload.legs || []).map((l) =>
      `${esc(l.exchange)} ${esc(l.market || "spot")} ${esc(l.side)} ${esc(l.symbol)} $${esc(l.usd)}`).join("<br>");
    const actions = p.status === "pending"
      ? `<button data-act="approve" data-id="${p.id}">批准执行</button> <button class="danger" data-act="reject" data-id="${p.id}">拒绝</button>`
      : `<span class="muted">${esc(p.status)}</span>`;
    return `<div class="card"><div class="row"><b>#${p.id} ${esc(p.payload.title)}</b>
      <span class="muted">置信度 ${esc(p.payload.confidence)}</span><span style="flex:1"></span>${actions}</div>
      <div>${esc(p.payload.rationale)}</div><div class="muted">${legs}</div>
      <div class="muted">退出：${esc(p.payload.exit_plan)}</div>
      ${p.note ? `<div class="warn">${esc(p.note)}</div>` : ""}</div>`;
  }).join("") || `<p class="muted">暂无</p>`;

  $("trades").innerHTML = trades.map((t) => `<tr><td>${esc(t.ts)}</td><td>${esc(t.mode)}</td><td>${esc(t.exchange)}</td>
    <td>${esc(t.symbol)}</td><td>${esc(t.side)}</td><td>${num(t.amount, 6)}</td><td>${num(t.price, 4)}</td>
    <td>${num(t.cost_usd)}</td><td>${num(t.fee_usd, 4)}</td><td>${num(t.pnl_usd, 4)}</td></tr>`).join("");
}

document.addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const { act, id } = b.dataset;
  if (act === "approve" && !confirm(`确认执行建议 #${id}？`)) return;
  b.disabled = true;
  try {
    const r = await api(`/api/proposals/${id}/${act}`, {});
    if (r.errors && r.errors.length) alert(r.errors.join("\n"));
  } catch (err) { alert(err.message); }
  refresh();
});
$("run").onclick = () => api("/api/run", { use_llm: true }).then(refresh);
$("runScan").onclick = () => api("/api/run", { use_llm: false }).then(refresh);

refresh().catch(() => {});
setInterval(() => refresh().catch(() => {}), 10000);
