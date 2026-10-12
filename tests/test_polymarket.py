import json
from datetime import datetime, timedelta, timezone

import pytest

from arbdesk import polymarket as pm
from arbdesk import risk
from arbdesk.config import load_settings
from arbdesk.executor import Executor
from arbdesk.service import Desk
from arbdesk.storage import Store


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "config.toml").write_text(f'data_dir = "{tmp_path / "data"}"\n')
    return load_settings(tmp_path / "config.toml", tmp_path / ".env")


@pytest.fixture
def store(settings):
    return Store(settings.data_dir / "t.db")


def raw_market(mid, title, bid, ask, end_days=10, fees=True):
    end = (datetime.now(timezone.utc) + timedelta(days=end_days)).isoformat().replace("+00:00", "Z")
    return {"id": mid, "question": f"Will {title} win?", "groupItemTitle": title,
            "outcomes": '["Yes", "No"]', "outcomePrices": json.dumps([str(ask), str(1 - ask)]),
            "clobTokenIds": json.dumps([f"{mid}-yes", f"{mid}-no"]),
            "bestBid": bid, "bestAsk": ask, "active": True, "closed": False,
            "liquidityNum": 50000, "volume24hr": 1000, "endDate": end, "feesEnabled": fees,
            "orderMinSize": 5}


def event(markets, neg_risk=True, augmented=False):
    return pm.parse_event({"id": "e1", "title": "Who wins?", "slug": "who-wins", "negRisk": neg_risk,
                           "negRiskAugmented": augmented, "markets": markets})


