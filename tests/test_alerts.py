from datetime import datetime, timedelta, timezone

import pytest

from arbdesk import alerts
from arbdesk.alerts import AlertMonitor, OutageTracker
from arbdesk.config import AlertConfig, load_settings
from arbdesk.deribit import Smile
from arbdesk.storage import Store

CFG = AlertConfig()


def test_price_shock_levels():
    assert alerts.price_alerts("BTC", 100, 102, 101, CFG) == []
    (a,) = alerts.price_alerts("BTC", 94, 100, None, CFG)
    assert a.level == "warning" and a.key == "move1h:BTC:down" and "-6.0%" in a.title
    (a,) = alerts.price_alerts("BTC", 87, 100, None, CFG)
    assert a.level == "critical"
    (a,) = alerts.price_alerts("ETH", 115, None, 100, CFG)
    assert a.key == "move24h:ETH:up"


def test_vol_funding_depeg():
    assert [a.key for a in alerts.vol_alerts("BTC", 1.05, 0.80, CFG)] == ["ivhigh:BTC", "ivjump:BTC"]
    assert alerts.vol_alerts("BTC", 0.55, 0.50, CFG) == []
    assert alerts.funding_alerts("BTC", 0.0002, CFG) == []
    (a,) = alerts.funding_alerts("BTC", -0.002, CFG)
    assert a.key == "funding:BTC:neg"
    assert alerts.depeg_alerts("USDT/USD", 0.998, CFG) == []
    assert alerts.depeg_alerts("USDC/USD", 0.993, CFG)[0].level == "warning"
    assert alerts.depeg_alerts("USDC/USD", 0.97, CFG)[0].level == "critical"


def test_outage_only_after_working_and_once():
    t = OutageTracker(3)
    for _ in range(5):  # never worked (e.g. geo-blocked exchange): no alert
        assert t.update(set(), {"binance:spot"}) == []
    t.update({"kraken:spot"}, set())
    assert t.update(set(), {"kraken:spot"}) == [] and t.update(set(), {"kraken:spot"}) == []
    (a,) = t.update(set(), {"kraken:spot"})
    assert a.key == "outage:kraken:spot"
    assert t.update(set(), {"kraken:spot"}) == []      # once per outage
    t.update({"kraken:spot"}, set())                    # recovers...
    for _ in range(2):
        t.update(set(), {"kraken:spot"})
    assert t.update(set(), {"kraken:spot"})             # ...and a new outage alerts again


def test_atm_vol_skips_too_near_expiry():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    ks = [80000.0, 100000.0, 120000.0]
    near = Smile(now + timedelta(days=1), 100000.0, ks, [2.0, 2.0, 2.0])
    nxt = Smile(now + timedelta(days=7), 100000.0, ks, [0.7, 0.6, 0.65])
    assert alerts.atm_vol([near, nxt], now) == pytest.approx(0.6)


# --------------------------------------------------------------------------
# Monitor: samples, cooldown, kill switch
# --------------------------------------------------------------------------

@pytest.fixture
def settings(tmp_path):
    (tmp_path / "config.toml").write_text(f'data_dir = "{(tmp_path / "data").as_posix()}"\n')
    s = load_settings(tmp_path / "config.toml", tmp_path / ".env")
    s.alerts.desktop_notify = False
    return s


class Feeds:
    def __init__(self, prices, funding=None, stables=None):
        self.prices, self.funding, self.stables = prices, funding or {}, stables or {}

    def index_price(self, ccy):
        return self.prices[ccy]

    def funding_8h(self, ccy):
        return self.funding.get(ccy)

    def stable_prices(self, ex, syms):
        return self.stables


def backdate(store, key, value, hours):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    store.conn.execute("INSERT INTO samples (ts, key, value) VALUES (?, ?, ?)", (ts, key, value))


def test_monitor_cooldown_and_escalation(settings):
    store = Store(settings.data_dir / "t.db")
    backdate(store, "price:BTC", 100000, 1)
    backdate(store, "price:ETH", 4000, 1)
    m = AlertMonitor(settings, store, Feeds({"BTC": 93000, "ETH": 4000}))
    fired = m.check()
    assert [a["key"] for a in fired] == ["move1h:BTC:down"] and fired[0]["level"] == "warning"
    assert m.check() == []                               # same condition: cooldown
    m.feeds.prices["BTC"] = 86000                        # escalates to critical: fires again
    assert [a["level"] for a in m.check()] == ["critical"]
    assert len(store.alerts(unacked_only=True)) == 2
    store.ack_alerts()
    assert store.alerts(unacked_only=True) == []


def test_critical_alert_halts_only_when_live(settings):
    store = Store(settings.data_dir / "t.db")
    m = AlertMonitor(settings, store, Feeds({"BTC": 100000, "ETH": 4000}, stables={"USDC/USD": 0.95}))
    m.check()
    assert store.halted() is None                        # everything in paper mode: no halt
    settings.kalshi.mode = "live"
    store.conn.execute("DELETE FROM alerts")
    m.check()
    assert "USDC/USD" in store.halted()


def test_feed_failure_is_contained(settings):
    store = Store(settings.data_dir / "t.db")

    class Broken(Feeds):
        def index_price(self, ccy):
            raise ConnectionError("down")

    m = AlertMonitor(settings, store, Broken({}))
    assert m.check() == []                               # no crash, no false alerts
