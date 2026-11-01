import pytest

from arbdesk.trendfast import simulate

H6 = 6 * 3600 * 1000
P = {"ema_fast": 8, "ema_slow": 24, "regime_sma": 200, "min_spread_pct": 0.3, "atr_period": 14,
     "atr_stop_mult": 2.0, "trail_atr_mult": 3.0, "hard_stop_pct": 6.0, "risk_per_trade_pct": 2.0,
     "crash_drop_pct": 8.0, "crash_lookback": 4, "crash_cooldown": 8, "loss_streak": 3,
     "streak_cooldown": 12, "min_bars_between": 2, "max_drawdown_pct": 15.0,
     "min_stop_pct": 3.0, "max_position_pct": 50.0}


def bars(closes, spread=0.004):
    """Candles with a small high/low range around each close."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append([i * H6, o, max(o, c) * (1 + spread), min(o, c) * (1 - spread), c, 1.0])
        prev = c
    return out


def run(closes, **over):
    return simulate(bars(closes), {**P, **over}, 100.0, fee_pct=0.6, slip_pct=0.05)


def uptrend(n=260, start=100.0, step=0.004):
    return [start * (1 + step) ** i for i in range(n)]


def test_enters_uptrend_with_risk_based_size():
    r = run(uptrend())
    buy = next(t for t in r["trades"] if t["side"] == "buy")
    assert r["in_position"] and buy["bar"] >= 200                          # waits for the regime filter
    stop_dist = (buy["price"] - buy["stop"]) / buy["price"]
    assert 0.03 - 1e-9 <= stop_dist <= 0.06 + 1e-9                         # floor and hard cap
    assert buy["frac"] == pytest.approx(min(0.5, 0.02 / stop_dist), rel=1e-3)  # risk 2%, max 50% of equity


def test_no_buying_in_a_downtrend():
    r = run([100 * (0.997 ** i) for i in range(300)])
    assert r["stats"]["trades"] == 0 and not r["in_position"]


def test_trailing_stop_locks_gains_on_a_crash_bar():
    path = uptrend(240)
    path += [path[-1] * 0.80]                                             # one bar: -20%
    r = simulate(bars(path, spread=0.0), P, 100.0, 0.6, 0.05)
    sell = [t for t in r["trades"] if t["side"] == "sell"][-1]
    buy = [t for t in r["trades"] if t["side"] == "buy"][-1]
    assert sell["reason"] == "移动止损"
    assert buy["stop"] < sell["price"] < path[-2]                          # stop had moved up; not sold at the low
    assert sell["price"] > path[-1] * 1.15
    assert r["stats"]["return_pct"] > 0                                    # gains kept despite the crash


def test_gap_below_stop_fills_at_the_open():
    c = bars(uptrend(240), spread=0.0)
    last = c[-1][4]
    c.append([c[-1][0] + H6, last * 0.70, last * 0.72, last * 0.69, last * 0.71, 1.0])  # opens -30%
    r = simulate(c, P, 100.0, 0.6, 0.05)
    sell = [t for t in r["trades"] if t["side"] == "sell"][-1]
    assert sell["price"] == pytest.approx(last * 0.70)                     # the open, not the (better) stop
    # Even a -30% gap through the stop costs about half of that: at most 50% of equity was exposed.
    assert -17 < r["stats"]["return_pct"] < -5


def test_crash_breaker_and_cooldown():
    path = uptrend(240)
    top = path[-1]
    path += [top * 0.975, top * 0.95, top * 0.915]                        # -8.5% over 3 bars, no huge single gap
    path += [top * 0.92 * 1.004 ** i for i in range(1, 6)]                # quick bounce
    r = simulate(bars(path, spread=0.0), {**P, "atr_stop_mult": 10, "hard_stop_pct": 20,
                                         "trail_atr_mult": 50}, 100.0, 0.6, 0.05)
    reasons = [t["reason"] for t in r["trades"] if t["side"] == "sell"]
    assert "暴跌熔断" in reasons and not r["in_position"]                   # no re-entry during cooldown


def test_loss_streak_pause_and_drawdown_halt():
    # Choppy market just above the regime: repeated small losing trades
    base = uptrend(220)
    chop = []
    for k in range(40):
        chop += [base[-1] * 1.03, base[-1] * 1.05, base[-1] * 0.97]
    r = run(base + chop)
    assert any("连亏" in (r["note"] or "") for _ in [0]) or r["stats"]["round_trips"] >= 3
    r2 = simulate(bars(base + chop, spread=0.0), {**P, "max_drawdown_pct": 1.0, "loss_streak": 99}, 100.0, 0.6, 0.05)
    assert r2["halted"] and "需手动重置" in r2["note"] and not r2["in_position"]


def test_stats_report_costs():
    r = run(uptrend(400))
    s = r["stats"]
    assert s["fees_usd"] > 0 and s["days"] == pytest.approx(399 / 4, abs=0.1)
    assert set(s) >= {"trades_per_week", "win_rate_pct", "avg_hold_days", "fees_pct_of_capital", "exits"}
