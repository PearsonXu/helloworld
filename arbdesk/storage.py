"""Local SQLite storage for scans, AI analyses, proposals and trades."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    summary TEXT NOT NULL,          -- JSON: counts, errors
    opportunities TEXT NOT NULL     -- JSON list
);
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER REFERENCES scans(id),
    ts TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    content TEXT NOT NULL           -- JSON returned by the model
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER REFERENCES analyses(id),
    ts TEXT NOT NULL,
    status TEXT NOT NULL,           -- pending / approved / rejected / executed / failed
    payload TEXT NOT NULL,          -- JSON: legs, rationale
    note TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER REFERENCES proposals(id),
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,             -- paper / live
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    amount REAL NOT NULL,
    price REAL NOT NULL,
    cost_usd REAL NOT NULL,
    fee_usd REAL NOT NULL DEFAULT 0,
    pnl_usd REAL,
    order_id TEXT,
    raw TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        new = not path.exists()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
        if new and os.name == "posix":
            os.chmod(path, 0o600)

    def _insert(self, sql: str, params: tuple) -> int:
        with self.lock, self.conn:
            return self.conn.execute(sql, params).lastrowid

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # scans -------------------------------------------------------------
    def add_scan(self, summary: dict, opportunities: list[dict]) -> int:
        return self._insert(
            "INSERT INTO scans (ts, summary, opportunities) VALUES (?, ?, ?)",
            (now_iso(), json.dumps(summary), json.dumps(opportunities)),
        )

    def scans(self, limit: int = 20) -> list[dict]:
        rows = self._rows("SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["summary"] = json.loads(r["summary"])
            r["opportunities"] = json.loads(r["opportunities"])
        return rows

    # analyses ----------------------------------------------------------
    def add_analysis(self, scan_id: int, provider: str, model: str, content: dict) -> int:
        return self._insert(
            "INSERT INTO analyses (scan_id, ts, provider, model, content) VALUES (?, ?, ?, ?, ?)",
            (scan_id, now_iso(), provider, model, json.dumps(content, ensure_ascii=False)),
        )

    def analyses(self, limit: int = 20) -> list[dict]:
        rows = self._rows("SELECT * FROM analyses ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["content"] = json.loads(r["content"])
        return rows

    def llm_calls_today(self) -> int:
        today = now_iso()[:10]
        return self._rows("SELECT COUNT(*) AS n FROM analyses WHERE ts LIKE ?", (today + "%",))[0]["n"]

    # proposals ---------------------------------------------------------
    def add_proposal(self, analysis_id: int | None, payload: dict, status: str = "pending", note: str = "") -> int:
        return self._insert(
            "INSERT INTO proposals (analysis_id, ts, status, payload, note) VALUES (?, ?, ?, ?, ?)",
            (analysis_id, now_iso(), status, json.dumps(payload, ensure_ascii=False), note),
        )

    def proposal(self, pid: int) -> dict | None:
        rows = self._rows("SELECT * FROM proposals WHERE id = ?", (pid,))
        if not rows:
            return None
        rows[0]["payload"] = json.loads(rows[0]["payload"])
        return rows[0]

    def proposals(self, limit: int = 50) -> list[dict]:
        rows = self._rows("SELECT * FROM proposals ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    def set_proposal_status(self, pid: int, status: str, note: str = "") -> None:
        with self.lock, self.conn:
            self.conn.execute("UPDATE proposals SET status = ?, note = ? WHERE id = ?", (status, note, pid))

    # trades ------------------------------------------------------------
    def add_trade(self, **t) -> int:
        cols = ["proposal_id", "mode", "exchange", "symbol", "side", "amount", "price",
                "cost_usd", "fee_usd", "pnl_usd", "order_id", "raw"]
        vals = [t.get(c) for c in cols]
        return self._insert(
            f"INSERT INTO trades (ts, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
            (now_iso(), *vals),
        )

    def trades(self, limit: int = 100) -> list[dict]:
        return self._rows("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))

    def today_stats(self) -> dict:
        today = now_iso()[:10] + "%"
        row = self._rows(
            "SELECT COUNT(DISTINCT proposal_id) AS n, COALESCE(SUM(pnl_usd), 0) AS pnl, "
            "COALESCE(SUM(fee_usd), 0) AS fees FROM trades WHERE ts LIKE ?",
            (today,),
        )[0]
        return {"trades": row["n"], "pnl_usd": row["pnl"], "fees_usd": row["fees"]}