def book(bids, asks):
    return {"bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}


def test_parse_double_encoded_fields():
    m = pm.parse_market(raw_market("1", "A", 0.4, 0.42))
    assert m["yes_token"] == "1-yes" and m["no_token"] == "1-no"
    assert m["yes_price"] == 0.42 and m["fees_enabled"] is True


def test_fee_curve():
    assert pm.taker_fee_per_share(0.5, 0.07) == pytest.approx(0.0175)
    assert pm.taker_fee_per_share(0.99, 0.07) < 0.001
    assert pm.taker_fee_per_share(0.5, 0.07, enabled=False) == 0


def test_screen_negrisk():
    ev = event([raw_market("1", "A", 0.28, 0.30), raw_market("2", "B", 0.28, 0.30),
                raw_market("3", "C", 0.33, 0.35)])
    assert pm.screen_negrisk(ev) == {"direction": "buy_yes", "sum": pytest.approx(0.95)}
    assert pm.screen_negrisk(event(ev_raw := [raw_market("1", "A", 0.5, 0.6), raw_market("2", "B", 0.4, 0.5)])) is None
    assert pm.screen_negrisk(event(ev_raw, neg_risk=False)) is None
    # augmented negRisk events are not guaranteed exhaustive
    assert pm.screen_negrisk(event([raw_market("1", "A", 0.2, 0.3), raw_market("2", "B", 0.2, 0.3)],
                                   augmented=True)) is None
    hi = event([raw_market("1", "A", 0.55, 0.57), raw_market("2", "B", 0.50, 0.52)])
    assert pm.screen_negrisk(hi)["direction"] == "buy_no"


def test_walk_bundle_respects_depth_and_fees():
    ladders = [[(0.30, 10), (0.40, 100)], [(0.30, 100)], [(0.30, 100)]]
    res = pm.walk_bundle(ladders, payout=1.0, fee_rate=0.0, fees_enabled=[True] * 3,
                         budget_usd=1000, min_margin=0.0)
    # Only 10 shares at 0.90/bundle; next level costs 1.00 -> no profit.
    assert res["shares"] == pytest.approx(10)
    assert res["profit"] == pytest.approx(1.0)
    # With fees the 0.90 bundle is still profitable: 3 * 0.07*0.3*0.7 = 0.0441
    res = pm.walk_bundle(ladders, 1.0, 0.07, [True] * 3, 1000, 0.0)
    assert res["profit"] == pytest.approx(10 * (1 - 0.9 - 0.0441))
    # Budget cap
    res = pm.walk_bundle(ladders, 1.0, 0.0, [True] * 3, budget_usd=4.5, min_margin=0.0)
    assert res["shares"] == pytest.approx(5)


def test_negrisk_buy_yes_opportunity(settings):
    ev = event([raw_market("1", "A", 0.28, 0.30), raw_market("2", "B", 0.28, 0.30),
                raw_market("3", "C", 0.28, 0.30)])
    books = {"1-yes": book([(0.28, 50)], [(0.30, 50)]),
             "2-yes": book([(0.28, 50)], [(0.30, 50)]),
             "3-yes": book([(0.28, 50)], [(0.30, 50)])}
    o = pm.negrisk_opportunity(ev, books, "buy_yes", settings.polymarket)
    assert o and o["direction"] == "buy_yes" and o["profit_usd"] > 0
    assert [l["token_id"] for l in o["legs"]] == ["1-yes", "2-yes", "3-yes"]
    assert o["payout_usd"] == pytest.approx(o["shares"])
    assert o["cost_usd"] == pytest.approx(o["shares"] * (0.9 + 3 * 0.07 * 0.3 * 0.7), rel=1e-3)
    # Sum of asks 0.95 < 1, but taker fees make the bundle unprofitable
    books["3-yes"] = book([(0.33, 50)], [(0.35, 50)])
    assert pm.negrisk_opportunity(ev, books, "buy_yes", settings.polymarket) is None


def test_negrisk_buy_no_opportunity(settings):
    ev = event([raw_market("1", "A", 0.60, 0.62), raw_market("2", "B", 0.50, 0.52)])
    books = {"1-yes": book([(0.60, 50)], [(0.62, 50)]), "2-yes": book([(0.50, 50)], [(0.52, 50)])}
    o = pm.negrisk_opportunity(ev, books, "buy_no", settings.polymarket)
    # NO prices: 0.40 + 0.50 = 0.90 for payout 1 (one of two NOs wins)
    assert o and o["payout_usd"] == pytest.approx(o["shares"] * 1.0)
    assert {l["outcome"] for l in o["legs"]} == {"NO"}
    assert [l["token_id"] for l in o["legs"]] == ["1-no", "2-no"]


def test_forecast_candidates_filter():
    evs = [event([raw_market("1", "A", 0.4, 0.42, end_days=10),
                  raw_market("2", "B", 0.4, 0.42, end_days=0.01),   # too soon
                  raw_market("3", "C", 0.97, 0.98, end_days=10)])]  # too extreme
    cands = pm.pick_forecast_candidates(evs, pm.PolymarketConfig())
    assert [c["id"] for c in cands] == ["1"]


def test_resolution():
    assert pm.resolution({"closed": False}) is None
    assert pm.resolution({"closed": True, "outcomePrices": '["1", "0"]'}) == {"yes": 1}
    assert pm.resolution({"closed": True, "outcomePrices": '["0", "1"]'}) == {"yes": 0}
    assert pm.resolution({"closed": True, "outcomePrices": '["0.5", "0.5"]'}) is None


def pm_leg(**kw):
    leg = {"exchange": "polymarket", "market_id": "1", "token_id": "1-yes", "outcome": "YES",
           "label": "A", "side": "buy", "shares": 10, "limit_price": 0.4}
    leg.update(kw)
    return leg


def test_risk_polymarket(settings, store):
    assert risk.validate({"legs": [pm_leg()]}, settings, store)[1] == []
    assert risk.validate({"legs": [pm_leg(limit_price=1.2)]}, settings, store)[1]
    assert risk.validate({"legs": [pm_leg(shares=1000)]}, settings, store)[1]   # > max_usd_per_market
    assert risk.validate({"legs": [pm_leg(side="sell")]}, settings, store)[1]
    mixed = {"legs": [pm_leg(), {"exchange": "okx", "symbol": "BTC/USDT", "side": "buy", "usd": 5}]}
    assert risk.validate(mixed, settings, store)[1]
    # Live mode for crypto does not need keys for paper-only Polymarket
    settings.risk.mode = "live"
    assert risk.validate({"legs": [pm_leg()]}, settings, store)[1] == []


class FakePM:
    def __init__(self, books, markets=None):
        self.books, self.markets = books, markets or {}

    def book(self, token_id):
        return self.books[token_id]

    def market(self, mid):
        return self.markets[mid]


def test_paper_execution_and_settlement(settings, store):
    ex = Executor(settings, store)
    ex.pm = FakePM({"1-yes": book([], [(0.30, 5), (0.32, 100)]), "2-yes": book([], [(0.60, 100)])})
    pid = store.add_proposal(None, {"title": "bundle", "legs": [
        pm_leg(shares=10, limit_price=0.31),
        pm_leg(market_id="2", token_id="2-yes", label="B", shares=10, limit_price=0.60)]})
    r = ex.execute(pid)
    assert r["ok"], r
    trades = store.trades()
    assert len(trades) == 2 and all(t["mode"] == "paper" and t["pnl_usd"] is None for t in trades)
    leg1 = next(t for t in trades if json.loads(t["raw"])["market_id"] == "1")
    assert leg1["price"] == pytest.approx(0.31)  # 5 @ 0.30 + 5 @ 0.32

    desk = Desk(settings, store)
    client = FakePM({}, {"1": {"closed": True, "outcomePrices": '["1", "0"]'},
                         "2": {"closed": True, "outcomePrices": '["0", "1"]'}})
    assert desk.settle_polymarket(client) == 2
    pnl = {json.loads(t["raw"])["market_id"]: t["pnl_usd"] for t in store.trades()}
    assert pnl["1"] == pytest.approx(10 - 3.1 - leg1["fee_usd"])
    assert pnl["2"] < 0


def test_paper_execution_rejects_price_move(settings, store):
    ex = Executor(settings, store)
    ex.pm = FakePM({"1-yes": book([], [(0.50, 100)])})
    pid = store.add_proposal(None, {"title": "x", "legs": [pm_leg(limit_price=0.40)]})
    r = ex.execute(pid)
    assert not r["ok"] and store.trades() == []
    assert store.proposal(pid)["status"] == "failed"


def test_forecast_scores(store):
    store.add_forecast(market_id="1", question="q", end_date=None, market_prob=0.6, ai_prob=0.8,
                       confidence=0.7, rationale="")
    store.resolve_forecasts("1", 1)
    s = store.forecast_scores()
    assert s["resolved"] == 1 and s["ai_brier"] == pytest.approx(0.04) and s["market_brier"] == pytest.approx(0.16)


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE scans (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
                 "summary TEXT NOT NULL, opportunities TEXT NOT NULL)")
    conn.commit(); conn.close()
    st = Store(path)
    st.add_scan({}, [], "polymarket")
    assert st.scans(source="polymarket")[0]["source"] == "polymarket"


def test_polymarket_cycle_end_to_end(settings, store, monkeypatch):
    ev = event([raw_market("1", "A", 0.28, 0.30), raw_market("2", "B", 0.28, 0.30),
                raw_market("3", "C", 0.28, 0.30)])
    client = FakePM({f"{i}-yes": book([(0.28, 50)], [(0.30, 50)]) for i in "123"})
    client.events = lambda limit: [ev]

    class StubAnalyst:
        available, provider, model = True, "stub", "stub"

    monkeypatch.setattr("arbdesk.service.forecast_markets", lambda analyst, markets: [
        {"market_id": "1", "prob": 0.60, "confidence": 0.8, "rationale": "test"},   # big YES edge
        {"market_id": "2", "prob": 0.31, "confidence": 0.9, "rationale": "test"},   # no edge
    ])
    desk = Desk(settings, store, analyst=StubAnalyst())
    desk.pm_client = client
    out = desk.polymarket_cycle(use_llm=True)
    assert out["arbitrage"] == 1 and out["forecasts"] == 2
    props = store.proposals()
    titles = [p["payload"]["title"] for p in props]
    assert any("多结果套利" in t for t in titles)
    ai = [p for p in props if p["payload"]["title"].startswith("Polymarket YES")]
    assert len(ai) == 1 and ai[0]["note"] == ""   # passes risk pre-check
    assert len(store.forecasts()) == 2
    assert store.scans(source="polymarket")[0]["opportunities"][0]["type"] == "pm_negrisk"
