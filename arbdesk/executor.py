"""Executes approved proposals in paper or live mode.

Nothing runs without a human clicking "approve" in the panel (or the CLI), and
real-money proposals additionally need an explicit live confirmation.
This module never calls withdraw/transfer endpoints.
"""
from __future__ import annotations

import json
import logging

import ccxt

from . import risk
from .config import Settings
from .pm_live import LiveTradingError, PolymarketTrader
from .polymarket import PolymarketClient, book_side, taker_fee_per_share
from .storage import Store

log = logging.getLogger(__name__)


class Executor:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self._clients: dict[tuple[str, str, bool], ccxt.Exchange] = {}
        self.pm = PolymarketClient()
        self._pm_trader: PolymarketTrader | None = None

    @property
    def pm_trader(self) -> PolymarketTrader:
        if self._pm_trader is None:
            self._pm_trader = PolymarketTrader(self.settings.secrets, self.settings.polymarket)
        return self._pm_trader

    def halt(self, reason: str) -> None:
        """Kill switch: blocks all live execution until resumed."""
        self.store.set_flag("halt", reason)
        log.warning("LIVE TRADING HALTED: %s", reason)
        try:
            if self._pm_trader is not None:
                self._pm_trader.cancel_all()
        except Exception as e:
            log.warning("cancel_all failed: %s", e)

    def resume(self) -> None:
        self.store.set_flag("halt", "")

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

    def execute(self, proposal_id: int, confirm_live: bool = False) -> dict:
        p = self.store.proposal(proposal_id)
        if not p:
            raise ValueError("proposal not found")
        if p["status"] not in ("pending", "approved"):
            raise ValueError(f"proposal is {p['status']}")
        legs, errors = risk.validate(p["payload"], self.settings, self.store)
        if errors:
            self.store.set_proposal_status(proposal_id, "rejected", "; ".join(errors))
            return {"ok": False, "errors": errors}

        if risk.is_live(legs, self.settings) and not confirm_live:
            # Leave the proposal pending: the caller must ask the human to confirm real money.
            return {"ok": False, "needs_confirm": True,
                    "errors": ["real-money order: explicit live confirmation required"]}

        if legs[0]["exchange"] == "polymarket":
            if self.settings.polymarket.mode == "live":
                return self._execute_polymarket_live(proposal_id, legs)
            return self._execute_polymarket(proposal_id, legs)

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
        status = "executed" if not failures else ("partial" if fills else "failed")
        note = "; ".join(failures) or (f"realized pnl {pnl:.4f} USD" if pnl is not None else "position opened")
        self.store.set_proposal_status(proposal_id, status, note)
        if mode == "live" and failures and fills:
            self.halt(f"partial fill on proposal #{proposal_id}")
        return {"ok": not failures, "fills": fills, "errors": failures, "pnl_usd": pnl}

    def _execute_polymarket(self, proposal_id: int, legs: list[dict]) -> dict:
        """Paper only. All legs are priced against live books first; nothing is
        recorded unless every leg can fill within its limit price."""
        fills, failures = [], []
        for leg in legs:
            try:
                fills.append(self._pm_paper_fill(leg))
            except Exception as e:
                failures.append(f"{leg['label'] or leg['token_id']}: {type(e).__name__}: {e}")
        if failures:
            self.store.set_proposal_status(proposal_id, "failed", "; ".join(failures))
            return {"ok": False, "errors": failures}
        for f in fills:
            self.store.add_trade(proposal_id=proposal_id, mode="paper", pnl_usd=None, **f)
        cost = sum(f["cost_usd"] + f["fee_usd"] for f in fills)
        self.store.set_proposal_status(proposal_id, "executed",
                                       f"paper position opened, cost ${cost:.2f}; settles at resolution")
        return {"ok": True, "fills": fills, "errors": [], "pnl_usd": None}

    def _pm_book_check(self, leg: dict) -> tuple[float, float, float]:
        """Prices `leg` against the live book. Returns (avg, cost, depth within limit)."""
        cap = leg["limit_price"] + self.settings.polymarket.price_tolerance
        asks = book_side(self.pm.book(leg["token_id"]), "asks")
        need, cost = leg["shares"], 0.0
        for price, size in asks:
            if price > cap + 1e-9:
                break
            q = min(need, size)
            cost += q * price
            need -= q
            if need <= 1e-9:
                break
        if need > 1e-9:
            raise ValueError(f"not enough depth at <= {cap:.4f}")
        depth = sum(sz for p, sz in asks if p <= cap + 1e-9)
        return cost / leg["shares"], cost, depth

    def _pm_trade(self, leg: dict, avg: float, cost: float, order_id=None, extra=None) -> dict:
        fee = leg["shares"] * taker_fee_per_share(avg, self.settings.polymarket.taker_fee_rate)
        return {"exchange": "polymarket", "symbol": f"{leg['label'][:80]} [{leg['outcome']}]",
                "side": "buy", "amount": leg["shares"], "price": avg, "cost_usd": cost, "fee_usd": fee,
                "order_id": order_id,
                "raw": json.dumps({"market_id": leg["market_id"], "token_id": leg["token_id"],
                                   "outcome": leg["outcome"], **(extra or {})})}

    def _execute_polymarket_live(self, proposal_id: int, legs: list[dict]) -> dict:
        """Real money. Pre-flight everything, then send FOK orders thinnest-book first.

        A multi-leg bundle cannot be atomic on Polymarket. If a later leg fails after
        earlier ones filled, the proposal is marked 'partial' and the kill switch is
        engaged so a human reviews the unhedged position before anything else runs.
        """
        tol = self.settings.polymarket.price_tolerance
        try:
            trader = self.pm_trader
            trader.ensure_allowed()
            checks = [(leg, *self._pm_book_check(leg)) for leg in legs]
            need = sum(l["shares"] * (l["limit_price"] + tol) for l in legs) * 1.05  # + fee buffer
            balance = trader.collateral_balance()
            if balance < need:
                raise LiveTradingError(f"balance ${balance:.2f} < required ${need:.2f}")
        except Exception as e:
            note = f"pre-flight failed, nothing sent: {type(e).__name__}: {e}"
            self.store.set_proposal_status(proposal_id, "failed", note)
            return {"ok": False, "errors": [note]}

        checks.sort(key=lambda c: c[3] / c[0]["shares"])  # least spare depth first
        fills = []
        for leg, *_ in checks:
            try:
                f = trader.buy_fok(leg["token_id"], leg["shares"], leg["limit_price"])
            except Exception as e:
                err = f"{leg['label'] or leg['token_id']}: {type(e).__name__}: {e}"
                for done in fills:
                    self.store.add_trade(proposal_id=proposal_id, mode="live", pnl_usd=None, **done)
                if fills:
                    msg = (f"PARTIAL: {len(fills)}/{len(legs)} legs filled, then {err}. "
                           "Review open positions on polymarket.com")
                    self.store.set_proposal_status(proposal_id, "partial", msg)
                    self.halt(f"partial fill on proposal #{proposal_id}")
                else:
                    msg = f"first order not filled, no position taken: {err}"
                    self.store.set_proposal_status(proposal_id, "failed", msg)
                return {"ok": False, "errors": [msg], "fills": fills}
            leg = {**leg, "shares": f.shares}
            fills.append(self._pm_trade(leg, f.avg_price, f.cost_usd, f.order_id, f.raw))
        for done in fills:
            self.store.add_trade(proposal_id=proposal_id, mode="live", pnl_usd=None, **done)
        cost = sum(f["cost_usd"] for f in fills)
        self.store.set_proposal_status(
            proposal_id, "executed",
            f"LIVE: {len(fills)} order(s) filled, cost ${cost:.2f}. "
            "Winnings must be redeemed on polymarket.com after resolution.")
        return {"ok": True, "fills": fills, "errors": [], "pnl_usd": None}

    def _pm_paper_fill(self, leg: dict) -> dict:
        avg, cost, _ = self._pm_book_check(leg)
        return self._pm_trade(leg, avg, cost)

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
