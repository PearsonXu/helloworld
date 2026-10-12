"""Polymarket module (paper trading only).

Public, unauthenticated endpoints:
  Gamma  https://gamma-api.polymarket.com  — events / markets metadata and prices
  CLOB   https://clob.polymarket.com       — order books

Arbitrage notes
  * Plain YES/NO markets share one mirrored order book (buying NO at q is the same
    as selling YES at 1-q), so YES ask + NO ask is always >= 1. No arb there.
  * Multi-outcome "negRisk" events (exactly one outcome resolves YES) have a
    separate book per outcome, so their prices can drift out of line:
      - sum of YES asks < 1   -> buy one YES of every outcome, payout exactly 1
      - sum of YES bids > 1   -> buy one NO of every outcome, payout exactly N-1
  The detectors walk the real order books so the size is executable, not just
  a top-of-book illusion.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


@dataclass
class PolymarketConfig:
    enabled: bool = True
    max_events: int = 200
    min_liquidity_usd: float = 5000
    # Taker fee per share = taker_fee_rate * p * (1 - p) on markets with fees enabled.
    # Makers pay nothing. Polymarket's per-category rates change; 0.07 is a
    # deliberately conservative default — check docs.polymarket.com/trading/fees.
    taker_fee_rate: float = 0.07
    min_arb_profit_pct: float = 0.5
    max_usd_per_market: float = 20
    ai_markets_per_run: int = 8
    ai_min_edge: float = 0.08
    ai_horizon_days: list[int] = field(default_factory=lambda: [1, 60])
    # --- live trading (real money) ---
    mode: str = "paper"                 # "paper" or "live"
    max_live_usd_per_day: float = 20    # total live spend per UTC day
    max_live_exposure_usd: float = 50   # cost of all unresolved live positions
    price_tolerance: float = 0.01       # max price above the proposal's limit
    environment: str = "prod"           # Kalshi only: "demo" (fake money) or "prod"


# The same settings shape is used for every prediction venue ([polymarket], [kalshi], [polymarket_us]).
VenueConfig = PolymarketConfig


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def _loads(v, default):
    """Gamma returns several list fields as JSON-encoded strings."""
    if v is None:
        return default
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return default
    return v


def parse_market(m: dict) -> dict | None:
    outcomes = _loads(m.get("outcomes"), [])
    prices = [float(p) for p in _loads(m.get("outcomePrices"), [])]
    tokens = _loads(m.get("clobTokenIds"), [])
    if len(outcomes) != 2 or len(tokens) != 2:
        return None
    return {
        "id": str(m.get("id")),
        "question": m.get("question") or "",
        "group_title": m.get("groupItemTitle") or "",
        "description": (m.get("description") or "")[:600],
        "end_date": m.get("endDate"),
        "yes_token": tokens[0], "no_token": tokens[1],
        "yes_price": prices[0] if prices else None,
        "best_bid": _f(m.get("bestBid")), "best_ask": _f(m.get("bestAsk")),
        "liquidity": _f(m.get("liquidityNum") or m.get("liquidity")) or 0.0,
        "volume_24h": _f(m.get("volume24hr")) or 0.0,
        "active": bool(m.get("active")) and not m.get("closed"),
        "accepting_orders": m.get("acceptingOrders", True) and m.get("enableOrderBook", True),
        # Unknown -> assume fees apply (conservative).
        "fees_enabled": m.get("feesEnabled", True) is not False,
        "min_size": _f(m.get("orderMinSize")) or 5.0,
    }


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_event(e: dict) -> dict:
    markets = [pm for m in e.get("markets") or [] if (pm := parse_market(m))]
    return {
        "id": str(e.get("id")), "title": e.get("title") or "", "slug": e.get("slug") or "",
        "neg_risk": bool(e.get("negRisk") or e.get("enableNegRisk")),
        # Augmented events keep adding outcomes ("Other"/placeholders): not exhaustive yet.
        "augmented": bool(e.get("negRiskAugmented")),
        "liquidity": _f(e.get("liquidity")) or 0.0,
        "markets": markets,
    }


def book_side(book: dict, side: str) -> list[tuple[float, float]]:
    """Returns [(price, size)] best-first. side: 'asks' or 'bids'."""
    levels = [(float(l["price"]), float(l["size"])) for l in book.get(side) or []]
    return sorted(levels, key=lambda x: x[0], reverse=(side == "bids"))


# --------------------------------------------------------------------------
# Pure arbitrage math
# --------------------------------------------------------------------------

def taker_fee_per_share(price: float, rate: float, enabled: bool = True) -> float:
    return rate * price * (1 - price) if enabled else 0.0


def walk_bundle(ladders: list[list[tuple[float, float]]], payout: float, fee_rate: float,
                fees_enabled: list[bool], budget_usd: float, min_margin: float) -> dict:
    """Buy equal shares on every ladder while each extra bundle still profits.

    ladders: per leg, ask levels best-first. One "bundle" = 1 share of every leg,
    worth `payout` at resolution. Stops when the marginal bundle's cost (incl.
    taker fees) exceeds payout * (1 - min_margin) or the budget is used.
    """
    idx = [0] * len(ladders)
    left = [l[0][1] if l else 0.0 for l in ladders]
    shares, cost = 0.0, 0.0
    leg_cost = [0.0] * len(ladders)
    while all(i < len(l) for i, l in zip(idx, ladders)):
        prices = [l[i][0] for i, l in zip(idx, ladders)]
        unit = sum(p + taker_fee_per_share(p, fee_rate, fe) for p, fe in zip(prices, fees_enabled))
        if unit >= payout * (1 - min_margin) - 1e-12:
            break
        q = min(left)
        if budget_usd - cost < unit * q:
            q = max(0.0, (budget_usd - cost) / unit)
        if q <= 1e-9:
            break
        shares += q
        cost += unit * q
        for k, p in enumerate(prices):
            leg_cost[k] += p * q
            left[k] -= q
            if left[k] <= 1e-9:
                idx[k] += 1
                if idx[k] < len(ladders[k]):
                    left[k] = ladders[k][idx[k]][1]
    return {"shares": shares, "cost": cost, "payout": shares * payout,
            "profit": shares * payout - cost,
            "avg_prices": [c / shares if shares else None for c in leg_cost]}


def screen_negrisk(event: dict) -> dict | None:
    """Cheap top-of-book screen using Gamma prices. Returns direction or None."""
    if not event["neg_risk"] or event["augmented"] or len(event["markets"]) < 2:
        return None
    ms = event["markets"]
    if not all(m["active"] and m["accepting_orders"] for m in ms):
        return None
    if any(m["best_ask"] is None or m["best_bid"] is None for m in ms):
        return None
    sum_ask = sum(m["best_ask"] for m in ms)
    sum_bid = sum(m["best_bid"] for m in ms)
    if sum_ask < 1:
        return {"direction": "buy_yes", "sum": sum_ask}
    if sum_bid > 1:
        return {"direction": "buy_no", "sum": sum_bid}
    return None


def negrisk_opportunity(event: dict, books: dict[str, dict], direction: str,
                        cfg: PolymarketConfig) -> dict | None:
    """Exact sizing from order books. books: token_id -> CLOB book (YES tokens)."""
    ms = event["markets"]
    n = len(ms)
    if direction == "buy_yes":
        ladders = [book_side(books[m["yes_token"]], "asks") for m in ms]
        payout, tokens = 1.0, [m["yes_token"] for m in ms]
    else:
        # A NO ask at 1-b is the mirror of a YES bid at b.
        ladders = [[(round(1 - p, 6), s) for p, s in book_side(books[m["yes_token"]], "bids")] for m in ms]
        payout, tokens = float(n - 1), [m["no_token"] for m in ms]
    budget = min(cfg.max_usd_per_market * n, cfg.max_usd_per_market * 4)
    res = walk_bundle(ladders, payout, cfg.taker_fee_rate, [m["fees_enabled"] for m in ms],
                      budget, cfg.min_arb_profit_pct / 100)
    # Order sizes are in hundredths of a share; round down and scale the totals.
    shares = int(res["shares"] * 100) / 100
    if shares < max(m["min_size"] for m in ms) or res["profit"] <= 0:
        return None
    scale = shares / res["shares"]
    res.update(cost=res["cost"] * scale, payout=shares * payout, profit=res["profit"] * scale)
    side_label = "YES" if direction == "buy_yes" else "NO"
    return {
        "type": "pm_negrisk",
        "venue": "polymarket",
        "event_id": event["id"], "title": event["title"], "slug": event["slug"],
        "url": f"https://polymarket.com/event/{event['slug']}",
        "direction": direction, "outcomes": n,
        "shares": shares,
        "cost_usd": round(res["cost"], 4), "payout_usd": round(res["payout"], 4),
        "profit_usd": round(res["profit"], 4),
        "net_pct": round(res["profit"] / res["cost"] * 100, 3) if res["cost"] else 0,
        "legs": [{"exchange": "polymarket", "market_id": m["id"], "token_id": t,
                  "outcome": side_label, "label": m["group_title"] or m["question"],
                  "side": "buy", "shares": shares,
                  "limit_price": round(p, 4)}
                 for m, t, p in zip(ms, tokens, res["avg_prices"])],
        "note": f"buy 1 {side_label} of each of {n} outcomes; payout fixed at {payout:g}/bundle. "
                "Risks: resolution disputes, outcome list changing, partial fills.",
    }


def pick_forecast_candidates(events: list[dict], cfg: PolymarketConfig, now: datetime | None = None) -> list[dict]:
    """Liquid, medium-horizon markets for the AI to estimate (skips fast crypto markets)."""
    now = now or datetime.now(timezone.utc)
    lo, hi = cfg.ai_horizon_days
    out = []
    for e in events:
        for m in e["markets"]:
            if not (m["active"] and m["accepting_orders"]) or m["best_ask"] is None:
                continue
            if not 0.05 <= m["best_ask"] <= 0.95 or m["liquidity"] < cfg.min_liquidity_usd:
                continue
            try:
                end = datetime.fromisoformat(m["end_date"].replace("Z", "+00:00"))
            except (AttributeError, ValueError):
                continue
            days = (end - now).total_seconds() / 86400
            if lo <= days <= hi:
                out.append({**m, "event_title": e["title"], "days_left": round(days, 1)})
    out.sort(key=lambda m: -m["volume_24h"])
    return out[: cfg.ai_markets_per_run]


def brier(prob: float, outcome: int) -> float:
    return (prob - outcome) ** 2


# --------------------------------------------------------------------------
# HTTP client (public endpoints only, no wallet / no keys)
# --------------------------------------------------------------------------

class PolymarketClient:
    def __init__(self, timeout: float = 15):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "arbdesk/0.1"
        self.timeout = timeout

    def _get(self, url: str, **params):
        r = self.s.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def events(self, limit: int) -> list[dict]:
        out, offset = [], 0
        while len(out) < limit:
            page = self._get(f"{GAMMA}/events", active="true", closed="false", archived="false",
                             order="volume24hr", ascending="false",
                             limit=min(100, limit - len(out)), offset=offset)
            if not page:
                break
            out.extend(parse_event(e) for e in page)
            offset += len(page)
        return out

    def market(self, market_id: str) -> dict:
        return self._get(f"{GAMMA}/markets/{market_id}")

    def book(self, token_id: str) -> dict:
        return self._get(f"{CLOB}/book", token_id=token_id)


def resolution(raw_market: dict) -> dict | None:
    """Returns {'yes': 1|0} once a market has resolved, else None."""
    if not raw_market.get("closed"):
        return None
    prices = [float(p) for p in _loads(raw_market.get("outcomePrices"), [])]
    if len(prices) != 2 or max(prices) < 0.99:
        return None  # closed but not cleanly resolved (e.g. still disputed)
    return {"yes": 1 if prices[0] > prices[1] else 0}


def scan(client: PolymarketClient, cfg: PolymarketConfig) -> tuple[list[dict], list[dict], dict]:
    """Returns (arbitrage opportunities, forecast candidates, summary)."""
    events = client.events(cfg.max_events)
    opps, errors = [], {}
    screened = [(e, s) for e in events if (s := screen_negrisk(e))]
    for e, s in screened[:15]:
        try:
            books = {m["yes_token"]: client.book(m["yes_token"]) for m in e["markets"]}
            if (o := negrisk_opportunity(e, books, s["direction"], cfg)):
                opps.append(o)
        except Exception as ex:
            errors[f"pm:{e['id']}"] = f"{type(ex).__name__}: {str(ex)[:150]}"
    opps.sort(key=lambda o: -o["net_pct"])
    summary = {"events": len(events), "negrisk_events": sum(e["neg_risk"] for e in events),
               "screened": len(screened), "errors": errors}
    return opps, pick_forecast_candidates(events, cfg), summary
