"""Extreme-market alerts (no trading, no AI tokens).

Checked on every code scan (default every 5 minutes):
  * price shock   — BTC/ETH index move over 1h / 24h (Deribit index, public)
  * vol spike     — near-dated at-the-money implied vol high, or jumped vs 24h ago
  * funding       — perpetual funding extreme in either direction (crowded positioning)
  * stablecoin    — USDT / USDC off their $1 peg (Kraken, public)
  * outage        — a data source that was working fails several scans in a row

Each alert has a key and a cooldown so a persisting condition isn't repeated.
"critical" alerts can engage the kill switch when live trading is on: extreme
moves are when fills, quotes and exchanges are least reliable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

DERIBIT = "https://www.deribit.com/api/v2/public"


@dataclass
class Alert:
    key: str          # dedupe key, e.g. "move1h:BTC:down"
    level: str        # info | warning | critical
    title: str
    detail: str


# --------------------------------------------------------------------------
# Pure detectors
# --------------------------------------------------------------------------

def pct(a: float, b: float) -> float:
    return (a / b - 1) * 100


def price_alerts(ccy: str, now_price: float, price_1h: float | None, price_24h: float | None, cfg) -> list[Alert]:
    out = []
    if price_1h:
        ch = pct(now_price, price_1h)
        if abs(ch) >= cfg.move_1h_pct:
            level = "critical" if abs(ch) >= cfg.critical_move_1h_pct else "warning"
            word = "暴跌" if ch < 0 else "暴涨"
            out.append(Alert(f"move1h:{ccy}:{'down' if ch < 0 else 'up'}", level,
                             f"{ccy} 1 小时{word} {ch:+.1f}%",
                             f"{price_1h:,.0f} → {now_price:,.0f}。极端行情下报价和成交最不可靠；"
                             "黑天鹅定价表和抄底机会值得看一眼，但先确认持仓风险。"))
    if price_24h:
        ch = pct(now_price, price_24h)
        if abs(ch) >= cfg.move_24h_pct:
            out.append(Alert(f"move24h:{ccy}:{'down' if ch < 0 else 'up'}", "warning",
                             f"{ccy} 24 小时{'下跌' if ch < 0 else '上涨'} {ch:+.1f}%",
                             f"{price_24h:,.0f} → {now_price:,.0f}"))
    return out


def vol_alerts(ccy: str, iv: float, iv_24h: float | None, cfg) -> list[Alert]:
    out = []
    if iv >= cfg.iv_high:
        out.append(Alert(f"ivhigh:{ccy}", "warning", f"{ccy} 隐含波动率 {iv * 100:.0f}%",
                         "期权市场在为大幅波动定价；尾部合约普遍变贵，黑天鹅彩票更难便宜。"))
    if iv_24h and iv - iv_24h >= cfg.iv_jump_points:
        out.append(Alert(f"ivjump:{ccy}", "warning", f"{ccy} 隐含波动率 24 小时上升 {(iv - iv_24h) * 100:.0f} 个点",
                         f"{iv_24h * 100:.0f}% → {iv * 100:.0f}%：市场恐慌或预期重大事件。"))
    return out


def funding_alerts(ccy: str, funding_8h: float | None, cfg) -> list[Alert]:
    if funding_8h is None or abs(funding_8h) < cfg.funding_8h_abs:
        return []
    side = "多头" if funding_8h > 0 else "空头"
    return [Alert(f"funding:{ccy}:{'pos' if funding_8h > 0 else 'neg'}", "warning",
                  f"{ccy} 永续资金费率极端：{funding_8h * 100:+.3f}%/8h",
                  f"{side}拥挤（年化约 {funding_8h * 3 * 365 * 100:+.0f}%），容易出现反向踩踏和连环爆仓。")]


def depeg_alerts(symbol: str, price: float, cfg) -> list[Alert]:
    dev = (price - 1) * 100
    if abs(dev) < cfg.depeg_pct:
        return []
    level = "critical" if abs(dev) >= cfg.critical_depeg_pct else "warning"
    return [Alert(f"depeg:{symbol}", level, f"{symbol} 脱锚 {dev:+.2f}%（{price:.4f}）",
                  "稳定币偏离 $1：所有以它计价的报价、保证金和提现都可能受影响。")]


class OutageTracker:
    """Alert when a source that has worked before fails `n` scans in a row (once per outage)."""

    def __init__(self, n: int):
        self.n, self.fails, self.ever_ok, self.alerted = n, {}, set(), set()

    def update(self, ok: set[str], failed: set[str]) -> list[Alert]:
        out = []
        for s in ok:
            self.ever_ok.add(s)
            self.fails[s] = 0
            self.alerted.discard(s)
        for s in failed:
            self.fails[s] = self.fails.get(s, 0) + 1
            if s in self.ever_ok and self.fails[s] >= self.n and s not in self.alerted:
                self.alerted.add(s)
                out.append(Alert(f"outage:{s}", "warning", f"数据源连续 {self.fails[s]} 次失败：{s}",
                                 "可能是平台宕机、维护或网络问题；极端行情时交易所宕机本身就是风险信号。"))
        return out


def atm_vol(smiles, now: datetime | None = None, min_days: float = 3) -> float | None:
    """At-the-money implied vol of the nearest expiry at least `min_days` out."""
    now = now or datetime.now(timezone.utc)
    for s in smiles:
        if (s.expiry - now).total_seconds() >= min_days * 86400:
            return s.vol(s.forward)
    return None


# --------------------------------------------------------------------------
# Data + orchestration
# --------------------------------------------------------------------------

class MarketFeeds:
    """Public endpoints only."""

    def __init__(self, session: requests.Session | None = None, timeout: float = 10):
        self.s = session or requests.Session()
        self.timeout = timeout

    def index_price(self, ccy: str) -> float:
        r = self.s.get(f"{DERIBIT}/get_index_price", params={"index_name": f"{ccy.lower()}_usd"}, timeout=self.timeout)
        r.raise_for_status()
        return float(r.json()["result"]["index_price"])

    def funding_8h(self, ccy: str) -> float | None:
        r = self.s.get(f"{DERIBIT}/get_book_summary_by_instrument",
                       params={"instrument_name": f"{ccy}-PERPETUAL"}, timeout=self.timeout)
        r.raise_for_status()
        res = r.json().get("result") or [{}]
        v = res[0].get("funding_8h")
        return float(v) if v is not None else None

    def stable_prices(self, exchange: str, symbols: list[str]) -> dict[str, float]:
        import ccxt
        ex = getattr(ccxt, exchange)({"enableRateLimit": True, "timeout": 10000})
        t = ex.fetch_tickers(symbols)
        return {s: float(v["last"]) for s, v in t.items() if v.get("last")}


class AlertMonitor:
    def __init__(self, settings, store, feeds: MarketFeeds | None = None):
        self.settings = settings
        self.store = store
        self.feeds = feeds or MarketFeeds()
        self.outages = OutageTracker(settings.alerts.outage_consecutive)

    def check(self, smiles_by_ccy: dict | None = None, sources_ok: set[str] = frozenset(),
              sources_failed: set[str] = frozenset()) -> list[dict]:
        cfg = self.settings.alerts
        found: list[Alert] = []
        errors = {}
        for ccy in cfg.currencies:
            try:
                p = self.feeds.index_price(ccy)
                self.store.add_sample(f"price:{ccy}", p)
                found += price_alerts(ccy, p, self.store.sample_near(f"price:{ccy}", hours=1),
                                      self.store.sample_near(f"price:{ccy}", hours=24), cfg)
            except Exception as e:
                errors[f"price:{ccy}"] = f"{type(e).__name__}: {e}"
            try:
                found += funding_alerts(ccy, self.feeds.funding_8h(ccy), cfg)
            except Exception as e:
                errors[f"funding:{ccy}"] = f"{type(e).__name__}: {e}"
            iv = atm_vol((smiles_by_ccy or {}).get(ccy) or [])
            if iv:
                self.store.add_sample(f"iv:{ccy}", iv)
                found += vol_alerts(ccy, iv, self.store.sample_near(f"iv:{ccy}", hours=24), cfg)
        if cfg.stablecoins:
            try:
                for sym, price in self.feeds.stable_prices(cfg.stable_exchange, cfg.stablecoins).items():
                    found += depeg_alerts(sym, price, cfg)
            except Exception as e:
                errors["stablecoins"] = f"{type(e).__name__}: {e}"
        deribit_ok = not any(k.startswith("price:") for k in errors)
        found += self.outages.update(set(sources_ok) | ({"deribit"} if deribit_ok else set()),
                                     set(sources_failed) | (set() if deribit_ok else {"deribit"}))
        if errors:
            log.info("alert feeds failed: %s", errors)
        fired = []
        for a in found:
            if self.store.alert_recent(a.key, hours=cfg.cooldown_hours, level=a.level):
                continue
            aid = self.store.add_alert(a.key, a.level, a.title, a.detail)
            fired.append({"id": aid, **a.__dict__})
            if a.level == "critical":
                self._critical(a)
        if fired:
            notify(fired, cfg.desktop_notify)
        self.store.prune_samples()
        return fired

    def _critical(self, a: Alert) -> None:
        s = self.settings
        any_live = s.risk.mode == "live" or any(
            s.venue_cfg(v).enabled and s.venue_cfg(v).mode == "live" for v in ("polymarket", "kalshi", "polymarket_us"))
        if s.alerts.halt_on_critical and any_live and not self.store.halted():
            self.store.set_flag("halt", f"自动停止：{a.title}")
            log.warning("kill switch engaged by alert: %s", a.title)


def notify(alerts: list[dict], desktop: bool) -> None:
    for a in alerts:
        log.warning("ALERT [%s] %s — %s", a["level"], a["title"], a["detail"])
    if not desktop:
        return
    try:
        from plyer import notification  # optional; Windows / macOS / Linux desktop toasts
        for a in alerts[:3]:
            notification.notify(title=f"ArbDesk · {a['title']}", message=a["detail"][:240], timeout=15)
    except Exception:
        pass  # no notifier available: the panel banner and log still show it
