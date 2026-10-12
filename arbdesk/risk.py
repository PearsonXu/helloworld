"""Hard risk limits. Every proposal — AI-generated or manual — passes through here
before anything is executed, and the limits are applied in code, not by the model."""
from __future__ import annotations

from .config import Settings
from .storage import Store

MAX_LEGS = 4
MAX_PREDICTION_LEGS = 30
PREDICTION_VENUES = ("polymarket", "kalshi", "polymarket_us")
LIVE_YES_ONLY = {"polymarket_us"}


def validate(payload: dict, settings: Settings, store: Store) -> tuple[list[dict], list[str]]:
    """Returns (normalized legs, errors). Execution is allowed only if errors is empty."""
    r = settings.risk
    errors: list[str] = []
    legs_in = payload.get("legs") or []
    if not isinstance(legs_in, list) or not legs_in:
        return [], ["proposal has no legs"]

    names = [str(l.get("exchange", "")).lower() if isinstance(l, dict) else "" for l in legs_in]
    if any(n in PREDICTION_VENUES for n in names):
        if len(set(names)) != 1:
            return [], ["all legs must be on the same prediction venue (no mixing with exchanges)"]
        legs, errors = _validate_prediction(legs_in, names[0], settings)
        if settings.venue_cfg(names[0]).mode == "live" and legs:
            errors += _live_prediction_checks(legs, names[0], settings, store)
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
    if legs[0]["exchange"] in PREDICTION_VENUES:
        return settings.venue_cfg(legs[0]["exchange"]).mode == "live"
    return settings.risk.mode == "live"


def _live_prediction_checks(legs: list[dict], venue: str, settings: Settings, store: Store) -> list[str]:
    cfg, sec, errors = settings.venue_cfg(venue), settings.secrets, []
    total = sum(l["usd"] for l in legs)
    if store.live_spent_today(venue) + total > cfg.max_live_usd_per_day:
        errors.append(f"would exceed {venue}.max_live_usd_per_day ({cfg.max_live_usd_per_day})")
    if store.live_open_exposure(venue) + total > cfg.max_live_exposure_usd:
        errors.append(f"would exceed {venue}.max_live_exposure_usd ({cfg.max_live_exposure_usd})")
    missing = {
        "polymarket": not sec.pm_private_key,
        "kalshi": not (sec.kalshi_key_id and sec.kalshi_private_key),
        "polymarket_us": not (sec.pmus_key_id and sec.pmus_secret_key),
    }[venue]
    if missing:
        errors.append(f"live {venue} needs its API credentials in the vault")
    if venue in LIVE_YES_ONLY and any(l["outcome"] != "YES" for l in legs):
        errors.append(f"live {venue} orders can only buy YES")
    return errors


def _validate_prediction(legs_in: list[dict], venue: str, settings: Settings) -> tuple[list[dict], list[str]]:
    """Prediction-market legs (buy-only). Multi-outcome bundles may exceed MAX_LEGS."""
    cfg = settings.venue_cfg(venue)
    errors: list[str] = []
    if not cfg.enabled:
        return [], [f"{venue} is disabled in config.toml"]
    if len(legs_in) > MAX_PREDICTION_LEGS:
        errors.append(f"too many legs (max {MAX_PREDICTION_LEGS})")
    legs = []
    for i, leg in enumerate(legs_in[:MAX_PREDICTION_LEGS]):
        try:
            norm = {
                "exchange": venue,
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
        if norm["outcome"] not in ("YES", "NO"):
            errors.append(f"leg {i}: outcome must be YES or NO")
        if not 0 < norm["limit_price"] < 1:
            errors.append(f"leg {i}: limit_price must be in (0, 1)")
        if norm["shares"] <= 0:
            errors.append(f"leg {i}: shares must be > 0")
        norm["usd"] = norm["shares"] * norm["limit_price"]
        if norm["usd"] > cfg.max_usd_per_market:
            errors.append(f"leg {i}: ${norm['usd']:.2f} exceeds {venue}.max_usd_per_market")
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
