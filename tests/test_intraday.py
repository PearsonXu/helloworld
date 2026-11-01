import pytest

from arbdesk.intraday import DEFAULTS, simulate

M15 = 900_000
DAY = 86_400_000


def bar(i, c, o=None, h=None, lo=None, v=1.0, start=0):
    o = c if o is None else o
    return [start + i * M15, o, max(o, c) if h is None else h, min(o, c) if lo is None else lo, c, v]


def run(kind, candles, **over):
    p = {**DEFAULTS[kind], **over}
    return simulate(kind, candles, p, 100.0, 0.6, 0.05)


def flat(n, px=100.0, start_i=0, v=1.0):
    return [bar(start_i + i, px, h=px * 1.001, lo=px * 0.999, v=v) for i in range(n)]


def test_vwap_buys_a_stretch_below_vwap_and_sells_back_at_vwap():
    c = flat(20)
    c.append(bar(20, 97.0, o=100.0, h=100.0, lo=96.9))         # flush far below VWAP
    c += [bar(21 + i, 97.5) for i in range(3)]
    c.append(bar(24, 100.2, o=97.5))                            # back above VWAP
    c += flat(5, 100.2, 25)
    r = run("vwap_revert", c, regime_bars=8, regime_tolerance_pct=5)
    sides = [(t["side"], t["reason"]) for t in r["trades"]]
    assert sides[:2] == [("buy", "跌离 VWAP"), ("sell", "回到 VWAP")]
    assert r["trades"][0]["bar"] == 20 and r["trades"][1]["bar"] == 24


def test_vwap_skips_when_the_multi_day_trend_is_down():
    c = [bar(i, 120 - i * 0.5) for i in range(30)]               # steady slide: below its own average
    c.append(bar(30, 100.0, o=105.0))
    assert run("vwap_revert", c, regime_bars=8)["trades"] == []


def orb_day(after, start_i=0):
    rng = [bar(start_i + i, 100.5, h=101.0, lo=100.0) for i in range(4)]   # 00:00-01:00 range 100-101
    return rng + [bar(start_i + 4 + i, px) for i, px in enumerate(after)]


def test_orb_breakout_hits_target():
    c = orb_day([100.6, 101.5, 102.5, 104.6, 104.8])
    r = run("orb", c)
    buy, sell = r["trades"][0], r["trades"][1]
    assert buy["reason"] == "突破开盘区间" and buy["price"] == 101.5
    assert buy["stop"] == pytest.approx(100.0)                  # range low (1.5% away, inside the clamp)
    assert sell["reason"] == "达到目标" and sell["price"] == 104.6   # entry + 2R = 104.5


def test_orb_closes_at_session_end_and_trades_once_per_session():
    day1 = orb_day([101.5] + [101.6] * 91)                      # 96 bars = one full day, no target
    day2 = orb_day([100.5] * 92, start_i=96)
    r = run("orb", day1 + day2)
    sells = [t for t in r["trades"] if t["side"] == "sell"]
    assert len([t for t in r["trades"] if t["side"] == "buy"]) == 1
    assert sells[0]["reason"] == "收盘平仓" and sells[0]["bar"] == 95


def test_orb_stop_fills_inside_the_bar_or_at_a_gap_open():
    c = orb_day([101.5, 101.2])
    c.append(bar(6, 99.0, o=99.5, lo=98.8))                      # gaps open below the 100 stop
    r = run("orb", c)
    assert r["trades"][1]["reason"] == "止损" and r["trades"][1]["price"] == 99.5


def test_momentum_needs_volume_and_exits_on_time():
    base = flat(120)
    quiet = base + [bar(120, 102.0, o=100.0, h=102.0, lo=100.0, v=1.0)] + flat(30, 102.0, 121)
    assert run("momentum_vol", quiet, regime_bars=20)["trades"] == []            # no volume spike
    loud = base + [bar(120, 102.0, o=100.0, h=102.0, lo=100.0, v=5.0)] + flat(30, 102.0, 121)
    r = run("momentum_vol", loud, regime_bars=20)
    assert r["trades"][0]["reason"] == "放量急涨"
    assert r["trades"][1]["reason"] == "持有到时" and r["trades"][1]["bar"] == 120 + 24


def test_daily_loss_limit_stops_trading_for_the_rest_of_the_session():
    c = orb_day([101.5, 101.4])
    c.append(bar(6, 100.1, o=101.4, lo=100.05))                  # not stopped yet (stop at 100)
    p = dict(daily_loss_pct=0.5, risk_per_trade_pct=1.0)
    r = run("orb", c, **p)
    assert r["trades"][-1]["reason"] == "当日亏损上限" and "今天不再开仓" in r["note"]


def test_position_size_is_risk_based_and_capped():
    r = run("orb", orb_day([101.5, 101.6]))
    buy = r["trades"][0]
    dist = (buy["price"] - buy["stop"]) / buy["price"]
    assert buy["frac"] == pytest.approx(min(0.5, 0.01 / dist), rel=1e-3)


def test_lab_runs_intraday_with_low_fee_comparison(tmp_path):
    from arbdesk import strategies as S
    from arbdesk.config import load_settings
    from arbdesk.storage import Store
    from test_strategies import FakeMarket

    (tmp_path / "config.toml").write_text(f'data_dir = "{(tmp_path / "data").as_posix()}"\n')
    s = load_settings(tmp_path / "config.toml", tmp_path / ".env")
    s.strategies.enabled = []                                    # old config: intraday still auto-runs
    s.strategies.params["custom1"]["enabled"] = False
    c = []
    for d in range(4):
        c += orb_day([101.5, 102.5, 104.6] + [104.6] * 89, start_i=d * 96)
    m = FakeMarket({"15m": c}, price=104.6)
    lab = S.StrategyLab(s, store := Store(s.data_dir / "t.db"), market=m)
    assert lab.enabled() == ["vwap_revert", "orb", "momentum_vol"]
    out = lab.tick(c[-1][0] + M15)
    assert not out["errors"], out["errors"]
    rows = {(r["name"], r["symbol"]): r for r in lab.status()["rows"]}
    assert ("orb", "ETH/USD") in rows and rows[("orb", "BTC/USD")]["label"] == "开盘区间突破（日内）"
    bt = rows[("orb", "BTC/USD")]["backtest"]
    assert bt["round_trips"] >= 3 and bt["low_fee"]["return_pct"] > bt["return_pct"]
    assert store.strat_state("orb", "BTC/USD")


def test_lab_tops_up_long_histories_instead_of_refetching():
    from arbdesk import strategies as S

    class M:
        def __init__(self):
            self.calls = []

        def ohlcv(self, sym, tf, limit):
            self.calls.append(limit)
            return [[i * M15, 1, 1, 1, 1, 1] for i in range(1000 - limit, 1000)]

    lab = S.StrategyLab.__new__(S.StrategyLab)
    lab._market = M()
    assert len(lab._history("BTC/USD", "15m", 800)) == 800
    assert len(lab._history("BTC/USD", "15m", 800)) == 800
    assert lab._market.calls == [800, 300]
