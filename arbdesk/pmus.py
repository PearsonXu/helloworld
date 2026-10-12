"""Polymarket US (CFTC-regulated, separate from polymarket.com): data and live orders.

Public data:  https://gateway.polymarket.us   (no auth)
Trading:      https://api.polymarket.us       (Ed25519-signed, keys from polymarket.us/developer)
Live orders go through the official `polymarket-us` SDK (pip install -r requirements-live.txt).

Each market is one binary contract identified by its slug; the book quotes the
long ("YES") side. Live trading here only buys the long side (ORDER_INTENT_BUY_LONG):
the price convention for short orders is not documented clearly enough to send
real money on it. Paper trading supports both sides.

There is no exclusivity flag on Polymarket US events, so no bundle arbitrage is
proposed automatically; this venue is used for AI forecasts and single trades.
"""
from __future__ import annotations

import logging
import math

import requests

from .pm_live import LiveFill, LiveTradingError, round_down
from .polymarket import VenueConfig, pick_forecast_candidates

log = logging.getLogger(__name__)

GATEWAY = "https://gateway.polymarket.us"
TICK = 0.01


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _px(amount) -> float | None:
    return _f(amount.get("value")) if isinstance(amount, dict) else _f(amount)


def token(slug: str, side: str) -> str:
    return f"{slug}|{side}"


def split_token(tok: str) -> tuple[str, str]:
    slug, _, side = tok.rpartition("|")
    if side not in ("yes", "no") or not slug:
        raise ValueError(f"bad Polymarket US token {tok!r}")
    return slug, side


def ladders(book: dict) -> dict[str, list[tuple[float, float]]]:
    """Book of the long side -> ask ladders for buying YES (offers) and NO (mirrored bids)."""
    md = book.get("marketData", book)
    offers = [(_px(l.get("px")), _f(l.get("qty"))) for l in md.get("offers") or []]
    bids = [(_px(l.get("px")), _f(l.get("qty"))) for l in md.get("bids") or []]
    return {
        "yes": sorted(((p, q) for p, q in offers if p and q), key=lambda x: x[0]),
        "no": sorted(((round(1 - p, 6), q) for p, q in bids if p and q), key=lambda x: x[0]),
    }


def parse_market(m: dict, event: dict, bbo: dict | None = None) -> dict:
    slug = m["slug"]
    md = (bbo or {}).get("marketData", bbo or {})
    title = m.get("title") or m.get("outcome") or slug
    return {
        "id": slug,
        "question": f"{event.get('title', '')} — {title}".strip(" —"),
        "group_title": m.get("outcome") or title,
        "description": (m.get("description") or event.get("description") or "")[:600],
        "end_date": event.get("endTime"),
        "yes_token": token(slug, "yes"), "no_token": token(slug, "no"),
        "yes_price": _px(md.get("lastTradePx")),
        "best_bid": _px(md.get("bestBid")), "best_ask": _px(md.get("bestAsk")),
        "liquidity": _f(m.get("liquidity")) or 0.0,
        "volume_24h": _f(m.get("volume")) or 0.0,
        "active": bool(m.get("active", True)) and not m.get("closed"),
        "accepting_orders": bool(m.get("active", True)) and not m.get("closed"),
        "fees_enabled": True, "min_size": 1.0,
    }


def resolution(market: dict, settlement: dict | None) -> dict | None:
    m = market.get("market", market)
    if not m.get("closed") or not settlement:
        return None
    value = _f(settlement.get("settlement"))
    if value is None or 0.01 < value < 0.99:
        return None
    return {"yes": 1 if value >= 0.99 else 0}


