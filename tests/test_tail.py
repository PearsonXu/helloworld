import math
from datetime import datetime, timedelta, timezone

import pytest

from arbdesk import tail
from arbdesk.config import TailConfig, load_settings
from arbdesk.deribit import Smile, build_smiles, norm_cdf, parse_instrument, prob_above
from arbdesk.service import Desk
from arbdesk.storage import Store

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def flat_smile(days, fwd=100_000.0, vol=0.6):
    ks = [float(k) for k in range(40_000, 220_001, 5_000)]
    return Smile(NOW + timedelta(days=days), fwd, ks, [vol] * len(ks))


def test_parse_instrument():
    exp, k, kind = parse_instrument("BTC-27DEC26-150000-C")
    assert exp == datetime(2026, 12, 27, 8, tzinfo=timezone.utc) and k == 150000 and kind == "C"
    assert parse_instrument("BTC-PERPETUAL") is None


def test_build_smiles_uses_otm_and_percent_iv():
    rows = [
        {"instrument_name": "BTC-27DEC26-90000-P", "mark_iv": 70.0, "underlying_price": 100000},
        {"instrument_name": "BTC-27DEC26-90000-C", "mark_iv": 99.0, "underlying_price": 100000},  # ITM call ignored
        {"instrument_name": "BTC-27DEC26-110000-C", "mark_iv": 60.0, "underlying_price": 100000},
        {"instrument_name": "BTC-27DEC26-120000-C", "mark_iv": 0.62, "underlying_price": 100000},  # decimal form
        {"instrument_name": "BTC-27OCT26-90000-P", "mark_iv": 50.0, "underlying_price": 100000},  # expired
    ]
    (s,) = build_smiles(rows, NOW)
    assert s.strikes == [90000, 110000, 120000] and s.vols == pytest.approx([0.70, 0.60, 0.62])


def test_prob_above_matches_black_scholes_on_flat_vol():
    s = flat_smile(30)
    t, k = 30 / 365.25, 130_000
    sq = 0.6 * math.sqrt(t)
    expected = norm_cdf((math.log(100_000 / k) - 0.5 * sq * sq) / sq)
    assert prob_above([s], k, s.expiry, NOW) == pytest.approx(expected, rel=1e-9)


def test_skew_raises_downside_tail():
    ks = [float(k) for k in range(40_000, 220_001, 5_000)]
    skewed = Smile(NOW + timedelta(days=30), 100_000.0, ks, [0.6 + 0.3 * (100_000 - k) / 100_000 for k in ks])
    flat = flat_smile(30)
    down_skew = 1 - prob_above([skewed], 70_000, skewed.expiry, NOW)
    down_flat = 1 - prob_above([flat], 70_000, flat.expiry, NOW)
    assert down_skew > down_flat * 1.3


def test_prob_above_interpolates_between_expiries_and_refuses_extrapolation():
    a, b = flat_smile(14, vol=0.5), flat_smile(44, vol=0.7)
    mid = NOW + timedelta(days=29)
    p = prob_above([a, b], 120_000, mid, NOW)
    # total variance interpolated linearly in time
    w = 0.25 * 14 / 365.25 + (0.49 * 44 / 365.25 - 0.25 * 14 / 365.25) * (29 - 14) / 30
    sq = math.sqrt(w)
    assert p == pytest.approx(norm_cdf((math.log(100_000 / 120_000) - 0.5 * w) / sq), rel=1e-6)
    assert prob_above([a, b], 120_000, NOW + timedelta(days=60), NOW) is None   # beyond last expiry
    assert prob_above([a, b], 500_000, mid, NOW) is None                         # outside quoted strikes


def km(ticker, st, lo=None, hi=None, bid=0.02, ask=0.03, title="Bitcoin price today at 5pm EST?", days=29):
    return {"id": ticker, "event_title": title, "question": ticker, "group_title": ticker, "active": True,
            "strike_type": st, "floor_strike": lo, "cap_strike": hi, "best_bid": bid, "best_ask": ask,
            "expiry": (NOW + timedelta(days=days)).isoformat(), "yes_token": f"{ticker}|yes",
            "no_token": f"{ticker}|no", "min_size": 1.0}


def test_kalshi_contract_mapping():
    cs = tail.kalshi_contracts([km("A", "greater", lo=130000), km("B", "less", hi=70000),
                                km("C", "between", lo=100000, hi=105000), km("D", "custom"),
                                km("E", "greater", lo=5, title="Fed rate above 5%?")], ["BTC", "ETH"])
    assert [(c.market_id, c.kind) for c in cs] == [("A", "above"), ("B", "below"), ("C", "between")]


