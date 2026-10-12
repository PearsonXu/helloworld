import json

import pytest
from fastapi.testclient import TestClient

from arbdesk import scanners, risk
from arbdesk.config import RedactingFilter, load_settings
from arbdesk.executor import Executor
from arbdesk.llm import parse_json
from arbdesk.service import Desk
from arbdesk.storage import Store
from arbdesk.web import create_app


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "config.toml").write_text(f'data_dir = "{tmp_path / "data"}"\n')
    (tmp_path / ".env").write_text("LLM_API_KEY=sk-test-123456\nEXCHANGE_OKX_API_KEY=abcdefgh\n")
    return load_settings(tmp_path / "config.toml", tmp_path / ".env")


@pytest.fixture
def store(settings):
    return Store(settings.data_dir / "t.db")


def test_secrets_parsed(settings):
    assert settings.secrets.llm_api_key == "sk-test-123456"
    assert settings.secrets.exchange_keys == {"okx": {"apiKey": "abcdefgh"}}
    assert settings.risk.mode == "paper"


def test_cross_exchange():
    t = {"binance": {"BTC/USDT": {"bid": 100.0, "ask": 100.1}},
         "okx": {"BTC/USDT": {"bid": 101.0, "ask": 101.1}}}
    opps = scanners.find_cross_exchange(t, taker_fee_pct=0.1, min_net_pct=0.2)
    assert opps[0]["buy_exchange"] == "binance" and opps[0]["sell_exchange"] == "okx"
    assert opps[0]["net_pct"] == pytest.approx((101 - 100.1) / 100.1 * 100 - 0.2, abs=1e-3)
    assert scanners.find_cross_exchange(t, 0.1, 1.0) == []


def test_funding():
    f = {"binance": {"BTC/USDT": {"rate": 0.0003, "interval_h": 8}},
         "okx": {"BTC/USDT": {"rate": 0.0, "interval_h": 8}}}
    opps = scanners.find_funding(f, min_apr_pct=10, taker_fee_pct=0.1)
    types = {o["type"] for o in opps}
    assert types == {"funding_carry", "funding_spread"}
    carry = next(o for o in opps if o["type"] == "funding_carry")
    assert carry["apr_pct"] == pytest.approx(32.85)


def test_triangular():
    t = {"ETH/USDT": {"bid": 2000, "ask": 2000, "quoteVolume": 1e9},
         "BTC/USDT": {"bid": 40000, "ask": 40000, "quoteVolume": 1e9},
         "ETH/BTC": {"bid": 0.051, "ask": 0.051, "quoteVolume": 1e9}}
    opps = scanners.find_triangular(t, "USDT", taker_fee_pct=0.1, min_profit_pct=0.05)
    # USDT->ETH->BTC->USDT: 1/2000 * 0.051 * 40000 = 1.02 before fees
    assert opps and opps[0]["path"] == "USDT->ETH->BTC->USDT"
    assert opps[0]["net_pct"] == pytest.approx((1.02 * 0.999 ** 3 - 1) * 100, abs=1e-3)


def test_risk_limits(settings, store):
    ok = {"legs": [{"exchange": "okx", "symbol": "BTC/USDT", "side": "buy", "usd": 20}]}
    assert risk.validate(ok, settings, store)[1] == []
    big = {"legs": [{"exchange": "okx", "symbol": "BTC/USDT", "side": "buy", "usd": 500}]}
    assert risk.validate(big, settings, store)[1]
    bad_sym = {"legs": [{"exchange": "okx", "symbol": "PEPE/USDT", "side": "buy", "usd": 5}]}
    assert risk.validate(bad_sym, settings, store)[1]
    bad_ex = {"legs": [{"exchange": "evilex", "symbol": "BTC/USDT", "side": "buy", "usd": 5}]}
    assert risk.validate(bad_ex, settings, store)[1]
    settings.risk.mode = "live"
    no_key = {"legs": [{"exchange": "binance", "symbol": "BTC/USDT", "side": "buy", "usd": 5}]}
    assert any("no API key" in e for e in risk.validate(no_key, settings, store)[1])


def test_paper_execution_and_pnl(settings, store, monkeypatch):
    ex = Executor(settings, store)
    quotes = {"binance": {"bid": 99.9, "ask": 100.0}, "okx": {"bid": 101.0, "ask": 101.1}}
    monkeypatch.setattr(ex, "quote", lambda leg: quotes[leg["exchange"]])
    pid = store.add_proposal(None, {"title": "x", "legs": [
        {"exchange": "binance", "symbol": "BTC/USDT", "side": "buy", "usd": 20},
        {"exchange": "okx", "symbol": "BTC/USDT", "side": "sell", "usd": 20 * 101.0 / 100.0},
    ]})
    r = ex.execute(pid)
    assert r["ok"] and r["pnl_usd"] == pytest.approx(0.2 * 1 - 0.0404, abs=1e-3)
    assert store.proposal(pid)["status"] == "executed"
    assert len(store.trades()) == 2
    with pytest.raises(ValueError):
        ex.execute(pid)  # cannot run twice


def test_daily_loss_limit_blocks(settings, store):
    store.add_trade(proposal_id=1, mode="paper", exchange="okx", symbol="BTC/USDT", side="buy",
                    amount=1, price=1, cost_usd=1, fee_usd=0, pnl_usd=-50)
    ok = {"legs": [{"exchange": "okx", "symbol": "BTC/USDT", "side": "buy", "usd": 5}]}
    assert any("loss limit" in e for e in risk.validate(ok, settings, store)[1])


def test_parse_json():
    assert parse_json('```json\n{"proposals": []}\n```')["proposals"] == []
    assert parse_json("garbage")["proposals"] == []


def test_llm_payload_has_no_secrets(settings, store):
    desk = Desk(settings, store)
    payload = json.dumps(desk.analyst.build_payload([{"type": "x"}], []))
    for s in settings.secrets.all_values():
        assert s not in payload


def test_redaction():
    import logging
    rec = logging.LogRecord("x", logging.INFO, "", 0, "key=%s", ("sk-test-123456",), None)
    RedactingFilter(["sk-test-123456"]).filter(rec)
    assert "sk-test-123456" not in rec.getMessage()


def test_panel_auth_and_host(settings, store):
    desk = Desk(settings, store)
    app = create_app(settings, store, desk, Executor(settings, store), token="tok")
    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.get("/api/state").status_code == 401
    assert c.get("/?token=wrong", follow_redirects=False).status_code == 401
    assert c.get("/?token=tok", follow_redirects=False).status_code == 303
    assert c.get("/api/state").status_code == 200
    # form-style POST (CSRF vector) rejected
    assert c.post("/api/run", data={"a": "1"}).status_code == 415
    evil = TestClient(app, base_url="http://evil.example")
    assert evil.get("/").status_code == 403
