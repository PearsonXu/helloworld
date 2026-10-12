import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from fastapi.testclient import TestClient

from arbdesk import kalshi, pmus, risk
from arbdesk.config import load_settings
from arbdesk.executor import Executor
from arbdesk.pm_live import LiveTradingError
from arbdesk.polymarket import VenueConfig
from arbdesk.service import Desk
from arbdesk.storage import Store
from arbdesk.venues import KalshiVenue, PolymarketUSVenue
from arbdesk.web import create_app


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "config.toml").write_text(f'data_dir = "{(tmp_path / "data").as_posix()}"\n')
    return load_settings(tmp_path / "config.toml", tmp_path / ".env")


@pytest.fixture
def store(settings):
    return Store(settings.data_dir / "t.db")


def pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


# --------------------------------------------------------------------------
# Kalshi
# --------------------------------------------------------------------------

def k_market(ticker, yes_bid, yes_ask, status="active", **kw):
    return {"ticker": ticker, "event_ticker": "EV", "title": f"Will {ticker}?", "yes_sub_title": ticker,
            "status": status, "yes_bid_dollars": str(yes_bid), "yes_ask_dollars": str(yes_ask),
            "yes_bid_size_fp": "10.00", "yes_ask_size_fp": "10.00", "open_interest_fp": "5000.00",
            "volume_24h_fp": "800.00", "close_time": "2099-01-01T00:00:00Z", "rules_primary": "rules", **kw}


def k_event(markets, mutually_exclusive=True):
    return kalshi.parse_event({"event_ticker": "EV", "series_ticker": "SER", "title": "Who wins?",
                               "mutually_exclusive": mutually_exclusive, "markets": markets})


def k_book(yes_bids, no_bids):
    return {"orderbook_fp": {"yes_dollars": [[str(p), str(s)] for p, s in yes_bids],
                             "no_dollars": [[str(p), str(s)] for p, s in no_bids]}}


def test_kalshi_fee_rounds_up_per_order():
    assert kalshi.kalshi_fee(0.5, 10, 0.07) == 0.18     # 0.175 -> 0.18
    assert kalshi.kalshi_fee(0.5, 1, 0.07) == 0.02      # 0.0175 -> 0.02
    assert kalshi.kalshi_fee(0.99, 1, 0.07) == 0.01


def test_kalshi_ladders_mirror_book():
    lad = kalshi.ladders(k_book(yes_bids=[(0.40, 10), (0.42, 5)], no_bids=[(0.55, 20)]))
    assert lad["yes"] == [(0.45, 20.0)]
    assert lad["no"] == [(0.58, 5.0), (0.60, 10.0)]


def test_kalshi_no_bundle_arbitrage():
    cfg = VenueConfig(environment="demo")
    ev = k_event([k_market("A", 0.40, 0.45), k_market("B", 0.35, 0.40), k_market("C", 0.35, 0.40)])
    assert kalshi.screen(ev)                      # YES bids sum to 1.10 > 1
    books = {"A": k_book([(0.40, 10)], []), "B": k_book([(0.35, 10)], []), "C": k_book([(0.35, 10)], [])}
    o = kalshi.no_bundle_opportunity(ev, books, cfg)
    assert o and o["direction"] == "buy_no" and o["shares"] == 10
    assert {l["outcome"] for l in o["legs"]} == {"NO"}
    assert [l["token_id"] for l in o["legs"]] == ["A|no", "B|no", "C|no"]
    fees = 0.07 * (0.6 * 0.4 + 0.65 * 0.35 * 2)
    assert o["cost_usd"] == pytest.approx(10 * (1.9 + fees) + 0.03, abs=1e-3)
    assert o["payout_usd"] == 20


