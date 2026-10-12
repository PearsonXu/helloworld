"""Real-money Polymarket order placement (CLOB V2).

Uses the official `py-clob-client-v2` SDK (install: pip install -r requirements-live.txt).
The legacy `py-clob-client` stopped working at the April 2026 V2 cutover.

Safety properties:
  * every order is FOK (fill-or-kill): it fills completely at <= the limit price
    or not at all, so nothing is left resting on the book;
  * the jurisdiction check (polymarket.com/api/geoblock) runs before any order and
    fails closed: if it can't be confirmed, no order is sent;
  * the private key comes only from the encrypted vault and never leaves this
    process except as order signatures (the SDK signs locally);
  * no withdraw / transfer / approval transactions are ever sent from here.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import requests

from .config import Secrets
from .polymarket import CLOB, PolymarketConfig

log = logging.getLogger(__name__)

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
POLYGON = 137
COLLATERAL_DECIMALS = 6  # pUSD


class LiveTradingError(Exception):
    pass


@dataclass
class LiveFill:
    order_id: str
    status: str
    shares: float
    avg_price: float
    cost_usd: float
    raw: dict


def check_geoblock(session: requests.Session | None = None) -> dict:
    """Raises unless Polymarket confirms this IP may place orders."""
    s = session or requests
    try:
        r = s.get(GEOBLOCK_URL, timeout=10)
        r.raise_for_status()
        info = r.json()
    except Exception as e:
        raise LiveTradingError(f"could not verify jurisdiction ({type(e).__name__}); not trading") from None
    if info.get("blocked") is not False:
        raise LiveTradingError(
            f"Polymarket reports trading is restricted here (country={info.get('country')}, "
            f"region={info.get('region')}); not trading")
    return info


def round_down(x: float, step: float) -> float:
    return math.floor(x / step + 1e-9) * step


class PolymarketTrader:
    def __init__(self, secrets: Secrets, cfg: PolymarketConfig, client=None, session=None):
        if not secrets.pm_private_key:
            raise LiveTradingError("no Polymarket key in the vault "
                                   "(python -m arbdesk vault set POLYMARKET_PRIVATE_KEY)")
        if secrets.pm_signature_type in (1, 2) and not secrets.pm_funder:
            raise LiveTradingError("POLYMARKET_FUNDER (your polymarket.com wallet address) is required "
                                   "for proxy/Safe wallets")
        self.secrets = secrets
        self.cfg = cfg
        self.session = session
        self._client = client
        self._geo_ok = False

    # ------------------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            try:
                from py_clob_client_v2 import ClobClient
            except ImportError:
                raise LiveTradingError("live trading needs: pip install -r requirements-live.txt") from None
            s = self.secrets
            c = ClobClient(host=CLOB, chain_id=POLYGON, key=s.pm_private_key,
                           signature_type=s.pm_signature_type, funder=s.pm_funder or None)
            # L2 API credentials are derived deterministically from the key: nothing to store.
            c.set_api_creds(c.create_or_derive_api_key())
            self._client = c
        return self._client

    def ensure_allowed(self) -> dict:
        info = check_geoblock(self.session)
        self._geo_ok = True
        return info

    def collateral_balance(self) -> float:
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams
        resp = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return int(resp.get("balance", 0)) / 10 ** COLLATERAL_DECIMALS

    def readiness(self) -> dict:
        """Everything short of placing an order: jurisdiction, auth, balance."""
        geo = self.ensure_allowed()
        return {"address": self.client.get_address(), "funder": self.secrets.pm_funder,
                "signature_type": self.secrets.pm_signature_type,
                "country": geo.get("country"), "collateral_balance": self.collateral_balance()}

    # ------------------------------------------------------------------
    def buy_fok(self, token_id: str, shares: float, limit_price: float, neg_risk: bool | None = None) -> LiveFill:
        """Buy `shares` at <= limit_price + tolerance, all or nothing."""
        if not self._geo_ok:
            self.ensure_allowed()
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions

        c = self.client
        tick = float(c.get_tick_size(token_id))
        price = round_down(min(limit_price + self.cfg.price_tolerance, 1 - tick), tick)
        size = round_down(shares, 0.01)
        if price <= 0 or size <= 0:
            raise LiveTradingError(f"invalid order: price={price} size={size}")
        args = OrderArgs(token_id=token_id, price=price, size=size, side="BUY")
        opts = PartialCreateOrderOptions(neg_risk=neg_risk) if neg_risk is not None else None
        resp = c.create_and_post_order(args, opts, OrderType.FOK)
        if not isinstance(resp, dict) or not resp.get("success", False) or resp.get("errorMsg"):
            raise LiveTradingError(f"order rejected: {(resp or {}).get('errorMsg') or resp}")
        order_id = resp.get("orderID") or resp.get("orderId") or ""
        # The order record is the source of truth for what actually filled.
        order = (c.get_order(order_id) if order_id else None) or {}
        order = order.get("order", order) if isinstance(order, dict) else {}
        filled = float(order.get("size_matched") or resp.get("takingAmount") or 0)
        if filled + 1e-6 < size:
            raise LiveTradingError(f"FOK order {order_id} not fully filled ({filled}/{size}); "
                                   f"status={order.get('status') or resp.get('status')}")
        # Prefer the reported collateral spent; fall back to the limit price (an upper bound)
        # if the field is missing or not in plain dollar units.
        try:
            making = float(resp.get("makingAmount") or 0)
        except (TypeError, ValueError):
            making = 0.0
        cost = making if 0 < making <= filled * price * 1.001 else filled * price
        return LiveFill(order_id=order_id, status=str(order.get("status") or resp.get("status")),
                        shares=filled, avg_price=cost / filled, cost_usd=cost,
                        raw={"order_id": order_id, "status": resp.get("status"),
                             "tx": resp.get("transactionsHashes")})

    def cancel_all(self) -> None:
        if self._client is not None:
            self._client.cancel_all()
