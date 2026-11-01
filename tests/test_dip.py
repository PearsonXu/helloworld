import json

import pytest

from arbdesk.config import load_settings
from arbdesk.dip import DipManager
from arbdesk.storage import Store


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "config.toml").write_text(f'data_dir = "{(tmp_path / "data").as_posix()}"\n')
    return load_settings(tmp_path / "config.toml", tmp_path / ".env")


@pytest.fixture
def store(settings):
    return Store(settings.data_dir / "t.db")


class FakeMarket:
    def __init__(self, prices, lows=None, min_cost=1.0):
        self.prices, self.lows, self.min_cost = prices, lows or {}, min_cost
        self.placed, self.cancelled, self.states = [], [], {}

    def price(self, s):
        return self.prices[s]

    def low_since(self, s, since):
        return self.lows.get(s)

    def limits(self, s):
        return {"min_amount": 0, "min_cost": self.min_cost}

    def place(self, s, amount, price):
        oid = f"o{len(self.placed) + 1}"
        self.placed.append((s, amount, price))
        self.states[oid] = {"status": "open", "filled": 0, "average": 0, "fee": 0}
        return oid

    def order(self, oid, s):
        return self.states[oid]

    def cancel(self, oid, s):
        self.cancelled.append(oid)


PRICES = {"BTC/USD": 100_000.0, "ETH/USD": 4_000.0}


def test_paper_auto_arm_places_ladder(settings, store):
    dm = DipManager(settings, store, market=FakeMarket(dict(PRICES)))
    out = dm.cycle()
    assert set(out["rearmed"]) == {"BTC/USD", "ETH/USD"}
    btc = sorted(o["price"] for o in store.dip_orders(status="open", symbol="BTC/USD"))
    assert btc == pytest.approx([45_000, 60_000, 75_000])   # -55%, -40%, -25%
    assert store.dip_open_usd("paper") == 30                 # 6 levels x $5 = max_open_usd
    assert dm.cycle()["rearmed"] == []                      # already armed: nothing to do


def test_paper_fill_from_candle_low(settings, store):
    m = FakeMarket(dict(PRICES))
    dm = DipManager(settings, store, market=m)
    dm.cycle()
    m.lows = {"BTC/USD": 59_000.0}           # a wick through -25% and -40%, not -55%
    m.prices["BTC/USD"] = 80_000.0
    assert dm.cycle()["filled"] == 2
    filled = store.dip_orders(status="filled", symbol="BTC/USD")
    assert sorted(o["fill_price"] for o in filled) == pytest.approx([60_000, 75_000])  # filled at limit, not the low
    trades = store.trades()
    assert len(trades) == 2 and all(json.loads(t["raw"])["kind"] == "dip" for t in trades)
    assert trades[0]["fee_usd"] == pytest.approx(5 * 0.006)
    assert len(store.dip_orders(status="open", symbol="BTC/USD")) == 1
    assert any("抄底挂单成交" in a["title"] for a in store.alerts())
    st = dm.status()["symbols"][0]
    assert st["position_cost"] == pytest.approx(10) and st["unrealized"] is not None


def test_ladder_moves_up_never_down(settings, store):
    m = FakeMarket(dict(PRICES))
    dm = DipManager(settings, store, market=m)
    dm.cycle()
    m.prices["BTC/USD"] = 80_000.0            # falls: ladder stays where it was
    dm.cycle()
    assert max(o["anchor"] for o in store.dip_orders(status="open", symbol="BTC/USD")) == 100_000
    m.prices["BTC/USD"] = 111_000.0           # rises >10% above the anchor: ladder follows
    assert "BTC/USD" in dm.cycle()["rearmed"]
    open_ = store.dip_orders(status="open", symbol="BTC/USD")
    assert len(open_) == 3 and {o["anchor"] for o in open_} == {111_000}
    assert len(store.dip_orders(status="cancelled", symbol="BTC/USD")) == 3   # replaced, not stacked


def test_budget_refusal_keeps_existing_ladder(settings, store):
    m = FakeMarket(dict(PRICES))
    dm = DipManager(settings, store, market=m)
    dm.cycle()
    settings.dip.quarterly_budget_usd = 20    # less than the 30 already resting
    res = dm.arm(symbols=["BTC/USD"])
    assert not res["ok"] and "quarterly_budget" in res["errors"][0]
    assert len(store.dip_orders(status="open", symbol="BTC/USD")) == 3      # untouched


# --------------------------------------------------------------------------
# Live path (fake exchange)
# --------------------------------------------------------------------------

@pytest.fixture
def live(settings, store):
    settings.dip.mode = "live"
    settings.dip.ladders = [{"symbol": "BTC/USD", "levels_pct": [-25, -40], "usd_per_level": 5}]
    pub, trade = FakeMarket(dict(PRICES)), FakeMarket(dict(PRICES))
    return DipManager(settings, store, market=pub, trade_market=trade), trade


def test_live_requires_confirmation_and_never_auto_arms(live, store):
    dm, trade = live
    assert dm.cycle()["rearmed"] == [] and trade.placed == []               # no auto-arm in live
    r = dm.arm()
    assert r["needs_confirm"] and trade.placed == []
    r = dm.arm(confirm_live=True)
    assert r["ok"] and [p[2] for p in trade.placed] == pytest.approx([75_000, 60_000])
    assert {o["exchange_order_id"] for o in store.dip_orders(status="open")} == {"o1", "o2"}


def test_live_refuses_below_exchange_minimum(live, store):
    dm, trade = live
    trade.min_cost = 10.0
    r = dm.arm(confirm_live=True)
    assert not r["ok"] and "minimum" in r["errors"][0] and trade.placed == []


def test_live_fill_detection(live, store):
    dm, trade = live
    dm.arm(confirm_live=True)
    trade.states["o1"] = {"status": "closed", "filled": 0.0000667, "average": 74_990.0, "fee": 0.03}
    assert dm.cycle()["filled"] == 1
    t = store.trades()[0]
    assert t["mode"] == "live" and t["price"] == 74_990.0 and t["fee_usd"] == 0.03 and t["order_id"] == "o1"


def test_manual_halt_cancels_auto_halt_keeps(live, store):
    dm, trade = live
    dm.arm(confirm_live=True)
    store.set_flag("halt", "自动停止：BTC 1 小时暴跌 -13.0%")
    dm.cycle()
    assert trade.cancelled == [] and len(store.dip_orders(status="open")) == 2   # crash: keep catching
    assert not dm.arm(confirm_live=True)["ok"]                                    # but no new arming while halted
    store.set_flag("halt", "manual halt")
    dm.cycle()
    assert sorted(trade.cancelled) == ["o1", "o2"] and store.dip_orders(status="open") == []


def test_live_rise_alerts_instead_of_moving_orders(live, store):
    dm, trade = live
    dm.arm(confirm_live=True)
    dm.market.prices["BTC/USD"] = 115_000.0
    dm.cycle()
    assert len(trade.placed) == 2                                  # no automatic re-pricing of real orders
    assert any(a["key"] == "dip_rearm:BTC/USD" for a in store.alerts())
