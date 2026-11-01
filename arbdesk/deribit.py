"""Option-implied probabilities from Deribit's public BTC/ETH option chains.

Read-only public market data (no account, no keys, no trading). Deribit doesn't
serve US persons for trading; this module only reads quotes.

For a strike K and horizon T, the risk-neutral probability that the price ends
above K is the digital-call value. With a volatility smile sigma(K):

    P(S_T > K) = N(d2) - F * phi(d1) * sqrt(T) * dsigma/dK

The second term is the skew correction; without it, N(d2) misprices tails.
Between listed expiries, total implied variance (sigma^2 * T) is interpolated
linearly in time at the same strike.

Caveats worth keeping in mind:
  * risk-neutral != real-world probability; option tails usually carry a premium,
    so a prediction-market price BELOW the option-implied probability is a
    meaningful discount, not just noise;
  * prediction markets settle on a different index (e.g. CF Benchmarks BRTI)
    and at a different time of day than Deribit's 08:00 UTC expiry;
  * strikes outside the quoted range are not extrapolated (returns None).
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

API = "https://www.deribit.com/api/v2/public"
MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
YEAR_S = 365.25 * 86400


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def parse_instrument(name: str) -> tuple[datetime, float, str] | None:
    """'BTC-27DEC26-150000-C' -> (expiry 08:00 UTC, strike, 'C')."""
    try:
        _, exp, strike, kind = name.split("-")
        day, mon, yr = int(exp[:-5]), MONTHS[exp[-5:-2]], 2000 + int(exp[-2:])
        return datetime(yr, mon, day, 8, tzinfo=timezone.utc), float(strike), kind
    except (ValueError, KeyError):
        return None


@dataclass
class Smile:
    expiry: datetime
    forward: float
    strikes: list[float] = field(default_factory=list)
    vols: list[float] = field(default_factory=list)  # annualised, decimal

    def years(self, now: datetime) -> float:
        return (self.expiry - now).total_seconds() / YEAR_S

    def vol(self, k: float) -> float | None:
        """Linear interpolation in log-moneyness; no extrapolation beyond quoted strikes."""
        if len(self.strikes) < 3 or not self.strikes[0] <= k <= self.strikes[-1]:
            return None
        i = bisect.bisect_left(self.strikes, k)
        if self.strikes[i] == k:
            return self.vols[i]
        x0, x1 = math.log(self.strikes[i - 1] / self.forward), math.log(self.strikes[i] / self.forward)
        w = (math.log(k / self.forward) - x0) / (x1 - x0)
        return self.vols[i - 1] * (1 - w) + self.vols[i] * w


def build_smiles(summaries: list[dict], now: datetime | None = None) -> list[Smile]:
    """Deribit book summaries -> one smile per expiry, using out-of-the-money options
    (puts below the forward, calls above), which carry the cleanest implied vols."""
    now = now or datetime.now(timezone.utc)
    by_exp: dict[datetime, dict] = {}
    for s in summaries:
        parsed = parse_instrument(s.get("instrument_name", ""))
        iv, fwd = s.get("mark_iv"), s.get("underlying_price")
        if not parsed or not iv or not fwd:
            continue
        exp, k, kind = parsed
        if exp <= now:
            continue
        iv = float(iv)
        iv = iv / 100 if iv > 3 else iv  # Deribit quotes percent (65.2); accept decimals too
        e = by_exp.setdefault(exp, {"fwd": [], "pts": {}})
        e["fwd"].append(float(fwd))
        otm = (kind == "P" and k <= float(fwd)) or (kind == "C" and k >= float(fwd))
        if otm or k not in e["pts"]:
            e["pts"][k] = iv
    smiles = []
    for exp, e in sorted(by_exp.items()):
        ks = sorted(e["pts"])
        smiles.append(Smile(exp, sum(e["fwd"]) / len(e["fwd"]), ks, [e["pts"][k] for k in ks]))
    return smiles


def prob_above(smiles: list[Smile], k: float, when: datetime, now: datetime | None = None,
               max_gap_days: float = 45) -> float | None:
    """Option-implied P(S_when > k), or None if the chain can't support it."""
    now = now or datetime.now(timezone.utc)
    t = (when - now).total_seconds() / YEAR_S
    if t <= 0 or not smiles:
        return None
    after = next((s for s in smiles if s.expiry >= when), None)
    before = next((s for s in reversed(smiles) if s.expiry < when), None)
    if after is None:
        return None  # beyond the last listed expiry
    if before is None or before.years(now) <= 0:
        if (after.expiry - when).total_seconds() > max_gap_days * 86400:
            return None
        before = None
    elif (after.expiry - before.expiry).total_seconds() > 2 * max_gap_days * 86400:
        return None

    def total_var(s: Smile, strike: float) -> float | None:
        v = s.vol(strike)
        return None if v is None else v * v * max(s.years(now), 1e-9)

    def sigma_at(strike: float) -> float | None:
        wa = total_var(after, strike)
        if wa is None:
            return None
        if before is None:
            return math.sqrt(wa / after.years(now))  # same vol, scaled to the shorter horizon
        wb = total_var(before, strike)
        if wb is None:
            return None
        ta, tb = after.years(now), before.years(now)
        w = wb + (wa - wb) * (t - tb) / (ta - tb)
        return math.sqrt(max(w, 1e-12) / t)

    if before is None:
        fwd = after.forward
    else:
        ta, tb = after.years(now), before.years(now)
        fwd = before.forward + (after.forward - before.forward) * (t - tb) / (ta - tb)
    sig = sigma_at(k)
    if sig is None or sig <= 0:
        return None
    h = k * 0.01
    s_up, s_dn = sigma_at(k + h), sigma_at(k - h)
    slope = (s_up - s_dn) / (2 * h) if s_up is not None and s_dn is not None else 0.0
    sq = sig * math.sqrt(t)
    d1 = (math.log(fwd / k) + 0.5 * sq * sq) / sq
    d2 = d1 - sq
    p = norm_cdf(d2) - fwd * norm_pdf(d1) * math.sqrt(t) * slope
    return min(1.0, max(0.0, p))


class DeribitClient:
    def __init__(self, session: requests.Session | None = None, timeout: float = 15):
        self.s = session or requests.Session()
        self.timeout = timeout

    def option_summaries(self, currency: str) -> list[dict]:
        r = self.s.get(f"{API}/get_book_summary_by_currency",
                       params={"currency": currency, "kind": "option"}, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("result") or []

    def index_price(self, currency: str) -> float | None:
        r = self.s.get(f"{API}/get_index_price", params={"index_name": f"{currency.lower()}_usd"},
                       timeout=self.timeout)
        r.raise_for_status()
        return (r.json().get("result") or {}).get("index_price")
