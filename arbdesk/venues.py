"""Prediction-market venues behind one interface.

Each venue provides public data (scan, order-book asks, resolution), its fee
model, and a live trader. Proposal legs carry `exchange` = the venue name.
"""
from __future__ import annotations

from . import kalshi, pmus
from . import polymarket as pm
from .config import Settings
from .polymarket import VenueConfig, book_side, taker_fee_per_share
from .risk import PREDICTION_VENUES

LABELS = {"polymarket": "Polymarket（国际）", "kalshi": "Kalshi", "polymarket_us": "Polymarket US"}


class Venue:
    name = ""
    live_outcomes = frozenset({"YES", "NO"})

    def __init__(self, cfg: VenueConfig, settings: Settings | None = None):
        self.cfg = cfg
        self.settings = settings
        # Every active market seen in the last scan (common dict shape + "event_title"),
        # used by the cross-venue matcher.
        self.last_markets: list[dict] = []

    def _remember(self, events: list[dict]) -> None:
        self.last_markets = [{**m, "event_title": e["title"]} for e in events for m in e["markets"]
                             if m["active"]]

    @property
    def label(self) -> str:
        return LABELS[self.name]

    def scan(self) -> tuple[list[dict], list[dict], dict]:
        raise NotImplementedError

    def asks(self, token_id: str) -> list[tuple[float, float]]:
        raise NotImplementedError

    def resolution(self, market_id: str) -> dict | None:
        raise NotImplementedError

    def bid(self, token_id: str) -> float | None:
        """Best price you could sell `token_id` at right now (for marking positions)."""
        raise NotImplementedError

    def taker_fee(self, price: float, shares: float) -> float:
        return shares * taker_fee_per_share(price, self.cfg.taker_fee_rate)

    def has_live_credentials(self) -> bool:
        raise NotImplementedError

    def make_trader(self):
        raise NotImplementedError


class PolymarketVenue(Venue):
    name = "polymarket"

    def __init__(self, cfg, settings=None, client=None):
        super().__init__(cfg, settings)
        self.client = client or pm.PolymarketClient()

    def scan(self):
        events = self.client.events(self.cfg.max_events)
        self._remember(events)
        return pm.scan(self.client, self.cfg, events)

    def asks(self, token_id):
        return book_side(self.client.book(token_id), "asks")

    def resolution(self, market_id):
        return pm.resolution(self.client.market(market_id))

    def bid(self, token_id):
        bids = book_side(self.client.book(token_id), "bids")
        return bids[0][0] if bids else None

    def has_live_credentials(self):
        return bool(self.settings.secrets.pm_private_key)

    def make_trader(self):
        from .pm_live import PolymarketTrader
        return PolymarketTrader(self.settings.secrets, self.cfg)


class KalshiVenue(Venue):
    name = "kalshi"

    def __init__(self, cfg, settings=None, client=None):
        super().__init__(cfg, settings)
        if cfg.environment not in kalshi.HOSTS:
            raise ValueError("kalshi.environment must be 'demo' or 'prod'")
        # Orders (and `check`) go to the account's environment. Paper mode studies real
        # prod prices; live mode reads the same environment it trades on, so pre-flight
        # checks see the book the order will hit.
        sec = settings.secrets if settings else None
        self.trade_client = client or kalshi.KalshiClient(
            kalshi.HOSTS[cfg.environment],
            key_id=sec.kalshi_key_id if sec else "", private_key_pem=sec.kalshi_private_key if sec else "")
        same = client is not None or cfg.mode == "live" or cfg.environment == "prod"
        self.client = self.trade_client if same else kalshi.KalshiClient(kalshi.HOSTS["prod"])

    def scan(self):
        events = self.client.events(self.cfg.max_events)
        self._remember(events)
        return kalshi.scan(self.client, self.cfg, events)

    def asks(self, token_id):
        ticker, side = kalshi.split_token(token_id)
        return kalshi.ladders(self.client.orderbook(ticker))[side]

    def resolution(self, market_id):
        return kalshi.resolution(self.client.market(market_id))

    def bid(self, token_id):
        ticker, side = kalshi.split_token(token_id)
        ob = self.client.orderbook(ticker)
        ob = ob.get("orderbook_fp") or ob
        bids = [float(p) for p, _ in ob.get(f"{side}_dollars") or []]  # resting bids for that side
        return max(bids) if bids else None

    def taker_fee(self, price, shares):
        return kalshi.kalshi_fee(price, shares, self.cfg.taker_fee_rate)

    def has_live_credentials(self):
        s = self.settings.secrets
        return bool(s.kalshi_key_id and s.kalshi_private_key)

    def make_trader(self):
        return kalshi.KalshiTrader(self.trade_client, self.cfg)


class PolymarketUSVenue(Venue):
    name = "polymarket_us"
    live_outcomes = frozenset({"YES"})

    def __init__(self, cfg, settings=None, client=None):
        super().__init__(cfg, settings)
        self.client = client or pmus.PMUSClient()

    def scan(self):
        raw = self.client.events(self.cfg.max_events)
        # Titles/rules only (no quotes): enough to match events; prices are fetched per pair.
        self._remember([{"title": e.get("title", ""), "markets": [pmus.parse_market(m, e) for m in e.get("markets") or []]}
                        for e in raw])
        return pmus.scan(self.client, self.cfg, raw)

    def asks(self, token_id):
        slug, side = pmus.split_token(token_id)
        return pmus.ladders(self.client.book(slug))[side]

    def resolution(self, market_id):
        return pmus.resolution(self.client.market(market_id), self.client.settlement(market_id))

    def bid(self, token_id):
        slug, side = pmus.split_token(token_id)
        book = self.client.book(slug)
        if side == "yes":  # a YES holder sells into the long bids
            md = book.get("marketData", book)
            bids = [b for b in (pmus._px(l.get("px")) for l in md.get("bids") or []) if b]
            return max(bids) if bids else None
        offers = pmus.ladders(book)["yes"]  # a NO holder exits by buying back YES at the offer
        return round(1 - offers[0][0], 6) if offers else None

    def has_live_credentials(self):
        s = self.settings.secrets
        return bool(s.pmus_key_id and s.pmus_secret_key)

    def make_trader(self):
        s = self.settings.secrets
        return pmus.PMUSTrader(s.pmus_key_id, s.pmus_secret_key, self.cfg)


CLASSES = {"polymarket": PolymarketVenue, "kalshi": KalshiVenue, "polymarket_us": PolymarketUSVenue}


def build_venues(settings: Settings) -> dict[str, Venue]:
    """Enabled venues only."""
    return {name: CLASSES[name](settings.venue_cfg(name), settings)
            for name in PREDICTION_VENUES if settings.venue_cfg(name).enabled}
