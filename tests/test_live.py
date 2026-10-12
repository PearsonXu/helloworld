import json
import os
import stat

import pytest

from arbdesk import risk
from arbdesk.config import load_settings
from arbdesk.executor import Executor
from arbdesk.pm_live import LiveFill, LiveTradingError, PolymarketTrader, check_geoblock
from arbdesk.storage import Store
from arbdesk.vault import Vault, VaultError

FAST = {"n": 2 ** 14, "r": 8, "p": 1}
PW = "correct horse battery"


# --------------------------------------------------------------------------
# Vault
# --------------------------------------------------------------------------

def test_vault_roundtrip_and_file_mode(tmp_path):
    v = Vault(tmp_path / "vault.json", FAST)
    v.create(PW)
    v.set(PW, "POLYMARKET_PRIVATE_KEY", "0xdeadbeef" * 6)
    v.set(PW, "LLM_API_KEY", "sk-abc123456")
    assert v.unlock(PW)["LLM_API_KEY"] == "sk-abc123456"
    raw = (tmp_path / "vault.json").read_text()
    assert "deadbeef" not in raw and "sk-abc" not in raw and "LLM_API_KEY" not in raw
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "vault.json").stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".*.tmp"))


def test_vault_wrong_password_and_tamper(tmp_path):
    path = tmp_path / "vault.json"
    v = Vault(path, FAST)
    v.create(PW)
    v.set(PW, "LLM_API_KEY", "sk-abc123456")
    with pytest.raises(VaultError):
        v.unlock("wrong password!!")
    blob = json.loads(path.read_text())
    blob["kdf"]["n"] = 2 ** 15  # tamper with authenticated header
    path.write_text(json.dumps(blob))
    with pytest.raises(VaultError):
        v.unlock(PW)


def test_vault_rejects_weak_kdf_and_short_password(tmp_path):
    with pytest.raises(VaultError):
        Vault(tmp_path / "a.json", FAST).create("short")
    weak = Vault(tmp_path / "b.json", {"n": 2 ** 10, "r": 8, "p": 1})
    blob = weak.encrypt(PW, {"A": "b"})
    with pytest.raises(VaultError):
        Vault.decrypt(PW, blob)


def test_vault_change_password_and_delete(tmp_path):
    v = Vault(tmp_path / "vault.json", FAST)
    v.create(PW)
    v.set(PW, "EXCHANGE_OKX_API_KEY", "abcdefgh")
    v.change_password(PW, "another long password")
    with pytest.raises(VaultError):
        v.unlock(PW)
    assert v.delete("another long password", "EXCHANGE_OKX_API_KEY")
    assert v.unlock("another long password") == {}
    with pytest.raises(VaultError):
        v.set("another long password", "bad-name", "x")


# --------------------------------------------------------------------------
# Secrets loading
# --------------------------------------------------------------------------

@pytest.fixture
def cfg_files(tmp_path):
    (tmp_path / "config.toml").write_text(
        f'data_dir = "{tmp_path / "data"}"\n[polymarket]\nmode = "live"\n'
        'max_live_usd_per_day = 20\nmax_live_exposure_usd = 30\n')
    return tmp_path


def test_private_key_only_from_vault(cfg_files):
    (cfg_files / ".env").write_text("POLYMARKET_PRIVATE_KEY=0xfromenv\nLLM_API_KEY=sk-env-123456\n")
    s = load_settings(cfg_files / "config.toml", cfg_files / ".env")
    assert s.secrets.pm_private_key == ""          # ignored from .env
    s = load_settings(cfg_files / "config.toml", cfg_files / ".env",
                      vault_data={"POLYMARKET_PRIVATE_KEY": "0xfromvault", "LLM_API_KEY": "sk-vault-123"})
    assert s.secrets.pm_private_key == "0xfromvault"
    assert s.secrets.llm_api_key == "sk-vault-123"  # vault wins
    assert "0xfromvault" not in repr(s.secrets)


# --------------------------------------------------------------------------
# Live execution with a fake trader
# --------------------------------------------------------------------------

