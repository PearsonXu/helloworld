"""Scan -> AI analysis -> pending proposals, plus a simple daily scheduler."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

from . import risk
from .config import Settings
from .llm import Analyst, forecast_markets
from .scanners import run_scan
from .storage import Store
from .venues import Venue, build_venues

log = logging.getLogger(__name__)


class Desk:
    def __init__(self, settings: Settings, store: Store, analyst: Analyst | None = None):
        self.settings = settings
        self.store = store
        self.analyst = analyst or Analyst(settings)
        self._lock = threading.Lock()
        self.running = False
        self.venues: dict[str, Venue] = build_venues(settings)

    def run_cycle(self, use_llm: bool = True) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"skipped": "a cycle is already running"}
        self.running = True
        try:
            summary, opps, overview = run_scan(self.settings.scan)
            scan_id = self.store.add_scan({**summary, "market": overview}, opps)
            log.info("scan %s: %d opportunities, errors=%s", scan_id, len(opps), list(summary["errors"]))
            result = {"scan_id": scan_id, "opportunities": len(opps), "errors": summary["errors"]}
            if use_llm:
                result.update(self.analyze(scan_id, opps, overview))
            for name, venue in self.venues.items():
                result[name] = self.venue_cycle(venue, use_llm)
            return result
        finally:
            self.running = False
            self._lock.release()

    def analyze(self, scan_id: int, opps: list[dict], overview: list[dict]) -> dict:
        if not self.analyst.available:
            return {"llm": "not configured"}
        if self.store.llm_calls_today() >= self.settings.llm.max_calls_per_day:
            return {"llm": "daily call budget used up"}
        try:
            content = self.analyst.analyze(opps, overview)
        except Exception as e:
            log.warning("LLM analysis failed: %s", e)
            return {"llm": f"error: {type(e).__name__}"}
        analysis_id = self.store.add_analysis(scan_id, self.analyst.provider, self.analyst.model, content)
        created = 0
        for prop in content.get("proposals", [])[:2]:
            if not isinstance(prop, dict):
                continue
            # Pre-check so the panel shows why a proposal can't run; still requires approval.
            _, errors = risk.validate(prop, self.settings, self.store)
            self.store.add_proposal(analysis_id, prop, "pending", "; ".join(errors))
            created += 1
        return {"analysis_id": analysis_id, "proposals": created}

    # ------------------------------------------------------------------
    # Prediction markets (Polymarket, Kalshi, Polymarket US)
    # ------------------------------------------------------------------
    def venue_cycle(self, venue: Venue, use_llm: bool) -> dict:
        out: dict = {}
        try:
            out["settled"] = self.settle(venue)
        except Exception as e:
            log.warning("%s settlement failed: %s", venue.name, e)
        try:
            opps, candidates, summary = venue.scan()
        except Exception as e:
            log.warning("%s scan failed: %s", venue.name, e)
            self.store.add_scan({"errors": {venue.name: f"{type(e).__name__}: {e}"}}, [], venue.name)
            return {"error": f"{type(e).__name__}"}
        scan_id = self.store.add_scan(summary, opps, venue.name)
        out.update(scan_id=scan_id, arbitrage=len(opps))
        # Exact, rule-based arbitrage becomes a proposal directly (still needs approval).
        for o in opps[:3]:
            side = o["legs"][0]["outcome"]
            payout = f"至少 ${o['payout_usd']}" if o.get("direction") == "buy_no" else f"${o['payout_usd']}"
            payload = {"title": f"{venue.label} 多结果套利：{o['title']}",
                       "rationale": f"买入全部 {o['outcomes']} 个结果的 {side}，成本 ${o['cost_usd']}，"
                                    f"到期兑付 {payout}，预计利润 ${o['profit_usd']}（{o['net_pct']}%）",
                       "confidence": 0.9, "legs": o["legs"],
                       "exit_plan": "持有到结算；结算前出现规则争议则人工处理"}
            _, errors = risk.validate(payload, self.settings, self.store)
            self.store.add_proposal(None, payload, "pending", "; ".join(errors))
        if use_llm and candidates:
            out["forecasts"] = self.forecast(venue, candidates)
        return out

    def forecast(self, venue: Venue, candidates: list[dict]) -> int | str:
        cfg = venue.cfg
        if not self.analyst.available:
            return "llm not configured"
        if self.store.llm_calls_today() >= self.settings.llm.max_calls_per_day:
            return "daily call budget used up"
        try:
            forecasts = forecast_markets(self.analyst, candidates)
        except Exception as e:
            log.warning("forecast failed: %s", e)
            return f"error: {type(e).__name__}"
        by_id = {m["id"]: m for m in candidates}
        analysis_id = self.store.add_analysis(None, self.analyst.provider, self.analyst.model,
                                              {"market_view": f"{venue.label} 概率评估", "kind": "pm_forecast",
                                               "venue": venue.name, "forecasts": forecasts,
                                               "ranked": [], "proposals": [], "risks": []})
        for f in forecasts:
            m = by_id[f["market_id"]]
            self.store.add_forecast(venue=venue.name, market_id=m["id"], question=m["question"],
                                    end_date=m["end_date"], market_prob=m["best_ask"], ai_prob=f["prob"],
                                    confidence=f["confidence"], rationale=f["rationale"])
            # Edge after paying the ask and the taker fee; buy YES if AI is higher, NO if lower.
            fee = lambda p: cfg.taker_fee_rate * p * (1 - p)  # per contract, before per-order rounding
            yes_edge = f["prob"] - m["best_ask"] - fee(m["best_ask"])
            no_price = 1 - m["best_bid"] if m["best_bid"] else None
            no_edge = (1 - f["prob"]) - no_price - fee(no_price) if no_price else -1
            edge, outcome, token, price = max(
                (yes_edge, "YES", m["yes_token"], m["best_ask"]),
                (no_edge, "NO", m["no_token"], no_price or 1), key=lambda x: x[0])
            if edge < cfg.ai_min_edge or f["confidence"] < 0.5:
                continue
            budget = min(cfg.max_usd_per_market, self.settings.risk.max_per_trade_usd) * 0.5
            shares = max(m["min_size"], float(int(budget / price)))
            payload = {"title": f"{venue.label} {outcome}：{m['question'][:80]}",
                       "rationale": f"AI 概率 {f['prob']:.2f} vs 市场 {m['best_ask']:.2f}，"
                                    f"扣费后优势 {edge:.2f}。{f['rationale']}",
                       "confidence": f["confidence"],
                       "legs": [{"exchange": venue.name, "market_id": m["id"], "token_id": token,
                                 "outcome": outcome, "label": m["question"], "side": "buy",
                                 "shares": shares, "limit_price": round(price, 4)}],
                       "exit_plan": "持有到结算，或市场价格达到 AI 估值时手动平仓"}
            _, errors = risk.validate(payload, self.settings, self.store)
            self.store.add_proposal(analysis_id, payload, "pending", "; ".join(errors))
        return len(forecasts)

    def settle(self, venue: Venue) -> int:
        """Resolve forecasts and close positions (paper and live) on resolved markets."""
        market_ids = set(self.store.unresolved_forecast_markets(venue.name))
        trades = self.store.open_venue_trades(venue.name)
        for t in trades:
            market_ids.add(json.loads(t["raw"])["market_id"])
        settled = 0
        for mid in market_ids:
            res = venue.resolution(mid)
            if res is None:
                continue
            self.store.resolve_forecasts(venue.name, mid, res["yes"])
            for t in trades:
                info = json.loads(t["raw"])
                if info["market_id"] != mid:
                    continue
                won = (info["outcome"] == "YES") == (res["yes"] == 1)
                pnl = (t["amount"] if won else 0.0) - t["cost_usd"] - t["fee_usd"]
                self.store.settle_trade(t["id"], pnl)
                settled += 1
        return settled


class Scheduler(threading.Thread):
    """Runs a cycle at each configured local time (HH:MM) once per day."""

    def __init__(self, desk: Desk):
        super().__init__(daemon=True, name="scheduler")
        self.desk = desk
        self.done: set[str] = set()

    def run(self) -> None:
        while True:
            now = datetime.now()
            stamp = now.strftime("%Y-%m-%d %H:%M")
            if now.strftime("%H:%M") in self.desk.settings.scan.times and stamp not in self.done:
                self.done.add(stamp)
                try:
                    self.desk.run_cycle()
                except Exception:
                    log.exception("scheduled cycle failed")
            time.sleep(20)
