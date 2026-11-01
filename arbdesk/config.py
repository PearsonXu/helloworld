"""Configuration loading.

Secrets (API keys) live only in `.env`; non-secret settings live in `config.toml`.
Both files stay on the local machine and are git-ignored.
"""
from __future__ import annotations

import os
import stat
import tomllib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from .polymarket import PolymarketConfig, VenueConfig

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

LLM_PRESETS = {
    # Both providers expose an OpenAI-compatible Chat Completions API. Defaults are the
    # cheap "flash" tier; model ids change over time, so verify with
    # `python -m arbdesk check llm` and override with LLM_MODEL in .env if needed.
    # Prices are USD per 1M tokens (input, cached input, output) and only feed the
    # local cost estimate / monthly cap; set exact values under [llm] in config.toml.
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-flash",
                 "price": (0.30, 0.006, 1.20)},
    # International Model Studio endpoint. Mainland China accounts use
    # https://dashscope.aliyuncs.com/compatible-mode/v1 ; newer workspaces may
    # require a workspace-specific host — override with LLM_BASE_URL.
    "qwen": {"base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "model": "qwen-flash",
             "price": (0.05, 0.005, 0.40)},
}


@dataclass
class ScanConfig:
    exchanges: list[str] = field(default_factory=lambda: ["binance", "okx", "bybit", "gate", "kucoin"])
    symbols: list[str] = field(default_factory=lambda: ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT"])
    interval_minutes: int = 5     # code-only scans (no tokens): crypto + prediction markets
    times: list[str] = field(default_factory=list)  # deprecated, ignored (replaced by intervals)
    taker_fee_pct: float = 0.1
    min_net_spread_pct: float = 0.2
    min_funding_apr_pct: float = 10.0
    triangular_exchange: str = "binance"
    triangular_min_profit_pct: float = 0.05


@dataclass
class RiskConfig:
    mode: str = "paper"  # "paper" or "live"
    max_capital_usd: float = 150.0
    max_per_trade_usd: float = 30.0
    daily_loss_limit_usd: float = 10.0
    max_trades_per_day: int = 5
    max_slippage_pct: float = 0.3
    allow_live_swap: bool = False  # perpetuals in live mode need margin; off by default
    allowed_symbols: list[str] = field(default_factory=list)  # empty = scan.symbols


@dataclass
class LLMConfig:
    enabled: bool = True
    interval_minutes: int = 60          # AI runs at most this often, and only when something changed
    daily_token_budget: int = 300_000   # input + output tokens per UTC day
    monthly_usd_cap: float = 5.0        # estimated spend per calendar month (from the prices below)
    max_calls_per_day: int = 200        # extra hard stop
    temperature: float = 0.2
    # USD per 1M tokens; 0 = use the provider preset
    price_input: float = 0.0
    price_cached_input: float = 0.0
    price_output: float = 0.0
    # Re-forecast a market only if its price moved this much, or this many hours passed.
    reforecast_price_move: float = 0.05
    reforecast_hours: float = 24.0
    # Extra request fields passed through to the provider (e.g. to switch "thinking" off).
    extra_body: dict = field(default_factory=dict)


@dataclass
class CrossVenueConfig:
    """Same event on two venues: YES on one + NO on the other for < $1 total."""
    enabled: bool = True
    venues: list[str] = field(default_factory=lambda: ["kalshi", "polymarket_us"])
    min_similarity: float = 0.35        # title-token similarity for the code pre-filter
    max_end_date_diff_days: float = 3.0
    max_new_pairs_per_run: int = 15     # pairs sent to the AI for matching per AI run
    min_match_confidence: float = 0.8
    min_profit_pct: float = 1.0         # extra margin: two venues' rules can differ
    max_usd_per_pair: float = 20


@dataclass
class PanelConfig:
    host: str = "127.0.0.1"
    port: int = 18765  # uncommon on purpose: 8765 is used by other desktop apps


@dataclass
class Secrets:
    llm_provider: str = "deepseek"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    exchange_keys: dict[str, dict[str, str]] = field(default_factory=dict)
    # Polymarket wallet: accepted ONLY from the encrypted vault, never from .env.
    pm_private_key: str = ""
    pm_funder: str = ""             # proxy / Safe wallet address shown on polymarket.com
    pm_signature_type: int = 1      # 0 EOA, 1 email/Magic proxy, 2 browser-wallet Safe
    kalshi_key_id: str = ""
    kalshi_private_key: str = ""    # PEM, vault only
    pmus_key_id: str = ""
    pmus_secret_key: str = ""       # base64 Ed25519, vault only
    smtp_password: str = ""         # vault only
    source: str = "env"             # "env", "vault" or "vault+env"

    def all_values(self) -> list[str]:
        vals = [self.llm_api_key, self.pm_private_key, self.kalshi_private_key, self.pmus_secret_key,
                self.smtp_password]
        for creds in self.exchange_keys.values():
            vals.extend(creds.values())
        return [v for v in vals if v and len(v) >= 6]

    def __repr__(self) -> str:  # never print secrets via repr()/logging of the object
        return (f"Secrets(source={self.source!r}, llm_provider={self.llm_provider!r}, "
                f"llm_key={'set' if self.llm_api_key else 'unset'}, "
                f"exchanges={sorted(self.exchange_keys)}, "
                f"polymarket_wallet={'set' if self.pm_private_key else 'unset'}, "
                f"kalshi={'set' if self.kalshi_private_key else 'unset'}, "
                f"polymarket_us={'set' if self.pmus_secret_key else 'unset'})")


@dataclass
class TailConfig:
    """Black-swan tickets: crypto threshold contracts priced against Deribit options."""
    enabled: bool = True
    currencies: list[str] = field(default_factory=lambda: ["BTC", "ETH"])
    venues: list[str] = field(default_factory=lambda: ["kalshi", "polymarket_us"])
    kalshi_series: list[str] = field(default_factory=lambda: ["KXBTCD", "KXBTC", "KXETHD", "KXETH"])
    max_price: float = 0.15            # only "lottery tickets": sides costing <= 15 cents
    min_hours_to_expiry: float = 24    # skip intraday / same-day contracts
    min_distance_pct: float = 5.0      # strike(s) at least this far from the current price
    min_edge: float = 0.02             # option-implied prob - price - fee
    min_ratio: float = 1.5             # option-implied prob / price
    max_expiry_gap_days: float = 45    # contract date must sit near listed option expiries
    max_usd_per_bet: float = 5.0
    quarterly_budget_usd: float = 30.0  # all tail bets per calendar quarter (paper and live)
    max_pmus_quotes: int = 40          # Polymarket US crypto markets quoted per scan


@dataclass
class AlertConfig:
    """Extreme-market alerts (no trading)."""
    enabled: bool = True
    currencies: list[str] = field(default_factory=lambda: ["BTC", "ETH"])
    move_1h_pct: float = 5.0
    critical_move_1h_pct: float = 12.0
    move_24h_pct: float = 12.0
    iv_high: float = 1.0                # ATM implied vol >= 100%
    iv_jump_points: float = 0.20        # +20 vol points vs 24h ago
    funding_8h_abs: float = 0.0015      # |funding| >= 0.15% per 8h (~160% APR)
    stablecoins: list[str] = field(default_factory=lambda: ["USDT/USD", "USDC/USD"])
    stable_exchange: str = "kraken"     # public tickers, US-accessible
    depeg_pct: float = 0.5
    critical_depeg_pct: float = 2.0
    outage_consecutive: int = 3         # scans in a row before an outage alert
    cooldown_hours: float = 6.0         # same alert not repeated within this window (unless it escalates)
    desktop_notify: bool = True         # Windows/macOS toast via plyer, if installed
    halt_on_critical: bool = True       # critical alert engages the kill switch when any live mode is on


@dataclass
class DipConfig:
    """Resting limit buys far below the market (spot only, no leverage)."""
    enabled: bool = True
    mode: str = "paper"                 # "live" places real GTC orders after you type LIVE to arm
    exchange: str = "coinbase"          # ccxt id; US-available and low minimum order size
    ladders: list[dict] = field(default_factory=lambda: [
        {"symbol": "BTC/USD", "levels_pct": [-25, -40, -55], "usd_per_level": 5},
        {"symbol": "ETH/USD", "levels_pct": [-30, -45, -60], "usd_per_level": 5},
    ])
    max_open_usd: float = 30.0          # all resting dip orders together
    quarterly_budget_usd: float = 60.0  # filled + resting dip buys per calendar quarter
    rearm_pct: float = 10.0             # move the ladder up after the price rises this much (never down)
    maker_fee_pct: float = 0.6          # paper-fill fee estimate (check your exchange's maker fee)
    auto_arm_paper: bool = True         # paper mode arms itself; live mode is armed by you
    keep_orders_on_auto_halt: bool = True  # an alert-triggered halt keeps resting dip orders (manual halt cancels)


@dataclass
class SimConfig:
    """Simulated account defaults (override per run: `sim start --capital 200 --hours 24`)."""
    capital: float = 150.0
    hours: float = 24.0
    auto_execute: bool = True      # execute passing paper proposals automatically while a session runs


@dataclass
class StrategyConfig:
    """Strategy lab: classic BTC/ETH spot strategies, paper-traded on live candles."""
    enabled: list[str] = field(default_factory=lambda: ["dca", "sma_cross", "donchian", "rsi", "trend_fast"])
    exchange: str = "coinbase"
    symbols: list[str] = field(default_factory=lambda: ["BTC/USD", "ETH/USD"])
    capital_per_strategy: float = 25.0  # virtual $ per strategy and symbol (separate from the sim account)
    fee_pct: float = 0.6                # per fill; Coinbase's entry-tier fees can be higher, check yours
    slippage_pct: float = 0.05
    params: dict = field(default_factory=lambda: {
        "dca": {"timeframe": "1d", "every_days": 7, "amount_usd": 5.0},
        "sma_cross": {"timeframe": "1d", "fast": 20, "slow": 50},
        "donchian": {"timeframe": "1d", "entry": 20, "exit": 10},
        "rsi": {"timeframe": "1h", "period": 14, "buy_below": 30, "sell_above": 70},
        # Fast trend with crash protection (see trendfast.py). Bars are 6 hours.
        "trend_fast": {"timeframe": "6h", "history_bars": 900, "ema_fast": 8, "ema_slow": 24,
                       "regime_sma": 200, "min_spread_pct": 0.3, "atr_period": 14, "atr_stop_mult": 2.0,
                       "trail_atr_mult": 3.0, "min_stop_pct": 3.0, "hard_stop_pct": 6.0,
                       "risk_per_trade_pct": 2.0, "max_position_pct": 50.0, "crash_drop_pct": 8.0,
                       "crash_lookback": 4, "crash_cooldown": 8, "loss_streak": 3, "streak_cooldown": 12,
                       "min_bars_between": 2, "max_drawdown_pct": 15.0,
                       # LPPLS bubble filter (lppls.py): daily fits over several windows.
                       "use_lppls": True, "lppls_windows": [30, 45, 60, 75, 90, 105, 120],
                       "lppls_tighten": 0.25, "lppls_block": 0.5, "lppls_exit": 0.75,
                       "lppls_trail_atr_mult": 1.5,
                       "lppls_min_r2": 0.8},
    })


@dataclass
class EmailConfig:
    """Email digests for black-swan alerts. Password: `vault set SMTP_PASSWORD` (Gmail: App Password)."""
    enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    security: str = "starttls"         # "starttls" (port 587) or "ssl" (port 465)
    username: str = ""                 # usually your email address
    from_addr: str = ""                # defaults to username
    to_addr: str = ""                  # where alerts go
    min_level: str = "warning"         # warning | critical
    categories: list[str] = field(default_factory=lambda: [
        "move1h", "move24h", "depeg", "ivjump", "ivhigh", "funding", "tail_cheap", "dip_fill", "lppls"])
    max_per_hour: int = 4
    lookback_hours: float = 6.0        # ignore older alerts (e.g. right after enabling)
    subject_prefix: str = "[ArbDesk]"


# Venue-specific defaults on top of VenueConfig's.
KALSHI_DEFAULTS = {"environment": "demo", "min_liquidity_usd": 500}
PMUS_DEFAULTS = {"min_liquidity_usd": 1000}


@dataclass
class Settings:
    scan: ScanConfig
    risk: RiskConfig
    llm: LLMConfig
    panel: PanelConfig
    polymarket: PolymarketConfig
    secrets: Secrets
    data_dir: Path
    crossvenue: CrossVenueConfig = field(default_factory=CrossVenueConfig)
    tail: TailConfig = field(default_factory=TailConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    dip: DipConfig = field(default_factory=DipConfig)
    sim: SimConfig = field(default_factory=SimConfig)
    strategies: StrategyConfig = field(default_factory=StrategyConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    kalshi: VenueConfig = field(default_factory=lambda: VenueConfig(**KALSHI_DEFAULTS))
    polymarket_us: VenueConfig = field(default_factory=lambda: VenueConfig(**PMUS_DEFAULTS))

    def venue_cfg(self, name: str) -> VenueConfig:
        return {"polymarket": self.polymarket, "kalshi": self.kalshi, "polymarket_us": self.polymarket_us}[name]

    @property
    def allowed_symbols(self) -> list[str]:
        return self.risk.allowed_symbols or self.scan.symbols


def _check_private(path: Path) -> None:
    """Warn when a secrets file is readable by other users (POSIX only)."""
    if os.name != "posix" or not path.exists():
        return
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning("%s is readable by other users; run: chmod 600 %s", path.name, path)


VAULT_ONLY = ("POLYMARKET_PRIVATE_KEY", "KALSHI_PRIVATE_KEY", "POLYMARKET_US_SECRET_KEY", "SMTP_PASSWORD")


def _load_secrets(env_path: Path, vault_data: dict[str, str] | None = None) -> Secrets:
    _check_private(env_path)
    env = {**dotenv_values(env_path)} if env_path.exists() else {}
    # Real environment variables take precedence over the file.
    env.update({k: v for k, v in os.environ.items() if k.startswith(("LLM_", "EXCHANGE_"))})
    for k in VAULT_ONLY:
        if env.pop(k, None):
            log.warning("%s found in .env/environment and IGNORED: store it with "
                        "`python -m arbdesk vault set %s`", k, k)
    source = "env"
    if vault_data is not None:
        overlap = sorted(k for k in vault_data if env.get(k))
        if overlap:
            log.warning("these secrets exist in both .env and the vault (vault wins); "
                        "delete them from .env: %s", ", ".join(overlap))
        source = "vault+env" if any(env.get(k) for k in env
                                    if k.startswith(("LLM_API", "EXCHANGE_", "KALSHI_", "POLYMARKET_US_"))) else "vault"
        env.update({k: v for k, v in vault_data.items() if v})

    exchange_keys: dict[str, dict[str, str]] = {}
    for key, value in env.items():
        # EXCHANGE_<ID>_API_KEY / _SECRET / _PASSWORD
        if not key.startswith("EXCHANGE_") or not value:
            continue
        for suffix, name in (("_API_KEY", "apiKey"), ("_SECRET", "secret"), ("_PASSWORD", "password")):
            if key.endswith(suffix):
                ex_id = key[len("EXCHANGE_"):-len(suffix)].lower()
                exchange_keys.setdefault(ex_id, {})[name] = value
    return Secrets(
        llm_provider=(env.get("LLM_PROVIDER") or "deepseek").lower(),
        llm_api_key=env.get("LLM_API_KEY") or "",
        llm_base_url=env.get("LLM_BASE_URL") or "",
        llm_model=env.get("LLM_MODEL") or "",
        exchange_keys=exchange_keys,
        pm_private_key=env.get("POLYMARKET_PRIVATE_KEY") or "",
        pm_funder=env.get("POLYMARKET_FUNDER") or "",
        pm_signature_type=int(env.get("POLYMARKET_SIGNATURE_TYPE") or 1),
        kalshi_key_id=env.get("KALSHI_API_KEY_ID") or "",
        kalshi_private_key=env.get("KALSHI_PRIVATE_KEY") or "",
        pmus_key_id=env.get("POLYMARKET_US_KEY_ID") or "",
        pmus_secret_key=env.get("POLYMARKET_US_SECRET_KEY") or "",
        smtp_password=env.get("SMTP_PASSWORD") or "",
        source=source,
    )


def _strategy_config(raw: dict) -> StrategyConfig:
    """Per-strategy params from config.toml are merged over the defaults, key by key."""
    base = StrategyConfig()
    params = {k: dict(v) for k, v in base.params.items()}
    for k, v in (raw.get("params") or {}).items():
        params.setdefault(k, {}).update(v)
    return StrategyConfig(**{**{k: v for k, v in raw.items() if k != "params"}, "params": params})


def load_settings(config_path: Path | None = None, env_path: Path | None = None,
                  vault_data: dict[str, str] | None = None) -> Settings:
    config_path = config_path or ROOT / "config.toml"
    env_path = env_path or ROOT / ".env"
    raw: dict = {}
    if config_path.exists():
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    data_dir = Path(raw.get("data_dir", ROOT / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(data_dir, 0o700)
    settings = Settings(
        scan=ScanConfig(**raw.get("scan", {})),
        risk=RiskConfig(**raw.get("risk", {})),
        llm=LLMConfig(**raw.get("llm", {})),
        panel=PanelConfig(**raw.get("panel", {})),
        polymarket=PolymarketConfig(**raw.get("polymarket", {})),
        secrets=_load_secrets(env_path, vault_data),
        data_dir=data_dir,
        crossvenue=CrossVenueConfig(**raw.get("crossvenue", {})),
        tail=TailConfig(**raw.get("tail", {})),
        alerts=AlertConfig(**raw.get("alerts", {})),
        dip=DipConfig(**raw.get("dip", {})),
        sim=SimConfig(**raw.get("sim", {})),
        strategies=_strategy_config(raw.get("strategies", {})),
        email=EmailConfig(**raw.get("email", {})),
        kalshi=VenueConfig(**{**KALSHI_DEFAULTS, **raw.get("kalshi", {})}),
        polymarket_us=VenueConfig(**{**PMUS_DEFAULTS, **raw.get("polymarket_us", {})}),
    )
    if settings.risk.mode not in ("paper", "live"):
        raise ValueError("risk.mode must be 'paper' or 'live'")
    for name in ("polymarket", "kalshi", "polymarket_us"):
        vc = settings.venue_cfg(name)
        if vc.mode not in ("paper", "live"):
            raise ValueError(f"{name}.mode must be 'paper' or 'live'")
    if settings.email.security not in ("starttls", "ssl"):
        raise ValueError("email.security must be 'starttls' or 'ssl'")
    if settings.dip.mode not in ("paper", "live"):
        raise ValueError("dip.mode must be 'paper' or 'live'")
    if settings.kalshi.environment not in ("demo", "prod"):
        raise ValueError("kalshi.environment must be 'demo' or 'prod'")
    return settings


def vault_path(config_path: Path | None = None) -> Path:
    """Vault location follows data_dir, without creating anything."""
    config_path = config_path or ROOT / "config.toml"
    raw = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    return Path(raw.get("data_dir", ROOT / "data")) / "vault.json"


class RedactingFilter(logging.Filter):
    """Masks any configured secret value that ends up in a log line."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self.secrets:
            if s in msg:
                msg = msg.replace(s, s[:3] + "***")
        record.msg, record.args = msg, None
        return True


def setup_logging(settings: Settings) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter(settings.secrets.all_values()))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
