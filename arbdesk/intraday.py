"""Intraday strategies on 15-minute candles, long-only, flat by the end of each session.

    vwap_revert   VWAP mean reversion: buy when price is stretched `band_sd` standard deviations
                  below the session VWAP while the multi-day trend is not down; sell back at VWAP
    orb           opening range breakout: the first `range_minutes` of the session set a range;
                  buy a close above its high, stop at its low, target `target_r` x the risk
    momentum_vol  momentum + volume: buy a strong 1-hour move on a volume spike that closes near
                  its high, in an uptrend; ATR trailing stop and a maximum holding time

Common risk controls (engine):
    sizing        risk-based: a stop-out loses at most `risk_per_trade_pct` of equity, stop
                  distance clamped to [min_stop_pct, hard_stop_pct], position <= max_position_pct
    stops         hit inside the bar (fill at the stop, or at the open if the bar gapped through)
    session end   every position is closed on the last bar of the session (no overnight risk)
    daily loss    equity down `daily_loss_pct` since the session start -> flat, no entries until
                  the next session
    drawdown      equity drawdown >= max_drawdown_pct -> stop until reset
    LPPLS         optional (use_lppls): no new entries while the daily bubble confidence known
                  at the bar's close is >= lppls_block

A session is a UTC day shifted by `session_start_utc` hours (0 = midnight UTC; 13.5 = the US
equity open in summer, 14.5 in winter). Like trendfast.simulate, `simulate` replays the whole
history deterministically and is both the backtest and the source of the live position.
"""
from __future__ import annotations

import bisect
import math

DAY_MS = 86_400_000
TF_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}

COMMON = {
    "timeframe": "15m", "history_bars": 2880,          # 30 days of 15-minute candles
    "session_start_utc": 0.0,
    "risk_per_trade_pct": 1.0, "min_stop_pct": 0.8, "hard_stop_pct": 3.0, "max_position_pct": 50.0,
    "daily_loss_pct": 3.0, "max_drawdown_pct": 15.0, "min_bars_between": 2,
    "use_lppls": False, "lppls_windows": [30, 45, 60, 75, 90, 105, 120], "lppls_block": 0.5,
    "lppls_tighten": 0.25, "lppls_exit": 0.75, "lppls_min_r2": 0.8,
}
DEFAULTS = {
    "vwap_revert": {**COMMON, "band_sd": 2.0, "min_band_pct": 0.6, "stop_sd": 1.5, "regime_bars": 288,
                    "regime_tolerance_pct": 1.0, "warmup_bars": 8, "last_entry_hour": 20.0},
    "orb": {**COMMON, "range_minutes": 60, "target_r": 2.0, "last_entry_hour": 12.0, "min_range_pct": 0.3,
            "max_range_pct": 3.0},
    "momentum_vol": {**COMMON, "lookback_bars": 4, "move_pct": 1.5, "volume_mult": 2.0, "volume_bars": 96,
                     "close_in_range": 0.7, "regime_bars": 192, "atr_period": 14, "atr_stop_mult": 2.0,
                     "trail_atr_mult": 2.0, "max_hold_bars": 24, "last_entry_hour": 22.0},
}
LABELS = {"vwap_revert": "VWAP 回归（日内）", "orb": "开盘区间突破（日内）", "momentum_vol": "动量+放量突破（日内）"}


def _session(ts: int, offset_ms: int) -> int:
    return (ts - offset_ms) // DAY_MS


def _rolling_mean(values: list[float], n: int) -> list[float | None]:
    out, s = [], 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


# --------------------------------------------------------------------------
# Rules: each returns a stop (entry), an exit reason, or a new stop, per bar
# --------------------------------------------------------------------------

class VwapRevert:
    def __init__(self, c, p, sess, bar_in_sess):
        self.p, self.c, self.bis = p, c, bar_in_sess
        closes = [x[4] for x in c]
        self.regime = _rolling_mean(closes, p["regime_bars"])
        self.vwap, self.sd = [], []
        pv = v = pv2 = 0.0
        for i, x in enumerate(c):
            if i == 0 or sess[i] != sess[i - 1]:
                pv = v = pv2 = 0.0
            tp = (x[2] + x[3] + x[4]) / 3
            vol = x[5] or 1e-12
            pv, v, pv2 = pv + tp * vol, v + vol, pv2 + tp * tp * vol
            vw = pv / v
            self.vwap.append(vw)
            self.sd.append(math.sqrt(max(pv2 / v - vw * vw, 0.0)))

    def entry(self, i, hour):
        p, cl = self.p, self.c[i][4]
        reg = self.regime[i]
        if reg is None or self.bis[i] < p["warmup_bars"] or hour >= p["last_entry_hour"]:
            return None
        if cl < reg * (1 - p["regime_tolerance_pct"] / 100):
            return None                                  # multi-day trend is down: don't catch the knife
        vw, sd = self.vwap[i], self.sd[i]
        band = max(p["band_sd"] * sd, vw * p["min_band_pct"] / 100)
        if cl > vw - band:
            return None
        return cl - p["stop_sd"] * max(sd, band / p["band_sd"]), "跌离 VWAP"

    def exit(self, i, pos):
        return "回到 VWAP" if self.c[i][4] >= self.vwap[i] else None

    def trail(self, i, pos):
        return None


