"""Kalshi (CFTC-regulated US exchange): data, multi-outcome arbitrage, live orders.

API: https://external-api.kalshi.com/trade-api/v2 (demo: external-api.demo.kalshi.co).
Requests are signed with the API key's private key over
`timestamp_ms + METHOD + path` (RSA-PSS/SHA-256, or Ed25519 for Ed25519 keys),
matching Kalshi's official SDK. Market data and order books are public.

Prices are dollars in [0, 1]. On the single YES book, buying NO at q is the
same as selling YES at 1 - q, so the NO asks are the mirror of the YES bids.

Arbitrage: in an event flagged `mutually_exclusive`, at most one market can
resolve YES. Buying one NO in each of its N markets therefore pays at least
N - 1 (N if none resolves YES), so a bundle costing less than N - 1 after fees
is locked-in profit. Buying every YES is NOT safe here (the outcomes may not be
exhaustive, so none might resolve YES); that direction is never proposed.
"""
from __future__ import annotations

import base64
import logging
import math
import time
import uuid
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from .pm_live import LiveFill, LiveTradingError, round_down
from .polymarket import VenueConfig, pick_forecast_candidates, walk_bundle

log = logging.getLogger(__name__)

HOSTS = {
    "prod": "https://external-api.kalshi.com/trade-api/v2",
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
}
TICK = 0.01


def kalshi_fee(price: float, contracts: float, rate: float) -> float:
    """Taker fee for one order: rate * C * P * (1 - P), rounded up to the cent."""
    raw = rate * contracts * price * (1 - price)
    return math.ceil(raw * 100 - 1e-9) / 100 if raw > 0 else 0.0


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def token(ticker: str, side: str) -> str:
    return f"{ticker}|{side}"


def split_token(tok: str) -> tuple[str, str]:
    ticker, _, side = tok.rpartition("|")
    if side not in ("yes", "no") or not ticker:
        raise ValueError(f"bad Kalshi token {tok!r}")
    return ticker, side


def parse_market(m: dict) -> dict:
    ticker = m["ticker"]
    bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
    active = m.get("status") == "active"
    return {
        "id": ticker,
        "question": m.get("title") or m.get("yes_sub_title") or ticker,
        "group_title": m.get("yes_sub_title") or "",
        "description": ((m.get("rules_primary") or "") + " " + (m.get("rules_secondary") or ""))[:600],
        "end_date": m.get("close_time"),
        "yes_token": token(ticker, "yes"), "no_token": token(ticker, "no"),
        "yes_price": _f(m.get("last_price_dollars")),
        # A zero bid/ask means "no quote" on Kalshi.
        "best_bid": bid if bid else None, "best_ask": ask if ask and ask < 1 else None,
        "bid_size": _f(m.get("yes_bid_size_fp")) or 0.0, "ask_size": _f(m.get("yes_ask_size_fp")) or 0.0,
        "liquidity": (_f(m.get("open_interest_fp")) or 0.0),
        "volume_24h": _f(m.get("volume_24h_fp")) or 0.0,
        "active": active, "accepting_orders": active,
        "fees_enabled": True, "min_size": 1.0,
    }


def parse_event(e: dict) -> dict:
    return {
        "id": e["event_ticker"], "title": e.get("title") or e["event_ticker"],
        "slug": (e.get("series_ticker") or e["event_ticker"]).lower(),
        "neg_risk": bool(e.get("mutually_exclusive")), "augmented": False,
        "markets": [parse_market(m) for m in e.get("markets") or []],
    }


def ladders(orderbook: dict) -> dict[str, list[tuple[float, float]]]:
    """Kalshi book (bids for YES and for NO) -> ask ladders for buying each side, best first."""
    ob = orderbook.get("orderbook_fp") or orderbook
    yes_bids = [(float(p), float(s)) for p, s in ob.get("yes_dollars") or []]
    no_bids = [(float(p), float(s)) for p, s in ob.get("no_dollars") or []]
    return {
        "yes": sorted(((round(1 - p, 6), s) for p, s in no_bids), key=lambda x: x[0]),
        "no": sorted(((round(1 - p, 6), s) for p, s in yes_bids), key=lambda x: x[0]),
    }


def resolution(raw_market: dict) -> dict | None:
    m = raw_market.get("market", raw_market)
    if m.get("status") not in ("settled", "finalized", "determined"):
        return None
    if m.get("result") not in ("yes", "no"):
        return None  # e.g. voided / scalar markets
    return {"yes": 1 if m["result"] == "yes" else 0}


