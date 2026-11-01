"""Strategy lab: classic BTC/ETH spot strategies, paper-traded on live candles.

Each (strategy, symbol) pair has its own virtual wallet, separate from the
simulated account, so strategies can be compared on equal terms. Long-only
spot, no leverage, all-in / all-out (DCA only buys). Paper only: nothing here
places orders on an exchange.

Strategies (signals use CLOSED candles only, so there's no look-ahead):
  dca        buy a fixed amount every N days, never sell (benchmark for "do nothing clever")
  sma_cross  hold while the fast moving average is above the slow one (trend following)
  donchian   enter on a close above the N-day high, exit below the M-day low (breakout)
  rsi        buy when RSI < low threshold, sell when RSI > high threshold (mean reversion)
  trend_fast fast trend on 6h bars with crash protection (trendfast.py)
  custom*    user-named variants: params carry "base" (the engine, e.g. trend_fast), "label",
             optional "symbols"; e.g. custom1 = 定制策略1 = trend_fast + LPPLS on BTC and ETH

The same exposure function drives both the live paper trading and a backtest
over recent history, so the backtest is an honest preview of the live logic.
Every result is compared with simple buy-and-hold over the same period.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

TF_SECONDS = {"1h": 3600, "2h": 7200, "6h": 21600, "1d": 86400}
LABELS = {"dca": "定投", "sma_cross": "均线交叉（趋势）", "donchian": "突破（唐奇安通道）", "rsi": "RSI 均值回归",
          "trend_fast": "快速趋势+风控"}


def engine(name: str, p: dict) -> str:
    """The rule set a strategy runs: its own name, or the "base" of a custom variant."""
    return p.get("base", name)


def label(name: str, p: dict | None) -> str:
    return (p or {}).get("label") or LABELS.get(name, name)


# --------------------------------------------------------------------------
# Indicators and exposure rules (pure)
# --------------------------------------------------------------------------

def sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder's RSI of the last value."""
    if len(closes) <= n:
        return None
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def exposure_series(name: str, candles: list[list], p: dict) -> list[int]:
    """Desired position (1 = long, 0 = flat) after each closed candle. Stateful rules are
    replayed from the start, so live and backtest agree exactly."""
    closes = [c[4] for c in candles]
    highs, lows = [c[2] for c in candles], [c[3] for c in candles]
    out, pos = [], 0
    for i in range(len(candles)):
        if name == "sma_cross":
            f, s = sma(closes[: i + 1], p["fast"]), sma(closes[: i + 1], p["slow"])
            pos = 1 if f is not None and s is not None and f > s else 0
        elif name == "donchian":
            n, m = p["entry"], p["exit"]
            if i >= n and closes[i] > max(highs[i - n:i]):
                pos = 1
            elif i >= m and closes[i] < min(lows[i - m:i]):
                pos = 0
        elif name == "rsi":
            r = rsi(closes[: i + 1], p["period"])
            if r is not None:
                if pos == 0 and r < p["buy_below"]:
                    pos = 1
                elif pos == 1 and r > p["sell_above"]:
                    pos = 0
        out.append(pos)
    return out


def closed_candles(candles: list[list], tf: str, now_ms: int) -> list[list]:
    """Drop the candle that is still forming."""
    return [c for c in candles if c[0] + TF_SECONDS[tf] * 1000 <= now_ms]