def book(asks):
    return {"bids": [], "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}


class FakeBooks:
    def __init__(self, books):
        self.books = books

    def book(self, token_id):
        return self.books[token_id]


class FakeTrader:
    def __init__(self, balance=100.0, fail_on=None):
        self.balance, self.fail_on, self.orders, self.cancelled = balance, fail_on, [], False

    def ensure_allowed(self):
        return {"blocked": False}

    def collateral_balance(self):
        return self.balance

    def buy_fok(self, token_id, shares, limit_price, neg_risk=None):
        if token_id == self.fail_on:
            raise LiveTradingError("FOK not filled")
        self.orders.append(token_id)
        return LiveFill(order_id=f"o-{token_id}", status="matched", shares=shares,
                        avg_price=limit_price, cost_usd=shares * limit_price, raw={})

    def cancel_all(self):
        self.cancelled = True


def leg(token, shares=10, price=0.3, market="m1"):
    return {"exchange": "polymarket", "market_id": market, "token_id": token, "outcome": "YES",
            "label": token, "side": "buy", "shares": shares, "limit_price": price}


@pytest.fixture
def live(cfg_files):
    s = load_settings(cfg_files / "config.toml", cfg_files / ".env",
                      vault_data={"POLYMARKET_PRIVATE_KEY": "0x" + "1" * 64, "POLYMARKET_FUNDER": "0xabc"})
    store = Store(s.data_dir / "t.db")
    ex = Executor(s, store)
    return s, store, ex


def test_live_requires_confirmation(live):
    s, store, ex = live
    ex.pm = FakeBooks({"a": book([(0.3, 100)])})
    ex._pm_trader = FakeTrader()
    pid = store.add_proposal(None, {"title": "x", "legs": [leg("a")]})
    r = ex.execute(pid)
    assert r["needs_confirm"] and store.proposal(pid)["status"] == "pending"
    assert ex._pm_trader.orders == []
    r = ex.execute(pid, confirm_live=True)
    assert r["ok"] and ex._pm_trader.orders == ["a"]
    t = store.trades()[0]
    assert t["mode"] == "live" and t["order_id"] == "o-a"


def test_live_preflight_blocks_without_sending(live):
    s, store, ex = live
    ex.pm = FakeBooks({"a": book([(0.3, 100)]), "b": book([(0.5, 100)])})  # b moved above limit
    ex._pm_trader = FakeTrader()
    pid = store.add_proposal(None, {"title": "x", "legs": [leg("a"), leg("b")]})
    r = ex.execute(pid, confirm_live=True)
    assert not r["ok"] and "nothing sent" in r["errors"][0]
    assert ex._pm_trader.orders == [] and store.trades() == []

    ex.pm = FakeBooks({"a": book([(0.3, 100)])})
    ex._pm_trader = FakeTrader(balance=1.0)
    pid = store.add_proposal(None, {"title": "y", "legs": [leg("a")]})
    r = ex.execute(pid, confirm_live=True)
    assert not r["ok"] and "balance" in r["errors"][0] and ex._pm_trader.orders == []


def test_partial_fill_halts_everything(live):
    s, store, ex = live
    ex.pm = FakeBooks({"a": book([(0.3, 100)]), "b": book([(0.3, 12)])})
    trader = FakeTrader(fail_on="a")
    ex._pm_trader = trader
    pid = store.add_proposal(None, {"title": "bundle", "legs": [leg("a"), leg("b")]})
    r = ex.execute(pid, confirm_live=True)
    # b has the thinnest book, so it is sent first and fills; a then fails.
    assert trader.orders == ["b"] and not r["ok"]
    assert store.proposal(pid)["status"] == "partial"
    assert store.halted() and trader.cancelled
    assert len(store.trades()) == 1
    # Kill switch now blocks any other live proposal at validation time.
    pid2 = store.add_proposal(None, {"title": "next", "legs": [leg("c", market="m2")]})
    _, errors = risk.validate(store.proposal(pid2)["payload"], s, store)
    assert any("halted" in e for e in errors)
    ex.resume()
    assert store.halted() is None


def test_live_caps(live):
    s, store, ex = live
    # 70 shares * 0.3 = $21 > max_live_usd_per_day 20 (also per-market cap 20)
    _, errors = risk.validate({"legs": [leg("a", shares=70)]}, s, store)
    assert any("max_live_usd_per_day" in e for e in errors)
    store.add_trade(proposal_id=1, mode="live", exchange="polymarket", symbol="x", side="buy",
                    amount=50, price=0.5, cost_usd=25, fee_usd=0, pnl_usd=None,
                    raw=json.dumps({"market_id": "z", "token_id": "z", "outcome": "YES"}))
    _, errors = risk.validate({"legs": [leg("a", shares=20)]}, s, store)   # 25 + 6 > 30 exposure
    assert any("max_live_exposure_usd" in e for e in errors)


# --------------------------------------------------------------------------
# Trader guards
# --------------------------------------------------------------------------

class FakeResp:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError("http error")

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc = resp, exc

    def get(self, url, timeout=None):
        if self.exc:
            raise self.exc
        return self.resp


def test_geoblock_fails_closed():
    assert check_geoblock(FakeSession(FakeResp({"blocked": False, "country": "XX"})))["country"] == "XX"
    with pytest.raises(LiveTradingError):
        check_geoblock(FakeSession(FakeResp({"blocked": True, "country": "US"})))
    with pytest.raises(LiveTradingError):
        check_geoblock(FakeSession(FakeResp({})))               # missing field -> refuse
    with pytest.raises(LiveTradingError):
        check_geoblock(FakeSession(exc=ConnectionError("down")))


class FakeClob:
    def __init__(self, resp, order):
        self.resp, self.order, self.posted = resp, order, []

    def get_tick_size(self, token_id):
        return "0.01"

    def create_and_post_order(self, args, opts, order_type):
        self.posted.append((args, order_type))
        return self.resp

    def get_order(self, oid):
        return self.order


def trader_with(clob, cfg_files):
    s = load_settings(cfg_files / "config.toml", cfg_files / ".env",
                      vault_data={"POLYMARKET_PRIVATE_KEY": "0x" + "1" * 64, "POLYMARKET_FUNDER": "0xabc"})
    t = PolymarketTrader(s.secrets, s.polymarket, client=clob,
                         session=FakeSession(FakeResp({"blocked": False})))
    return t


def test_buy_fok_order_shape_and_fill_check(cfg_files):
    pytest.importorskip("py_clob_client_v2")
    clob = FakeClob({"success": True, "orderID": "abc", "status": "matched", "makingAmount": "3.1"},
                    {"size_matched": "10", "price": "0.31", "status": "MATCHED"})
    t = trader_with(clob, cfg_files)
    f = t.buy_fok("tok", 10.005, 0.30)
    args, order_type = clob.posted[0]
    assert order_type == "FOK" and args.side == "BUY"
    assert args.price == pytest.approx(0.31) and args.size == pytest.approx(10.0)
    assert f.cost_usd == pytest.approx(3.1) and f.avg_price == pytest.approx(0.31)

    clob = FakeClob({"success": True, "orderID": "abc"}, {"size_matched": "4"})
    with pytest.raises(LiveTradingError):
        trader_with(clob, cfg_files).buy_fok("tok", 10, 0.30)
    clob = FakeClob({"success": False, "errorMsg": "not enough balance"}, {})
    with pytest.raises(LiveTradingError):
        trader_with(clob, cfg_files).buy_fok("tok", 10, 0.30)


def test_trader_requires_vault_key(cfg_files):
    s = load_settings(cfg_files / "config.toml", cfg_files / ".env")
    with pytest.raises(LiveTradingError):
        PolymarketTrader(s.secrets, s.polymarket)


def test_live_without_wallet_fails_cleanly(cfg_files):
    s = load_settings(cfg_files / "config.toml", cfg_files / ".env")
    store = Store(s.data_dir / "t.db")
    ex = Executor(s, store)
    ex.pm = FakeBooks({"a": book([(0.3, 100)])})
    pid = store.add_proposal(None, {"title": "x", "legs": [leg("a")]})
    store.set_proposal_status(pid, "pending")
    legs, _ = risk.validate(store.proposal(pid)["payload"], s, store)
    r = ex._execute_polymarket_live(pid, legs)   # bypass risk to hit the trader path directly
    assert not r["ok"] and "nothing sent" in r["errors"][0]
