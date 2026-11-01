"""Fast trend strategy (6-hour candles) with layered crash protection.

Built on the daily SMA-cross idea (trade only with the trend), but faster, and
with risk controls aimed at one-sided crashes:

  entry     long-term regime: close above the 200-bar SMA (~50 days on 6h bars) — no
            buying in a bear market; fast EMA (8) above slow EMA (24) by >= min_spread_pct;
            not in a cooldown/pause; at least `min_bars_between` bars since the last exit
  sizing    risk-based: the position is sized so that hitting the stop loses at most
            `risk_per_trade_pct` of equity (stop 6% away, risk 2% -> ~1/3 of equity), with the
            stop distance floored at min_stop_pct and the position capped at max_position_pct,
            because a gap can open far below any stop
  exits     initial stop: entry - atr_stop_mult * ATR, never wider than hard_stop_pct
            trailing stop: highest high since entry - trail_atr_mult * ATR (only moves up)
            trend exit: fast EMA below slow EMA, or close below the regime SMA
            crash breaker: close >= crash_drop_pct below the highest close of the last
                           `crash_lookback` bars -> exit, no new entries for crash_cooldown bars
  pauses    `loss_streak` losing trades in a row -> pause `streak_cooldown` bars
            equity drawdown >= max_drawdown_pct from its peak -> stop until reset
  LPPLS     optional bubble filter (lppls.py), daily confidence of a positive bubble:
            >= lppls_tighten -> trailing stop tightens to lppls_trail_atr_mult ATR
            >= lppls_block   -> no new entries
            >= lppls_exit    -> exit now
            Each bar only sees the confidence of the last day that had fully closed
            before the bar closed, so there is no look-ahead.

Stops are hit intra-bar in the backtest (fill at the stop, or at the open if the
bar gapped through it); live, the engine checks the stop against the current
price on every scan instead of waiting for the bar to close.

`simulate` replays the whole history deterministically: the same function is
the backtest and the source of the live position, so the two cannot drift.
"""
from __future__ import annotations

import bisect

BAR_HOURS = 6
DAY_MS = 86_400_000


