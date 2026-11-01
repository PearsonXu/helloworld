"""Black-swan ("tail") pricing monitor: prediction-market crypto contracts vs option prices.

For every BTC/ETH price-threshold contract on Kalshi / Polymarket US, compute the
probability implied by Deribit's option chain at the same strike and date, and
compare it with what the contract costs. A cheap side (a few cents) whose
option-implied probability is clearly higher than its price is a "cheap
lottery ticket": max loss = the price paid, payoff = 1.

This is NOT arbitrage. Most such tickets still expire worthless; the point is
to buy them only when they are cheap relative to a liquid, independent market,
inside a small quarterly budget enforced by the risk module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .deribit import prob_above

CURRENCY_PATTERNS = {
    "BTC": re.compile(r"\b(btc|bitcoin)\b", re.I),
    "ETH": re.compile(r"\b(eth|ether|ethereum)\b", re.I),
}
_NUM = r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmM])?\b"
TITLE_RULES = [  # (pattern, kind); touch = "at any time before", terminal = "at expiry"
    (re.compile(r"\b(?:reach|hit|touch|exceed|break)\w*\s+" + _NUM, re.I), "touch_above"),
    (re.compile(r"\b(?:dip|fall|drop|crash|sink)\w*\s+(?:to|below|under)\s+" + _NUM, re.I), "touch_below"),
    (re.compile(r"\b(?:above|over|greater than|higher than|at least)\s+" + _NUM, re.I), "above"),
    (re.compile(r"\b(?:below|under|less than|lower than)\s+" + _NUM, re.I), "below"),
]


@dataclass
class Contract:
    venue: str
    market_id: str
    label: str
    currency: str
    kind: str             # above | below | between | touch_above | touch_below
    lo: float | None      # strike for above / touch_above, lower bound for between
    hi: float | None      # strike for below / touch_below, upper bound for between
    expiry: datetime
    yes_bid: float | None
    yes_ask: float | None
    yes_token: str
    no_token: str
    min_size: float = 1.0


def _when(s) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def currency_of(text: str, currencies: list[str]) -> str | None:
    hits = [c for c in currencies if CURRENCY_PATTERNS.get(c) and CURRENCY_PATTERNS[c].search(text or "")]
    return hits[0] if len(hits) == 1 else None  # ambiguous titles (both coins) are skipped


def _amount(num: str, suffix: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * {"k": 1e3, "m": 1e6}.get((suffix or "").lower(), 1)


def kalshi_contracts(markets: list[dict], currencies: list[str]) -> list[Contract]:
    """Kalshi publishes the strike structure: no text parsing needed."""
    out = []
    for m in markets:
        cur = currency_of(f"{m.get('event_title', '')} {m.get('question', '')}", currencies)
        exp = _when(m.get("expiry") or m.get("end_date"))
        st, lo, hi = m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike")
        if not cur or not exp or not m.get("active"):
            continue
        if st in ("greater", "greater_or_equal") and lo:
            kind, lo, hi = "above", lo, None
        elif st in ("less", "less_or_equal") and hi:
            kind, lo, hi = "below", None, hi
        elif st == "between" and lo and hi:
            kind = "between"
        else:
            continue
        out.append(Contract("kalshi", m["id"], m.get("group_title") or m["question"], cur, kind, lo, hi, exp,
                            m.get("best_bid"), m.get("best_ask"), m["yes_token"], m["no_token"], m.get("min_size", 1.0)))
    return out


def title_contract(m: dict, currencies: list[str], venue: str) -> Contract | None:
    """Polymarket US has no strike fields: parse simple, unambiguous titles only."""
    text = f"{m.get('event_title', '')} {m.get('question', '')}"
    cur = currency_of(text, currencies)
    exp = _when(m.get("end_date"))
    if not cur or not exp:
        return None
    q = m.get("question") or ""
    for pat, kind in TITLE_RULES:
        found = pat.findall(q)
        if len(found) != 1:  # none, or several numbers (ranges etc.): skip rather than guess
            continue
        k = _amount(*found[0])
        if k < 100:  # "above 5%" and similar are not price levels
            return None
        lo, hi = (k, None) if kind in ("above", "touch_above") else (None, k)
        return Contract(venue, m["id"], q, cur, kind, lo, hi, exp, m.get("best_bid"), m.get("best_ask"),
                        m["yes_token"], m["no_token"], m.get("min_size", 1.0))
    return None


def implied_yes(c: Contract, smiles, now: datetime, max_gap_days: float) -> float | None:
    """Option-implied probability that the contract resolves YES."""
    def above(k):
        return prob_above(smiles, k, c.expiry, now, max_gap_days)

    if c.kind == "above":
        return above(c.lo)
    if c.kind == "below":
        p = above(c.hi)
        return None if p is None else 1 - p
    if c.kind == "between":
        a, b = above(c.lo), above(c.hi)
        return None if a is None or b is None else max(0.0, a - b)
    # Touch ("reach $X before ..."): for a driftless price, P(touch) ~= 2 * P(end beyond).
    if c.kind == "touch_above":
        p = above(c.lo)
        return None if p is None else min(1.0, 2 * p)
    if c.kind == "touch_below":
        p = above(c.hi)
        return None if p is None else min(1.0, 2 * (1 - p))
    return None


def evaluate(contracts: list[Contract], smiles_by_ccy: dict, cfg, fee_rate: dict[str, float],
             now: datetime | None = None) -> list[dict]:
    """One row per priced side; `cheap` marks tail tickets worth proposing."""
    now = now or datetime.now(timezone.utc)
    rows = []
    for c in contracts:
        smiles = smiles_by_ccy.get(c.currency)
        if not smiles:
            continue
        p_yes = implied_yes(c, smiles, now, cfg.max_expiry_gap_days)
        if p_yes is None:
            continue
        sides = []
        if c.yes_ask:
            sides.append(("YES", c.yes_ask, p_yes, c.yes_token))
        if c.yes_bid:
            sides.append(("NO", round(1 - c.yes_bid, 4), 1 - p_yes, c.no_token))
        for side, price, p, tok in sides:
            if not 0 < price < 1:
                continue
            rate = fee_rate.get(c.venue, 0.07)
            edge = p - price - rate * price * (1 - price)
            ratio = p / price
            rows.append({
                "venue": c.venue, "market_id": c.market_id, "label": c.label, "currency": c.currency,
                "kind": c.kind, "lo": c.lo, "hi": c.hi, "expiry": c.expiry.isoformat(timespec="minutes"),
                "side": side, "price": price, "implied": round(p, 4), "edge": round(edge, 4),
                "ratio": round(ratio, 2), "token_id": tok, "min_size": c.min_size,
                "cheap": price <= cfg.max_price and edge >= cfg.min_edge and ratio >= cfg.min_ratio,
            })
    rows.sort(key=lambda r: (not r["cheap"], -r["ratio"] if r["price"] <= cfg.max_price else 0, -r["edge"]))
    return rows
