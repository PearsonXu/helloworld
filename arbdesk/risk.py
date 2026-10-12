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

    is_pm = [isinstance(l, dict) and str(l.get("exchange", "")).lower() == "polymarket" for l in legs_in]
    if any(is_pm):
        if not all(is_pm):
            return [], ["cannot mix Polymarket and exchange legs in one proposal"]
        legs, errors = _validate_polymarket(legs_in, settings)
        if settings.polymarket.mode == "live" and legs:
            total = sum(l["usd"] for l in legs)
            pm = settings.polymarket
            if store.live_pm_spent_today() + total > pm.max_live_usd_per_day:
                errors.append(f"would exceed polymarket.max_live_usd_per_day ({pm.max_live_usd_per_day})")
            if store.live_pm_open_exposure() + total > pm.max_live_exposure_usd:
                errors.append(f"would exceed polymarket.max_live_exposure_usd ({pm.max_live_exposure_usd})")
            if not settings.secrets.pm_private_key:
                errors.append("live Polymarket needs POLYMARKET_PRIVATE_KEY in the vault")
    else:
        legs = _validate_exchange_legs(legs_in, settings, errors)

    if sum(l["usd"] for l in legs) > r.max_capital_usd:
        errors.append(f"total size exceeds max_capital_usd {r.max_capital_usd}")

    stats = store.today_stats()
    if stats["trades"] >= r.max_trades_per_day:
        errors.append(f"daily trade limit reached ({r.max_trades_per_day})")
    if is_live(legs, settings) and (reason := store.halted()):
        errors.append(f"live trading halted: {reason}")
    if stats["pnl_usd"] <= -abs(r.daily_loss_limit_usd):
        errors.append(f"daily loss limit hit ({stats['pnl_usd']:.2f} USD)")
    return legs, errors


def is_live(legs: list[dict], settings: Settings) -> bool:
    if not legs:
        return False
    if legs[0]["exchange"] == "polymarket":
        return settings.polymarket.mode == "live"
    return settings.risk.mode == "live"


def _validate_polymarket(legs_in: list[dict], settings: Settings) -> tuple[list[dict], list[str]]:
    """Polymarket legs: always paper-traded; multi-outcome bundles may exceed MAX_LEGS."""
    pm = settings.polymarket
    errors: list[str] = []
    if not pm.enabled:
        return [], ["polymarket module disabled"]
    if len(legs_in) > 30:
        errors.append("too many Polymarket legs (max 30)")
    legs = []
    for i, leg in enumerate(legs_in[:30]):
        try:
            norm = {
                "exchange": "polymarket",
                "market_id": str(leg["market_id"]),
                "token_id": str(leg["token_id"]),
                "outcome": str(leg.get("outcome", "YES")).upper(),
                "label": str(leg.get("label", ""))[:200],
                "side": str(leg.get("side", "buy")).lower(),
                "shares": float(leg["shares"]),
                "limit_price": float(leg["limit_price"]),
            }
        except (KeyError, TypeError, ValueError):
            errors.append(f"leg {i}: malformed")
            continue
        if norm["side"] != "buy":
            errors.append(f"leg {i}: only buys are supported")
        if not 0 < norm["limit_price"] < 1:
            errors.append(f"leg {i}: limit_price must be in (0, 1)")
        if norm["shares"] <= 0:
            errors.append(f"leg {i}: shares must be > 0")
        norm["usd"] = norm["shares"] * norm["limit_price"]
        if norm["usd"] > pm.max_usd_per_market:
            errors.append(f"leg {i}: ${norm['usd']:.2f} exceeds polymarket.max_usd_per_market")
        legs.append(norm)
    return legs, errors


def _validate_exchange_legs(legs_in: list, settings: Settings, errors: list[str]) -> list[dict]:
    r = settings.risk
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
    return legs
