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

from .polymarket import PolymarketConfig

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

LLM_PRESETS = {
    # Both providers expose an OpenAI-compatible Chat Completions API.
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    # International Model Studio endpoint. Mainland China accounts use
    # https://dashscope.aliyuncs.com/compatible-mode/v1 ; newer workspaces may
    # require a workspace-specific host — override with LLM_BASE_URL.
    "qwen": {"base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
}


@dataclass
class ScanConfig:
    exchanges: list[str] = field(default_factory=lambda: ["binance", "okx", "bybit", "gate", "kucoin"])
    symbols: list[str] = field(default_factory=lambda: ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT"])
    times: list[str] = field(default_factory=lambda: ["08:00", "14:00", "20:00"])
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
    max_calls_per_day: int = 10
    temperature: float = 0.2


@dataclass
class PanelConfig:
    host: str = "127.0.0.1"
    port: int = 8765


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
    source: str = "env"             # "env", "vault" or "vault+env"

    def all_values(self) -> list[str]:
        vals = [self.llm_api_key, self.pm_private_key]
        for creds in self.exchange_keys.values():
            vals.extend(creds.values())
        return [v for v in vals if v and len(v) >= 6]

    def __repr__(self) -> str:  # never print secrets via repr()/logging of the object
        return (f"Secrets(source={self.source!r}, llm_provider={self.llm_provider!r}, "
                f"llm_key={'set' if self.llm_api_key else 'unset'}, "
                f"exchanges={sorted(self.exchange_keys)}, "
                f"polymarket_wallet={'set' if self.pm_private_key else 'unset'})")


@dataclass
class Settings:
    scan: ScanConfig
    risk: RiskConfig
    llm: LLMConfig
    panel: PanelConfig
    polymarket: PolymarketConfig
    secrets: Secrets
    data_dir: Path

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


PM_VAULT_ONLY = ("POLYMARKET_PRIVATE_KEY",)


def _load_secrets(env_path: Path, vault_data: dict[str, str] | None = None) -> Secrets:
    _check_private(env_path)
    env = {**dotenv_values(env_path)} if env_path.exists() else {}
    # Real environment variables take precedence over the file.
    env.update({k: v for k, v in os.environ.items() if k.startswith(("LLM_", "EXCHANGE_"))})
    for k in PM_VAULT_ONLY:
        if env.pop(k, None):
            log.warning("%s found in .env/environment and IGNORED: store it with "
                        "`python -m arbdesk vault set %s`", k, k)
    source = "env"
    if vault_data is not None:
        overlap = sorted(k for k in vault_data if env.get(k))
        if overlap:
            log.warning("these secrets exist in both .env and the vault (vault wins); "
                        "delete them from .env: %s", ", ".join(overlap))
        source = "vault+env" if any(env.get(k) for k in env if k.startswith(("LLM_API", "EXCHANGE_"))) else "vault"
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
        source=source,
    )


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
    )
    if settings.risk.mode not in ("paper", "live"):
        raise ValueError("risk.mode must be 'paper' or 'live'")
    if settings.polymarket.mode not in ("paper", "live"):
        raise ValueError("polymarket.mode must be 'paper' or 'live'")
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
