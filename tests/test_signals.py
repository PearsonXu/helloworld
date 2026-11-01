import json

import pytest

from arbdesk import signals
from arbdesk import strategies as S
from arbdesk.config import load_settings
from arbdesk.notify_email import EmailNotifier
from arbdesk.storage import Store
from test_strategies import DAY, FakeMarket, candles

# Up, down (the strategy sits out most of the fall), then a rebound: the 5/20 cross fires on candle 79.
# Backtest before the signal: about +13% vs buy-and-hold about -21%.
CLOSES = [100 + 3 * i for i in range(40)] + [217 - 6 * i for i in range(30)] + [43 + 4 * i for i in range(1, 16)]
ALL = candles(CLOSES)


@pytest.fixture
def lab(tmp_path):
    (tmp_path / "config.toml").write_text(f'data_dir = "{(tmp_path / "data").as_posix()}"\n')
    s = load_settings(tmp_path / "config.toml", tmp_path / ".env")
    s.strategies.enabled = ["sma_cross"]
    s.strategies.symbols = ["ETH/USD"]
    s.strategies.params["sma_cross"].update(fast=5, slow=20)
    for k in ("custom1", "vwap_revert", "orb", "momentum_vol"):
        s.strategies.params[k]["enabled"] = False
    s.email.timezone = "UTC"
    store = Store(s.data_dir / "t.db")
    m = FakeMarket({"1d": ALL[:78]}, price=ALL[77][4])
    return S.StrategyLab(s, store, market=m), s, store, m


def step_to(lab_, m, n, price=None):
    m.series["1d"] = ALL[:n]
    m.px = price or ALL[n - 1][4]
    return lab_.tick(ALL[n - 1][0] + DAY + 60_000)


def test_qualifies_rules():
    cfg = load_settings().strategies
    good = {"return_pct": 26.45, "buy_hold_pct": -32.09, "max_drawdown_pct": 13.9, "trades": 4}
    assert signals.qualifies(good, cfg)[0]
    assert not signals.qualifies({**good, "return_pct": 10, "buy_hold_pct": 40}, cfg)[0]    # lags holding
    assert not signals.qualifies({**good, "return_pct": -3, "buy_hold_pct": -30}, cfg)[0]   # loses money
    assert not signals.qualifies({**good, "max_drawdown_pct": 40}, cfg)[0]
    assert not signals.qualifies({**good, "trades": 1}, cfg)[0]
    assert not signals.qualifies({}, cfg)[0]


def test_fresh_buy_signal_becomes_one_detailed_email(lab):
    L, s, store, m = lab
    step_to(L, m, 78)
    assert store.alerts() == []                                    # flat, nothing to say
    out = step_to(L, m, 79)
    assert [t["side"] for t in out["trades"]] == ["buy"] and not out["errors"]
    a = store.alerts()[0]
    assert a["key"].startswith("signal:sma_cross:ETH/USD:buy:")
    assert a["title"] == "【买入信号】均线交叉（趋势） ETH/USD @ $79.00"
    d = a["detail"]
    for part in ("一、信号分析", "5 日均线", "上穿 20 日均线", "二、回测依据", "vs 持有",
                 "三、建议操作", "四、信号时效", "下一根 K 线收盘", "不高于信号价 3%", "$81.37"):
        assert part in d, part
    assert "状态同步" not in d
    assert L.status()["rows"][0]["signal"]["on"]

    sent = []
    s.email.enabled, s.email.username, s.email.to_addr = True, "me@example.com", "me@example.com"
    s.secrets.smtp_password = "x"
    n = EmailNotifier(s, store, sender=lambda cfg, pw, subj, body: sent.append((subj, body)))
    assert n.run()["sent"] == 1
    assert sent[0][0] == "[ArbDesk] 【买入信号】均线交叉（趋势） ETH/USD @ $79.00"
    assert "四、信号时效" in sent[0][1]
    assert n.run() == {"sent": 0}                                  # not sent twice


def test_signal_skipped_when_backtest_does_not_qualify(lab):
    L, s, store, m = lab
    s.strategies.signal_min_excess_pct = 1000                      # nothing beats holding by that much
    step_to(L, m, 78)
    out = step_to(L, m, 79)
    assert out["trades"] and store.alerts() == []
    row = L.status()["rows"][0]
    assert not row["signal"]["on"] and "没有跑赢" in row["signal"]["why"]


def test_startup_sync_is_flagged(lab):
    L, s, store, m = lab
    step_to(L, m, 82)                                              # starts 3 days after the cross: syncs into it
    d = store.alerts()[0]["detail"]
    assert "状态同步" in d and "不是刚发生的信号" in d


def test_intraday_strategies_never_signal(lab):
    L, s, store, m = lab
    assert L.signal_eligible("orb", "BTC/USD", {"return_pct": 50, "buy_hold_pct": 0, "max_drawdown_pct": 5,
                                                "trades": 10}) == (False, "这个策略不发信号提醒（太快，手动跟不上）")
    assert L.signal_eligible("custom1", "BTC/USD", {"return_pct": 50, "buy_hold_pct": 0, "max_drawdown_pct": 5,
                                                    "trades": 10})[0]                 # custom1 runs on trend_fast


def test_realtime_stop_signal(tmp_path):
    from test_trendfast import bars, uptrend

    (tmp_path / "config.toml").write_text(f'data_dir = "{(tmp_path / "data").as_posix()}"\n')
    s = load_settings(tmp_path / "config.toml", tmp_path / ".env")
    s.strategies.enabled = ["trend_fast"]
    s.strategies.symbols = ["BTC/USD"]
    s.strategies.signal_min_excess_pct = -1000                     # let this synthetic backtest qualify
    s.strategies.signal_max_drawdown_pct = 100
    s.strategies.signal_min_trades = 1
    s.email.timezone = "UTC"
    for k in ("custom1", "vwap_revert", "orb", "momentum_vol"):
        s.strategies.params[k]["enabled"] = False
    store = Store(s.data_dir / "t.db")
    up = bars(uptrend(300))
    m = FakeMarket({"6h": up}, price=up[-1][4])
    L = S.StrategyLab(s, store, market=m)
    now = up[-1][0] + 6 * 3600 * 1000
    L.tick(now)
    buy = store.alerts()[0]
    assert "【买入信号】快速趋势+风控" in buy["title"] and "止损价" in buy["detail"] and "EMA8" in buy["detail"]
    m.px = store.strat_state("trend_fast", "BTC/USD")["stop"] * 0.97
    L.tick(now + 60_000)
    stop = store.alerts()[0]
    assert stop["title"].startswith("【止损卖出】") and "立即执行" in stop["detail"]
