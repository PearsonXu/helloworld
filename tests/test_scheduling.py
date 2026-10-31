import json
from types import SimpleNamespace

import pytest

from arbdesk import crossvenue
from arbdesk.config import load_settings
from arbdesk.executor import Executor
from arbdesk.llm import Analyst, BudgetExceeded, usage_numbers
from arbdesk.polymarket import VenueConfig
from arbdesk.service import Desk
from arbdesk.storage import Store


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "config.toml").write_text(f'data_dir = "{(tmp_path / "data").as_posix()}"\n')
    (tmp_path / ".env").write_text("LLM_PROVIDER=deepseek\nLLM_API_KEY=sk-test-123456\n")
    return load_settings(tmp_path / "config.toml", tmp_path / ".env")


@pytest.fixture
def store(settings):
    return Store(settings.data_dir / "t.db")


class FakeOpenAI:
    """Stands in for openai.OpenAI: returns `reply(payload)` and a usage block."""

    def __init__(self, reply, prompt=1000, cached=0, completion=200, deepseek=True):
        self.calls = []
        outer = self

        def create(**kw):
            outer.calls.append(kw)
            payload = json.loads(kw["messages"][1]["content"])
            usage = {"prompt_tokens": prompt, "completion_tokens": completion}
            if deepseek:
                usage["prompt_cache_hit_tokens"] = cached
            else:
                usage["prompt_tokens_details"] = {"cached_tokens": cached}
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(reply(payload))))],
                                   usage=usage)

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def analyst(settings, store, reply, **kw):
    a = Analyst(settings, store)
    a.client = FakeOpenAI(reply, **kw)
    return a


# --------------------------------------------------------------------------
# Token accounting and budget
# --------------------------------------------------------------------------

def test_usage_numbers_both_styles():
    assert usage_numbers({"prompt_tokens": 10, "completion_tokens": 2, "prompt_cache_hit_tokens": 4}) == (10, 4, 2)
    assert usage_numbers({"prompt_tokens": 10, "completion_tokens": 2,
                          "prompt_tokens_details": {"cached_tokens": 6}}) == (10, 6, 2)
    assert usage_numbers(None) == (0, 0, 0)


def test_flash_defaults_and_cost(settings, store):
    a = Analyst(settings, store)
    assert a.model == "deepseek-flash" and a.prices == (0.30, 0.006, 1.20)
    # 1M input of which 0.5M cached, 0.1M output
    assert a.cost(1_000_000, 500_000, 100_000) == pytest.approx(0.15 + 0.003 + 0.12)


def test_calls_are_metered_and_capped(settings, store):
    settings.llm.daily_token_budget = 2450
    a = analyst(settings, store, lambda p: {"ok": True}, prompt=1000, cached=400, completion=200)
    a.chat_json("sys", {"x": 1}, "test", max_tokens=50)
    u = store.usage_today()
    assert u == {"calls": 1, "tokens": 1200, "usd": pytest.approx(a.cost(1000, 400, 200))}
    assert a.client.calls[0]["max_tokens"] == 50          # output is capped
    a.chat_json("sys", {"x": 1}, "test", max_tokens=50)   # 2400 tokens used now
    with pytest.raises(BudgetExceeded):
        a.chat_json("sys", {"x": 1}, "test", max_tokens=50)
    assert len(a.client.calls) == 2                       # the third call never reached the API


def test_monthly_cap_stops_calls(settings, store):
    settings.llm.monthly_usd_cap = 0.001
    store.add_usage(purpose="x", model="m", input_tokens=1, cached_tokens=0, output_tokens=1, cost_usd=0.002)
    a = analyst(settings, store, lambda p: {})
    assert a.budget_status()["reason"] == "monthly USD cap reached"
    with pytest.raises(BudgetExceeded):
        a.chat_json("sys", {}, "test")


# --------------------------------------------------------------------------
# Change gating
# --------------------------------------------------------------------------

def test_crypto_analysis_only_when_opportunities_change(settings, store):
    desk = Desk(settings, store, analyst=analyst(settings, store, lambda p: {"market_view": "v", "proposals": []}))
    opps = [{"type": "funding_carry", "symbol": "BTC/USDT", "exchange": "okx", "apr_pct": 12.3}]
    assert "analysis_id" in desk.analyze(1, opps, [])
    assert desk.analyze(2, opps, [])["llm"] == "skipped: nothing changed"
    opps[0]["apr_pct"] = 20.0
    assert "analysis_id" in desk.analyze(3, opps, [])
    assert len(desk.analyst.client.calls) == 2