class OpeningRange:
    def __init__(self, c, p, sess, bar_in_sess, bar_ms):
        self.p, self.c, self.bis = p, c, bar_in_sess
        self.n = max(1, round(p["range_minutes"] * 60_000 / bar_ms))
        self.hi, self.lo, self.traded = {}, {}, set()
        for i, x in enumerate(c):
            if bar_in_sess[i] < self.n:
                s = sess[i]
                self.hi[s] = max(self.hi.get(s, x[2]), x[2])
                self.lo[s] = min(self.lo.get(s, x[3]), x[3])
        self.sess = sess

    def entry(self, i, hour):
        p, s, cl = self.p, self.sess[i], self.c[i][4]
        if self.bis[i] < self.n or s in self.traded or s not in self.hi or hour >= p["last_entry_hour"]:
            return None
        hi, lo = self.hi[s], self.lo[s]
        width = (hi - lo) / lo * 100
        if not p["min_range_pct"] <= width <= p["max_range_pct"] or cl <= hi:
            return None
        self.traded.add(s)                                # one breakout per session
        return lo, "突破开盘区间"

    def exit(self, i, pos):
        risk = pos["entry"] - pos["stop0"]
        return "达到目标" if self.c[i][4] >= pos["entry"] + self.p["target_r"] * risk else None

    def trail(self, i, pos):
        return None


class MomentumVolume:
    def __init__(self, c, p, sess, bar_in_sess):
        self.p, self.c = p, c
        closes = [x[4] for x in c]
        self.regime = _rolling_mean(closes, p["regime_bars"])
        vols = [x[5] for x in c]
        self.vavg = [None] + _rolling_mean(vols, p["volume_bars"])[:-1]   # average of the bars BEFORE i
        self.atr, a = [], None
        for i, x in enumerate(c):
            pc = c[i - 1][4] if i else x[4]
            tr = max(x[2] - x[3], abs(x[2] - pc), abs(x[3] - pc))
            a = tr if a is None else (a * (p["atr_period"] - 1) + tr) / p["atr_period"]
            self.atr.append(a)

    def entry(self, i, hour):
        p, x = self.p, self.c[i]
        k = p["lookback_bars"]
        if i < k or self.regime[i] is None or self.vavg[i] is None or hour >= p["last_entry_hour"]:
            return None
        cl, ref = x[4], self.c[i - k][4]
        if cl <= self.regime[i] or (cl / ref - 1) * 100 < p["move_pct"]:
            return None
        if x[5] < p["volume_mult"] * self.vavg[i]:
            return None
        if x[2] > x[3] and (cl - x[3]) / (x[2] - x[3]) < p["close_in_range"]:
            return None                                   # closed well off the high: buyers faded
        return cl - p["atr_stop_mult"] * self.atr[i], "放量急涨"

    def exit(self, i, pos):
        return "持有到时" if i - pos["bar"] >= self.p["max_hold_bars"] else None

    def trail(self, i, pos):
        return pos["high"] - self.p["trail_atr_mult"] * self.atr[i]


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

