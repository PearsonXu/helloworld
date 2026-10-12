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

    @property
    def label(self) -> str:
        return LABELS[self.name]

    def scan(self) -> tuple[list[dict], list[dict], dict]:
        raise NotImplementedError

    def asks(self, token_id: str) -> list[tuple[float, float]]:
        raise NotImplementedError

    def resolution(self, market_id: str) -> dict | None:
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
        return pm.scan(self.client, self.cfg)

    def asks(self, token_id):
        return book_side(self.client.book(token_id), "asks")

    def resolution(self, market_id):
        return pm.resolution(self.client.market(market_id))

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
        return kalshi.scan(self.client, self.cfg)

    def asks(self, token_id):
        ticker, side = kalshi.split_token(token_id)
        return kalshi.ladders(self.client.orderbook(ticker))[side]

    def resolution(self, market_id):
        return kalshi.resolution(self.client.market(market_id))

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
        return pmus.scan(self.client, self.cfg)

    def asks(self, token_id):
        slug, side = pmus.split_token(token_id)
        return pmus.ladders(self.client.book(slug))[side]

    def resolution(self, market_id):
        return pmus.resolution(self.client.market(market_id), self.client.settlement(market_id))

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
