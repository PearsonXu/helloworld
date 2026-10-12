"""Scan -> AI analysis -> pending proposals, plus a simple daily scheduler."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from . import risk
from .config import Settings
from .llm import Analyst
from .scanners import run_scan
from .storage import Store

log = logging.getLogger(__name__)


class Desk:
    def __init__(self, settings: Settings, store: Store, analyst: Analyst | None = None):
        self.settings = settings
        self.store = store
        self.analyst = analyst or Analyst(settings)
        self._lock = threading.Lock()
        self.running = False

    def run_cycle(self, use_llm: bool = True) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"skipped": "a cycle is already running"}
        self.running = True
        try:
            summary, opps, overview = run_scan(self.settings.scan)
            scan_id = self.store.add_scan({**summary, "market": overview}, opps)
            log.info("scan %s: %d opportunities, errors=%s", scan_id, len(opps), list(summary["errors"]))
            result = {"scan_id": scan_id, "opportunities": len(opps), "errors": summary["errors"]}
            if use_llm:
                result.update(self.analyze(scan_id, opps, overview))
            return result
        finally:
            self.running = False
            self._lock.release()

    def analyze(self, scan_id: int, opps: list[dict], overview: list[dict]) -> dict:
        if not self.analyst.available:
            return {"llm": "not configured"}
        if self.store.llm_calls_today() >= self.settings.llm.max_calls_per_day:
            return {"llm": "daily call budget used up"}
        try:
            content = self.analyst.analyze(opps, overview)
        except Exception as e:
            log.warning("LLM analysis failed: %s", e)
            return {"llm": f"error: {type(e).__name__}"}
        analysis_id = self.store.add_analysis(scan_id, self.analyst.provider, self.analyst.model, content)
        created = 0
        for prop in content.get("proposals", [])[:2]:
            if not isinstance(prop, dict):
                continue
            # Pre-check so the panel shows why a proposal can't run; still requires approval.
            _, errors = risk.validate(prop, self.settings, self.store)
            self.store.add_proposal(analysis_id, prop, "pending", "; ".join(errors))
            created += 1
        return {"analysis_id": analysis_id, "proposals": created}


class Scheduler(threading.Thread):
    """Runs a cycle at each configured local time (HH:MM) once per day."""

    def __init__(self, desk: Desk):
        super().__init__(daemon=True, name="scheduler")
        self.desk = desk
        self.done: set[str] = set()

    def run(self) -> None:
        while True:
            now = datetime.now()
            stamp = now.strftime("%Y-%m-%d %H:%M")
            if now.strftime("%H:%M") in self.desk.settings.scan.times and stamp not in self.done:
                self.done.add(stamp)
                try:
                    self.desk.run_cycle()
                except Exception:
                    log.exception("scheduled cycle failed")
            time.sleep(20)
