"""Opportunity scanners.

Data fetching uses only public market-data endpoints (no API keys). The
`find_*` functions are pure so they can be tested without network access.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from itertools import permutations

import ccxt

from .config import ScanConfig

log = logging.getLogger(__name__)

Tickers = dict[str, dict[str, dict]]  # exchange -> symbol -> ticker


# --------------------------------------------------------------------------
# Pure analysis
# --------------------------------------------------------------------------

def find_cross_exchange(tickers: Tickers, taker_fee_pct: float, min_net_pct: float) -> list[dict]:
    """Buy on the exchange with the lowest ask, sell where the bid is highest.

    Needs inventory pre-funded on both venues (USDT on the buy side, coin on
    the sell side); moving funds between exchanges is too slow for this.
    """
    symbols = {s for per_ex in tickers.values() for s in per_ex}
    out = []
    for sym in sorted(symbols):
        quotes = [(ex, t) for ex, per_ex in tickers.items() if (t := per_ex.get(sym))
                  and t.get("bid") and t.get("ask")]
        if len(quotes) < 2:
            continue
        buy_ex, buy_t = min(quotes, key=lambda q: q[1]["ask"])
        sell_ex, sell_t = max(quotes, key=lambda q: q[1]["bid"])
        if buy_ex == sell_ex:
            continue
        gross = (sell_t["bid"] - buy_t["ask"]) / buy_t["ask"] * 100
        net = gross - 2 * taker_fee_pct
        if net >= min_net_pct:
            out.append({
                "type": "cross_exchange",
                "symbol": sym,
                "buy_exchange": buy_ex, "buy_price": buy_t["ask"],
                "sell_exchange": sell_ex, "sell_price": sell_t["bid"],
                "gross_pct": round(gross, 4), "net_pct": round(net, 4),
                "note": "requires pre-funded USDT on buy side and coin on sell side",
            })
    return sorted(out, key=lambda o: -o["net_pct"])


def funding_apr(rate: float, interval_hours: float = 8) -> float:
    return rate * (24 / interval_hours) * 365 * 100


def find_funding(funding: dict[str, dict[str, dict]], min_apr_pct: float, taker_fee_pct: float) -> list[dict]:
    """Cash-and-carry (spot long + perp short) and cross-exchange funding spreads.

    `funding`: exchange -> base symbol ("BTC/USDT") -> {"rate": float, "interval_h": float}
    """
    out = []
    for ex, per_ex in funding.items():
        for sym, f in per_ex.items():
            apr = funding_apr(f["rate"], f.get("interval_h", 8))
            if apr >= min_apr_pct:
                out.append({
                    "type": "funding_carry",
                    "symbol": sym, "exchange": ex,
                    "funding_rate_pct": round(f["rate"] * 100, 5),
                    "apr_pct": round(apr, 2),
                    # 4 fills: open/close spot and perp
                    "round_trip_fee_pct": round(4 * taker_fee_pct, 3),
                    "note": "long spot + short perp of equal size; funding can flip negative",
                })
    symbols = {s for per_ex in funding.values() for s in per_ex}
    for sym in symbols:
        rates = [(ex, funding_apr(f["rate"], f.get("interval_h", 8)))
                 for ex, per_ex in funding.items() if (f := per_ex.get(sym))]
        if len(rates) < 2:
            continue
        lo, hi = min(rates, key=lambda r: r[1]), max(rates, key=lambda r: r[1])
        if hi[1] - lo[1] >= min_apr_pct and lo[0] != hi[0]:
            out.append({
                "type": "funding_spread",
                "symbol": sym,
                "long_perp_exchange": lo[0], "short_perp_exchange": hi[0],
                "apr_spread_pct": round(hi[1] - lo[1], 2),
                "round_trip_fee_pct": round(4 * taker_fee_pct, 3),
                "note": "perp long on low-funding venue, perp short on high-funding venue; margin on both",
            })
    return sorted(out, key=lambda o: -o.get("apr_pct", o.get("apr_spread_pct", 0)))


def find_triangular(tickers: dict[str, dict], start: str, taker_fee_pct: float,
                    min_profit_pct: float, min_quote_volume: float = 0) -> list[dict]:
    """Cycles start -> X -> Y -> start within a single exchange."""
    rate: dict[tuple[str, str], tuple[float, str, str]] = {}
    for sym, t in tickers.items():
        if "/" not in sym or ":" in sym or not t.get("bid") or not t.get("ask"):
            continue
        if (t.get("quoteVolume") or 0) < min_quote_volume:
            continue
        base, quote = sym.split("/")
        rate[(base, quote)] = (t["bid"], sym, "sell")      # base -> quote
        rate[(quote, base)] = (1 / t["ask"], sym, "buy")   # quote -> base
    assets = {a for pair in rate for a in pair} - {start}
    neighbours = {a for (s, a) in rate if s == start}
    fee = 1 - taker_fee_pct / 100
    out = []
    for x, y in permutations(neighbours & assets, 2):
        legs = [(start, x), (x, y), (y, start)]
        if not all(l in rate for l in legs):
            continue
        value = 1.0
        for l in legs:
            value *= rate[l][0] * fee
        profit = (value - 1) * 100
        if profit >= min_profit_pct:
            out.append({
                "type": "triangular",
                "path": f"{start}->{x}->{y}->{start}",
                "legs": [{"symbol": rate[l][1], "side": rate[l][2]} for l in legs],
                "net_pct": round(profit, 4),
                "note": "quotes move in milliseconds; usually gone before a manual fill",
            })
    return sorted(out, key=lambda o: -o["net_pct"])


def market_overview(tickers: Tickers, symbols: list[str]) -> list[dict]:
    """24h change / volume snapshot (context for the AI, not an arbitrage)."""
    rows = []
    for sym in symbols:
        for ex, per_ex in tickers.items():
            t = per_ex.get(sym)
            if t and t.get("last"):
                rows.append({"symbol": sym, "exchange": ex, "last": t["last"],
                             "change_24h_pct": t.get("percentage"),
                             "quote_volume_24h": t.get("quoteVolume")})
                break
    return rows


# --------------------------------------------------------------------------
# Fetching (public endpoints only)
# --------------------------------------------------------------------------

def _public_client(ex_id: str, default_type: str = "spot") -> ccxt.Exchange:
    cls = getattr(ccxt, ex_id)
    return cls({"enableRateLimit": True, "timeout": 15000, "options": {"defaultType": default_type}})


def _slim(t: dict) -> dict:
    return {k: t.get(k) for k in ("bid", "ask", "last", "percentage", "quoteVolume")}


def fetch_spot_tickers(ex_id: str, symbols: list[str] | None) -> dict[str, dict]:
    client = _public_client(ex_id)
    client.load_markets()
    if symbols:
        wanted = [s for s in symbols if s in client.markets]
        raw = client.fetch_tickers(wanted)
    else:
        raw = client.fetch_tickers()
    return {s: _slim(t) for s, t in raw.items() if client.markets.get(s, {}).get("spot", True)}


def fetch_funding(ex_id: str, symbols: list[str]) -> dict[str, dict]:
    client = _public_client(ex_id, "swap")
    if not client.has.get("fetchFundingRates"):
        return {}
    client.load_markets()
    perps = [f"{s}:{s.split('/')[1]}" for s in symbols]
    perps = [p for p in perps if p in client.markets]
    out = {}
    for p, f in client.fetch_funding_rates(perps).items():
        if f.get("fundingRate") is None:
            continue
        interval = f.get("interval") or "8h"
        hours = float(interval.rstrip("h")) if isinstance(interval, str) and interval.endswith("h") else 8.0
        out[p.split(":")[0]] = {"rate": float(f["fundingRate"]), "interval_h": hours}
    return out


def run_scan(cfg: ScanConfig) -> tuple[dict, list[dict], list[dict]]:
    """Returns (summary, opportunities, market_overview)."""
    errors: dict[str, str] = {}
    tickers: Tickers = {}
    funding: dict[str, dict] = {}

    def spot(ex):
        return ex, fetch_spot_tickers(ex, cfg.symbols)

    def perp(ex):
        return ex, fetch_funding(ex, cfg.symbols)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for fn, target, label in ((spot, tickers, "spot"), (perp, funding, "funding")):
            futures = {ex: pool.submit(fn, ex) for ex in cfg.exchanges}
            for ex, fut in futures.items():
                try:
                    target[fut.result()[0]] = fut.result()[1]
                except Exception as e:  # network / geo-block / exchange errors
                    errors[f"{ex}:{label}"] = f"{type(e).__name__}: {str(e)[:200]}"
                    log.warning("%s %s fetch failed: %s", ex, label, e)

    opps = find_cross_exchange(tickers, cfg.taker_fee_pct, cfg.min_net_spread_pct)
    opps += find_funding(funding, cfg.min_funding_apr_pct, cfg.taker_fee_pct)
    try:
        tri = fetch_spot_tickers(cfg.triangular_exchange, None)
        opps += find_triangular(tri, "USDT", cfg.taker_fee_pct, cfg.triangular_min_profit_pct,
                                min_quote_volume=100_000)[:10]
    except Exception as e:
        errors[f"{cfg.triangular_exchange}:triangular"] = f"{type(e).__name__}: {str(e)[:200]}"

    summary = {
        "exchanges_ok": sorted(tickers), "funding_ok": sorted(funding),
        "errors": errors, "count": len(opps),
    }
    return summary, opps, market_overview(tickers, cfg.symbols)
