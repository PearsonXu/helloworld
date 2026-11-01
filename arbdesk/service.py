"""Scan -> (AI only when something changed) -> pending proposals, on two clocks.

* Code scans run every `scan.interval_minutes` (default 5). They cost no tokens:
  crypto spreads/funding, prediction-market bundles, cross-venue prices.
* The AI runs at most every `llm.interval_minutes` (default 60) and only on what
  changed since its last look: a new crypto opportunity set, markets whose price
  moved (or whose last forecast is old), and new cross-venue candidate pairs.
  Every call is metered against the daily token budget and monthly USD cap.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import crossvenue, risk, tail
from .deribit import DeribitClient, build_smiles
from .config import Settings
from .llm import Analyst, BudgetExceeded, forecast_markets
from .scanners import run_scan
from .storage import Store
from .venues import Venue, build_venues

log = logging.getLogger(__name__)


def _ago(ts: str | None) -> timedelta:
    if not ts:
        return timedelta.max
    return datetime.now(timezone.utc) - datetime.fromisoformat(ts)


class Desk:
    def __init__(self, settings: Settings, store: Store, analyst: Analyst | None = None):
        self.settings = settings
        self.store = store
        self.analyst = analyst or Analyst(settings, store)
        self._lock = threading.Lock()
        self.running = False
        self.venues: dict[str, Venue] = build_venues(settings)
        self.last_run: dict[str, str] = {}
        self.deribit = DeribitClient()

    # ------------------------------------------------------------------
    def run_cycle(self, use_llm: bool = True) -> dict:
        """One pass. `use_llm=False` is the cheap 5-minute pass; settlement and pruning
        also ride on the AI pass because they don't need to run every 5 minutes."""
        if not self._lock.acquire(blocking=False):
            return {"skipped": "a cycle is already running"}
        self.running = True
        try:
            summary, opps, overview = run_scan(self.settings.scan)
            scan_id = self.store.add_scan({**summary, "market": overview}, opps)
            log.info("scan %s: %d opportunities, errors=%s", scan_id, len(opps), list(summary["errors"]))
            result = {"scan_id": scan_id, "opportunities": len(opps), "errors": summary["errors"]}
            if use_llm:
                result["crypto_ai"] = self.analyze(scan_id, opps, overview)
            for name, venue in self.venues.items():
                result[name] = self.venue_cycle(venue, use_llm, settle=use_llm)
            if self.settings.crossvenue.enabled:
                result["crossvenue"] = self.crossvenue_cycle(use_llm)
            if self.settings.tail.enabled:
                result["tail"] = self.tail_cycle()
            if use_llm:
                result["pruned_scans"] = self.store.prune_scans()
            self.last_run["scan"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if use_llm:
                self.last_run["ai"] = self.last_run["scan"]
            return result
        finally:
            self.running = False
            self._lock.release()

    def _ai_ready(self) -> str | None:
        """Reason the AI can't run right now, or None."""
        if not self.analyst.available:
            return "llm not configured"
        st = self.analyst.budget_status()
        return None if st["ok"] else st["reason"]

    # ------------------------------------------------------------------
    # Crypto
    # ------------------------------------------------------------------
    def analyze(self, scan_id: int, opps: list[dict], overview: list[dict]) -> dict:
        if (reason := self._ai_ready()):
            return {"llm": reason}
        # Only when the opportunity set changed, plus one market view per day.
        fp = hashlib.sha1(json.dumps(sorted(
            (o["type"], o.get("symbol") or o.get("path"), o.get("buy_exchange") or o.get("exchange") or "",
             round(o.get("net_pct") or o.get("apr_pct") or o.get("apr_spread_pct") or 0, 1)) for o in opps
        ), default=str).encode()).hexdigest()
        last = self.store.get_flag("crypto_ai_fp")
        stale = _ago(last["ts"] if last else None) > timedelta(hours=24)
        if last and last["value"] == fp and not stale:
            return {"llm": "skipped: nothing changed"}
        if not opps and not stale:
            return {"llm": "skipped: no opportunities"}
        try:
            content = self.analyst.analyze(opps, overview)
        except BudgetExceeded as e:
            return {"llm": f"budget: {e}"}
        except Exception as e:
            log.warning("LLM analysis failed: %s", e)
            return {"llm": f"error: {type(e).__name__}"}
        self.store.set_flag("crypto_ai_fp", fp)
        analysis_id = self.store.add_analysis(scan_id, self.analyst.provider, self.analyst.model, content)
        created = 0
        for prop in content.get("proposals", [])[:2]:
            if not isinstance(prop, dict):
                continue
            # Pre-check so the panel shows why a proposal can't run; still requires approval.
            _, errors = risk.validate(prop, self.settings, self.store)
            key = "crypto:" + hashlib.sha1(json.dumps(prop.get("legs"), sort_keys=True, default=str).encode()).hexdigest()
            self.store.add_proposal(analysis_id, prop, "pending", "; ".join(errors), dedupe_key=key)
            created += 1
        return {"analysis_id": analysis_id, "proposals": created}

    # ------------------------------------------------------------------
    # Prediction markets (Polymarket, Kalshi, Polymarket US)
    # ------------------------------------------------------------------
    def venue_cycle(self, venue: Venue, use_llm: bool, settle: bool = True) -> dict:
        out: dict = {}
        if settle:
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
            self.store.add_proposal(None, payload, "pending", "; ".join(errors),
                                    dedupe_key=f"{venue.name}:bundle:{o['event_id']}:{o['direction']}")
        if use_llm and candidates:
            out["forecasts"] = self.forecast(venue, candidates)
        return out

    def _needs_forecast(self, venue: Venue, m: dict) -> bool:
        last = self.store.last_forecast(venue.name, m["id"])
        if last is None:
            return True
        c = self.settings.llm
        moved = abs((m["best_ask"] or 0) - (last["market_prob"] or 0)) >= c.reforecast_price_move
        return moved or _ago(last["ts"]) >= timedelta(hours=c.reforecast_hours)

    def forecast(self, venue: Venue, candidates: list[dict]) -> int | str:
        cfg = venue.cfg
        if (reason := self._ai_ready()):
            return reason
        todo = [m for m in candidates if self._needs_forecast(venue, m)]
        if not todo:
            return "skipped: no market moved"
        try:
            forecasts = forecast_markets(self.analyst, todo)
        except BudgetExceeded as e:
            return f"budget: {e}"
        except Exception as e:
            log.warning("forecast failed: %s", e)
            return f"error: {type(e).__name__}"
        by_id = {m["id"]: m for m in todo}
        analysis_id = self.store.add_analysis(None, self.analyst.provider, self.analyst.model,
                                              {"market_view": f"{venue.label} 概率评估", "kind": "pm_forecast",
                                               "venue": venue.name, "forecasts": forecasts,
                                               "ranked": [], "proposals": [], "risks": []})
        fee = lambda p: cfg.taker_fee_rate * p * (1 - p)  # per contract, before per-order rounding
        for f in forecasts:
            m = by_id[f["market_id"]]
            self.store.add_forecast(venue=venue.name, market_id=m["id"], question=m["question"],
                                    end_date=m["end_date"], market_prob=m["best_ask"], ai_prob=f["prob"],
                                    confidence=f["confidence"], rationale=f["rationale"])
            # Edge after paying the ask and the taker fee; buy YES if AI is higher, NO if lower.
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
            self.store.add_proposal(analysis_id, payload, "pending", "; ".join(errors),
                                    dedupe_key=f"{venue.name}:forecast:{m['id']}:{outcome}")
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

    # ------------------------------------------------------------------
    # Cross-venue (same event on two venues)
    # ------------------------------------------------------------------
    def crossvenue_cycle(self, use_llm: bool) -> dict:
        cfg = self.settings.crossvenue
        if len(cfg.venues) != 2 or not all(v in self.venues for v in cfg.venues):
            return {"skipped": f"needs both venues enabled: {cfg.venues}"}
        va, vb = (self.venues[v] for v in cfg.venues)
        out: dict = {}
        # 1+2. New candidate pairs -> AI verdicts (cached forever), only on the AI clock.
        if use_llm and va.last_markets and vb.last_markets:
            cands = crossvenue.candidate_pairs(va.name, va.last_markets, vb.name, vb.last_markets,
                                               cfg, self.store.pair_keys())
            out["new_candidates"] = len(cands)
            if cands and not (reason := self._ai_ready()):
                try:
                    verdicts = {v["key"]: v for v in crossvenue.match_pairs(self.analyst, cands)}
                except BudgetExceeded as e:
                    verdicts, out["llm"] = {}, f"budget: {e}"
                except Exception as e:
                    log.warning("cross-venue matching failed: %s", e)
                    verdicts, out["llm"] = {}, f"error: {type(e).__name__}"
                for c in cands:
                    v = verdicts.get(c["key"])
                    if v is None:
                        continue  # not answered: retried on the next AI run
                    self.store.add_pair_match(
                        key=c["key"], venue_a=va.name, market_a=c["a"]["id"], title_a=crossvenue.text_of(c["a"]).strip(),
                        venue_b=vb.name, market_b=c["b"]["id"], title_b=crossvenue.text_of(c["b"]).strip(),
                        similarity=c["similarity"], verdict=v["verdict"], confidence=v["confidence"],
                        reason=v["reason"])
                out["judged"] = len(verdicts)
            elif cands:
                out["llm"] = reason
        # 3. Price every confirmed pair from live books (code only, every scan).
        opps, errors = [], {}
        live_a = {m["id"] for m in va.last_markets}
        live_b = {m["id"] for m in vb.last_markets}
        for pair in self.store.matched_pairs(cfg.min_match_confidence):
            if pair["market_a"] not in live_a or pair["market_b"] not in live_b:
                continue  # closed / no longer listed
            try:
                if (o := crossvenue.pair_opportunity(pair, va, vb, cfg)):
                    opps.append(o)
            except Exception as e:
                errors[pair["key"]] = f"{type(e).__name__}: {str(e)[:150]}"
        opps.sort(key=lambda o: -o["net_pct"])
        self.store.add_scan({"matched_pairs": len(self.store.matched_pairs(cfg.min_match_confidence)),
                             "errors": errors}, opps, "crossvenue")
        for o in opps[:3]:
            payload = {"title": f"跨平台套利：{o['title'][:70]}",
                       "rationale": f"{va.label} 买 {o['legs'][0]['outcome']} + {vb.label} 买 {o['legs'][1]['outcome']}，"
                                    f"成本 ${o['cost_usd']}，兑付 ${o['payout_usd']}，预计利润 ${o['profit_usd']}"
                                    f"（{o['net_pct']}%）。前提：两边结算规则完全一致，批准前请对照两边规则。",
                       "confidence": 0.7, "legs": o["legs"],
                       "exit_plan": "持有到结算；两边结算结果不一致属于规则风险"}
            _, errs = risk.validate(payload, self.settings, self.store)
            self.store.add_proposal(None, payload, "pending", "; ".join(errs),
                                    dedupe_key=f"cross:{o['key']}:{o['direction']}")
        out["opportunities"] = len(opps)
        return out


    # ------------------------------------------------------------------
    # Black-swan tickets: crypto threshold contracts vs Deribit options
    # ------------------------------------------------------------------
    def tail_cycle(self) -> dict:
        cfg = self.settings.tail
        errors: dict[str, str] = {}
        smiles = {}
        for ccy in cfg.currencies:
            try:
                smiles[ccy] = build_smiles(self.deribit.option_summaries(ccy))
            except Exception as e:
                errors[f"deribit:{ccy}"] = f"{type(e).__name__}: {str(e)[:150]}"
        contracts = []
        if "kalshi" in cfg.venues and "kalshi" in self.venues:
            k = self.venues["kalshi"]
            for series in cfg.kalshi_series:
                try:
                    evs = k.client.events(200, series_ticker=series)
                    contracts += tail.kalshi_contracts(
                        [{**m, "event_title": e["title"]} for e in evs for m in e["markets"]], cfg.currencies)
                except Exception as e:
                    errors[f"kalshi:{series}"] = f"{type(e).__name__}: {str(e)[:150]}"
        if "polymarket_us" in cfg.venues and "polymarket_us" in self.venues:
            u = self.venues["polymarket_us"]
            found = [c for m in u.last_markets if (c := tail.title_contract(m, cfg.currencies, "polymarket_us"))]
            for c in found[: cfg.max_pmus_quotes]:  # quotes only for parsed crypto contracts
                try:
                    md = u.client.bbo(c.market_id).get("marketData", {})
                    c.yes_bid = float(md["bestBid"]["value"]) if md.get("bestBid") else None
                    c.yes_ask = float(md["bestAsk"]["value"]) if md.get("bestAsk") else None
                    contracts.append(c)
                except Exception as e:
                    errors[f"pmus:{c.market_id}"] = f"{type(e).__name__}: {str(e)[:150]}"
        fees = {n: v.cfg.taker_fee_rate for n, v in self.venues.items()}
        rows = tail.evaluate(contracts, smiles, cfg, fees)
        cheap = [r for r in rows if r["cheap"]]
        spent = self.store.tail_spent_quarter()
        self.store.add_scan({"contracts": len(contracts), "priced": len(rows), "errors": errors,
                             # the cheap side of each contract is what matters for tail tickets
                             "rows": [r for r in rows if r["price"] <= 0.5][:40], "spent_quarter": spent,
                             "budget_quarter": cfg.quarterly_budget_usd}, cheap, "tail")
        for r in cheap[:3]:
            shares = max(r["min_size"], float(int(cfg.max_usd_per_bet / r["price"])))
            payload = {
                "kind": "tail",
                "title": f"黑天鹅彩票：{r['label'][:60]} 买 {r['side']} @ {r['price'] * 100:.1f}¢",
                "rationale": f"期权市场反推概率 {r['implied'] * 100:.1f}%，合约只卖 {r['price'] * 100:.1f}¢"
                             f"（{r['ratio']}倍）。最多亏 ${shares * r['price']:.2f}，命中兑付 ${shares:.0f}。"
                             "这不是套利：大多数这类合约到期归零。",
                "confidence": round(min(0.9, r["implied"]), 2),
                "legs": [{"exchange": r["venue"], "market_id": r["market_id"], "token_id": r["token_id"],
                          "outcome": r["side"], "label": r["label"], "side": "buy", "shares": shares,
                          "limit_price": r["price"]}],
                "exit_plan": "持有到结算；不追加、不补仓",
            }
            _, errs = risk.validate(payload, self.settings, self.store)
            self.store.add_proposal(None, payload, "pending", "; ".join(errs),
                                    dedupe_key=f"tail:{r['venue']}:{r['market_id']}:{r['side']}")
        return {"contracts": len(contracts), "priced": len(rows), "cheap": len(cheap), "errors": errors}


class Scheduler(threading.Thread):
    """Code scans every scan.interval_minutes; the AI joins at most every llm.interval_minutes."""

    def __init__(self, desk: Desk):
        super().__init__(daemon=True, name="scheduler")
        self.desk = desk

    def run(self) -> None:
        scan_s = max(1, self.desk.settings.scan.interval_minutes) * 60
        ai_s = max(1, self.desk.settings.llm.interval_minutes) * 60
        last_scan = last_ai = float("-inf")
        while True:
            now = time.monotonic()
            if now - last_scan >= scan_s and not self.desk.running:
                use_llm = now - last_ai >= ai_s
                last_scan = now
                if use_llm:
                    last_ai = now
                try:
                    self.desk.run_cycle(use_llm=use_llm)
                except Exception:
                    log.exception("scheduled cycle failed")
            time.sleep(10)
