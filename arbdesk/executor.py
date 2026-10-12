"""Executes approved proposals in paper or live mode.

Nothing runs without a human clicking "approve" in the panel (or the CLI).
This module never calls withdraw/transfer endpoints.
"""
from __future__ import annotations

import json
import logging

import ccxt

from . import risk
from .config import Settings
from .storage import Store

log = logging.getLogger(__name__)


class Executor:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self._clients: dict[tuple[str, str, bool], ccxt.Exchange] = {}

    def client(self, ex_id: str, market: str, authed: bool) -> ccxt.Exchange:
        key = (ex_id, market, authed)
        if key not in self._clients:
            params = {"enableRateLimit": True, "timeout": 15000, "options": {"defaultType": market}}
            if authed:
                params.update(self.settings.secrets.exchange_keys[ex_id])
            c = getattr(ccxt, ex_id)(params)
            c.load_markets()
            self._clients[key] = c
        return self._clients[key]

    @staticmethod
    def market_symbol(leg: dict) -> str:
        sym = leg["symbol"]
        return f"{sym}:{sym.split('/')[1]}" if leg["market"] == "swap" else sym

    def quote(self, leg: dict) -> dict:
        t = self.client(leg["exchange"], leg["market"], False).fetch_ticker(self.market_symbol(leg))
        return {"bid": t["bid"], "ask": t["ask"]}

    def execute(self, proposal_id: int) -> dict:
        p = self.store.proposal(proposal_id)
        if not p:
            raise ValueError("proposal not found")
        if p["status"] not in ("pending", "approved"):
            raise ValueError(f"proposal is {p['status']}")
        legs, errors = risk.validate(p["payload"], self.settings, self.store)
        if errors:
            self.store.set_proposal_status(proposal_id, "rejected", "; ".join(errors))
            return {"ok": False, "errors": errors}

        mode = self.settings.risk.mode
        fills, failures = [], []
        for leg in legs:
            try:
                fill = self._paper_fill(leg) if mode == "paper" else self._live_fill(leg)
                fills.append(fill)
            except Exception as e:
                log.exception("leg failed")
                failures.append(f"{leg['exchange']} {leg['symbol']}: {type(e).__name__}: {e}")
                break  # do not continue a multi-leg trade after a failed leg

        pnl = self._offsetting_pnl(fills) if not failures else None
        for i, f in enumerate(fills):
            self.store.add_trade(proposal_id=proposal_id, mode=mode,
                                 pnl_usd=pnl if i == 0 else None, **f)
        status = "executed" if not failures else "failed"
        note = "; ".join(failures) or (f"realized pnl {pnl:.4f} USD" if pnl is not None else "position opened")
        self.store.set_proposal_status(proposal_id, status, note)
        return {"ok": not failures, "fills": fills, "errors": failures, "pnl_usd": pnl}

    def _paper_fill(self, leg: dict) -> dict:
        q = self.quote(leg)
        price = q["ask"] if leg["side"] == "buy" else q["bid"]
        amount = leg["usd"] / price
        fee = leg["usd"] * self.settings.scan.taker_fee_pct / 100
        return {"exchange": leg["exchange"], "symbol": self.market_symbol(leg), "side": leg["side"],
                "amount": amount, "price": price, "cost_usd": amount * price, "fee_usd": fee,
                "order_id": None, "raw": None}

    def _live_fill(self, leg: dict) -> dict:
        c = self.client(leg["exchange"], leg["market"], True)
        sym = self.market_symbol(leg)
        q = self.quote(leg)
        slip = self.settings.risk.max_slippage_pct / 100
        # Marketable limit order: never fills worse than the slippage cap.
        ref = q["ask"] if leg["side"] == "buy" else q["bid"]
        limit = ref * (1 + slip) if leg["side"] == "buy" else ref * (1 - slip)
        amount = float(c.amount_to_precision(sym, leg["usd"] / ref))
        limits = c.market(sym).get("limits", {})
        min_amt = (limits.get("amount") or {}).get("min") or 0
        min_cost = (limits.get("cost") or {}).get("min") or 0
        if amount < min_amt or amount * ref < min_cost:
            raise ValueError(f"below exchange minimum (amount {min_amt}, cost {min_cost})")
        if leg["market"] == "swap" and c.has.get("setLeverage"):
            try:
                c.set_leverage(1, sym)
            except Exception as e:
                log.warning("set_leverage failed: %s", e)
        order = c.create_order(sym, "limit", leg["side"], amount, float(c.price_to_precision(sym, limit)))
        filled = order.get("filled") or 0
        avg = order.get("average") or order.get("price") or limit
        fee = (order.get("fee") or {}).get("cost") or filled * avg * self.settings.scan.taker_fee_pct / 100
        return {"exchange": leg["exchange"], "symbol": sym, "side": leg["side"],
                "amount": filled or amount, "price": avg, "cost_usd": (filled or amount) * avg,
                "fee_usd": float(fee), "order_id": order.get("id"),
                "raw": json.dumps({k: order.get(k) for k in ("id", "status", "filled", "remaining")})}

    @staticmethod
    def _offsetting_pnl(fills: list[dict]) -> float | None:
        """Realized PnL when legs fully offset (e.g. buy on A + sell on B of the same coin)."""
        bases = {f["symbol"].split("/")[0] for f in fills}
        sides = {f["side"] for f in fills}
        if any(":" in f["symbol"] for f in fills):
            return None  # spot/perp hedge: opening, not a realized round trip
        if len(fills) < 2 or len(bases) != 1 or sides != {"buy", "sell"}:
            return None
        bought = sum(f["amount"] for f in fills if f["side"] == "buy")
        sold = sum(f["amount"] for f in fills if f["side"] == "sell")
        qty = min(bought, sold)
        if qty <= 0 or abs(bought - sold) / max(bought, sold) > 0.05:
            return None
        buy_px = sum(f["cost_usd"] for f in fills if f["side"] == "buy") / bought
        sell_px = sum(f["cost_usd"] for f in fills if f["side"] == "sell") / sold
        return qty * (sell_px - buy_px) - sum(f["fee_usd"] for f in fills)
