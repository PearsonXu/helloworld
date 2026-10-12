"""CLI: python -m arbdesk [panel|scan|polymarket|proposals|approve ID|reject ID]"""
from __future__ import annotations

import argparse
import json
import logging

from .config import load_settings, setup_logging
from .executor import Executor
from .service import Desk, Scheduler
from .storage import Store
from .web import create_app, is_loopback, load_or_create_token

log = logging.getLogger("arbdesk")


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
    for name in ("approve", "reject"):
        sub.add_parser(name).add_argument("id", type=int)
    args = ap.parse_args()

    settings = load_settings()
    setup_logging(settings)
    store = Store(settings.data_dir / "arbdesk.db")
    desk = Desk(settings, store)
    executor = Executor(settings, store)

    if settings.risk.mode == "live":
        log.warning("LIVE mode: make sure exchange API keys have trading only — "
                    "withdrawals disabled and IP whitelist set.")

    if args.cmd == "panel":
        if not is_loopback(settings.panel.host):
            raise SystemExit("panel.host must be a loopback address (127.0.0.1); "
                             "use an SSH tunnel if you need remote access")
        token = load_or_create_token(settings.data_dir)
        if not args.no_scheduler:
            Scheduler(desk).start()
        url = f"http://127.0.0.1:{settings.panel.port}/?token={token}"
        print(f"\n  ArbDesk panel: {url}\n  (mode={settings.risk.mode}; token stored in data/panel_token)\n")
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
        print(json.dumps(executor.execute(args.id), ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "reject":
        store.set_proposal_status(args.id, "rejected", "rejected by user")


if __name__ == "__main__":
    main()
