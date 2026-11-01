"""Local SQLite storage for scans, AI analyses, proposals and trades."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
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
    note TEXT,
    dedupe_key TEXT                 -- same opportunity seen again refreshes the pending row
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
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    purpose TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER NOT NULL,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pair_matches (
    key TEXT PRIMARY KEY,           -- "<venue_a>:<market_a>|<venue_b>:<market_b>"
    ts TEXT NOT NULL,
    venue_a TEXT NOT NULL, market_a TEXT NOT NULL, title_a TEXT,
    venue_b TEXT NOT NULL, market_b TEXT NOT NULL, title_b TEXT,
    similarity REAL,
    verdict TEXT NOT NULL,          -- same / different
    confidence REAL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS flags (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pm_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    venue TEXT NOT NULL DEFAULT 'polymarket',
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
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(pm_forecasts)")}
            if "venue" not in cols:  # databases created before Kalshi / Polymarket US
                self.conn.execute("ALTER TABLE pm_forecasts ADD COLUMN venue TEXT NOT NULL DEFAULT 'polymarket'")
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(proposals)")}
            if "dedupe_key" not in cols:
                self.conn.execute("ALTER TABLE proposals ADD COLUMN dedupe_key TEXT")
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
        return self.usage_today()["calls"]

    # LLM usage ----------------------------------------------------------
    def add_usage(self, **u) -> int:
        cols = ["purpose", "model", "input_tokens", "cached_tokens", "output_tokens", "cost_usd"]
        return self._insert(
            f"INSERT INTO llm_usage (ts, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
            (now_iso(), *[u.get(c) for c in cols]),
        )

    def usage_today(self) -> dict:
        r = self._rows("SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens, "
                       "COALESCE(SUM(cost_usd), 0) AS usd FROM llm_usage WHERE ts LIKE ?",
                       (now_iso()[:10] + "%",))[0]
        return {"calls": r["calls"], "tokens": r["tokens"], "usd": r["usd"]}

    def usage_month_usd(self) -> float:
        return self._rows("SELECT COALESCE(SUM(cost_usd), 0) AS usd FROM llm_usage WHERE ts LIKE ?",
                          (now_iso()[:7] + "%",))[0]["usd"]

    def usage_by_purpose(self, days: int = 1) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        return self._rows("SELECT purpose, COUNT(*) AS calls, SUM(input_tokens) AS input, "
                          "SUM(cached_tokens) AS cached, SUM(output_tokens) AS output, SUM(cost_usd) AS usd "
                          "FROM llm_usage WHERE ts >= ? GROUP BY purpose ORDER BY usd DESC", (since,))

    # proposals ---------------------------------------------------------
    def add_proposal(self, analysis_id: int | None, payload: dict, status: str = "pending", note: str = "",
                     dedupe_key: str | None = None) -> int:
        """With a dedupe_key, a still-pending proposal for the same opportunity is refreshed
        (latest prices, note, timestamp) instead of adding a duplicate."""
        body = json.dumps(payload, ensure_ascii=False)
        if dedupe_key:
            with self.lock, self.conn:
                row = self.conn.execute("SELECT id FROM proposals WHERE dedupe_key = ? AND status = 'pending' "
                                        "ORDER BY id DESC LIMIT 1", (dedupe_key,)).fetchone()
                if row:
                    self.conn.execute("UPDATE proposals SET payload = ?, note = ?, ts = ?, analysis_id = "
                                      "COALESCE(?, analysis_id) WHERE id = ?",
                                      (body, note, now_iso(), analysis_id, row["id"]))
                    return row["id"]
        return self._insert(
            "INSERT INTO proposals (analysis_id, ts, status, payload, note, dedupe_key) VALUES (?, ?, ?, ?, ?, ?)",
            (analysis_id, now_iso(), status, body, note, dedupe_key),
        )

    # cross-venue pair matches -------------------------------------------
    def pair_keys(self) -> set[str]:
        return {r["key"] for r in self._rows("SELECT key FROM pair_matches")}

    def add_pair_match(self, **m) -> None:
        cols = ["key", "venue_a", "market_a", "title_a", "venue_b", "market_b", "title_b",
                "similarity", "verdict", "confidence", "reason"]
        with self.lock, self.conn:
            self.conn.execute(
                f"INSERT OR REPLACE INTO pair_matches (ts, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
                (now_iso(), *[m.get(c) for c in cols]))

    def matched_pairs(self, min_confidence: float, limit: int = 200) -> list[dict]:
        return self._rows("SELECT * FROM pair_matches WHERE verdict = 'same' AND confidence >= ? "
                          "ORDER BY ts DESC LIMIT ?", (min_confidence, limit))

    def pair_matches(self, limit: int = 50) -> list[dict]:
        return self._rows("SELECT * FROM pair_matches ORDER BY ts DESC LIMIT ?", (limit,))

    # black-swan budget --------------------------------------------------
    @staticmethod
    def quarter_start() -> str:
        now = datetime.now(timezone.utc)
        return datetime(now.year, 3 * ((now.month - 1) // 3) + 1, 1, tzinfo=timezone.utc).isoformat(timespec="seconds")

    def tail_spent_quarter(self) -> float:
        """Cost of tail ("black swan") bets placed this calendar quarter, paper and live."""
        return self._rows(
            "SELECT COALESCE(SUM(t.cost_usd + t.fee_usd), 0) AS s FROM trades t "
            "JOIN proposals p ON p.id = t.proposal_id "
            "WHERE json_extract(p.payload, '$.kind') = 'tail' AND t.ts >= ?", (self.quarter_start(),))[0]["s"]

    # maintenance --------------------------------------------------------
    def prune_scans(self, keep_days: float = 2.0) -> int:
        """Frequent scans add up: drop old scans that found nothing (keeps the latest per source)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(timespec="seconds")
        with self.lock, self.conn:
            cur = self.conn.execute(
                "DELETE FROM scans WHERE ts < ? AND opportunities = '[]' "
                "AND id NOT IN (SELECT MAX(id) FROM scans GROUP BY source)", (cutoff,))
            return cur.rowcount

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

    # flags (kill switch) -----------------------------------------------
    def set_flag(self, name: str, value: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO flags (name, value, ts) VALUES (?, ?, ?) "
                              "ON CONFLICT(name) DO UPDATE SET value = excluded.value, ts = excluded.ts",
                              (name, value, now_iso()))

    def get_flag(self, name: str) -> dict | None:
        rows = self._rows("SELECT * FROM flags WHERE name = ?", (name,))
        return rows[0] if rows else None

    def halted(self) -> str | None:
        """Reason string when live trading is halted, else None."""
        f = self.get_flag("halt")
        return f["value"] if f and f["value"] else None

    def live_spent_today(self, venue: str) -> float:
        row = self._rows("SELECT COALESCE(SUM(cost_usd + fee_usd), 0) AS s FROM trades "
                         "WHERE exchange = ? AND mode = 'live' AND ts LIKE ?",
                         (venue, now_iso()[:10] + "%"))[0]
        return row["s"]

    def live_open_exposure(self, venue: str) -> float:
        row = self._rows("SELECT COALESCE(SUM(cost_usd + fee_usd), 0) AS s FROM trades "
                         "WHERE exchange = ? AND mode = 'live' AND pnl_usd IS NULL", (venue,))[0]
        return row["s"]

    def open_venue_trades(self, venue: str) -> list[dict]:
        """Unsettled prediction-market positions (paper and live) on one venue."""
        return self._rows("SELECT * FROM trades WHERE exchange = ? AND pnl_usd IS NULL", (venue,))

    def settle_trade(self, tid: int, pnl: float) -> None:
        with self.lock, self.conn:
            self.conn.execute("UPDATE trades SET pnl_usd = ? WHERE id = ?", (pnl, tid))

    # prediction-market forecasts ----------------------------------------
    def add_forecast(self, venue: str = "polymarket", **f) -> int:
        cols = ["market_id", "question", "end_date", "market_prob", "ai_prob", "confidence", "rationale"]
        return self._insert(
            f"INSERT INTO pm_forecasts (ts, venue, {', '.join(cols)}) VALUES (?, ?, {', '.join('?' * len(cols))})",
            (now_iso(), venue, *[f.get(c) for c in cols]),
        )

    def forecasts(self, limit: int = 100, venue: str | None = None) -> list[dict]:
        if venue:
            return self._rows("SELECT * FROM pm_forecasts WHERE venue = ? ORDER BY id DESC LIMIT ?", (venue, limit))
        return self._rows("SELECT * FROM pm_forecasts ORDER BY id DESC LIMIT ?", (limit,))

    def unresolved_forecast_markets(self, venue: str) -> list[str]:
        return [r["market_id"] for r in self._rows(
            "SELECT DISTINCT market_id FROM pm_forecasts WHERE outcome IS NULL AND venue = ?", (venue,))]

    def resolve_forecasts(self, venue: str, market_id: str, outcome: int) -> None:
        with self.lock, self.conn:
            self.conn.execute("UPDATE pm_forecasts SET outcome = ?, resolved_ts = ? "
                              "WHERE venue = ? AND market_id = ? AND outcome IS NULL",
                              (outcome, now_iso(), venue, market_id))

    def last_forecast(self, venue: str, market_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM pm_forecasts WHERE venue = ? AND market_id = ? ORDER BY id DESC LIMIT 1",
                          (venue, market_id))
        return rows[0] if rows else None

    def forecast_scores(self, venue: str | None = None) -> dict:
        """Brier scores over resolved markets, counting each market ONCE (its latest
        forecast, with the market price recorded at that same moment). Re-forecasting a
        market many times must not give it extra weight."""
        sql = ("SELECT f.ai_prob, f.market_prob, f.outcome FROM pm_forecasts f JOIN "
               "(SELECT MAX(id) AS id FROM pm_forecasts WHERE outcome IS NOT NULL GROUP BY venue, market_id) l "
               "ON f.id = l.id")
        rows = self._rows(sql + " WHERE f.venue = ?", (venue,)) if venue else self._rows(sql)
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