def backtest(name: str, candles: list[list], p: dict, capital: float, fee_pct: float,
             slip_pct: float) -> dict:
    """All-in/all-out at the close of the signal candle (DCA: periodic buys), with costs."""
    if len(candles) < 2:
        return {}
    if engine(name, p) == "trend_fast":
        from .trendfast import simulate
        return simulate(candles, p, capital, fee_pct, slip_pct)["stats"]
    cost = (fee_pct + slip_pct) / 100
    cash, qty, trades, peak, mdd = capital, 0.0, 0, capital, 0.0
    if name == "dca":
        every = max(1, int(p["every_days"] * 86400 // TF_SECONDS[p["timeframe"]]))
        amount = p["amount_usd"]
        for i, c in enumerate(candles):
            if i % every == 0 and cash >= amount:
                qty += amount * (1 - cost) / c[4]
                cash -= amount
                trades += 1
            eq = cash + qty * c[4]
            peak, mdd = max(peak, eq), max(mdd, (peak - eq) / peak)
    else:
        exp = exposure_series(name, candles, p)
        for c, e in zip(candles, exp):
            px = c[4]
            if e == 1 and qty == 0 and cash > 0:
                qty, cash, trades = cash * (1 - cost) / px, 0.0, trades + 1
            elif e == 0 and qty > 0:
                cash, qty, trades = qty * px * (1 - cost), 0.0, trades + 1
            eq = cash + qty * px
            peak, mdd = max(peak, eq), max(mdd, (peak - eq) / peak)
    end = cash + qty * candles[-1][4]
    hold = candles[-1][4] / candles[0][4] * (1 - cost) - 1
    return {"from": candles[0][0], "to": candles[-1][0], "candles": len(candles),
            "return_pct": round((end / capital - 1) * 100, 2), "buy_hold_pct": round(hold * 100, 2),
            "trades": trades, "max_drawdown_pct": round(mdd * 100, 2)}


# --------------------------------------------------------------------------
# Live paper engine
# --------------------------------------------------------------------------

class StrategyLab:
    def __init__(self, settings, store, market=None):
        self.settings = settings
        self.store = store
        self._market = market

    @property
    def cfg(self):
        return self.settings.strategies

    @property
    def market(self):
        if self._market is None:
            from .dip import DipMarket
            self._market = DipMarket(self.cfg.exchange)
        return self._market

    def params(self, name: str) -> dict:
        return self.cfg.params[name]

    def enabled(self) -> list[str]:
        """Listed strategies plus custom variants (params with a "base") that aren't switched off."""
        extra = [n for n, p in self.cfg.params.items()
                 if p.get("base") and p.get("enabled", True) and n not in self.cfg.enabled]
        return [n for n in self.cfg.enabled if n in self.cfg.params] + extra

    def _candles(self, symbol: str, tf: str, cache: dict, limit: int = 300) -> list[list]:
        key = (symbol, tf, limit)
        if key not in cache:  # one fetch per symbol/timeframe per tick
            cache[key] = self.market.ohlcv(symbol, tf, limit)
        return cache[key]

    def tick(self, now_ms: int | None = None) -> dict:
        now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
        cache, prices, out = {}, {}, {"trades": [], "errors": {}}
        cost = (self.cfg.fee_pct + self.cfg.slippage_pct) / 100
        for name in self.enabled():
            p = self.params(name)
            for sym in p.get("symbols") or self.cfg.symbols:
                try:
                    candles = closed_candles(self._candles(sym, p["timeframe"], cache, p.get("history_bars", 300)),
                                             p["timeframe"], now_ms)
                    if len(candles) < 2:
                        continue
                    st = self.store.strat_state(name, sym)
                    if sym not in prices:  # one price request per symbol per tick
                        prices[sym] = self.market.price(sym)
                        self.store.add_sample(f"strat_px:{sym}", prices[sym])
                    price = prices[sym]
                    if st is None:
                        st = self._start(name, sym, candles, price, now_ms)
                    risk = None
                    if engine(name, p) == "trend_fast" and p.get("use_lppls"):
                        try:
                            risk = self._lppls_risk(name, sym, p, candles[0][0], cache, now_ms)
                        except Exception as e:  # the filter fails open: the strategy's own stops still apply
                            out["errors"][f"lppls:{sym}"] = f"{type(e).__name__}: {str(e)[:150]}"
                    trade = self._step(name, sym, st, candles, price, cost, now_ms, risk)
                    if trade:
                        out["trades"].append(trade)
                    st = self.store.strat_state(name, sym)
                    eq = st["cash"] + st["qty"] * price
                    self.store.update_strat(name, sym, peak=max(st["peak"], eq),
                                            max_dd=max(st["max_dd"], (max(st["peak"], eq) - eq) / max(st["peak"], eq)))
                    self.store.add_sample(f"strat_eq:{name}:{sym}", eq)
                except Exception as e:
                    out["errors"][f"{name}:{sym}"] = f"{type(e).__name__}: {str(e)[:150]}"
        return out

    def _start(self, name, sym, candles, price, now_ms) -> dict:
        p = self.params(name)
        bt = backtest(name, candles, p, self.cfg.capital_per_strategy, self.cfg.fee_pct, self.cfg.slippage_pct)
        # Mark the previous candle as processed, so the first tick takes the strategy's CURRENT
        # position (e.g. already in an uptrend -> buy now) instead of waiting a full candle.
        self.store.add_strat(name, sym, self.cfg.capital_per_strategy, price, candles[-2][0],
                             json.dumps(bt))
        return self.store.strat_state(name, sym)

    def _step(self, name, sym, st, candles, price, cost, now_ms, risk=None) -> dict | None:
        p = self.params(name)
        last = candles[-1][0]
        if name == "dca":
            due = st["last_action_ms"] is None or now_ms - st["last_action_ms"] >= p["every_days"] * 86_400_000
            if due and st["cash"] >= p["amount_usd"] * 0.999:
                usd = min(p["amount_usd"], st["cash"])
                return self._fill(name, sym, "buy", usd * (1 - cost) / price, price, usd, now_ms, last)
            return None
        if engine(name, p) == "trend_fast":
            return self._step_trend(name, sym, st, candles, price, cost, now_ms, risk)
        if last <= st["last_candle_ts"]:
            return None  # act once per newly closed candle
        want = exposure_series(name, candles, p)[-1]
        self.store.update_strat(name, sym, last_candle_ts=last)
        if want == 1 and st["qty"] == 0 and st["cash"] > 0:
            usd = st["cash"]
            return self._fill(name, sym, "buy", usd * (1 - cost) / price, price, usd, now_ms, last)
        if want == 0 and st["qty"] > 0:
            return self._fill(name, sym, "sell", st["qty"], price, st["qty"] * price * (1 - cost), now_ms, last)
        return None

    # LPPLS bubble filter ------------------------------------------------------
    def _lppls_risk(self, name: str, sym: str, p: dict, first_bar_ms: int, cache: dict, now_ms: int) -> dict[int, float]:
        """{day_ms: bubble confidence} covering the trend backtest, from daily candles.
        Cached in the flags table; after the first run only newly closed days are fitted."""
        from . import lppls
        windows = sorted(p["lppls_windows"])
        need_from = (first_bar_ms // lppls.DAY_MS - 1) * lppls.DAY_MS
        days_needed = (now_ms - need_from) // lppls.DAY_MS + max(windows) + 5
        daily = closed_candles(self._candles(sym, "1d", cache, int(days_needed)), "1d", now_ms)
        daily = [(c[0], c[4]) for c in daily]
        key = f"lppls:{sym}"
        flag = self.store.get_flag(key)
        cached = {}
        if flag:
            data = json.loads(flag["value"])
            if data.get("cfg") == [windows, p["lppls_min_r2"]]:  # settings changed -> refit everything
                cached = {int(d): v for d, v in data["days"].items()}
        series = lppls.update_series(cached, daily, windows, p["lppls_min_r2"], need_from)
        if series.keys() != cached.keys():
            self.store.set_flag(key, json.dumps({"cfg": [windows, p["lppls_min_r2"]],
                                                 "days": {str(d): v for d, v in sorted(series.items())}}))
            self._lppls_alert(name, sym, series, p)
        return {d: v[0] for d, v in series.items()}

    def _lppls_alert(self, name: str, sym: str, series: dict, p: dict) -> None:
        if not series:
            return
        day = max(series)
        bubble, _, tc = series[day]
        if bubble < p["lppls_tighten"]:
            return
        if bubble >= p["lppls_exit"]:
            action = "已离场，不开新仓。"
        elif bubble >= p["lppls_block"]:
            action = "暂停开新仓，持仓的移动止损收紧。"
        else:
            action = "持仓的移动止损收紧（仍可开仓）。"
        key = f"lppls:{sym}"
        level = "warning"  # a paper-strategy signal, not a market emergency (critical can halt live trading)
        if self.store.alert_recent(key, hours=24, level=level):
            return
        when = f"，模型估计的临界点约在 {tc:.0f} 天后（误差很大，仅供参考）" if tc is not None else ""
        self.store.add_alert(
            key, level, f"{sym} LPPLS 泡沫置信度 {bubble:.0%}",
            f"日线价格呈“加速上涨 + 振荡收敛”的泡沫形态{when}。{label(name, p)}："
            + action + "LPPLS 误报很多，它只用来减少风险，不用来做空。")

    def lppls_status(self) -> dict:
        out = {}
        syms = dict.fromkeys(s for n in self.enabled() for s in (self.params(n).get("symbols") or self.cfg.symbols))
        for sym in syms:
            flag = self.store.get_flag(f"lppls:{sym}")
            if not flag:
                continue
            days = json.loads(flag["value"])["days"]
            if not days:
                continue
            d = max(days, key=int)
            bubble, anti, tc = days[d]
            out[sym] = {"day": int(d), "bubble": bubble, "anti": anti, "tc_days": tc,
                        "max_30d": max(v[0] for k, v in days.items() if int(k) >= int(d) - 30 * 86_400_000)}
        return out

    def _step_trend(self, name, sym, st, candles, price, cost, now_ms, risk=None) -> dict | None:
        """Fast trend: stops are checked against the live price on every scan; entries, exits
        and stop updates follow the deterministic replay at each newly closed candle."""
        from .trendfast import simulate
        p = self.params(name)
        if st["qty"] > 0 and st["stop"] and price <= st["stop"]:
            t = self._fill(name, sym, "sell", st["qty"], price, st["qty"] * price * (1 - cost), now_ms,
                           st["last_candle_ts"])
            self.store.update_strat(name, sym, stop=None, note=f"实时止损 @ {price:,.2f}")
            return t
        last = candles[-1][0]
        if last <= st["last_candle_ts"]:
            return None
        args = (candles, p, self.cfg.capital_per_strategy, self.cfg.fee_pct, self.cfg.slippage_pct)
        sim = simulate(*args, risk=risk)
        bt = dict(sim["stats"])
        if risk:
            plain = simulate(*args)["stats"]
            bt["without_lppls"] = {k: plain[k] for k in ("return_pct", "max_drawdown_pct", "trades", "win_rate_pct")}
            bt["bubble_now"] = sim["bubble"]
        note = sim["note"] or ""
        if sim["cooldown_left_bars"] and not sim["in_position"]:
            note = (note + " · " if note else "") + f"冷却中，约 {sim['cooldown_left_bars'] * 6} 小时后可再入场"
        self.store.update_strat(name, sym, last_candle_ts=last, note=note, backtest=json.dumps(bt),
                                stop=sim["stop"] if sim["in_position"] else None)
        if sim["in_position"] and st["qty"] == 0 and st["cash"] > 0:
            frac = next((t["frac"] for t in reversed(sim["trades"]) if t["side"] == "buy"), 0.0)
            usd = st["cash"] * frac
            if usd <= 0:
                return None
            return self._fill(name, sym, "buy", usd * (1 - cost) / price, price, usd, now_ms, last)
        if not sim["in_position"] and st["qty"] > 0:
            return self._fill(name, sym, "sell", st["qty"], price, st["qty"] * price * (1 - cost), now_ms, last)
        return None

    def _fill(self, name, sym, side, qty, price, usd, now_ms, candle_ts) -> dict:
        st = self.store.strat_state(name, sym)
        if side == "buy":
            cash, new_qty = st["cash"] - usd, st["qty"] + qty
        else:
            cash, new_qty = st["cash"] + usd, 0.0
        self.store.update_strat(name, sym, cash=cash, qty=new_qty, trades=st["trades"] + 1,
                                last_action_ms=now_ms, last_candle_ts=max(st["last_candle_ts"], candle_ts))
        self.store.add_strat_trade(name, sym, side, qty, price, usd)
        return {"strategy": name, "symbol": sym, "side": side, "qty": qty, "price": price, "usd": usd}

    def reset(self) -> None:
        self.store.reset_strats()

    def status(self) -> dict:
        rows = []
        for st in self.store.strat_states():
            last_px = self._last_price(st["symbol"])
            eq = (st["cash"] + st["qty"] * last_px) if last_px else \
                self.store.latest_sample(f"strat_eq:{st['name']}:{st['symbol']}")
            hold = (last_px / st["start_price"] - 1) * 100 if last_px else None
            rows.append({**{k: st[k] for k in ("name", "symbol", "capital", "cash", "qty", "trades", "start_ts")},
                         "label": label(st["name"], self.cfg.params.get(st["name"])), "equity": eq,
                         "return_pct": (eq / st["capital"] - 1) * 100 if eq else None,
                         "buy_hold_pct": hold, "max_dd_pct": st["max_dd"] * 100,
                         "position": ("持有" + (f"（止损 {st['stop']:,.2f}）" if st.get("stop") else "")
                                      if st["qty"] > 0 else "空仓"),
                         "note": st.get("note") or "",
                         "backtest": json.loads(st["backtest"] or "{}")})
        return {"enabled": self.enabled(), "rows": rows, "recent": self.store.strat_trades(30),
                "lppls": self.lppls_status(),
                "fee_pct": self.cfg.fee_pct, "slippage_pct": self.cfg.slippage_pct}

    def _last_price(self, symbol: str) -> float | None:
        """Price recorded by the last scan; the panel refreshes often and must not hit the exchange."""
        return self.store.latest_sample(f"strat_px:{symbol}") or self.store.latest_sample(f"dip_price:{symbol}")
