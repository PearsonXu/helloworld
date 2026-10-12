"""Hard risk limits. Every proposal — AI-generated or manual — passes through here
before anything is executed, and the limits are applied in code, not by the model."""
from __future__ import annotations

from .config import Settings
from .storage import Store

MAX_LEGS = 4


def validate(payload: dict, settings: Settings, store: Store) -> tuple[list[dict], list[str]]:
    """Returns (normalized legs, errors). Execution is allowed only if errors is empty."""
    r = settings.risk
    errors: list[str] = []
    legs_in = payload.get("legs") or []
    if not isinstance(legs_in, list) or not legs_in:
        return [], ["proposal has no legs"]
    if len(legs_in) > MAX_LEGS:
        errors.append(f"too many legs (max {MAX_LEGS})")

    legs = []
    for i, leg in enumerate(legs_in[:MAX_LEGS]):
        try:
            norm = {
                "exchange": str(leg["exchange"]).lower().strip(),
                "symbol": str(leg["symbol"]).upper().strip(),
                "market": str(leg.get("market", "spot")).lower(),
                "side": str(leg["side"]).lower(),
                "usd": float(leg["usd"]),
            }
        except (KeyError, TypeError, ValueError):
            errors.append(f"leg {i}: malformed")
            continue
        if norm["exchange"] not in settings.scan.exchanges:
            errors.append(f"leg {i}: exchange {norm['exchange']} not allowed")
        if norm["symbol"] not in settings.allowed_symbols:
            errors.append(f"leg {i}: symbol {norm['symbol']} not allowed")
        if norm["side"] not in ("buy", "sell"):
            errors.append(f"leg {i}: bad side")
        if norm["market"] not in ("spot", "swap"):
            errors.append(f"leg {i}: bad market")
        if not 0 < norm["usd"] <= r.max_per_trade_usd:
            errors.append(f"leg {i}: size ${norm['usd']:.2f} outside (0, {r.max_per_trade_usd}]")
        if r.mode == "live":
            if norm["exchange"] not in settings.secrets.exchange_keys:
                errors.append(f"leg {i}: no API key configured for {norm['exchange']}")
            if norm["market"] == "swap" and not r.allow_live_swap:
                errors.append(f"leg {i}: live perpetual trading disabled (risk.allow_live_swap)")
        legs.append(norm)

    if sum(l["usd"] for l in legs) > r.max_capital_usd:
        errors.append(f"total size exceeds max_capital_usd {r.max_capital_usd}")

    stats = store.today_stats()
    if stats["trades"] >= r.max_trades_per_day:
        errors.append(f"daily trade limit reached ({r.max_trades_per_day})")
    if stats["pnl_usd"] <= -abs(r.daily_loss_limit_usd):
        errors.append(f"daily loss limit hit ({stats['pnl_usd']:.2f} USD)")
    return legs, errors
