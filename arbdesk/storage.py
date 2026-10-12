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
    source TEXT NOT NULL DEFAULT 'crypto',  -- crypto / polymarket
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
CREATE TABLE IF NOT EXISTS pm_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    market_id TEXT NOT NULL,
    question TEXT NOT NULL,
    end_date TEXT,
    market_prob REAL NOT NULL,      -- YES ask at forecast time
    ai_prob REAL NOT NULL,
    confidence REAL,
    rationale TEXT,
    outcome INTEGER,                -- 1 YES / 0 NO once resolved
    resolved_ts TEXT
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
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(scans)")}
            if "source" not in cols:  # databases created before the Polymarket module
                self.conn.execute("ALTER TABLE scans ADD COLUMN source TEXT NOT NULL DEFAULT 'crypto'")
        if new and os.name == "posix":
            os.chmod(path, 0o600)

    def _insert(self, sql: str, params: tuple) -> int:
        with self.lock, self.conn:
            return self.conn.execute(sql, params).lastrowid

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # scans -------------------------------------------------------------
    def add_scan(self, summary: dict, opportunities: list[dict], source: str = "crypto") -> int:
        return self._insert(
            "INSERT INTO scans (ts, source, summary, opportunities) VALUES (?, ?, ?, ?)",
            (now_iso(), source, json.dumps(summary, ensure_ascii=False),
             json.dumps(opportunities, ensure_ascii=False)),
        )

    def scans(self, limit: int = 20, source: str | None = None) -> list[dict]:
        if source:
            rows = self._rows("SELECT * FROM scans WHERE source = ? ORDER BY id DESC LIMIT ?", (source, limit))
        else:
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

    def open_pm_trades(self) -> list[dict]:
        return self._rows("SELECT * FROM trades WHERE exchange = 'polymarket' AND pnl_usd IS NULL")

    def settle_trade(self, tid: int, pnl: float) -> None:
        with self.lock, self.conn:
            self.conn.execute("UPDATE trades SET pnl_usd = ? WHERE id = ?", (pnl, tid))

    # polymarket forecasts ----------------------------------------------
    def add_forecast(self, **f) -> int:
        cols = ["market_id", "question", "end_date", "market_prob", "ai_prob", "confidence", "rationale"]
        return self._insert(
            f"INSERT INTO pm_forecasts (ts, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
            (now_iso(), *[f.get(c) for c in cols]),
        )

    def forecasts(self, limit: int = 100) -> list[dict]:
        return self._rows("SELECT * FROM pm_forecasts ORDER BY id DESC LIMIT ?", (limit,))

    def unresolved_forecast_markets(self) -> list[str]:
        return [r["market_id"] for r in
                self._rows("SELECT DISTINCT market_id FROM pm_forecasts WHERE outcome IS NULL")]

    def resolve_forecasts(self, market_id: str, outcome: int) -> None:
        with self.lock, self.conn:
            self.conn.execute("UPDATE pm_forecasts SET outcome = ?, resolved_ts = ? "
                              "WHERE market_id = ? AND outcome IS NULL", (outcome, now_iso(), market_id))

    def forecast_scores(self) -> dict:
        rows = self._rows("SELECT ai_prob, market_prob, outcome FROM pm_forecasts WHERE outcome IS NOT NULL")
        n = len(rows)
        if not n:
            return {"resolved": 0, "ai_brier": None, "market_brier": None}
        ai = sum((r["ai_prob"] - r["outcome"]) ** 2 for r in rows) / n
        mk = sum((r["market_prob"] - r["outcome"]) ** 2 for r in rows) / n
        return {"resolved": n, "ai_brier": round(ai, 4), "market_brier": round(mk, 4)}

    def today_stats(self) -> dict:
        today = now_iso()[:10] + "%"
        row = self._rows(
            "SELECT COUNT(DISTINCT proposal_id) AS n, COALESCE(SUM(pnl_usd), 0) AS pnl, "
            "COALESCE(SUM(fee_usd), 0) AS fees FROM trades WHERE ts LIKE ?",
            (today,),
        )[0]
        return {"trades": row["n"], "pnl_usd": row["pnl"], "fees_usd": row["fees"]}