def test_no_opportunities_means_no_call_within_a_day(settings, store):
    desk = Desk(settings, store, analyst=analyst(settings, store, lambda p: {"market_view": "v"}))
    store.set_flag("crypto_ai_fp", "something")  # a recent analysis exists
    assert desk.analyze(1, [], [])["llm"] == "skipped: no opportunities"
    assert desk.analyst.client.calls == []


def market(mid, ask, bid=None):
    return {"id": mid, "question": f"Q {mid}?", "description": "rules", "end_date": "2099-01-01T00:00:00Z",
            "yes_token": f"{mid}|yes", "no_token": f"{mid}|no", "best_ask": ask, "best_bid": bid or ask - 0.02,
            "min_size": 1.0}


def test_forecasts_only_for_moved_or_stale_markets(settings, store):
    def reply(p):
        return {"forecasts": [{"market_id": m["market_id"], "prob": 0.5, "confidence": 0.3, "rationale": "r"}
                              for m in p["markets"]]}
    desk = Desk(settings, store, analyst=analyst(settings, store, reply))
    v = desk.venues["kalshi"]
    assert desk.forecast(v, [market("A", 0.40), market("B", 0.60)]) == 2
    assert desk.forecast(v, [market("A", 0.42), market("B", 0.60)]) == "skipped: no market moved"
    assert desk.forecast(v, [market("A", 0.47), market("B", 0.60)]) == 1   # A moved 7 points
    sent = desk.analyst.client.calls[-1]["messages"][1]["content"]
    assert '"A"' in sent and '"B"' not in sent


def test_brier_counts_each_market_once(store):
    for prob in (0.9, 0.9, 0.9, 0.6):  # re-forecast 4 times; latest is 0.6
        store.add_forecast(venue="kalshi", market_id="A", question="q", end_date=None,
                           market_prob=0.5, ai_prob=prob, confidence=0.5, rationale="")
    store.add_forecast(venue="kalshi", market_id="B", question="q", end_date=None,
                       market_prob=0.5, ai_prob=0.2, confidence=0.5, rationale="")
    store.resolve_forecasts("kalshi", "A", 1)
    store.resolve_forecasts("kalshi", "B", 0)
    s = store.forecast_scores("kalshi")
    assert s["resolved"] == 2
    assert s["ai_brier"] == pytest.approx(((0.6 - 1) ** 2 + 0.2 ** 2) / 2)


def test_pending_proposal_is_refreshed_not_duplicated(store):
    a = store.add_proposal(None, {"title": "x", "v": 1}, dedupe_key="k")
    b = store.add_proposal(None, {"title": "x", "v": 2}, dedupe_key="k")
    assert a == b and len(store.proposals()) == 1 and store.proposal(a)["payload"]["v"] == 2
    store.set_proposal_status(a, "rejected")
    c = store.add_proposal(None, {"title": "x", "v": 3}, dedupe_key="k")  # rejected ones don't block
    assert c != a


def test_prune_keeps_latest_and_hits(store):
    store.conn.execute("INSERT INTO scans (ts, source, summary, opportunities) VALUES "
                       "('2000-01-01T00:00:00+00:00', 'crypto', '{}', '[]'), "
                       "('2000-01-01T00:00:00+00:00', 'crypto', '{}', '[{\"x\":1}]'), "
                       "('2000-01-02T00:00:00+00:00', 'crypto', '{}', '[]')")
    assert store.prune_scans() == 1        # old empty scan removed; hit and latest kept
    assert len(store.scans(10, source="crypto")) == 2


# --------------------------------------------------------------------------
# Cross-venue
# --------------------------------------------------------------------------

def cm(mid, event, q, end="2026-11-05T00:00:00Z"):
    return {"id": mid, "event_title": event, "question": q, "group_title": "", "description": f"rules {mid}",
            "end_date": end, "active": True}


