import json
from datetime import datetime, timedelta, timezone

import pytest

from arbdesk.config import load_settings
from arbdesk.polymarket import VenueConfig
from arbdesk.service import Desk
from arbdesk.storage import Store


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "config.toml").write_text(f'data_dir = "{(tmp_path / "data").as_posix()}"\n')
    s = load_settings(tmp_path / "config.toml", tmp_path / ".env")
    s.alerts.desktop_notify = False
    return s


class BookVenue:
    def __init__(self, name, asks=None, bids=None):
        self.name, self.label = name, name
        self._asks, self._bids = asks or {}, bids or {}
        self.cfg = VenueConfig(taker_fee_rate=0.0)
        self.last_markets = []

    def asks(self, token):
        return self._asks.get(token, [])

    def bid(self, token):
        return self._bids.get(token)

    def taker_fee(self, price, shares):
        return 0.0


class FakeDipMarket:
    def __init__(self, prices):
        self.prices = prices

    def price(self, s):
        return self.prices[s]

    def low_since(self, s, since):
        return None


def make_desk(settings, store, venue):
    desk = Desk(settings, store)
    desk.venues = {"kalshi": venue}
    desk.executor._venues = {"kalshi": venue}
    desk.dip._market = FakeDipMarket({"BTC/USD": 100_000.0, "ETH/USD": 4_000.0})
    return desk


def leg(token, shares=10, price=0.30):
    return {"exchange": "kalshi", "market_id": token.split("|")[0], "token_id": token, "outcome": "YES",
            "label": token, "side": "buy", "shares": shares, "limit_price": price}


def test_session_books_cash_marks_at_bid_and_attributes(settings):
    store = Store(settings.data_dir / "t.db")
    v = BookVenue("kalshi", asks={"A|yes": [(0.30, 100)]}, bids={"A|yes": 0.27})
    desk = make_desk(settings, store, v)
    store.add_proposal(None, {"title": "old", "legs": [leg("A|yes")]})          # before the session
    settings.dip.enabled = False
    s = desk.sim.start(capital=100, hours=24)
    pid = store.add_proposal(None, {"title": "f", "legs": [leg("A|yes")]}, dedupe_key="kalshi:forecast:A:YES")
    out = desk.sim_step()
    assert out["executed"] == [pid]                                              # old one not auto-executed
    snap = desk.sim.snapshot(record=False)
    assert snap["cash"] == pytest.approx(97.0)                                   # 10 x 0.30
    assert snap["positions_value"] == pytest.approx(2.7)                         # marked at the 0.27 bid
    assert snap["equity"] == pytest.approx(99.7) and snap["unrealized"] == pytest.approx(-0.3)
    (strat,) = snap["by_strategy"]
    assert strat["label"] == "AI 概率判断" and strat["trades"] == 1

    # settlement: the YES pays $1 each -> cash comes back, position closes
    t = store.trades()[0]
    store.settle_trade(t["id"], 10 * 1.0 - 3.0)
    snap = desk.sim.snapshot(record=False)
    assert snap["cash"] == pytest.approx(107.0) and snap["positions"] == [] and snap["realized"] == pytest.approx(7.0)


def test_cash_limit_refuses_and_dip_orders_reserve_cash(settings):
    store = Store(settings.data_dir / "t.db")
    v = BookVenue("kalshi", asks={"A|yes": [(0.50, 1000)]})
    desk = make_desk(settings, store, v)
    desk.sim.start(capital=50, hours=24)
    desk.dip.cycle()                                                             # paper ladder: 6 x $5 reserved
    snap = desk.sim.snapshot(record=False)
    assert snap["reserved"] == pytest.approx(30) and snap["cash"] == pytest.approx(50)
    pid = store.add_proposal(None, {"title": "fits", "legs": [leg("A|yes", shares=40, price=0.50)]})  # $20 = available
    store.add_proposal(None, {"title": "no cash left", "legs": [leg("B|yes", shares=20, price=0.50)]})  # $10
    v._asks["B|yes"] = [(0.50, 1000)]
    out = desk.sim_step()
    assert out["executed"] == [pid] and len(out["refused"]) == 1                 # $20 fits, then nothing is left
    refused = store.proposal(out["refused"][0])
    assert refused["status"] == "failed" and "可用资金不足" in refused["note"]


def test_auto_execution_skips_live_noted_and_repeats(settings):
    store = Store(settings.data_dir / "t.db")
    v = BookVenue("kalshi", asks={"A|yes": [(0.30, 100)]})
    desk = make_desk(settings, store, v)
    settings.dip.enabled = False
    desk.sim.start(capital=100, hours=24)
    store.add_proposal(None, {"title": "blocked", "legs": [leg("A|yes")]}, note="daily loss limit hit")
    first = store.add_proposal(None, {"title": "a", "legs": [leg("A|yes")]}, dedupe_key="k1")
    assert desk.sim_step()["executed"] == [first]
    again = store.add_proposal(None, {"title": "a again", "legs": [leg("A|yes")]}, dedupe_key="k1")
    assert desk.sim_step()["executed"] == []                                     # one position per opportunity
    assert store.proposal(again)["status"] == "pending"
    settings.kalshi.mode = "live"
    store.add_proposal(None, {"title": "live", "legs": [leg("A|yes")]}, dedupe_key="k2")
    assert desk.sim_step()["executed"] == []                                     # real money: never automatic


def test_session_ends_with_report_and_alert(settings):
    store = Store(settings.data_dir / "t.db")
    desk = make_desk(settings, store, BookVenue("kalshi"))
    settings.dip.enabled = False
    s = desk.sim.start(capital=150, hours=1)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    store.conn.execute("UPDATE sim_sessions SET end_ts = ? WHERE id = ?", (past, s["id"]))
    out = desk.sim_step()
    assert out["finished"] and desk.sim.current() is None
    rep = desk.sim.report(s["id"])
    assert rep["equity"] == pytest.approx(150) and rep["reason"] == "time up"
    assert any("模拟结束" in a["title"] for a in store.alerts())
    with pytest.raises(ValueError):
        desk.sim.start(capital=0)
    desk.sim.start(capital=10, hours=1)
    with pytest.raises(ValueError):                                              # only one at a time
        desk.sim.start(capital=10, hours=1)


def test_no_session_means_no_cash_limit(settings):
    store = Store(settings.data_dir / "t.db")
    v = BookVenue("kalshi", asks={"A|yes": [(0.30, 100)]})
    desk = make_desk(settings, store, v)
    assert desk.sim.available() is None
    pid = store.add_proposal(None, {"title": "x", "legs": [leg("A|yes")]})
    assert desk.executor.execute(pid)["ok"]