class PMUSClient:
    """Public (unauthenticated) gateway endpoints."""

    def __init__(self, session: requests.Session | None = None, timeout: float = 15):
        self.s = session or requests.Session()
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None):
        r = self.s.get(GATEWAY + path, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def events(self, limit: int) -> list[dict]:
        out, offset = [], 0
        while len(out) < limit:
            page = self._get("/v1/events", {"active": "true", "closed": "false",
                                            "limit": min(100, limit - len(out)), "offset": offset})
            evs = page.get("events") or []
            if not evs:
                break
            out.extend(evs)
            offset += len(evs)
        return out

    def book(self, slug: str) -> dict:
        return self._get(f"/v1/markets/{slug}/book")

    def bbo(self, slug: str) -> dict:
        return self._get(f"/v1/markets/{slug}/bbo")

    def market(self, slug: str) -> dict:
        return self._get(f"/v1/market/slug/{slug}")

    def settlement(self, slug: str) -> dict | None:
        try:
            return self._get(f"/v1/markets/{slug}/settlement")
        except requests.HTTPError:
            return None


def scan(client: PMUSClient, cfg: VenueConfig) -> tuple[list[dict], list[dict], dict]:
    raw_events = client.events(cfg.max_events)
    pool = [(m, e) for e in raw_events for m in e.get("markets") or []
            if m.get("active", True) and not m.get("closed") and (_f(m.get("liquidity")) or 0) >= cfg.min_liquidity_usd]
    pool.sort(key=lambda me: -(_f(me[0].get("volume")) or 0))
    errors, by_event = {}, {}
    for m, e in pool[: cfg.ai_markets_per_run * 3]:  # quotes only for the most active markets
        try:
            pm = parse_market(m, e, client.bbo(m["slug"]))
        except Exception as ex:
            errors[f"pmus:{m.get('slug')}"] = f"{type(ex).__name__}: {str(ex)[:150]}"
            continue
        by_event.setdefault(e.get("slug") or e.get("id"), {"title": e.get("title", ""), "markets": []})["markets"].append(pm)
    candidates = pick_forecast_candidates(list(by_event.values()), cfg)
    summary = {"events": len(raw_events), "negrisk_events": 0, "screened": len(pool), "errors": errors}
    return [], candidates, summary


class PMUSTrader:
    def __init__(self, key_id: str, secret_key: str, cfg: VenueConfig, client=None):
        if not key_id or not secret_key:
            raise LiveTradingError("Polymarket US API key not configured "
                                   "(vault: POLYMARKET_US_KEY_ID, POLYMARKET_US_SECRET_KEY)")
        self.cfg = cfg
        self._client = client
        self._key_id, self._secret = key_id, secret_key

    @property
    def client(self):
        if self._client is None:
            try:
                from polymarket_us import PolymarketUS
            except ImportError:
                raise LiveTradingError("live trading needs: pip install -r requirements-live.txt") from None
            # max_retries only applies to idempotent requests; order placement is never retried.
            self._client = PolymarketUS(key_id=self._key_id, secret_key=self._secret)
        return self._client

    def ensure_allowed(self) -> dict:
        # Eligibility (KYC, state) is enforced by Polymarket US on the account; an
        # authenticated balance call proves the key works for this account.
        return {"balance_usd": self.collateral_balance()}

    def collateral_balance(self) -> float:
        bals = self.client.account.balances().get("balances") or []
        return float(bals[0].get("buyingPower", 0)) if bals else 0.0

    def readiness(self) -> dict:
        return {"balance_usd": self.collateral_balance()}

    def buy_fok(self, token_id: str, shares: float, limit_price: float, neg_risk=None) -> LiveFill:
        slug, side = split_token(token_id)
        if side != "yes":
            raise LiveTradingError("live NO orders are not supported on Polymarket US")
        qty = math.floor(shares)
        price = round_down(min(limit_price + self.cfg.price_tolerance, 1 - TICK), TICK)
        if qty < 1 or price <= 0:
            raise LiveTradingError(f"invalid order: quantity={qty} price={price}")
        resp = self.client.orders.create({
            "marketSlug": slug, "intent": "ORDER_INTENT_BUY_LONG", "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{price:.2f}", "currency": "USD"}, "quantity": qty,
            "tif": "TIME_IN_FORCE_FILL_OR_KILL", "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "synchronousExecution": True,
        })
        order_id = resp.get("id", "")
        execs = resp.get("executions") or []
        filled = sum(_f(x.get("lastShares")) or 0 for x in execs)
        cost = sum((_f(x.get("lastShares")) or 0) * (_px(x.get("lastPx")) or 0) for x in execs)
        fee = sum(_px(x.get("commissionNotionalCollected")) or 0 for x in execs)
        if filled < qty and order_id:  # fall back to the order record
            order = (self.client.orders.retrieve(order_id) or {}).get("order", {})
            filled = _f(order.get("cumQuantity")) or filled
            avg = _px(order.get("avgPx"))
            cost = filled * avg if avg else cost
            fee = _px(order.get("commissionNotionalTotalCollected")) or fee
        if filled + 1e-6 < qty:
            raise LiveTradingError(f"FOK order {order_id} not fully filled ({filled}/{qty})")
        return LiveFill(order_id=order_id, status="filled", shares=filled, avg_price=cost / filled,
                        cost_usd=cost, raw={"order_id": order_id, "fee_usd": fee, "price": price})

    def cancel_all(self) -> None:
        if self._client is not None:
            self._client.orders.cancel_all()
