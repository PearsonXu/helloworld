"""Simulated account: a timed paper-trading session with a fixed starting capital.

While a session runs:
  * every paper fill since the session start is booked against its cash;
  * resting paper dip orders reserve cash, as an exchange would lock funds;
  * paper proposals that pass the risk checks are executed automatically (live
    trading is never automatic), so strategies are actually exercised;
  * a proposal whose cost exceeds the available cash is refused.

Valuation is conservative: open prediction-market positions at the price you
could SELL at now (best bid), crypto at the last price. Right after buying, the
spread therefore shows up as a small loss — that is a real cost.

At the end, a report gives equity, return, realized vs. unrealized P&L, fees,
AI token cost in the window and a per-strategy breakdown.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from .risk import PREDICTION_VENUES

log = logging.getLogger(__name__)

STRATEGY_LABELS = {
    "dip": "抄底挂单", "tail": "黑天鹅彩票", "cross": "跨平台套利", "bundle": "多结果套利",
    "forecast": "AI 概率判断", "crypto": "加密货币", "other": "其他",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def strategy_of(trade: dict) -> str:
    raw = json.loads(trade.get("raw") or "{}") if trade.get("raw") else {}
    if raw.get("kind") == "dip":
        return "dip"
    if trade.get("proposal_kind") == "tail":
        return "tail"
    key = trade.get("dedupe_key") or ""
    if key.startswith("cross:"):
        return "cross"
    if ":bundle:" in key:
        return "bundle"
    if ":forecast:" in key:
        return "forecast"
    if key.startswith("crypto:") or trade.get("exchange") not in PREDICTION_VENUES:
        return "crypto"
    return "other"


class SimAccount:
    def __init__(self, settings, store, desk=None):
        self.settings = settings
        self.store = store
        self.desk = desk  # for live prices (venues, dip market); optional

    # ------------------------------------------------------------------
    def current(self) -> dict | None:
        return self.store.sim_running()

    def start(self, capital: float | None = None, hours: float | None = None) -> dict:
        if self.current():
            raise ValueError("a simulation is already running; stop it first")
        c = self.settings.sim
        capital = float(capital if capital is not None else c.capital)
        hours = float(hours if hours is not None else c.hours)
        if capital <= 0 or hours <= 0:
            raise ValueError("capital and hours must be positive")
        start = _now()
        sid = self.store.add_sim(start.isoformat(timespec="seconds"),
                                 (start + timedelta(hours=hours)).isoformat(timespec="seconds"), capital)
        # Proposals numbered above this were created during the session (timestamps are per second).
        self.store.set_flag(f"sim_last_pid:{sid}", str(self.store.max_proposal_id()))
        self.store.set_flag("sim_last_report", "")
        return self.store.sim(sid)

    def stop(self) -> dict | None:
        s = self.current()
        if not s:
            return None
        return self.finish(s, reason="stopped by user")

    # ------------------------------------------------------------------
    def _trades(self, s: dict) -> list[dict]:
        end = s["finished_ts"] or s["end_ts"]
        return self.store.trades_between(s["start_ts"], end, mode="paper")

    def ledger(self, s: dict | None = None) -> dict:
        """Cash and open positions from the trades (no prices needed)."""
        s = s or self.current()
        if not s:
            return {}
        cash, fees, realized = s["capital"], 0.0, 0.0
        pred: dict[tuple, dict] = {}
        crypto: dict[tuple, dict] = {}
        by_strategy: dict[str, dict] = {}
        for t in self._trades(s):
            strat = strategy_of(t)
            agg = by_strategy.setdefault(strat, {"trades": 0, "spent": 0.0, "realized": 0.0, "fees": 0.0})
            agg["trades"] += 1
            agg["fees"] += t["fee_usd"]
            fees += t["fee_usd"]
            if t["side"] == "buy":
                cash -= t["cost_usd"] + t["fee_usd"]
                agg["spent"] += t["cost_usd"] + t["fee_usd"]
            else:
                cash += t["cost_usd"] - t["fee_usd"]
            if t["exchange"] in PREDICTION_VENUES:
                if t["pnl_usd"] is not None:  # settled: the payout came back as cash
                    cash += t["pnl_usd"] + t["cost_usd"] + t["fee_usd"]
                    realized += t["pnl_usd"]
                    agg["realized"] += t["pnl_usd"]
                else:
                    info = json.loads(t["raw"] or "{}")
                    k = (t["exchange"], info.get("token_id"))
                    p = pred.setdefault(k, {"venue": t["exchange"], "token_id": info.get("token_id"),
                                            "label": t["symbol"], "qty": 0.0, "cost": 0.0, "strategy": strat})
                    p["qty"] += t["amount"]
                    p["cost"] += t["cost_usd"] + t["fee_usd"]
            else:
                k = (t["exchange"], t["symbol"])
                p = crypto.setdefault(k, {"venue": t["exchange"], "symbol": t["symbol"], "label": t["symbol"],
                                          "qty": 0.0, "cost": 0.0, "strategy": strat})
                sign = 1 if t["side"] == "buy" else -1
                p["qty"] += sign * t["amount"]
                p["cost"] += sign * (t["cost_usd"] + (t["fee_usd"] if sign > 0 else -t["fee_usd"]))
        reserved = self.store.dip_open_usd("paper") if s["status"] == "running" else 0.0
        return {"cash": cash, "reserved": reserved, "available": cash - reserved, "fees": fees,
                "realized": realized, "by_strategy": by_strategy,
                "positions": list(pred.values()) + [p for p in crypto.values() if abs(p["qty"]) > 1e-12]}

    def is_session_proposal(self, s: dict, p: dict) -> bool:
        """Created during the session, or an older opportunity refreshed (still live) after it began."""
        flag = self.store.get_flag(f"sim_last_pid:{s['id']}")
        last = int(flag["value"]) if flag and flag["value"] else 0
        return p["id"] > last or p["ts"] > s["start_ts"]

    def available(self) -> float | None:
        """Cash free for new paper trades, or None when no session runs (no limit)."""
        s = self.current()
        return self.ledger(s)["available"] if s else None

    # ------------------------------------------------------------------
    def _price(self, p: dict) -> float | None:
        """What an open position could be sold for now (per unit)."""
        if self.desk is None:
            return None
        try:
            if p["venue"] in PREDICTION_VENUES:
                v = self.desk.venues.get(p["venue"])
                return v.bid(p["token_id"]) if v else None
            return self.desk.dip.market.price(p["symbol"])
        except Exception as e:
            log.info("no price for %s: %s", p.get("label"), e)
            return None

    def snapshot(self, s: dict | None = None, record: bool = True) -> dict | None:
        s = s or self.current()
        if not s:
            return None
        led = self.ledger(s)
        value, unpriced = 0.0, 0
        for p in led["positions"]:
            px = self._price(p)
            if px is None:
                p["value"], p["price"] = p["cost"], None  # fall back to cost when no quote is available
                unpriced += 1
            else:
                p["price"], p["value"] = px, p["qty"] * px
            p["unrealized"] = p["value"] - p["cost"]
            agg = led["by_strategy"].setdefault(p["strategy"], {"trades": 0, "spent": 0.0, "realized": 0.0, "fees": 0.0})
            agg["unrealized"] = agg.get("unrealized", 0.0) + p["unrealized"]
            value += p["value"]
        equity = led["cash"] + value
        ai = self.store.usage_between(s["start_ts"], s["finished_ts"] or _now().isoformat(timespec="seconds"))
        snap = {
            **{k: s[k] for k in ("id", "status", "start_ts", "end_ts", "finished_ts", "capital")},
            "cash": led["cash"], "reserved": led["reserved"], "available": led["available"],
            "positions_value": value, "equity": equity, "pnl": equity - s["capital"],
            "return_pct": (equity / s["capital"] - 1) * 100, "realized": led["realized"],
            "unrealized": sum(p["unrealized"] for p in led["positions"]), "fees": led["fees"],
            "ai_cost_usd": ai["usd"], "ai_tokens": ai["tokens"],
            "net_after_ai": equity - s["capital"] - ai["usd"], "unpriced_positions": unpriced,
            "positions": led["positions"],
            "by_strategy": [{"strategy": k, "label": STRATEGY_LABELS.get(k, k), **v}
                            for k, v in sorted(led["by_strategy"].items())],
        }
        if record and s["status"] == "running":
            self.store.add_sample(f"sim_equity:{s['id']}", equity)
            # The panel shows this cached copy: valuing positions needs network quotes.
            self.store.set_flag(f"sim_snapshot:{s['id']}", json.dumps(snap, ensure_ascii=False, default=str))
        return snap

    def cached_snapshot(self, s: dict) -> dict:
        """Last snapshot taken by a scan (prices included); recomputed from the ledger at cost if none yet."""
        f = self.store.get_flag(f"sim_snapshot:{s['id']}")
        if f and f["value"]:
            snap = json.loads(f["value"])
            snap["as_of"] = f["ts"]
            return snap
        desk, self.desk = self.desk, None  # no network: positions valued at cost until the next scan
        try:
            return self.snapshot(s, record=False)
        finally:
            self.desk = desk

    # ------------------------------------------------------------------
    def tick(self) -> dict | None:
        """Called every scan: record equity; close the session when its time is up."""
        s = self.current()
        if not s:
            return None
        if _now() >= datetime.fromisoformat(s["end_ts"]):
            return self.finish(s, reason="time up")
        return self.snapshot(s)

    def finish(self, s: dict, reason: str) -> dict:
        self.store.finish_sim(s["id"], _now().isoformat(timespec="seconds"))
        s = self.store.sim(s["id"])
        snap = self.snapshot(s, record=False)
        snap["reason"] = reason
        snap["equity_curve"] = self.store.samples(f"sim_equity:{s['id']}")
        self.store.set_sim_report(s["id"], json.dumps(snap, ensure_ascii=False, default=str))
        sign = "+" if snap["pnl"] >= 0 else ""
        self.store.add_alert(f"sim_end:{s['id']}", "info",
                             f"模拟结束：${s['capital']:.0f} → ${snap['equity']:.2f}（{sign}{snap['return_pct']:.2f}%）",
                             f"已实现 {snap['realized']:+.2f}，浮动 {snap['unrealized']:+.2f}，手续费 {snap['fees']:.2f}，"
                             f"AI 花费 ${snap['ai_cost_usd']:.4f}。在面板「模拟账户」查看各策略明细。")
        return snap

    def report(self, sid: int) -> dict | None:
        s = self.store.sim(sid)
        if not s:
            return None
        return json.loads(s["report"]) if s["report"] else self.snapshot(s, record=False)