def simulate(candles: list[list], p: dict, capital: float, fee_pct: float, slip_pct: float,
             risk: dict[int, float] | None = None) -> dict:
    """`risk`: {day_start_ms: LPPLS bubble confidence using data through that day}; None = no filter."""
    cost = (fee_pct + slip_pct) / 100
    bar_ms = BAR_HOURS * 3_600_000
    use_risk = bool(risk) and p.get("use_lppls", True)
    risk_days = sorted(risk) if use_risk else []
    closes = [c[4] for c in candles]
    n_fast, n_slow, n_reg, n_atr = p["ema_fast"], p["ema_slow"], p["regime_sma"], p["atr_period"]
    kf, ks = 2 / (n_fast + 1), 2 / (n_slow + 1)

    cash, qty, fees = capital, 0.0, 0.0
    ef = es = atr = None
    entry = stop = trail_high = None
    entry_value = 0.0
    cooldown_until = last_exit = -10 ** 9
    streak, peak, mdd, halted = 0, capital, 0.0, False
    trades, held_bars, reason = [], 0, ""
    reg_sum = 0.0
    bubble = 0.0
    lppls_blocked = 0

    def sell(i, px, why):
        nonlocal cash, qty, fees, entry, stop, trail_high, last_exit, streak, cooldown_until, reason
        proceeds = qty * px * (1 - cost)
        fees += qty * px * cost
        pnl = proceeds - entry_value
        trades.append({"bar": i, "ts": candles[i][0], "side": "sell", "price": px, "reason": why, "pnl": pnl})
        cash += proceeds
        qty, entry, stop, trail_high, last_exit, reason = 0.0, None, None, None, i, why
        streak = streak + 1 if pnl < 0 else 0
        if streak >= p["loss_streak"]:
            cooldown_until = max(cooldown_until, i + p["streak_cooldown"])
            reason = f"连亏 {streak} 笔，暂停"
            streak = 0

    for i, c in enumerate(candles):
        o, h, lo, cl = c[1], c[2], c[3], c[4]
        prev_close = closes[i - 1] if i else cl
        tr = max(h - lo, abs(h - prev_close), abs(lo - prev_close))
        atr = tr if atr is None else (atr * (n_atr - 1) + tr) / n_atr
        ef = cl if ef is None else ef + kf * (cl - ef)
        es = cl if es is None else es + ks * (cl - es)
        reg_sum += cl
        if i >= n_reg:
            reg_sum -= closes[i - n_reg]
        regime = reg_sum / n_reg if i >= n_reg - 1 else None
        warm = i >= max(n_reg, n_slow, n_atr)
        if use_risk:
            k = bisect.bisect_right(risk_days, c[0] + bar_ms - DAY_MS) - 1
            bubble = risk[risk_days[k]] if k >= 0 else 0.0
        caution = use_risk and bubble >= p.get("lppls_block", 0.5)
        tighten = use_risk and bubble >= p.get("lppls_tighten", 0.25)

        if qty > 0:
            held_bars += 1
            if lo <= stop:                                   # stop hit inside the bar
                sell(i, min(o, stop), "止损" if trail_high is None or stop <= entry else "移动止损")
            else:
                recent = max(closes[max(0, i - p["crash_lookback"]):i] or [cl])
                if cl <= recent * (1 - p["crash_drop_pct"] / 100):
                    sell(i, cl, "暴跌熔断")
                    cooldown_until = max(cooldown_until, i + p["crash_cooldown"])
                elif ef < es or (regime is not None and cl < regime):
                    sell(i, cl, "趋势结束")
                elif use_risk and bubble >= p.get("lppls_exit", 0.75):
                    sell(i, cl, "LPPLS 泡沫离场")
                else:
                    trail_high = max(trail_high, h)
                    mult = p.get("lppls_trail_atr_mult", 1.5) if tighten else p["trail_atr_mult"]
                    stop = max(stop, trail_high - mult * atr)
        elif warm and not halted and i >= cooldown_until and i - last_exit >= p["min_bars_between"]:
            spread = (ef - es) / es * 100
            if regime is not None and cl > regime and spread >= p["min_spread_pct"] and caution:
                lppls_blocked += 1                           # the trend says buy, the bubble filter says no
                reason = f"LPPLS 泡沫置信度 {bubble:.0%}，暂停开新仓"
            elif regime is not None and cl > regime and spread >= p["min_spread_pct"]:
                # Stop distance: ATR-based, but never tighter than min_stop_pct (calm markets would
                # otherwise size up to all-in) nor wider than hard_stop_pct.
                dist = min(max(p["atr_stop_mult"] * atr / cl, p["min_stop_pct"] / 100), p["hard_stop_pct"] / 100)
                # A gap can open far below any stop, so cap the position as well.
                frac = min(p["max_position_pct"] / 100, (p["risk_per_trade_pct"] / 100) / dist)
                equity = cash
                spend = equity * frac
                qty = spend * (1 - cost) / cl
                fees += spend * cost
                cash -= spend
                entry, stop, trail_high, entry_value = cl, cl * (1 - dist), h, spend
                trades.append({"bar": i, "ts": c[0], "side": "buy", "price": cl, "frac": round(frac, 3),
                               "stop": stop, "reason": "入场"})
                reason = ""

        eq = cash + qty * cl
        peak = max(peak, eq)
        dd = (peak - eq) / peak
        mdd = max(mdd, dd)
        if not halted and dd >= p["max_drawdown_pct"] / 100:
            halted = True
            if qty > 0:
                sell(i, cl, "回撤熔断")
            reason = f"回撤达 {dd * 100:.1f}%，已停止，需手动重置"

    last = len(candles) - 1
    end_eq = cash + qty * (closes[-1] if closes else 0)
    sells = [t for t in trades if t["side"] == "sell"]
    days = (candles[-1][0] - candles[0][0]) / 86_400_000 if len(candles) > 1 else 0
    return {
        "in_position": qty > 0, "qty_frac": (qty * closes[-1] / end_eq) if qty and end_eq else 0.0,
        "stop": stop, "entry": entry, "halted": halted,
        "cooldown_left_bars": max(0, cooldown_until - last), "note": reason, "bubble": bubble,
        "stats": {
            "from": candles[0][0] if candles else None, "to": candles[-1][0] if candles else None,
            "candles": len(candles), "days": round(days, 1),
            "return_pct": round((end_eq / capital - 1) * 100, 2),
            "buy_hold_pct": round((closes[-1] / closes[0] * (1 - cost) - 1) * 100, 2) if closes else 0,
            "trades": len(trades), "round_trips": len(sells),
            "trades_per_week": round(len(trades) / days * 7, 2) if days else 0,
            "win_rate_pct": round(100 * sum(t["pnl"] > 0 for t in sells) / len(sells), 1) if sells else None,
            "avg_hold_days": round(held_bars * BAR_HOURS / 24 / len(sells), 1) if sells else None,
            "fees_usd": round(fees, 2), "fees_pct_of_capital": round(fees / capital * 100, 2),
            "max_drawdown_pct": round(mdd * 100, 2),
            "exits": {r: sum(t["reason"] == r for t in sells) for r in {t["reason"] for t in sells}},
            "lppls_used": use_risk, "lppls_blocked_bars": lppls_blocked,
        },
        "trades": trades[-20:],
    }