def simulate(kind: str, candles: list[list], p: dict, capital: float, fee_pct: float, slip_pct: float,
             risk: dict[int, float] | None = None) -> dict:
    cost = (fee_pct + slip_pct) / 100
    bar_ms = TF_MS[p["timeframe"]]
    off = int(p["session_start_utc"] * 3_600_000)
    sess = [_session(c[0], off) for c in candles]
    bis, n = [], 0
    for i in range(len(candles)):
        n = 0 if i == 0 or sess[i] != sess[i - 1] else n + 1
        bis.append(n)
    if kind == "vwap_revert":
        rules = VwapRevert(candles, p, sess, bis)
    elif kind == "orb":
        rules = OpeningRange(candles, p, sess, bis, bar_ms)
    elif kind == "momentum_vol":
        rules = MomentumVolume(candles, p, sess, bis)
    else:
        raise ValueError(f"unknown intraday strategy {kind}")
    use_risk = bool(risk) and p.get("use_lppls")
    risk_days = sorted(risk) if use_risk else []

    cash, qty, fees = capital, 0.0, 0.0
    pos = None
    trades, reason, held = [], "", 0
    peak, mdd, halted = capital, 0.0, False
    last_exit, sess_start_eq, day_stopped, cur_sess = -10 ** 9, capital, None, None
    bubble, blocked = 0.0, 0

    def sell(i, px, why):
        nonlocal cash, qty, fees, pos, last_exit, reason
        proceeds = qty * px * (1 - cost)
        fees += qty * px * cost
        pnl = proceeds - pos["value"]
        trades.append({"bar": i, "ts": candles[i][0], "side": "sell", "price": px, "reason": why, "pnl": pnl})
        cash += proceeds
        qty, pos, last_exit, reason = 0.0, None, i, why

    for i, c in enumerate(candles):
        o, h, lo, cl = c[1], c[2], c[3], c[4]
        if sess[i] != cur_sess:
            cur_sess, sess_start_eq = sess[i], cash + qty * o
        close_ms = c[0] + bar_ms
        hour = ((close_ms - off) % DAY_MS) / 3_600_000
        last_of_session = i + 1 < len(candles) and sess[i + 1] != sess[i] or \
            _session(close_ms, off) != sess[i]
        if use_risk:
            k = bisect.bisect_right(risk_days, close_ms - DAY_MS) - 1
            bubble = risk[risk_days[k]] if k >= 0 else 0.0

        if pos:
            held += 1
            if lo <= pos["stop"]:
                sell(i, min(o, pos["stop"]), "止损" if pos["stop"] <= pos["entry"] else "移动止损")
            else:
                why = rules.exit(i, pos)
                if why:
                    sell(i, cl, why)
                elif last_of_session:
                    sell(i, cl, "收盘平仓")
                else:
                    pos["high"] = max(pos["high"], h)
                    t = rules.trail(i, pos)
                    if t:
                        pos["stop"] = max(pos["stop"], t)
        eq = cash + qty * cl
        if not halted and day_stopped != sess[i] and eq <= sess_start_eq * (1 - p["daily_loss_pct"] / 100):
            if pos:
                sell(i, cl, "当日亏损上限")
            day_stopped, reason = sess[i], f"当日亏损达 {p['daily_loss_pct']}%，今天不再开仓"
        elif (not pos and not halted and day_stopped != sess[i] and not last_of_session
              and i - last_exit >= p["min_bars_between"]):
            sig = rules.entry(i, hour)
            if sig and use_risk and bubble >= p["lppls_block"]:
                blocked += 1
                reason = f"LPPLS 泡沫置信度 {bubble:.0%}，暂停开新仓"
            elif sig:
                stop, why = sig
                dist = (cl - stop) / cl if stop < cl else 0.0
                dist = min(max(dist, p["min_stop_pct"] / 100), p["hard_stop_pct"] / 100)
                frac = min(p["max_position_pct"] / 100, (p["risk_per_trade_pct"] / 100) / dist)
                spend = cash * frac
                qty = spend * (1 - cost) / cl
                fees += spend * cost
                cash -= spend
                pos = {"bar": i, "entry": cl, "stop": cl * (1 - dist), "stop0": cl * (1 - dist), "high": h,
                       "value": spend}
                trades.append({"bar": i, "ts": c[0], "side": "buy", "price": cl, "frac": round(frac, 3),
                               "stop": pos["stop"], "reason": why})
                reason = ""

        eq = cash + qty * cl
        peak = max(peak, eq)
        dd = (peak - eq) / peak
        mdd = max(mdd, dd)
        if not halted and dd >= p["max_drawdown_pct"] / 100:
            halted = True
            if pos:
                sell(i, cl, "回撤熔断")
            reason = f"回撤达 {dd * 100:.1f}%，已停止，需手动重置"

    closes = [c[4] for c in candles]
    end_eq = cash + qty * (closes[-1] if closes else 0)
    sells = [t for t in trades if t["side"] == "sell"]
    days = (candles[-1][0] - candles[0][0]) / DAY_MS if len(candles) > 1 else 0
    return {
        "in_position": pos is not None, "qty_frac": (qty * closes[-1] / end_eq) if qty and end_eq else 0.0,
        "stop": pos["stop"] if pos else None, "entry": pos["entry"] if pos else None, "halted": halted,
        "cooldown_left_bars": 0, "note": reason, "bubble": bubble,
        "stats": {
            "from": candles[0][0] if candles else None, "to": candles[-1][0] if candles else None,
            "candles": len(candles), "days": round(days, 1),
            "return_pct": round((end_eq / capital - 1) * 100, 2),
            "buy_hold_pct": round((closes[-1] / closes[0] * (1 - cost) - 1) * 100, 2) if closes else 0,
            "trades": len(trades), "round_trips": len(sells),
            "trades_per_week": round(len(trades) / days * 7, 2) if days else 0,
            "win_rate_pct": round(100 * sum(t["pnl"] > 0 for t in sells) / len(sells), 1) if sells else None,
            "avg_hold_days": round(held * bar_ms / DAY_MS / len(sells), 2) if sells else None,
            "fees_usd": round(fees, 2), "fees_pct_of_capital": round(fees / capital * 100, 2),
            "max_drawdown_pct": round(mdd * 100, 2),
            "exits": {r: sum(t["reason"] == r for t in sells) for r in {t["reason"] for t in sells}},
            "lppls_used": use_risk, "lppls_blocked_bars": blocked,
        },
        "trades": trades[-20:],
    }