def test_kalshi_never_bundles_yes_or_non_exclusive():
    ev = k_event([k_market("A", 0.20, 0.25), k_market("B", 0.20, 0.25)])  # YES asks sum 0.5: not proposed
    assert not kalshi.screen(ev)
    ev = k_event([k_market("A", 0.60, 0.62), k_market("B", 0.50, 0.52)], mutually_exclusive=False)
    assert not kalshi.screen(ev)


def test_kalshi_resolution():
    assert kalshi.resolution({"market": {"status": "active", "result": ""}}) is None
    assert kalshi.resolution({"market": {"status": "finalized", "result": "yes"}}) == {"yes": 1}
    assert kalshi.resolution({"market": {"status": "settled", "result": "no"}}) == {"yes": 0}
    assert kalshi.resolution({"market": {"status": "settled", "result": "void"}}) is None


@pytest.mark.parametrize("keytype", ["rsa", "ed25519"])
def test_kalshi_request_signature(keytype):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048) if keytype == "rsa" \
        else ed25519.Ed25519PrivateKey.generate()
    c = kalshi.KalshiClient(kalshi.HOSTS["demo"], key_id="kid", private_key_pem=pem(key))
    h = c.auth_headers("GET", "/portfolio/balance")
    msg = f"{h['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/portfolio/balance".encode()
    sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
    if keytype == "rsa":
        key.public_key().verify(sig, msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                     salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    else:
        key.public_key().verify(sig, msg)
    assert h["KALSHI-ACCESS-KEY"] == "kid"


class FakeKalshiClient:
    base = kalshi.HOSTS["demo"]

    def __init__(self, books=None, markets=None, fill=None, balance=100.0):
        self.books, self.markets, self.fill, self.bal, self.orders = books or {}, markets or {}, fill, balance, []

    def orderbook(self, ticker):
        return self.books[ticker]

    def market(self, ticker):
        return self.markets[ticker]

    def exchange_status(self):
        return {"trading_active": True}

    def balance(self):
        return self.bal

    def create_order(self, body):
        self.orders.append(body)
        return self.fill(body) if self.fill else {}


def test_kalshi_trader_order_shape():
    cfg = VenueConfig(environment="demo")

    def fill(body):
        return {"order_id": "o1", "fill_count": body["count"], "average_fill_price": "0.39",
                "average_fee_paid": "0.017", "ts_ms": 1}

    c = FakeKalshiClient(fill=fill)
    t = kalshi.KalshiTrader(c, cfg)
    f = t.buy_fok("A|no", 10, 0.60)
    body = c.orders[0]
    # Buying NO at <= 0.61 == selling YES (ask) at >= 0.39
    assert body["side"] == "ask" and body["price"] == "0.3900" and body["count"] == "10.00"
    assert body["time_in_force"] == "fill_or_kill"
    assert f.avg_price == pytest.approx(0.61) and f.raw["fee_usd"] == pytest.approx(0.17)

    t.buy_fok("A|yes", 5, 0.30)
    assert c.orders[1]["side"] == "bid" and c.orders[1]["price"] == "0.3100"

    c.fill = lambda body: {"order_id": "o2", "fill_count": "3.00", "average_fill_price": "0.31"}
    with pytest.raises(LiveTradingError):
        t.buy_fok("A|yes", 5, 0.30)


def test_kalshi_paper_execution_and_settlement(settings, store):
    v = KalshiVenue(settings.kalshi, settings, client=FakeKalshiClient(
        books={"A": k_book(yes_bids=[(0.40, 50)], no_bids=[(0.68, 50)])},
        markets={"A": {"market": {"status": "finalized", "result": "yes"}}}))
    ex = Executor(settings, store)
    ex._venues["kalshi"] = v
    pid = store.add_proposal(None, {"title": "k", "legs": [
        {"exchange": "kalshi", "market_id": "A", "token_id": "A|yes", "outcome": "YES", "label": "A",
         "shares": 10, "limit_price": 0.32}]})
    r = ex.execute(pid)
    assert r["ok"]
    t = store.trades()[0]
    assert t["exchange"] == "kalshi" and t["price"] == pytest.approx(0.32)
    assert t["fee_usd"] == pytest.approx(kalshi.kalshi_fee(0.32, 10, 0.07))
    desk = Desk(settings, store)
    assert desk.settle(v) == 1
    assert store.trades()[0]["pnl_usd"] == pytest.approx(10 - 3.2 - t["fee_usd"])


def test_kalshi_live_uses_demo_by_default(settings, store):
    assert settings.kalshi.environment == "demo"
    settings.kalshi.mode = "live"
    v = KalshiVenue(settings.kalshi, settings)
    assert "demo" in v.trade_client.base and v.client is v.trade_client


# --------------------------------------------------------------------------
# Polymarket US
# --------------------------------------------------------------------------

def amt(v):
    return {"value": str(v), "currency": "USD"}


def test_pmus_ladders_and_parse():
    book = {"marketData": {"bids": [{"px": amt(0.40), "qty": "100"}],
                           "offers": [{"px": amt(0.45), "qty": "50"}, {"px": amt(0.44), "qty": "10"}]}}
    lad = pmus.ladders(book)
    assert lad["yes"] == [(0.44, 10.0), (0.45, 50.0)]
    assert lad["no"] == [(0.6, 100.0)]
    m = pmus.parse_market({"slug": "lal-win", "title": "Lakers", "liquidity": 9000, "volume": 100},
                          {"title": "Lakers vs Celtics", "endTime": "2099-01-01T00:00:00Z"},
                          {"marketData": {"bestBid": amt(0.40), "bestAsk": amt(0.44)}})
    assert m["yes_token"] == "lal-win|yes" and m["best_ask"] == 0.44 and "Lakers vs Celtics" in m["question"]


def test_pmus_resolution():
    assert pmus.resolution({"market": {"closed": False}}, {"settlement": 1}) is None
    assert pmus.resolution({"market": {"closed": True}}, {"settlement": 1.0}) == {"yes": 1}
    assert pmus.resolution({"market": {"closed": True}}, {"settlement": 0}) == {"yes": 0}
    assert pmus.resolution({"market": {"closed": True}}, None) is None


class FakePMUSClient:
    def __init__(self):
        self.events_data = [{"slug": "ev", "title": "Game", "endTime": "2099-01-01T00:00:00Z",
                             "markets": [{"slug": "a", "title": "A", "liquidity": 9000, "volume": 50},
                                         {"slug": "b", "title": "B", "liquidity": 10, "volume": 900}]}]

    def events(self, limit):
        return self.events_data

    def bbo(self, slug):
        return {"marketData": {"bestBid": amt(0.40), "bestAsk": amt(0.42)}}


def test_pmus_scan_candidates(settings):
    settings.polymarket_us.ai_horizon_days = [1, 100000]
    opps, cands, summary = pmus.scan(FakePMUSClient(), settings.polymarket_us)
    assert opps == [] and [c["id"] for c in cands] == ["a"]  # "b" is below min liquidity


class FakeSDK:
    def __init__(self, resp, order=None):
        outer = self
        self.created = []

        class Orders:
            def create(self, params):
                outer.created.append(params)
                return resp

            def retrieve(self, oid):
                return {"order": order or {}}

            def cancel_all(self):
                pass

        class Account:
            def balances(self):
                return {"balances": [{"buyingPower": 55.5}]}

        self.orders, self.account = Orders(), Account()


def test_pmus_trader_order_shape(settings):
    sdk = FakeSDK({"id": "x1", "executions": [
        {"lastShares": "6", "lastPx": amt(0.41), "commissionNotionalCollected": amt(0.02)},
        {"lastShares": "4", "lastPx": amt(0.42)}]})
    t = pmus.PMUSTrader("kid", "c2VjcmV0", settings.polymarket_us, client=sdk)
    f = t.buy_fok("lal-win|yes", 10.7, 0.42)
    p = sdk.created[0]
    assert p["intent"] == "ORDER_INTENT_BUY_LONG" and p["tif"] == "TIME_IN_FORCE_FILL_OR_KILL"
    assert p["quantity"] == 10 and p["price"] == {"value": "0.43", "currency": "USD"}
    assert f.shares == 10 and f.cost_usd == pytest.approx(6 * 0.41 + 4 * 0.42)
    assert t.collateral_balance() == 55.5
    with pytest.raises(LiveTradingError):
        t.buy_fok("lal-win|no", 10, 0.5)
    partial = FakeSDK({"id": "x2", "executions": []}, order={"cumQuantity": "3", "avgPx": amt(0.4)})
    with pytest.raises(LiveTradingError):
        pmus.PMUSTrader("kid", "c2VjcmV0", settings.polymarket_us, client=partial).buy_fok("m|yes", 10, 0.4)


def test_pmus_live_no_rejected_by_risk(settings, store):
    settings.polymarket_us.mode = "live"
    settings.secrets.pmus_key_id, settings.secrets.pmus_secret_key = "kid", "secret"
    leg = {"exchange": "polymarket_us", "market_id": "m", "token_id": "m|no", "outcome": "NO",
           "label": "m", "shares": 10, "limit_price": 0.5}
    _, errors = risk.validate({"legs": [leg]}, settings, store)
    assert any("only buy YES" in e for e in errors)
    leg.update(token_id="m|yes", outcome="YES")
    assert risk.validate({"legs": [leg]}, settings, store)[1] == []


def test_cannot_mix_venues(settings, store):
    legs = [{"exchange": "kalshi", "market_id": "A", "token_id": "A|no", "outcome": "NO", "shares": 1, "limit_price": 0.5},
            {"exchange": "polymarket_us", "market_id": "m", "token_id": "m|yes", "outcome": "YES", "shares": 1, "limit_price": 0.5}]
    assert risk.validate({"legs": legs}, settings, store)[1]


def test_vault_only_venue_keys(tmp_path):
    (tmp_path / "config.toml").write_text(f'data_dir = "{(tmp_path / "data").as_posix()}"\n')
    (tmp_path / ".env").write_text("KALSHI_PRIVATE_KEY=x\nPOLYMARKET_US_SECRET_KEY=y\nKALSHI_API_KEY_ID=kid\n")
    s = load_settings(tmp_path / "config.toml", tmp_path / ".env")
    assert s.secrets.kalshi_private_key == "" and s.secrets.pmus_secret_key == ""
    assert s.secrets.kalshi_key_id == "kid"  # ids are not secret
    s = load_settings(tmp_path / "config.toml", tmp_path / ".env",
                      vault_data={"KALSHI_PRIVATE_KEY": "pem-data", "POLYMARKET_US_SECRET_KEY": "b64"})
    assert s.secrets.kalshi_private_key == "pem-data" and s.secrets.pmus_secret_key == "b64"


def test_panel_lists_venues(settings, store):
    desk = Desk(settings, store)
    app = create_app(settings, store, desk, Executor(settings, store), token="tok")
    c = TestClient(app, base_url="http://127.0.0.1")
    c.get("/?token=tok", follow_redirects=False)
    names = [v["name"] for v in c.get("/api/venues").json()]
    assert names == ["polymarket", "kalshi", "polymarket_us"]
    assert set(c.get("/api/state").json()["venues"]) == set(names)


def test_bad_kalshi_key_does_not_break_startup(settings, store):
    settings.secrets.kalshi_key_id, settings.secrets.kalshi_private_key = "kid", "not a pem"
    desk = Desk(settings, store)                       # must not raise
    with pytest.raises(LiveTradingError, match="not a valid PEM"):
        desk.venues["kalshi"].trade_client.auth_headers("GET", "/portfolio/balance")