# --------------------------------------------------------------------------
# Arbitrage
# --------------------------------------------------------------------------

def screen(event: dict) -> bool:
    ms = event["markets"]
    if not event["neg_risk"] or len(ms) < 2:
        return False
    if not all(m["active"] and m["best_bid"] for m in ms):
        return False  # a missing YES bid means that NO leg has no seller
    return sum(m["best_bid"] for m in ms) > 1


def no_bundle_opportunity(event: dict, books: dict[str, dict], cfg: VenueConfig) -> dict | None:
    """books: ticker -> raw orderbook. Buy one NO of each market."""
    ms = event["markets"]
    n = len(ms)
    lads = [ladders(books[m["id"]])["no"] for m in ms]
    budget = min(cfg.max_usd_per_market * n, cfg.max_usd_per_market * 4)
    res = walk_bundle(lads, float(n - 1), cfg.taker_fee_rate, [True] * n, budget, cfg.min_arb_profit_pct / 100)
    shares = math.floor(res["shares"])  # whole contracts keep fee rounding predictable
    if shares < 1 or res["profit"] <= 0:
        return None
    scale = shares / res["shares"]
    # Per-order fees round up to the cent: budget one extra cent per leg.
    cost = res["cost"] * scale + 0.01 * n
    payout = shares * (n - 1)
    profit = payout - cost
    if profit <= 0 or profit / cost * 100 < cfg.min_arb_profit_pct:
        return None
    return {
        "type": "kalshi_no_bundle", "venue": "kalshi",
        "event_id": event["id"], "title": event["title"], "slug": event["slug"],
        "url": f"https://kalshi.com/markets/{event['slug']}",
        "direction": "buy_no", "outcomes": n, "shares": shares,
        "cost_usd": round(cost, 4), "payout_usd": round(payout, 4), "profit_usd": round(profit, 4),
        "net_pct": round(profit / cost * 100, 3),
        "legs": [{"exchange": "kalshi", "market_id": m["id"], "token_id": m["no_token"], "outcome": "NO",
                  "label": m["group_title"] or m["question"], "side": "buy", "shares": shares,
                  "limit_price": round(p, 4)} for m, p in zip(ms, res["avg_prices"])],
        "note": f"buy 1 NO in each of {n} mutually exclusive markets; pays >= {n - 1} per bundle "
                f"({n} if none resolves YES). Risk: partial fills, market changes before settlement.",
    }


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------