@pytest.mark.parametrize("title,kind,strike", [
    ("Will Bitcoin be above $150,000 on Dec 31?", "above", 150000),
    ("Bitcoin below $60k by year end?", "below", 60000),
    ("Will ETH reach $10k in 2026?", "touch_above", 10000),
    ("Will Bitcoin dip to $55,000 in November?", "touch_below", 55000),
    ("Bitcoin between $100k and $110k?", None, None),
    ("Will Bitcoin dominance be above 60%?", None, None),
    ("Will Bitcoin and Ethereum both be above $1,000?", None, None),   # two coins: ambiguous
])
def test_title_contract_parsing(title, kind, strike):
    m = {"id": "x", "question": title, "event_title": "", "end_date": "2026-12-31T00:00:00Z",
         "best_bid": None, "best_ask": None, "yes_token": "x|yes", "no_token": "x|no"}
    c = tail.title_contract(m, ["BTC", "ETH"], "polymarket_us")
    assert (c.kind if c else None) == kind
    if c:
        assert (c.lo or c.hi) == strike


def test_evaluate_flags_cheap_tails():
    smiles = {"BTC": [flat_smile(45)]}
    cs = tail.kalshi_contracts([km("CRASH", "less", hi=80000, bid=0.03, ask=0.99),   # P(<80k in 29d) ~ 10.8%
                                km("PRICEY", "greater", lo=130000, bid=0.30, ask=0.31)], ["BTC"])
    rows = tail.evaluate(cs, smiles, TailConfig(), {"kalshi": 0.07}, NOW)
    crash_yes = next(r for r in rows if r["market_id"] == "CRASH" and r["side"] == "YES")
    assert crash_yes["cheap"] is False                      # 99c is no lottery ticket
    cs[0].yes_ask = 0.04                                    # now the crash ticket costs 4c
    rows = tail.evaluate(cs, smiles, TailConfig(), {"kalshi": 0.07}, NOW)
    crash_yes = next(r for r in rows if r["market_id"] == "CRASH" and r["side"] == "YES")
    assert 0.09 < crash_yes["implied"] < 0.13 and crash_yes["cheap"] and rows[0] is crash_yes


# --------------------------------------------------------------------------
# End to end: cycle -> proposal -> quarterly budget
# --------------------------------------------------------------------------

@pytest.fixture
def settings(tmp_path):
    (tmp_path / "config.toml").write_text(f'data_dir = "{(tmp_path / "data").as_posix()}"\n')
    return load_settings(tmp_path / "config.toml", tmp_path / ".env")


class FakeDeribit:
    def option_summaries(self, ccy):
        if ccy != "BTC":
            return []
        exp = (datetime.now(timezone.utc) + timedelta(days=40)).strftime("%d%b%y").upper()
        return [{"instrument_name": f"BTC-{exp}-{k}-{'P' if k < 100000 else 'C'}", "mark_iv": 60.0,
                 "underlying_price": 100000} for k in range(40000, 220001, 5000)]


class FakeKalshiClient:
    def events(self, limit, series_ticker=None):
        if series_ticker != "KXBTCD":
            return []
        close = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        return [{"title": "Bitcoin price on Dec 1?", "markets": [
            {"id": "KXBTCD-70K", "question": "BTC below 80k", "group_title": "Below $80,000", "active": True,
             "strike_type": "less", "floor_strike": None, "cap_strike": 80000.0, "best_bid": 0.03,
             "best_ask": 0.04, "expiry": close, "yes_token": "KXBTCD-70K|yes", "no_token": "KXBTCD-70K|no",
             "min_size": 1.0}]}]


def test_tail_cycle_proposal_and_budget(settings):
    store = Store(settings.data_dir / "t.db")
    desk = Desk(settings, store)
    desk.deribit = FakeDeribit()
    desk.venues["kalshi"].client = FakeKalshiClient()
    desk.venues.pop("polymarket_us")
    out = desk.tail_cycle()
    assert out["cheap"] == 1
    prop = store.proposals()[0]
    p = prop["payload"]
    assert p["kind"] == "tail" and p["legs"][0]["outcome"] == "YES" and prop["note"] == ""
    assert p["legs"][0]["shares"] * p["legs"][0]["limit_price"] <= settings.tail.max_usd_per_bet

    # Spending the quarter's budget blocks further tail bets.
    store.add_trade(proposal_id=prop["id"], mode="paper", exchange="kalshi", symbol="x", side="buy",
                    amount=1400, price=0.02, cost_usd=28.0, fee_usd=0.5, pnl_usd=None,
                    raw='{"market_id": "KXBTCD-70K", "token_id": "KXBTCD-70K|yes", "outcome": "YES"}')
    assert store.tail_spent_quarter() == pytest.approx(28.5)
    desk.tail_cycle()
    note = store.proposal(prop["id"])["note"]
    assert "black-swan budget" in note


def test_filters_drop_intraday_and_near_the_money():
    smiles = {"BTC": [flat_smile(45)]}
    cs = tail.kalshi_contracts([
        km("SOON", "less", hi=80000, bid=0.01, ask=0.02, days=0.2),        # 5 hours left
        km("ATM", "between", lo=99_000, hi=101_000, bid=0.01, ask=0.02),   # around the price
        km("TAIL", "less", hi=80000, bid=0.01, ask=0.04),                  # 20% away, 29 days
    ], ["BTC"])
    ids = {r["market_id"] for r in tail.evaluate(cs, smiles, TailConfig(), {"kalshi": 0.07}, NOW)}
    assert ids == {"TAIL"}
