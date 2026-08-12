"""SQLite persistence: scan runs, per-coin results, chart series, manual checks.

Everything the UI renders comes from here, so a page load never waits on a scan and
never touches the network. The full snapshot and plan JSON are stored verbatim —
nothing is recomputed or summarised on the way in, which is what makes every figure
on screen traceable back to the skill's own output.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,            -- running | done | failed | cancelled
    profile      TEXT NOT NULL,
    capital      REAL NOT NULL,
    capital_currency TEXT NOT NULL DEFAULT 'USDT',
    risk_pct     REAL NOT NULL,
    usdt_irt     REAL,                     -- rate used for cross-quote conversion
    total        INTEGER NOT NULL DEFAULT 0,
    completed    INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    current_coin TEXT,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id       INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    coin          TEXT NOT NULL,
    symbol        TEXT,
    quote         TEXT,
    fetched_at    TEXT NOT NULL,
    side          TEXT,
    side_tied     INTEGER NOT NULL DEFAULT 0,
    verdict       TEXT,                    -- TAKE | WATCH | INCOMPLETE | SKIP | ERROR
    score         REAL,
    score_coverage REAL,
    capital_used  REAL,
    snapshot_json TEXT,
    plan_json     TEXT,
    error         TEXT,
    UNIQUE(scan_id, coin)
);
CREATE INDEX IF NOT EXISTS idx_results_coin ON results(coin, scan_id DESC);

CREATE TABLE IF NOT EXISTS chart_series (
    coin        TEXT NOT NULL,
    role        TEXT NOT NULL,             -- decision | entry | bias | atr
    scan_id     INTEGER NOT NULL,
    timeframe   TEXT,
    updated_at  TEXT NOT NULL,
    series_json TEXT NOT NULL,
    PRIMARY KEY (coin, role)
);

-- A manual check is only as good as the scan it was confirmed against. Storing the
-- timestamp lets the UI expire a tick when fresher data arrives, instead of carrying
-- yesterday's confirmation into today's TAKE.
CREATE TABLE IF NOT EXISTS manual_checks (
    coin        TEXT NOT NULL,
    check_key   TEXT NOT NULL,
    resolved    INTEGER NOT NULL DEFAULT 0,
    note        TEXT,
    resolved_at TEXT,
    PRIMARY KEY (coin, check_key)
);

CREATE TABLE IF NOT EXISTS commentary (
    coin       TEXT NOT NULL,
    lang       TEXT NOT NULL,
    scan_id    INTEGER,
    text       TEXT,
    model      TEXT,
    status     TEXT,                       -- ok | rejected | unavailable
    reason     TEXT,
    reason_code   TEXT,                    -- machine-readable, so the UI can localise
    reason_params TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (coin, lang)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.ensure_dirs()
        conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        _local.conn = conn
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not add a
# column to a table that already exists, so an existing database would keep working
# right up until the first read of the new field.
_MIGRATIONS = (
    ("commentary", "reason_code", "TEXT"),
    ("commentary", "reason_params", "TEXT"),
)


def init() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA)
        for table, column, coltype in _MIGRATIONS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


# --------------------------------------------------------------------------------
# Scans
# --------------------------------------------------------------------------------


def start_scan(*, profile: str, capital: float, capital_currency: str,
               risk_pct: float, total: int, usdt_irt: float | None) -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO scans (started_at, status, profile, capital, "
            "capital_currency, risk_pct, usdt_irt, total) "
            "VALUES (?, 'running', ?, ?, ?, ?, ?, ?)",
            (now_iso(), profile, capital, capital_currency, risk_pct, usdt_irt, total),
        )
        return int(cur.lastrowid)


def update_scan(scan_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with tx() as conn:
        conn.execute(f"UPDATE scans SET {cols} WHERE id = ?",
                     (*fields.values(), scan_id))


def finish_scan(scan_id: int, status: str = "done", note: str | None = None) -> None:
    update_scan(scan_id, status=status, finished_at=now_iso(), note=note,
                current_coin=None)


def latest_scan() -> dict | None:
    row = connect().execute(
        "SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def running_scan() -> dict | None:
    row = connect().execute(
        "SELECT * FROM scans WHERE status = 'running' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def mark_stale_scans() -> None:
    """A scan left 'running' by a killed process would otherwise block the next one."""
    with tx() as conn:
        conn.execute(
            "UPDATE scans SET status='failed', finished_at=?, "
            "note='interrupted - process exited' WHERE status='running'",
            (now_iso(),))


# --------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------


def save_result(scan_id: int, coin: str, *, symbol: str | None, quote: str | None,
                side: str | None, side_tied: bool, snapshot: dict | None,
                plan: dict | None, capital_used: float | None,
                error: str | None = None) -> None:
    qual = (plan or {}).get("qualification") or {}
    verdict = qual.get("verdict") if plan else "ERROR"
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO results (scan_id, coin, symbol, quote, fetched_at,"
            " side, side_tied, verdict, score, score_coverage, capital_used,"
            " snapshot_json, plan_json, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scan_id, coin, symbol, quote, now_iso(), side, int(side_tied), verdict,
             qual.get("score"), qual.get("score_coverage"), capital_used,
             json.dumps(snapshot, ensure_ascii=False) if snapshot else None,
             json.dumps(plan, ensure_ascii=False) if plan else None,
             error),
        )


def latest_results() -> list[dict]:
    """Most recent result per coin, whichever scan it came from.

    Deliberately not 'results of the latest scan': a coin that errored in the current
    pass should keep showing its last good analysis with an honest timestamp, rather
    than vanishing from the board.
    """
    rows = connect().execute(
        "SELECT r.* FROM results r "
        "JOIN (SELECT coin, MAX(scan_id) AS scan_id FROM results GROUP BY coin) m "
        "  ON m.coin = r.coin AND m.scan_id = r.scan_id"
    ).fetchall()
    return [dict(r) for r in rows]


def result_for(coin: str) -> dict | None:
    row = connect().execute(
        "SELECT * FROM results WHERE coin = ? ORDER BY scan_id DESC LIMIT 1",
        (coin,)).fetchone()
    return dict(row) if row else None


def history_for(coin: str, limit: int = 60) -> list[dict]:
    rows = connect().execute(
        "SELECT r.scan_id, r.fetched_at, r.verdict, r.score, r.side, s.profile "
        "FROM results r JOIN scans s ON s.id = r.scan_id "
        "WHERE r.coin = ? AND r.verdict IS NOT NULL "
        "ORDER BY r.scan_id DESC LIMIT ?", (coin, limit)).fetchall()
    return [dict(r) for r in rows][::-1]


# --------------------------------------------------------------------------------
# Chart series
# --------------------------------------------------------------------------------


def save_series(coin: str, role: str, scan_id: int, timeframe: str | None,
                payload: dict) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chart_series "
            "(coin, role, scan_id, timeframe, updated_at, series_json) "
            "VALUES (?,?,?,?,?,?)",
            (coin, role, scan_id, timeframe, now_iso(),
             json.dumps(payload, ensure_ascii=False)),
        )


def series_for(coin: str, role: str = "decision") -> dict | None:
    row = connect().execute(
        "SELECT * FROM chart_series WHERE coin = ? AND role = ?",
        (coin, role)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["series"] = json.loads(out.pop("series_json"))
    return out


# --------------------------------------------------------------------------------
# Manual checks
# --------------------------------------------------------------------------------


def set_manual_check(coin: str, check_key: str, resolved: bool,
                     note: str | None = None) -> dict:
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO manual_checks "
            "(coin, check_key, resolved, note, resolved_at) VALUES (?,?,?,?,?)",
            (coin, check_key, int(resolved), note, now_iso() if resolved else None),
        )
    return {"coin": coin, "check_key": check_key, "resolved": resolved,
            "note": note, "resolved_at": now_iso() if resolved else None}


def manual_checks_for(coin: str) -> dict:
    rows = connect().execute(
        "SELECT check_key, resolved, note, resolved_at FROM manual_checks "
        "WHERE coin = ?", (coin,)).fetchall()
    return {r["check_key"]: dict(r) for r in rows}


def all_manual_checks() -> dict:
    rows = connect().execute(
        "SELECT coin, check_key, resolved, note, resolved_at FROM manual_checks"
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r["coin"], {})[r["check_key"]] = dict(r)
    return out


# --------------------------------------------------------------------------------
# Commentary
# --------------------------------------------------------------------------------


def save_commentary(coin: str, lang: str, *, scan_id: int | None, text: str | None,
                    model: str | None, status: str, reason: str | None = None,
                    reason_code: str | None = None,
                    reason_params: dict | None = None) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO commentary "
            "(coin, lang, scan_id, text, model, status, reason, reason_code, "
            " reason_params, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (coin, lang, scan_id, text, model, status, reason, reason_code,
             json.dumps(reason_params or {}, ensure_ascii=False), now_iso()))


def commentary_for(coin: str, lang: str) -> dict | None:
    row = connect().execute(
        "SELECT * FROM commentary WHERE coin = ? AND lang = ?", (coin, lang)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------------
# Housekeeping
# --------------------------------------------------------------------------------


def prune(keep_scans: int) -> int:
    with tx() as conn:
        row = conn.execute(
            "SELECT id FROM scans ORDER BY id DESC LIMIT 1 OFFSET ?",
            (keep_scans,)).fetchone()
        if not row:
            return 0
        cur = conn.execute("DELETE FROM scans WHERE id <= ?", (row["id"],))
        conn.execute("DELETE FROM results WHERE scan_id <= ?", (row["id"],))
        return cur.rowcount or 0


def get_kv(key: str, default=None):
    row = connect().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_kv(key: str, value) -> None:
    with tx() as conn:
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                     (key, json.dumps(value, ensure_ascii=False)))