def test_candidate_pairs_prefilter():
    cfg = load_settings.__globals__["CrossVenueConfig"]()
    ka = [cm("K1", "Fed decision November", "Will the Fed cut rates in November 2026?"),
          cm("K2", "Bitcoin price", "Will Bitcoin be above 150000 on Nov 30?", end="2026-11-30T00:00:00Z")]
    pb = [cm("p-fed", "FOMC November", "Fed cuts rates in November?"),
          cm("p-btc", "Bitcoin", "Bitcoin above 150000 Nov 30", end="2026-12-31T00:00:00Z")]  # date too far
    pairs = crossvenue.candidate_pairs("kalshi", ka, "polymarket_us", pb, cfg, set())
    assert [(p["a"]["id"], p["b"]["id"]) for p in pairs] == [("K1", "p-fed")]
    assert crossvenue.candidate_pairs("kalshi", ka, "polymarket_us", pb, cfg, {pairs[0]["key"]}) == []


class BookVenue:
    """Minimal venue: fixed ask ladders per token, last_markets for matching."""

    def __init__(self, name, label, asks, markets):
        self.name, self.label, self._asks, self.last_markets = name, label, asks, markets
        self.cfg = VenueConfig(taker_fee_rate=0.07)

    def asks(self, token):
        return self._asks.get(token, [])

    def taker_fee(self, price, shares):
        return 0.07 * price * (1 - price) * shares


def test_crossvenue_cycle_end_to_end(settings, store):
    def reply(p):
        return {"pairs": [{"key": x["key"], "verdict": "same", "confidence": 0.95, "reason": "相同"}
                          for x in p["pairs"]]}
    desk = Desk(settings, store, analyst=analyst(settings, store, reply))
    k = BookVenue("kalshi", "Kalshi", {"K1|yes": [(0.40, 50)], "K1|no": [(0.62, 50)]},
                  [cm("K1", "Fed decision November", "Will the Fed cut rates in November 2026?")])
    u = BookVenue("polymarket_us", "Polymarket US", {"p-fed|yes": [(0.45, 50)], "p-fed|no": [(0.50, 50)]},
                  [cm("p-fed", "FOMC November", "Fed cuts rates in November?")])
    desk.venues = {"kalshi": k, "polymarket_us": u}

    out = desk.crossvenue_cycle(use_llm=True)
    assert out["new_candidates"] == 1 and out["judged"] == 1 and out["opportunities"] == 1
    # K1 YES 0.40 + p-fed NO 0.50 = 0.90 < 1 (other direction 0.62 + 0.45 > 1)
    prop = store.proposals()[0]
    legs = prop["payload"]["legs"]
    assert [(l["exchange"], l["outcome"]) for l in legs] == [("kalshi", "YES"), ("polymarket_us", "NO")]
    assert prop["note"] == ""                       # passes risk checks across two venues

    # The next AI run doesn't re-ask about a judged pair; code pricing still runs.
    out = desk.crossvenue_cycle(use_llm=True)
    assert out["new_candidates"] == 0 and len(desk.analyst.client.calls) == 1
    assert len(store.proposals()) == 1             # refreshed, not duplicated

    # Paper execution spans both venues.
    ex = Executor(settings, store)
    ex._venues = {"kalshi": k, "polymarket_us": u}
    r = ex.execute(prop["id"])
    assert r["ok"] and {t["exchange"] for t in store.trades()} == {"kalshi", "polymarket_us"}


def test_scheduler_two_clocks(settings, monkeypatch):
    """5-minute scans, AI on the first scan of each hour (simulated clock)."""
    import arbdesk.service as service

    calls, clock = [], {"t": 0.0}

    class StubDesk:
        running = False

        def __init__(self):
            self.settings = settings

        def run_cycle(self, use_llm):
            calls.append((clock["t"], use_llm))

    class Stop(Exception):
        pass

    def fake_sleep(s):
        clock["t"] += s
        if clock["t"] > 2 * 3600 + 5:
            raise Stop

    monkeypatch.setattr(service.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(service.time, "sleep", fake_sleep)
    with pytest.raises(Stop):
        service.Scheduler(StubDesk()).run()
    assert len(calls) == 25                                   # every 5 min for 2 h, plus t=0
    ai = [t for t, use in calls if use]
    assert ai == [0.0, 3600.0, 7200.0]
