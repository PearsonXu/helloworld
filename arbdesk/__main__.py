"""CLI: python -m arbdesk <command>

  panel | scan | venue NAME | proposals | approve ID [--live] | reject ID
  vault init | set NAME | set-file NAME PATH | list | delete NAME | import-env | passwd
  halt [REASON] | resume | check VENUE
VENUE: polymarket | kalshi | polymarket_us
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from pathlib import Path

from dotenv import dotenv_values

from .config import ROOT, load_settings, setup_logging, vault_path
from .executor import Executor
from .service import Desk, Scheduler
from .storage import Store
from .vault import Vault, VaultError
from .web import create_app, is_loopback, load_or_create_token

log = logging.getLogger("arbdesk")

IMPORTABLE_PREFIXES = ("LLM_API_KEY", "EXCHANGE_", "POLYMARKET_", "KALSHI_")
VENUES = ("polymarket", "kalshi", "polymarket_us")


def ask_password(prompt: str = "Vault master password: ") -> str:
    if not sys.stdin.isatty():
        raise SystemExit("the vault password must be typed interactively")
    return getpass.getpass(prompt)


def vault_command(args, vault: Vault) -> None:
    try:
        if args.action == "init":
            pw = ask_password("New master password (>= 12 chars): ")
            if pw != ask_password("Repeat: "):
                raise SystemExit("passwords do not match")
            vault.create(pw)
            print(f"created {vault.path}. There is NO recovery if you forget this password.")
        elif args.action == "set":
            pw = ask_password()
            value = getpass.getpass(f"Value for {args.name} (hidden): ").strip()
            if not value:
                raise SystemExit("empty value")
            vault.set(pw, args.name, value)
            print(f"stored {args.name}")
        elif args.action == "set-file":
            # For multi-line secrets such as Kalshi's PEM private key.
            path = Path(args.path)
            value = path.read_text(encoding="utf-8").strip()
            if not value:
                raise SystemExit("file is empty")
            vault.set(ask_password(), args.name, value)
            print(f"stored {args.name} from {path.name}. Now delete the original file: {path}")
        elif args.action == "list":
            for name in sorted(vault.unlock(ask_password())):
                print(name)  # names only, never values
        elif args.action == "delete":
            print("deleted" if vault.delete(ask_password(), args.name) else "not found")
        elif args.action == "import-env":
            env_path = ROOT / ".env"
            env = {k: v for k, v in dotenv_values(env_path).items()
                   if v and k.startswith(IMPORTABLE_PREFIXES)} if env_path.exists() else {}
            if not env:
                raise SystemExit("no importable secrets in .env")
            pw = ask_password()
            data = vault.unlock(pw)
            data.update(env)
            vault.save(pw, data)
            print("imported:", ", ".join(sorted(env)))
            print("Now delete those lines from .env (keep only LLM_PROVIDER / LLM_MODEL / LLM_BASE_URL).")
        elif args.action == "passwd":
            old = ask_password("Current password: ")
            new = ask_password("New password (>= 12 chars): ")
            if new != ask_password("Repeat: "):
                raise SystemExit("passwords do not match")
            vault.change_password(old, new)
            print("password changed")
    except VaultError as e:
        raise SystemExit(f"vault: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="arbdesk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("panel", help="start the local web panel + scheduler")
    p.add_argument("--no-scheduler", action="store_true")
    s = sub.add_parser("scan", help="run one scan (and AI analysis) now")
    s.add_argument("--no-llm", action="store_true")
    vp = sub.add_parser("venue", help="run one prediction venue: settle, scan, AI forecasts")
    vp.add_argument("name", choices=VENUES)
    vp.add_argument("--no-llm", action="store_true")
    pmp = sub.add_parser("polymarket", help="same as: venue polymarket")
    pmp.add_argument("--no-llm", action="store_true")
    sub.add_parser("proposals", help="list proposals")
    ap_ = sub.add_parser("approve", help="execute a proposal")
    ap_.add_argument("id", type=int)
    ap_.add_argument("--live", action="store_true", help="confirm a real-money proposal")
    sub.add_parser("reject").add_argument("id", type=int)
    v = sub.add_parser("vault", help="manage the encrypted secret store")
    v.add_argument("action", choices=["init", "set", "set-file", "list", "delete", "import-env", "passwd"])
    v.add_argument("name", nargs="?")
    v.add_argument("path", nargs="?")
    h = sub.add_parser("halt", help="kill switch: block all live trading")
    h.add_argument("reason", nargs="?", default="manual halt")
    sub.add_parser("resume", help="lift the kill switch")
    ck = sub.add_parser("check", help="check live readiness of a venue without trading")
    ck.add_argument("name", choices=VENUES)
    sub.add_parser("pm-check", help="same as: check polymarket")
    args = ap.parse_args()

    vault = Vault(vault_path())
    if args.cmd == "vault":
        if args.action in ("set", "delete", "set-file") and not args.name:
            raise SystemExit(f"usage: vault {args.action} NAME")
        if args.action == "set-file" and not args.path:
            raise SystemExit("usage: vault set-file NAME PATH")
        vault_command(args, vault)
        return

    vault_data = None
    if vault.exists():
        try:
            vault_data = vault.unlock(ask_password())
        except VaultError as e:
            raise SystemExit(f"vault: {e}")

    settings = load_settings(vault_data=vault_data)
    del vault_data
    setup_logging(settings)
    store = Store(settings.data_dir / "arbdesk.db")
    desk = Desk(settings, store)
    executor = Executor(settings, store)

    if settings.risk.mode == "live":
        log.warning("LIVE exchange mode: API keys must have trading only — "
                    "withdrawals disabled and IP whitelist set.")
    for name in VENUES:
        vc = settings.venue_cfg(name)
        if vc.enabled and vc.mode == "live":
            money = "FAKE money (Kalshi demo)" if name == "kalshi" and vc.environment == "demo" else "REAL money"
            log.warning("LIVE %s mode, %s. Caps: $%s/day, $%s open exposure.",
                        name, money, vc.max_live_usd_per_day, vc.max_live_exposure_usd)

    if args.cmd == "panel":
        if not is_loopback(settings.panel.host):
            raise SystemExit("panel.host must be a loopback address (127.0.0.1); "
                             "use an SSH tunnel if you need remote access")
        token = load_or_create_token(settings.data_dir)
        if not args.no_scheduler:
            Scheduler(desk).start()
        url = f"http://127.0.0.1:{settings.panel.port}/?token={token}"
        modes = ", ".join(f"{n}={v.cfg.mode}" for n, v in desk.venues.items())
        print(f"\n  ArbDesk panel: {url}\n  (crypto={settings.risk.mode}, {modes}, "
              f"secrets from {settings.secrets.source})\n")
        import uvicorn
        uvicorn.run(create_app(settings, store, desk, executor, token),
                    host=settings.panel.host, port=settings.panel.port, log_level="warning")
    elif args.cmd == "scan":
        print(json.dumps(desk.run_cycle(use_llm=not args.no_llm), ensure_ascii=False, indent=2))
    elif args.cmd in ("venue", "polymarket"):
        name = getattr(args, "name", "polymarket")
        if name not in desk.venues:
            raise SystemExit(f"{name} is disabled; set enabled = true under [{name}] in config.toml")
        print(json.dumps(desk.venue_cycle(desk.venues[name], use_llm=not args.no_llm), ensure_ascii=False, indent=2))
    elif args.cmd == "proposals":
        for prop in store.proposals():
            print(prop["id"], prop["status"], prop["payload"].get("title"), prop["note"] or "")
    elif args.cmd == "approve":
        confirm = False
        if args.live:
            answer = input(f"Type LIVE to send real-money orders for proposal #{args.id}: ")
            confirm = answer.strip() == "LIVE"
        print(json.dumps(executor.execute(args.id, confirm_live=confirm), ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "reject":
        store.set_proposal_status(args.id, "rejected", "rejected by user")
    elif args.cmd == "halt":
        executor.halt(args.reason)
        print("live trading halted")
    elif args.cmd == "resume":
        executor.resume()
        print("kill switch lifted")
    elif args.cmd in ("check", "pm-check"):
        name = getattr(args, "name", "polymarket")
        try:
            print(json.dumps(executor.trader(name).readiness(), indent=2, default=str))
        except Exception as e:
            raise SystemExit(f"not ready: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