class KalshiClient:
    def __init__(self, base_url: str = HOSTS["prod"], key_id: str = "", private_key_pem: str = "",
                 session: requests.Session | None = None, timeout: float = 15):
        self.base = base_url.rstrip("/")
        self.prefix = urlparse(self.base).path  # "/trade-api/v2", part of the signed path
        self.key_id = key_id
        self.s = session or requests.Session()
        self.timeout = timeout
        self._pem = private_key_pem
        self._key = None  # parsed on first signed request, so a bad key can't break startup

    def _load_key(self):
        if self._key is None:
            if not self._pem or not self.key_id:
                raise LiveTradingError("Kalshi API key not configured (vault: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY)")
            try:
                key = serialization.load_pem_private_key(self._pem.encode(), password=None)
            except (ValueError, TypeError) as e:
                raise LiveTradingError(f"KALSHI_PRIVATE_KEY is not a valid PEM private key: {e}") from None
            if not isinstance(key, (RSAPrivateKey, Ed25519PrivateKey)):
                raise LiveTradingError("Kalshi private key must be RSA or Ed25519 (PEM)")
            self._key = key
        return self._key

    def sign(self, message: bytes) -> bytes:
        self._load_key()
        if isinstance(self._key, Ed25519PrivateKey):
            return self._key.sign(message)
        return self._key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                   salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())

    def auth_headers(self, method: str, path: str) -> dict:
        self._load_key()
        ts = str(int(time.time() * 1000))
        sig = self.sign(f"{ts}{method.upper()}{self.prefix}{path}".encode())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                auth: bool = False) -> dict:
        headers = self.auth_headers(method, path) if auth else {}
        r = self.s.request(method, self.base + path, params=params, json=body, headers=headers,
                           timeout=self.timeout)
        if r.status_code >= 400:
            raise LiveTradingError(f"Kalshi {method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    # public
    def events(self, limit: int) -> list[dict]:
        out, cursor = [], None
        while len(out) < limit:
            params = {"status": "open", "with_nested_markets": "true", "limit": min(200, limit - len(out))}
            if cursor:
                params["cursor"] = cursor
            page = self.request("GET", "/events", params)
            out.extend(parse_event(e) for e in page.get("events") or [])
            cursor = page.get("cursor")
            if not cursor or not page.get("events"):
                break
        return out

    def orderbook(self, ticker: str) -> dict:
        return self.request("GET", f"/markets/{ticker}/orderbook")

    def market(self, ticker: str) -> dict:
        return self.request("GET", f"/markets/{ticker}")

    def exchange_status(self) -> dict:
        return self.request("GET", "/exchange/status")

    # authenticated
    def balance(self) -> float:
        r = self.request("GET", "/portfolio/balance", auth=True)
        return float(r["balance_dollars"]) if r.get("balance_dollars") else int(r.get("balance", 0)) / 100

    def create_order(self, body: dict) -> dict:
        return self.request("POST", "/portfolio/events/orders", body=body, auth=True)


def scan(client: KalshiClient, cfg: VenueConfig) -> tuple[list[dict], list[dict], dict]:
    events = client.events(cfg.max_events)
    opps, errors = [], {}
    screened = [e for e in events if screen(e)]
    for e in screened[:15]:
        try:
            books = {m["id"]: client.orderbook(m["id"]) for m in e["markets"]}
            if (o := no_bundle_opportunity(e, books, cfg)):
                opps.append(o)
        except Exception as ex:
            errors[f"kalshi:{e['id']}"] = f"{type(ex).__name__}: {str(ex)[:150]}"
    opps.sort(key=lambda o: -o["net_pct"])
    summary = {"events": len(events), "negrisk_events": sum(e["neg_risk"] for e in events),
               "screened": len(screened), "errors": errors}
    return opps, pick_forecast_candidates(events, cfg), summary


# --------------------------------------------------------------------------
# Live trading
# --------------------------------------------------------------------------

class KalshiTrader:
    """FOK orders through the V2 order endpoint (single YES book, bid/ask).

    Buying YES at <= p is a `bid` at p. Buying NO at <= q is an `ask` (selling YES)
    at 1 - q. Kalshi itself enforces account eligibility (KYC, state).
    """

    def __init__(self, client: KalshiClient, cfg: VenueConfig):
        self.client = client
        self.cfg = cfg

    def ensure_allowed(self) -> dict:
        st = self.client.exchange_status()
        if not st.get("trading_active", False):
            raise LiveTradingError(f"Kalshi trading is not active right now: {st}")
        return st

    def collateral_balance(self) -> float:
        return self.client.balance()

    def readiness(self) -> dict:
        return {"environment": self.cfg.environment, "host": self.client.base,
                "exchange": self.ensure_allowed(), "balance_usd": self.collateral_balance()}

    def buy_fok(self, token_id: str, shares: float, limit_price: float, neg_risk=None) -> LiveFill:
        ticker, side = split_token(token_id)
        count = round_down(shares, 0.01)
        cap = min(limit_price + self.cfg.price_tolerance, 1 - TICK)
        if side == "yes":
            book_side, yes_price = "bid", round_down(cap, TICK)
        else:
            # Selling YES at >= 1 - cap  ==  buying NO at <= cap.
            book_side, yes_price = "ask", math.ceil((1 - cap) / TICK - 1e-9) * TICK
        if count <= 0 or not 0 < yes_price < 1:
            raise LiveTradingError(f"invalid order: count={count} price={yes_price}")
        body = {"ticker": ticker, "side": book_side, "count": f"{count:.2f}", "price": f"{yes_price:.4f}",
                "time_in_force": "fill_or_kill", "self_trade_prevention_type": "taker_at_cross",
                "client_order_id": str(uuid.uuid4())}
        resp = self.client.create_order(body)
        resp = resp.get("order", resp)
        filled = _f(resp.get("fill_count")) or _f(resp.get("fill_count_fp")) or 0.0
        if filled + 1e-6 < count:
            raise LiveTradingError(f"FOK order not filled ({filled}/{count}): {resp}")
        avg_yes = _f(resp.get("average_fill_price")) or yes_price
        avg = avg_yes if side == "yes" else 1 - avg_yes
        fee = (_f(resp.get("average_fee_paid")) or 0.0) * filled
        return LiveFill(order_id=str(resp.get("order_id", "")), status="executed", shares=filled,
                        avg_price=avg, cost_usd=avg * filled,
                        raw={"order_id": resp.get("order_id"), "fee_usd": fee, "book_side": book_side,
                             "yes_price": yes_price, "ts_ms": resp.get("ts_ms")})

    def cancel_all(self) -> None:
        pass  # only FOK orders are sent, so nothing can be left resting
