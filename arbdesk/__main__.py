"""CLI: python -m arbdesk <command>

  panel | scan | polymarket | proposals | approve ID [--live] | reject ID
  vault init | set NAME | list | delete NAME | import-env | passwd
  halt [REASON] | resume | pm-check
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys

from dotenv import dotenv_values

from .config import ROOT, load_settings, setup_logging, vault_path
from .executor import Executor
from .service import Desk, Scheduler
from .storage import Store
from .vault import Vault, VaultError
from .web import create_app, is_loopback, load_or_create_token

log = logging.getLogger("arbdesk")

IMPORTABLE_PREFIXES = ("LLM_API_KEY", "EXCHANGE_", "POLYMARKET_")


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
    pmp = sub.add_parser("polymarket", help="run only the Polymarket scan / forecasts / settlement")
    pmp.add_argument("--no-llm", action="store_true")
    sub.add_parser("proposals", help="list proposals")
    ap_ = sub.add_parser("approve", help="execute a proposal")
    ap_.add_argument("id", type=int)
    ap_.add_argument("--live", action="store_true", help="confirm a real-money proposal")
    sub.add_parser("reject").add_argument("id", type=int)
    v = sub.add_parser("vault", help="manage the encrypted secret store")
    v.add_argument("action", choices=["init", "set", "list", "delete", "import-env", "passwd"])
    v.add_argument("name", nargs="?")
    h = sub.add_parser("halt", help="kill switch: block all live trading")
    h.add_argument("reason", nargs="?", default="manual halt")
    sub.add_parser("resume", help="lift the kill switch")
    sub.add_parser("pm-check", help="check live Polymarket readiness without trading")
    args = ap.parse_args()

    vault = Vault(vault_path())
    if args.cmd == "vault":
        if args.action in ("set", "delete") and not args.name:
            raise SystemExit(f"usage: vault {args.action} NAME")
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
    if settings.polymarket.mode == "live":
        log.warning("LIVE Polymarket mode: real money. Caps: $%s/day, $%s open exposure.",
                    settings.polymarket.max_live_usd_per_day, settings.polymarket.max_live_exposure_usd)

    if args.cmd == "panel":
        if not is_loopback(settings.panel.host):
            raise SystemExit("panel.host must be a loopback address (127.0.0.1); "
                             "use an SSH tunnel if you need remote access")
        token = load_or_create_token(settings.data_dir)
        if not args.no_scheduler:
            Scheduler(desk).start()
        url = f"http://127.0.0.1:{settings.panel.port}/?token={token}"
        print(f"\n  ArbDesk panel: {url}\n  (crypto={settings.risk.mode}, polymarket={settings.polymarket.mode}, "
              f"secrets from {settings.secrets.source})\n")
        import uvicorn
        uvicorn.run(create_app(settings, store, desk, executor, token),
                    host=settings.panel.host, port=settings.panel.port, log_level="warning")
    elif args.cmd == "scan":
        print(json.dumps(desk.run_cycle(use_llm=not args.no_llm), ensure_ascii=False, indent=2))
    elif args.cmd == "polymarket":
        print(json.dumps(desk.polymarket_cycle(use_llm=not args.no_llm), ensure_ascii=False, indent=2))
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
    elif args.cmd == "pm-check":
        try:
            print(json.dumps(executor.pm_trader.readiness(), indent=2))
        except Exception as e:
            raise SystemExit(f"not ready: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
