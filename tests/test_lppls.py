import json

import numpy as np
import pytest

from arbdesk import lppls
from arbdesk.config import load_settings
from arbdesk.storage import Store
from arbdesk.trendfast import simulate
from test_trendfast import P, bars, uptrend

DAY = lppls.DAY_MS
W = [30, 45, 60, 75, 90, 105, 120]


def bubble(n=160, tc=170.0, m=0.5, w=9.0, B=-0.25, C=0.02, noise=0.004, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=float)
    dt = tc - t
    return t, 8 + B * dt ** m + C * dt ** m * np.cos(w * np.log(dt)) + rng.normal(0, noise, n)


def test_fit_recovers_parameters():
    t, y = bubble(noise=0.0)
    f = lppls.fit_window(t[40:], y[40:])
    assert f["r2"] > 0.999 and f["m"] == pytest.approx(0.5) and f["w"] == pytest.approx(9.0)
    assert f["B"] < 0 and lppls.qualifies(f)


@pytest.mark.parametrize("seed", range(3))
def test_bubble_detected_but_not_a_smooth_trend_or_random_walk(seed):
    t, y = bubble(seed=seed)
    assert lppls.confidence(t, y, 159, W)["bubble"] >= 0.8
    assert lppls.confidence(t, 16 - y, 159, W)["anti"] >= 0.8                 # mirrored: anti-bubble
    rng = np.random.default_rng(seed)
    exp = 8 + 0.004 * t + rng.normal(0, 0.004, len(t))                         # steady exponential rally
    assert lppls.confidence(t, exp, 159, W)["bubble"] == 0.0


def test_series_is_incremental_and_has_no_lookahead():
    t, y = bubble()
    daily = [(int(d) * DAY, float(np.exp(v))) for d, v in zip(t, y)]
    full = lppls.update_series({}, daily, W)
    # A day's value only uses data up to that day: computing on a truncated history gives the same number.
    part = lppls.update_series({}, daily[:140], W)
    assert all(full[d] == part[d] for d in part)
    calls = []
    orig = lppls.confidence
    lppls.confidence = lambda *a, **k: calls.append(1) or orig(*a, **k)
    try:
        lppls.update_series(part, daily[:141], W)
    finally:
        lppls.confidence = orig
    assert len(calls) == 1                                                     # only the new day is fitted
    assert min(lppls.update_series(full, daily, W, need_from=150 * DAY)) == 150 * DAY


def risk_from(closes, bubble_from_bar, value=1.0):
    """Bubble confidence `value` for every day from the one containing bar `bubble_from_bar`."""
    n_days = len(closes) * 6 // 24 + 2
    return {d * DAY: (value if d * DAY >= bubble_from_bar * 6 * 3_600_000 else 0.0) for d in range(n_days)}


def run(closes, risk, **over):
    return simulate(bars(closes), {**P, **over}, 100.0, 0.6, 0.05, risk=risk)


def test_high_bubble_confidence_blocks_entries_and_exits():
    up = uptrend(300)
    plain = run(up, None)
    first_buy = next(t["bar"] for t in plain["trades"] if t["side"] == "buy")
    blocked = run(up, risk_from(up, 0, 0.6))                                   # block (0.5) but not exit (0.75)
    assert not blocked["in_position"] and blocked["stats"]["lppls_blocked_bars"] > 0
    assert "LPPLS" in blocked["note"]
    out = run(up, risk_from(up, first_buy + 20))
    assert not out["in_position"] and any(t["reason"] == "LPPLS 泡沫离场" for t in out["trades"])


def test_moderate_confidence_only_tightens_the_trailing_stop():
    up = uptrend(300)
    plain, mild = run(up, None), run(up, risk_from(up, 0, 0.3))
    assert mild["in_position"] and mild["stop"] > plain["stop"]


def test_confidence_is_used_only_after_the_day_closed():
    up = uptrend(300)
    plain = run(up, None)
    first_buy = next(t["bar"] for t in plain["trades"] if t["side"] == "buy")
    # Bubble flagged on the day that *contains* the entry bar: that day has not closed yet when the
    # bar closes, so the entry must still happen.
    day = first_buy * 6 // 24
    risk = {d * DAY: (1.0 if d >= day else 0.0) for d in range(80)}
    r = run(up, risk)
    assert any(t["side"] == "buy" and t["bar"] == first_buy for t in r["trades"])
    assert run(up, risk, use_lppls=False)["stats"] == plain["stats"]


def test_lab_caches_series_and_alerts(tmp_path, monkeypatch):
    from arbdesk import strategies as S
    from test_strategies import FakeMarket, candles

    (tmp_path / "config.toml").write_text(f'data_dir = "{(tmp_path / "data").as_posix()}"\n')
    s = load_settings(tmp_path / "config.toml", tmp_path / ".env")
    s.strategies.enabled = ["trend_fast"]                     # plain baseline, no LPPLS
    for k in ("vwap_revert", "orb", "momentum_vol"):
        s.strategies.params[k]["enabled"] = False
    s.strategies.symbols = ["BTC/USD", "ETH/USD", "SOL/USD"]  # custom1 keeps its own list: BTC and ETH
    store = Store(s.data_dir / "t.db")
    t, y = bubble(n=200, tc=210.0)
    daily = candles([float(v) for v in np.exp(y)], tf_ms=DAY)
    six = candles([float(v) for v in np.exp(np.interp(np.arange(0, 200, 0.25), t, y))], tf_ms=DAY // 4)
    m = FakeMarket({"1d": daily, "6h": six}, price=six[-1][4])
    lab = S.StrategyLab(s, store, market=m)
    fits = []
    from arbdesk import lppls as L
    real = L.confidence
    monkeypatch.setattr(L, "confidence", lambda *a, **k: fits.append(1) or real(*a, **k))
    now = six[-1][0] + DAY // 4
    out = lab.tick(now)
    assert not out["errors"], out["errors"]
    n_first = len(fits)
    assert n_first > 0 and store.get_flag("lppls:BTC/USD")
    lab.tick(now + 60_000)
    assert len(fits) == n_first                                                # cached: nothing refitted
    st = lab.status()
    assert st["lppls"]["BTC/USD"]["bubble"] >= 0.25
    rows = {(r["name"], r["symbol"]): r for r in st["rows"]}
    assert set(rows) == {("custom1", "BTC/USD"), ("custom1", "ETH/USD"),
                         ("trend_fast", "BTC/USD"), ("trend_fast", "ETH/USD"), ("trend_fast", "SOL/USD")}
    custom = rows[("custom1", "BTC/USD")]
    assert custom["label"] == "定制策略1"
    assert "without_lppls" in custom["backtest"] and custom["backtest"]["lppls_used"]
    assert not rows[("trend_fast", "BTC/USD")]["backtest"].get("lppls_used")
    assert "custom1" in st["enabled"]
    alerts = store.alerts()
    assert {a["key"] for a in alerts} == {"lppls:BTC/USD", "lppls:ETH/USD"}
