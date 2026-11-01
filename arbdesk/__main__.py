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


def port_in_use(host: str, port: int) -> bool:
    """True if something already answers on host:port. A connect test is used because on
    Windows two programs can end up bound to the same port, and the browser then reaches
    whichever one answers first."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def keep_awake() -> None:
    """Windows: ask the OS not to sleep while the panel runs (the display may still turn off).
    Released automatically when the process exits. Closing a laptop lid can still sleep it."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception as e:  # never block startup on this
        log.warning("could not disable sleep: %s", e)


def ask_password(prompt: str = "Vault master password: ") -> str:
    if not sys.stdin.isatty():
        raise SystemExit("the vault password must be typed interactively")
    return getpass.getpass(prompt)


def vault_command(args, vault: Vault) -> None:
    if args.action != "init" and not vault.exists():
        raise SystemExit("还没有创建保险库，请先运行: python -m arbdesk vault init\n"
                         "(no vault yet: run `python -m arbdesk vault init` first)")
    if args.action == "init" and vault.exists():
        raise SystemExit(f"保险库已存在: {vault.path}\n(vault already exists; use `vault set NAME` to add keys)")
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
    p.add_argument("--no-browser", action="store_true", help="don't open the panel in the browser")
    s = sub.add_parser("scan", help="run one scan (and AI analysis) now")
    s.add_argument("--no-llm", action="store_true")
    vp = sub.add_parser("venue", help="run one prediction venue: settle, scan, AI forecasts")
    vp.add_argument("name", choices=VENUES)
    vp.add_argument("--no-llm", action="store_true")
    sub.add_parser("tail", help="price crypto threshold contracts against Deribit options once")
    sm = sub.add_parser("sim", help="simulated account: start [--capital N --hours H] | status | stop | report [ID]")
    sm.add_argument("action", choices=["start", "status", "stop", "report"])
    sm.add_argument("id", nargs="?", type=int)
    sm.add_argument("--capital", type=float)
    sm.add_argument("--hours", type=float)
    dp = sub.add_parser("dip", help="dip-buy ladder: status | arm [--live] | check | cancel")
    dp.add_argument("action", choices=["status", "arm", "check", "cancel"])
    dp.add_argument("--live", action="store_true", help="confirm arming real-money resting orders")
    al = sub.add_parser("alerts", help="run the extreme-market checks once and list recent alerts")
    al.add_argument("--ack", action="store_true", help="mark all alerts as read")
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
    ck = sub.add_parser("check", help="check a venue's live readiness (no trading), or the AI model")
    ck.add_argument("name", choices=VENUES + ("llm",))
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
    executor = desk.executor  # shared, so simulated-account limits apply everywhere

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
        if port_in_use(settings.panel.host, settings.panel.port):
            raise SystemExit(
                f"端口 {settings.panel.port} 已被其他程序占用（浏览器会打开那个程序，而不是面板）。\n"
                f"请在 config.toml 的 [panel] 里把 port 改成别的数字，例如 port = 18799，然后重新启动。\n"
                f"(port {settings.panel.port} is already in use by another program; set [panel] port in config.toml)")
        token = load_or_create_token(settings.data_dir)
        if not args.no_scheduler:
            keep_awake()
            Scheduler(desk).start()
        url = f"http://127.0.0.1:{settings.panel.port}/?token={token}"
        modes = ", ".join(f"{n}={v.cfg.mode}" for n, v in desk.venues.items())
        print(f"\n  ArbDesk panel: {url}\n  (crypto={settings.risk.mode}, {modes}, "
              f"secrets from {settings.secrets.source})\n"
              f"  scans every {settings.scan.interval_minutes} min, AI at most every "
              f"{settings.llm.interval_minutes} min ({desk.analyst.model}); "
              f"budget {settings.llm.daily_token_budget} tokens/day, ${settings.llm.monthly_usd_cap}/month\n"
              f"  keep this window open; the computer must not sleep\n")
        if not args.no_browser:  # opening it for you avoids copying a long, wrapped link by hand
            import threading
            import webbrowser
            threading.Timer(2.0, lambda: webbrowser.open(url)).start()
        print(f"  浏览器没有自动打开的话，复制上面整行链接；token 也保存在 {settings.data_dir / 'panel_token'}\n")
        import uvicorn
        uvicorn.run(create_app(settings, store, desk, executor, token),
                    host=settings.panel.host, port=settings.panel.port, log_level="warning")
    elif args.cmd == "scan":
        print(json.dumps(desk.run_cycle(use_llm=not args.no_llm), ensure_ascii=False, indent=2))
    elif args.cmd == "sim":
        def show(r):
            if not r:
                print("no simulation"); return
            print(f"#{r['id']} {r['status']} · {r['start_ts'][:16]} → {(r.get('finished_ts') or r['end_ts'])[:16]} UTC")
            print(f"capital ${r['capital']:.2f} → equity ${r['equity']:.2f} ({r['return_pct']:+.2f}%) · "
                  f"cash ${r['cash']:.2f} (reserved ${r['reserved']:.2f}) · positions ${r['positions_value']:.2f}")
            print(f"realized {r['realized']:+.2f} · unrealized {r['unrealized']:+.2f} · fees {r['fees']:.2f} · "
                  f"AI ${r['ai_cost_usd']:.4f} · net after AI {r['net_after_ai']:+.2f}")
            for b in r["by_strategy"]:
                print(f"  {b['label']:<10} trades {b['trades']:>3} · spent ${b['spent']:.2f} · "
                      f"realized {b['realized']:+.2f} · unrealized {b.get('unrealized', 0):+.2f}")
        if args.action == "start":
            s_ = desk.sim.start(args.capital, args.hours)
            print(f"started simulation #{s_['id']}: ${s_['capital']:.2f} until {s_['end_ts']} UTC. "
                  "Keep `python -m arbdesk panel` running so it trades and records.")
        elif args.action == "stop":
            show(desk.sim.stop())
        elif args.action == "status":
            cur = desk.sim.current()
            show(desk.sim.snapshot(cur, record=False) if cur else
                 (desk.sim.report(store.sims(1)[0]["id"]) if store.sims(1) else None))
        else:
            sid = args.id or (store.sims(1)[0]["id"] if store.sims(1) else None)
            show(desk.sim.report(sid) if sid else None)
    elif args.cmd == "dip":
        if args.action == "arm":
            confirm = False
            if settings.dip.mode == "live":
                if not args.live:
                    raise SystemExit("dip.mode is live: use `dip arm --live` and type LIVE")
                confirm = input("Type LIVE to place resting real-money buy orders: ").strip() == "LIVE"
            print(json.dumps(desk.dip.arm(confirm_live=confirm), ensure_ascii=False, indent=2))
        elif args.action == "check":
            print(json.dumps(desk.dip.cycle(), ensure_ascii=False, indent=2))
        elif args.action == "cancel":
            print(f"cancelled {desk.dip.cancel_all()} order(s)")
        st = desk.dip.status()
        print(f"\n{st['mode']} on {st['exchange']} · resting ${st['open_usd']:.2f}/${st['max_open_usd']} · "
              f"filled this quarter ${st['filled_quarter_usd']:.2f}/${st['quarterly_budget_usd']}")
        for r in st["symbols"]:
            print(f"  {r['symbol']}: price {r['price']} · open " +
                  ", ".join(f"{o['level_pct']:.0f}%@{o['price']:,.2f}" for o in r["open"]) +
                  (f" · position {r['position_qty']:.6f} cost ${r['position_cost']:.2f}" if r["position_qty"] else ""))
    elif args.cmd == "alerts":
        if args.ack:
            store.ack_alerts()
        else:
            if settings.tail.enabled:
                desk.tail_cycle()  # fills the option smiles used by the volatility check
            fired = desk.alerts.check(desk.last_smiles)
            print(f"new alerts this run: {len(fired)}")
            for a in store.alerts(15):
                print(f"{a['ts'][:16]}  {a['level']:8} {'  ' if a['acked'] else '* '}{a['title']} — {a['detail']}")
    elif args.cmd == "tail":
        if "polymarket_us" in desk.venues:  # its contracts come from the latest venue scan
            try:
                desk.venues["polymarket_us"].scan()
            except Exception as e:
                print(f"polymarket_us scan failed: {type(e).__name__}: {e}")
        out = desk.tail_cycle()
        scan = store.scans(1, source="tail")[0]
        for r in scan["summary"]["rows"][:15]:
            print(f"{'*' if r['cheap'] else ' '} {r['venue']:13} {r['currency']} {r['kind']:11} "
                  f"{r['lo'] or r['hi']:>10} {r['expiry'][:10]} {r['side']:3} {r['price'] * 100:5.1f}c "
                  f"implied {r['implied'] * 100:5.1f}%  x{r['ratio']}")
        print(json.dumps(out, ensure_ascii=False, indent=2))
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
        if settings.dip.mode == "live":
            print(f"cancelled {desk.dip.cancel_all()} resting dip order(s)")
        print("live trading halted")
    elif args.cmd == "resume":
        executor.resume()
        print("kill switch lifted")
    elif args.cmd in ("check", "pm-check"):
        name = getattr(args, "name", "polymarket")
        try:
            if name == "llm":
                print(json.dumps({**desk.analyst.ping(), "budget": desk.analyst.budget_status()},
                                 ensure_ascii=False, indent=2))
                return
            print(json.dumps(executor.trader(name).readiness(), indent=2, default=str))
        except Exception as e:
            raise SystemExit(f"not ready: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
