import json

import pytest

from arbdesk import strategies as S
from arbdesk.config import load_settings
from arbdesk.storage import Store

DAY = 86_400_000


def candles(closes, tf_ms=DAY, start=0):
    return [[start + i * tf_ms, c, c * 1.01, c * 0.99, c, 1.0] for i, c in enumerate(closes)]


def test_sma_and_rsi():
    assert S.sma([1, 2, 3, 4], 2) == 3.5 and S.sma([1], 2) is None
    assert S.rsi([float(i) for i in range(30)]) == 100.0                 # only gains
    assert S.rsi([float(30 - i) for i in range(30)]) == pytest.approx(0.0)
    assert 40 < S.rsi([100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101]) < 60


def test_exposure_rules():
    up = candles([100 + i for i in range(60)])
    assert S.exposure_series("sma_cross", up, {"fast": 5, "slow": 20})[-1] == 1
    down = candles([200 - i for i in range(60)])
    assert S.exposure_series("sma_cross", down, {"fast": 5, "slow": 20})[-1] == 0
    # breakout: flat range then a jump above the 20-day high -> long; a drop below the 10-day low -> flat
    path = [100.0] * 25 + [110.0] + [111.0] * 3 + [90.0]
    exp = S.exposure_series("donchian", candles(path), {"entry": 20, "exit": 10})
    assert exp[24] == 0 and exp[25] == 1 and exp[-1] == 0
    # RSI: falls hard (oversold -> buy), then rallies (overbought -> sell)
    path = [100 - 2 * i for i in range(20)] + [62 + 3 * i for i in range(20)]
    exp = S.exposure_series("rsi", candles(path), {"period": 14, "buy_below": 30, "sell_above": 70})
    assert 1 in exp and exp[-1] == 0


def test_no_look_ahead_drop_forming_candle():
    c = candles([1, 2, 3])
    assert len(S.closed_candles(c, "1d", now_ms=2 * DAY + 1)) == 2      # the last one is still forming
    assert len(S.closed_candles(c, "1d", now_ms=3 * DAY)) == 3


def test_backtest_costs_and_benchmark():
    up = candles([100 + i for i in range(100)])
    bt = S.backtest("sma_cross", up, {"fast": 5, "slow": 20}, 100, fee_pct=0.6, slip_pct=0.05)
    assert bt["trades"] == 1 and 0 < bt["return_pct"] < bt["buy_hold_pct"]  # enters late, pays costs
    flat = S.backtest("sma_cross", candles([100.0] * 60), {"fast": 5, "slow": 20}, 100, 0.6, 0.05)
    assert flat["trades"] == 0 and flat["return_pct"] == 0
    dca = S.backtest("dca", candles([100.0] * 30), {"timeframe": "1d", "every_days": 7, "amount_usd": 5}, 100, 0.6, 0.05)
    assert dca["trades"] == 5                                                # days 0, 7, 14, 21, 28


# --------------------------------------------------------------------------
# Live paper engine
# --------------------------------------------------------------------------

class FakeMarket:
    def __init__(self, series, price):
        self.series, self.px = series, price

    def ohlcv(self, symbol, tf):
        return self.series[tf]

    def price(self, symbol):
        return self.px


@pytest.fixture
def lab(tmp_path):
    (tmp_path / "config.toml").write_text(f'data_dir = "{(tmp_path / "data").as_posix()}"\n')
    s = load_settings(tmp_path / "config.toml", tmp_path / ".env")
    s.strategies.enabled = ["sma_cross", "dca"]
    s.strategies.symbols = ["BTC/USD"]
    s.strategies.params["sma_cross"].update(fast=5, slow=20)
    store = Store(s.data_dir / "t.db")
    up = candles([100.0 + i for i in range(60)])
    m = FakeMarket({"1d": up, "1h": up}, price=160.0)
    return S.StrategyLab(s, store, market=m), store, m, up


def test_engine_enters_current_trend_once_and_exits_on_next_close(lab):
    L, store, m, up = lab
    now = up[-1][0] + DAY  # all 60 candles closed
    out = L.tick(now)
    sides = {(t["strategy"], t["side"]) for t in out["trades"]}
    assert sides == {("sma_cross", "buy"), ("dca", "buy")}               # trend already up -> long now
    st = store.strat_state("sma_cross", "BTC/USD")
    assert st["cash"] == 0 and st["qty"] == pytest.approx(25 * (1 - 0.0065) / 160)
    assert json.loads(st["backtest"])["trades"] >= 1
    assert L.tick(now + 60_000)["trades"] == []                          # same candle: nothing new
    # a sharp drop closes: fast MA falls below slow -> sell
    m.series["1d"] = up + candles([100.0] * 10, start=up[-1][0] + DAY)
    m.px = 100.0
    out = L.tick(m.series["1d"][-1][0] + DAY)
    assert ("sma_cross", "sell") in {(t["strategy"], t["side"]) for t in out["trades"]}
    st = store.strat_state("sma_cross", "BTC/USD")
    assert st["qty"] == 0 and st["cash"] < 25 and st["trades"] == 2


def test_dca_buys_on_schedule_until_cash_runs_out(lab):
    L, store, m, up = lab
    L.settings.strategies.enabled = ["dca"]
    now = up[-1][0] + DAY
    L.tick(now)
    L.tick(now + DAY)                                                    # too early
    assert store.strat_state("dca", "BTC/USD")["trades"] == 1
    for k in range(1, 8):
        L.tick(now + k * 7 * DAY)
    st = store.strat_state("dca", "BTC/USD")
    assert st["trades"] == 5 and st["cash"] == pytest.approx(0)          # $25 / $5 = 5 buys


def test_status_and_reset(lab):
    L, store, m, up = lab
    L.tick(up[-1][0] + DAY)
    rows = {r["name"]: r for r in L.status()["rows"]}
    assert rows["sma_cross"]["position"] == "持有" and rows["sma_cross"]["equity"] == pytest.approx(25 * (1 - 0.0065))
    L.reset()
    assert L.status()["rows"] == [] and store.strat_trades() == []
