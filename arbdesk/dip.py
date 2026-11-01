"""Dip-buy ladder: resting limit buys far below the market to catch crash wicks.

Each configured symbol gets a ladder of buy orders at fixed percentages below an
anchor price (e.g. -25%, -40%, -55%, $5 each). The anchor is the price when the
ladder is armed; if the market later rises by `rearm_pct`, the ladder is moved
up (paper: automatically; live: an alert asks you to re-arm). It never moves
down — that would be chasing a falling market.

Paper mode: every scan reads 1-minute candles since the last check; an order
fills at its limit price if a candle's low traded at or below it. That catches
short wicks a 5-minute price snapshot would miss.

Live mode: real GTC limit orders (spot, no leverage) through ccxt with exchange
keys from the vault. Arming requires typing LIVE; after that the orders rest
on the exchange without per-order approval, bounded by the budgets below.
Nothing here withdraws, transfers or sells.

Budgets: `max_open_usd` for all resting orders together, and
`quarterly_budget_usd` for filled + resting dip buys per calendar quarter.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class DipMarket:
    """Exchange access through ccxt. Public data without keys; orders only with keys."""

    def __init__(self, exchange: str, credentials: dict | None = None):
        import ccxt
        self.exchange_id = exchange
        params = {"enableRateLimit": True, "timeout": 15000, **(credentials or {})}
        self.ex = getattr(ccxt, exchange)(params)
        self._loaded = False

    def _load(self):
        if not self._loaded:
            self.ex.load_markets()
            self._loaded = True

    def price(self, symbol: str) -> float:
        t = self.ex.fetch_ticker(symbol)
        return float(t.get("last") or t.get("close"))

    def ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> list[list]:
        """[[ts_ms, open, high, low, close, volume], ...] oldest first."""
        return self.ex.fetch_ohlcv(symbol, timeframe, limit=limit)

    def low_since(self, symbol: str, since_ms: int) -> float | None:
        """Lowest traded price since `since_ms` from 1-minute candles."""
        candles = self.ex.fetch_ohlcv(symbol, "1m", since=since_ms, limit=300)
        lows = [c[3] for c in candles if c[0] >= since_ms - 60_000]
        return min(lows) if lows else None

    def limits(self, symbol: str) -> dict:
        self._load()
        m = self.ex.market(symbol)
        lim = m.get("limits") or {}
        return {"min_amount": (lim.get("amount") or {}).get("min") or 0,
                "min_cost": (lim.get("cost") or {}).get("min") or 0}

    def place(self, symbol: str, amount: float, price: float) -> str:
        self._load()
        amt = float(self.ex.amount_to_precision(symbol, amount))
        px = float(self.ex.price_to_precision(symbol, price))
        o = self.ex.create_order(symbol, "limit", "buy", amt, px, {"timeInForce": "GTC"})
        return str(o["id"])

    def order(self, order_id: str, symbol: str) -> dict:
        o = self.ex.fetch_order(order_id, symbol)
        return {"status": o.get("status"), "filled": float(o.get("filled") or 0),
                "average": float(o.get("average") or o.get("price") or 0),
                "fee": float((o.get("fee") or {}).get("cost") or 0)}

    def cancel(self, order_id: str, symbol: str) -> None:
        self.ex.cancel_order(order_id, symbol)


class DipManager:
    def __init__(self, settings, store, market: DipMarket | None = None, trade_market: DipMarket | None = None):
        self.settings = settings
        self.store = store
        self._market = market            # public data
        self._trade_market = trade_market  # authenticated (live only)
        self.sim_available = None          # set while a simulated-account session runs

    @property
    def cfg(self):
        return self.settings.dip

    @property
    def market(self) -> DipMarket:
        if self._market is None:
            self._market = DipMarket(self.cfg.exchange)
        return self._market

    @property
    def trade_market(self) -> DipMarket:
        if self._trade_market is None:
            creds = self.settings.secrets.exchange_keys.get(self.cfg.exchange)
            if not creds:
                raise RuntimeError(f"live dip ladder needs {self.cfg.exchange} API keys in the vault "
                                   f"(EXCHANGE_{self.cfg.exchange.upper()}_API_KEY / _SECRET)")
            self._trade_market = DipMarket(self.cfg.exchange, creds)
        return self._trade_market

    # ------------------------------------------------------------------
    def plan(self, symbol: str, anchor: float, levels: list[float], usd: float) -> list[dict]:
        return [{"symbol": symbol, "level_pct": lv, "anchor": anchor, "price": round(anchor * (1 + lv / 100), 8),
                 "usd": usd, "amount": usd / (anchor * (1 + lv / 100))} for lv in levels]

    def _budget_errors(self, new_usd: float, replacing_usd: float = 0.0) -> list[str]:
        c, errors = self.cfg, []
        open_usd = self.store.dip_open_usd(self.cfg.mode) - replacing_usd
        if open_usd + new_usd > c.max_open_usd:
            errors.append(f"resting orders ${open_usd + new_usd:.2f} > dip.max_open_usd ({c.max_open_usd})")
        used = self.store.dip_filled_usd_quarter(self.cfg.mode)
        if used + open_usd + new_usd > c.quarterly_budget_usd:
            errors.append(f"filled ${used:.2f} + resting ${open_usd + new_usd:.2f} "
                          f"> dip.quarterly_budget_usd ({c.quarterly_budget_usd})")
        return errors

    def arm(self, confirm_live: bool = False, symbols: list[str] | None = None) -> dict:
        """(Re)place the ladder at the current price. Live mode needs explicit confirmation."""
        c = self.cfg
        if c.mode == "live":
            if not confirm_live:
                return {"ok": False, "needs_confirm": True,
                        "errors": ["live dip ladder: resting real-money orders need explicit LIVE confirmation"]}
            if self.store.halted():
                return {"ok": False, "errors": [f"live trading halted: {self.store.halted()}"]}
        results, errors = {}, []
        for ladder in c.ladders:
            sym = ladder["symbol"]
            if symbols and sym not in symbols:
                continue
            try:
                anchor = self.market.price(sym)
                self.store.add_sample(f"dip_price:{sym}", anchor)
            except Exception as e:
                errors.append(f"{sym}: price unavailable ({type(e).__name__})")
                continue
            plan = self.plan(sym, anchor, ladder["levels_pct"], float(ladder["usd_per_level"]))
            existing = sum(o["usd"] for o in self.store.dip_orders(status="open", symbol=sym, mode=c.mode))
            # Check before touching the current ladder, so a refusal leaves it in place.
            if (errs := self._budget_errors(sum(p["usd"] for p in plan), replacing_usd=existing)):
                errors += [f"{sym}: {e}" for e in errs]
                continue
            if c.mode == "paper" and self.sim_available is not None:
                avail = self.sim_available()  # resting orders lock cash, like on an exchange
                if avail is not None and avail + existing < sum(p["usd"] for p in plan):
                    errors.append(f"{sym}: 模拟账户可用资金不足（可用 ${avail + existing:.2f}）")
                    continue
            if c.mode == "live":
                lim = self.trade_market.limits(sym)
                small = [p for p in plan if p["amount"] < lim["min_amount"] or p["usd"] < lim["min_cost"]]
                if small:
                    errors.append(f"{sym}: ${plan[0]['usd']} per level is below {c.exchange}'s minimum order "
                                  f"(amount {lim['min_amount']}, cost {lim['min_cost']})")
                    continue
            self.cancel_symbol(sym)  # replace, never stack
            placed = []
            for p in plan:
                oid = None
                if c.mode == "live":
                    try:
                        oid = self.trade_market.place(sym, p["amount"], p["price"])
                    except Exception as e:
                        errors.append(f"{sym} {p['level_pct']}%: {type(e).__name__}: {e}")
                        continue
                self.store.add_dip_order(mode=c.mode, exchange=c.exchange, exchange_order_id=oid, **p)
                placed.append(p["level_pct"])
            self.store.set_flag(f"dip_checked:{sym}", str(now_ms()))
            results[sym] = {"anchor": anchor, "levels": placed}
        return {"ok": not errors, "placed": results, "errors": errors}

    def cancel_symbol(self, symbol: str) -> int:
        n = 0
        for o in self.store.dip_orders(status="open", symbol=symbol):
            if o["mode"] == "live" and o["exchange_order_id"]:
                try:
                    self.trade_market.cancel(o["exchange_order_id"], symbol)
                except Exception as e:
                    log.warning("cancel %s failed: %s", o["exchange_order_id"], e)
                    continue  # keep it open in our records; it may still be live on the exchange
            self.store.set_dip_status(o["id"], "cancelled")
            n += 1
        return n

    def cancel_all(self) -> int:
        return sum(self.cancel_symbol(l["symbol"]) for l in self.cfg.ladders)

    # ------------------------------------------------------------------
    def cycle(self) -> dict:
        """Called on every scan: detect fills, keep the ladder near the market, obey the kill switch."""
        c = self.cfg
        out = {"filled": 0, "rearmed": [], "errors": {}}
        halt = self.store.halted()
        if c.mode == "live" and halt:
            auto = halt.startswith("自动停止")
            if not (auto and c.keep_orders_on_auto_halt):
                out["cancelled"] = self.cancel_all()
                return out
        for ladder in c.ladders:
            sym = ladder["symbol"]
            open_orders = self.store.dip_orders(status="open", symbol=sym)
            if not open_orders:
                # Paper mode arms itself (and re-arms once every level has filled), within budget.
                if c.mode == "paper" and c.auto_arm_paper and not halt:
                    res = self.arm(symbols=[sym])
                    if res["placed"]:
                        out["rearmed"].append(sym)
                    elif res["errors"]:
                        out["errors"][sym] = "; ".join(res["errors"])
                continue
            try:
                out["filled"] += self._paper_fills(sym, open_orders) if c.mode == "paper" \
                    else self._live_fills(sym, open_orders)
                price = self.market.price(sym)
            except Exception as e:
                out["errors"][sym] = f"{type(e).__name__}: {str(e)[:150]}"
                continue
            self.store.add_sample(f"dip_price:{sym}", price)
            anchor = max(o["anchor"] for o in open_orders)
            if price >= anchor * (1 + c.rearm_pct / 100):
                if c.mode == "paper" and not halt:
                    self.arm(symbols=[sym])
                    out["rearmed"].append(sym)
                elif not self.store.alert_recent(f"dip_rearm:{sym}", hours=24, level="info"):
                    self.store.add_alert(f"dip_rearm:{sym}", "info", f"{sym} 已比挂单基准价高 {(price / anchor - 1) * 100:.0f}%",
                                         "抄底挂单离现价越来越远；需要的话在面板里重新布置（实盘需要输入 LIVE 确认）。")
        return out

    def _paper_fills(self, sym: str, open_orders: list[dict]) -> int:
        flag = self.store.get_flag(f"dip_checked:{sym}")
        since = int(flag["value"]) if flag and flag["value"] else now_ms() - 10 * 60_000
        low = self.market.low_since(sym, since)
        self.store.set_flag(f"dip_checked:{sym}", str(now_ms()))
        if low is None:
            return 0
        n = 0
        for o in open_orders:
            if low <= o["price"]:  # a limit buy fills at its own price when the market trades through it
                self._record_fill(o, o["amount"], o["price"], o["usd"] * self.cfg.maker_fee_pct / 100, low=low)
                n += 1
        return n

    def _live_fills(self, sym: str, open_orders: list[dict]) -> int:
        n = 0
        for o in open_orders:
            st = self.trade_market.order(o["exchange_order_id"], sym)
            if st["status"] == "closed" and st["filled"] > 0:
                self._record_fill(o, st["filled"], st["average"] or o["price"], st["fee"])
                n += 1
            elif st["status"] in ("canceled", "cancelled", "expired", "rejected"):
                self.store.set_dip_status(o["id"], "cancelled")
        return n

    def _record_fill(self, o: dict, amount: float, price: float, fee: float, low: float | None = None) -> None:
        self.store.set_dip_status(o["id"], "filled", fill_price=price)
        self.store.add_trade(proposal_id=None, mode=o["mode"], exchange=o["exchange"], symbol=o["symbol"],
                             side="buy", amount=amount, price=price, cost_usd=amount * price, fee_usd=fee,
                             pnl_usd=None, order_id=o["exchange_order_id"],
                             raw=json.dumps({"kind": "dip", "level_pct": o["level_pct"], "anchor": o["anchor"],
                                             "candle_low": low}))
        self.store.add_alert(f"dip_fill:{o['id']}", "warning", f"抄底挂单成交：{o['symbol']} {o['level_pct']}%",
                             f"{'模拟' if o['mode'] == 'paper' else '实盘'}买入 {amount:.6f} @ {price:,.2f}"
                             f"（基准 {o['anchor']:,.2f}）。持有，不自动卖出。")

    # ------------------------------------------------------------------
    def status(self) -> dict:
        rows = []
        for ladder in self.cfg.ladders:
            sym = ladder["symbol"]
            last = self.store.latest_sample(f"dip_price:{sym}")
            fills = self.store.dip_orders(status="filled", symbol=sym, mode=self.cfg.mode)
            qty = sum(f["amount"] for f in fills)
            cost = sum(f["amount"] * (f["fill_price"] or f["price"]) for f in fills)
            rows.append({"symbol": sym, "price": last, "levels_pct": ladder["levels_pct"],
                         "usd_per_level": ladder["usd_per_level"],
                         "open": self.store.dip_orders(status="open", symbol=sym, mode=self.cfg.mode),
                         "filled": fills, "position_qty": qty, "position_cost": cost,
                         "unrealized": (qty * last - cost) if last and qty else None})
        return {"enabled": self.cfg.enabled, "mode": self.cfg.mode, "exchange": self.cfg.exchange,
                "open_usd": self.store.dip_open_usd(self.cfg.mode),
                "filled_quarter_usd": self.store.dip_filled_usd_quarter(self.cfg.mode),
                "max_open_usd": self.cfg.max_open_usd, "quarterly_budget_usd": self.cfg.quarterly_budget_usd,
                "symbols": rows}
