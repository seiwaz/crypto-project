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

# One shared connection, not one per thread.
#
# ThreadingHTTPServer creates a thread per request, so a thread-local connection
# leaked a SQLite handle (plus its -wal and -shm) on every single request — 62 open
# handles and 70 MB RSS after a few minutes of normal polling, climbing without
# bound. sqlite3.threadsafety is 3 (serialized) here, so one connection is safe to
# share; the lock serialises whole transactions, which the C layer does not do for us.
_conn: sqlite3.Connection | None = None
_conn_lock = threading.RLock()

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
    exchange      TEXT,
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

-- Paper trading. One account row, enforced by the CHECK: two accounts would make
-- "current equity vs the 1000 start" ambiguous, and the report depends on that
-- reconciling exactly.
CREATE TABLE IF NOT EXISTS paper_account (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    exchange         TEXT NOT NULL,
    starting_capital REAL NOT NULL,
    balance          REAL NOT NULL,       -- realised cash, excludes open PnL
    slots            INTEGER NOT NULL,
    heat_cap_pct     REAL NOT NULL,
    created_at       TEXT NOT NULL,
    reset_at         TEXT
);

-- Open and closed positions share a table so the report and the live board read the
-- same rows; `status` separates them. Costs are stored as they accrue rather than
-- recomputed at close, because funding depends on how long the position was actually
-- held and at what rate each period.
CREATE TABLE IF NOT EXISTS paper_positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    coin          TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    exchange      TEXT NOT NULL,
    side          TEXT NOT NULL,
    status        TEXT NOT NULL,          -- open | closed
    slot          INTEGER,
    contracts     REAL NOT NULL,
    entry_price   REAL NOT NULL,
    leverage      REAL NOT NULL,
    margin        REAL NOT NULL,
    risk_amount   REAL NOT NULL,          -- 1R in USDT, fixes the R scale for life
    stop          REAL,
    tp1           REAL,
    tp2           REAL,
    opened_at     TEXT NOT NULL,
    opened_ts     REAL NOT NULL,
    bars_held     INTEGER NOT NULL DEFAULT 0,
    entry_fee     REAL NOT NULL DEFAULT 0,
    exit_fee      REAL NOT NULL DEFAULT 0,
    funding_paid  REAL NOT NULL DEFAULT 0,
    funding_periods INTEGER NOT NULL DEFAULT 0,
    -- MFE is the diagnostic for a stalled trade: ran to +1.4R and came back is a
    -- management failure, never moved is a thesis failure. Same P&L, different fix.
    mfe_r         REAL,
    mae_r         REAL,
    tp1_filled    INTEGER NOT NULL DEFAULT 0,
    realised_partial REAL NOT NULL DEFAULT 0,
    stop_moved_to_be INTEGER NOT NULL DEFAULT 0,
    original_contracts REAL,
    closed_at     TEXT,
    exit_price    REAL,
    exit_reason   TEXT,                   -- tp1 | tp2 | stopped | liquidated | time_stop | review_exit
    realised_pnl  REAL,
    scan_id       INTEGER,
    score         REAL,
    verdict       TEXT,
    plan_json     TEXT,
    context_json  TEXT                    -- the signal context at entry, for the report
);
CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_positions(status, opened_ts DESC);

-- Every action the manager takes, with its reasons. This is what makes an automated
-- demo auditable after the fact rather than a black box that changed its mind.
CREATE TABLE IF NOT EXISTS paper_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER REFERENCES paper_positions(id) ON DELETE CASCADE,
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,            -- open | funding | action | close | skip
    action      TEXT,                     -- HOLD | MOVE_STOP_BE | REDUCE | CLOSE
    amount      REAL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_events ON paper_events(position_id, id);

-- The path a position took, one row per cycle.
--
-- MFE and MAE on the position row are scalars, and a scalar cannot distinguish
-- "reached +1.4R twenty minutes in, then bled out over two days" from "reached
-- +1.4R on the final bar". Those have opposite fixes, so the shape is kept, not
-- just its extremes. Five positions on a 60s cycle is roughly 7k rows a day.
CREATE TABLE IF NOT EXISTS paper_samples (
    position_id  INTEGER NOT NULL REFERENCES paper_positions(id) ON DELETE CASCADE,
    at           TEXT NOT NULL,
    ts           REAL NOT NULL,
    hours_held   REAL,
    mark         REAL,
    unrealised   REAL,
    r            REAL,
    margin_ratio REAL
);
CREATE INDEX IF NOT EXISTS idx_paper_samples ON paper_samples(position_id, ts);

-- Every candidate the filler considered, taken or not.
--
-- This is the counterfactual, and it is the most valuable thing here for improving
-- selection: if the trades that were declined would have outperformed the ones
-- taken, the problem is the ranking, not the management. Without a record of what
-- was passed over there is no way to ask that question at all. `mark` is the price
-- at the moment of the decision, so the outcome can be reconstructed later from
-- candles without sampling every rejected coin continuously.
CREATE TABLE IF NOT EXISTS paper_decisions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    ts       REAL NOT NULL,
    scan_id  INTEGER,
    coin     TEXT NOT NULL,
    symbol   TEXT,
    side     TEXT,
    score    REAL,
    rank     INTEGER,
    action   TEXT NOT NULL,      -- opened | declined
    code     TEXT,               -- insufficient_margin | heat_cap | slots_full | ...
    mark     REAL,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_decisions ON paper_decisions(ts DESC, coin);

-- ---------------------------------------------------------------------------
-- LIVE positions on Tabdeal. Real money.
--
-- Deliberately a separate table from paper_positions rather than a flag on it.
-- One bad JOIN or a forgotten `WHERE live = 0` would let the demo's reporting,
-- resets and management touch real positions; separate tables make that mistake
-- impossible to write by accident.
--
-- These rows are ANNOTATIONS, not authority. The exchange is the only thing that
-- knows what is actually open — `live.reconcile()` reads positionRisk every cycle
-- and this table records the plan levels and timing the venue does not store.
CREATE TABLE IF NOT EXISTS live_positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    coin          TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    status        TEXT NOT NULL,          -- pending | open | closed | orphan
    quantity      REAL NOT NULL,
    entry_price   REAL,
    plan_entry    REAL,
    leverage      REAL,
    risk_amount   REAL,                   -- 1R in USDT, fixed at entry
    stop          REAL,
    tp1           REAL,
    tp2           REAL,
    order_id      TEXT,
    venue_position_id TEXT,               -- needed by positionSlTp
    sl_tp_set     INTEGER NOT NULL DEFAULT 0,
    tp1_filled    INTEGER NOT NULL DEFAULT 0,
    opened_at     TEXT,
    opened_ts     REAL,
    closed_at     TEXT,
    exit_price    REAL,
    exit_reason   TEXT,
    realised_pnl  REAL,
    scan_id       INTEGER,
    score         REAL,
    plan_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_status ON live_positions(status, opened_ts DESC);

CREATE TABLE IF NOT EXISTS live_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER REFERENCES live_positions(id) ON DELETE CASCADE,
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_events ON live_events(position_id, id);

-- One row per candidate per scan per outcome.
--
-- The filler runs every 60s but the candidate set only changes when a scan does, so
-- without this a full board would write the same "slots_full" verdict for the same
-- thirteen coins 1440 times a day. Deduplicating on the scan keeps the table a
-- record of distinct decisions rather than a record of how often the timer fired.
CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_decisions_unique
    ON paper_decisions(scan_id, coin, action, code);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    global _conn
    with _conn_lock:
        if _conn is None:
            config.ensure_dirs()
            conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            _conn = conn
        return _conn


@contextmanager
def tx():
    conn = connect()
    with _conn_lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _rows(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Guarded read. Reads share the connection with the scanner's writes, so they
    take the same lock rather than interleaving with an open transaction."""
    with _conn_lock:
        return connect().execute(sql, params).fetchall()


def _row(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    rows = _rows(sql, params)
    return rows[0] if rows else None


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not add a
# column to a table that already exists, so an existing database would keep working
# right up until the first read of the new field.
_MIGRATIONS = (
    ("commentary", "reason_code", "TEXT"),
    ("commentary", "reason_params", "TEXT"),
    # Results are venue-specific. Without this, switching exchanges left the other
    # venue's rows on the board — a Nobitex GRAM card sitting among Toobit prices,
    # with nothing on screen to say which venue it came from.
    ("results", "exchange", "TEXT"),
    ("chart_series", "exchange", "TEXT"),
    # Commentary describes one specific analysis. Without the venue it survived an
    # exchange switch and sat under a contradicting verdict — "advises skipping this
    # trade" printed beneath a TAKE.
    ("commentary", "exchange", "TEXT"),
    # TP1 is a partial exit, so it must fire once and only once. Without this flag a
    # position that oscillates around TP1 would be halved on every cycle.
    ("paper_positions", "tp1_filled", "INTEGER NOT NULL DEFAULT 0"),
    ("paper_positions", "realised_partial", "REAL NOT NULL DEFAULT 0"),
    ("paper_positions", "stop_moved_to_be", "INTEGER NOT NULL DEFAULT 0"),
    ("paper_positions", "original_contracts", "REAL"),
    # Execution drift: the plan named an entry, the fill happened at mark. That gap
    # is real slippage and was never recorded, so it could never be measured.
    ("paper_positions", "plan_entry", "REAL"),
    ("paper_positions", "entry_slippage_pct", "REAL"),
    # Regime and competition at entry, so results can be bucketed by the conditions
    # they were taken in rather than treated as one undifferentiated sample.
    ("paper_positions", "btc_bias", "TEXT"),
    ("paper_positions", "takes_available", "INTEGER"),
    # When the extremes happened, not just how large they were.
    ("paper_positions", "mfe_at", "TEXT"),
    ("paper_positions", "mae_at", "TEXT"),
    ("paper_positions", "mfe_hours", "REAL"),
    ("paper_positions", "mae_hours", "REAL"),
    # Maker entries wait at a limit rather than crossing the spread, so a position
    # now has a life before it is filled — and may never be.
    ("paper_positions", "limit_price", "REAL"),
    ("paper_positions", "placed_at", "TEXT"),
    ("paper_positions", "placed_ts", "REAL"),
    ("paper_positions", "maker", "INTEGER NOT NULL DEFAULT 0"),
)


def init() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA)
        added = set()
        for table, column, coltype in _MIGRATIONS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
                added.add((table, column))
        # Every row that predates the column came from Nobitex — it was the only
        # venue. Leaving them NULL would make them invisible to both venues' filters
        # rather than merely stale.
        if ("results", "exchange") in added:
            conn.execute("UPDATE results SET exchange = 'nobitex' WHERE exchange IS NULL")
        if ("chart_series", "exchange") in added:
            conn.execute("UPDATE chart_series SET exchange = 'nobitex' "
                         "WHERE exchange IS NULL")


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
    row = _row("SELECT * FROM scans ORDER BY id DESC LIMIT 1")
    return dict(row) if row else None


def running_scan() -> dict | None:
    row = _row("SELECT * FROM scans WHERE status = 'running' ORDER BY id DESC LIMIT 1")
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
                exchange: str | None = None, error: str | None = None) -> None:
    qual = (plan or {}).get("qualification") or {}
    verdict = qual.get("verdict") if plan else "ERROR"
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO results (scan_id, coin, exchange, symbol, quote,"
            " fetched_at, side, side_tied, verdict, score, score_coverage,"
            " capital_used, snapshot_json, plan_json, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scan_id, coin, exchange, symbol, quote, now_iso(), side, int(side_tied), verdict,
             qual.get("score"), qual.get("score_coverage"), capital_used,
             json.dumps(snapshot, ensure_ascii=False) if snapshot else None,
             json.dumps(plan, ensure_ascii=False) if plan else None,
             error),
        )


def latest_results(exchange: str | None = None) -> list[dict]:
    """Most recent result per coin on one venue.

    Deliberately not 'results of the latest scan': a coin that errored in the current
    pass should keep showing its last good analysis with an honest timestamp, rather
    than vanishing from the board. But it *is* scoped to the venue — a result from
    the other exchange is a different instrument at a different price, and mixing
    them put a Nobitex GRAM card in among Toobit's.
    """
    if exchange:
        rows = _rows(
            "SELECT r.* FROM results r JOIN ("
            "  SELECT coin, MAX(scan_id) AS scan_id FROM results "
            "  WHERE exchange = ? GROUP BY coin) m "
            "ON m.coin = r.coin AND m.scan_id = r.scan_id WHERE r.exchange = ?",
            (exchange, exchange))
    else:
        rows = _rows(
            "SELECT r.* FROM results r "
            "JOIN (SELECT coin, MAX(scan_id) AS scan_id FROM results GROUP BY coin) m "
            "  ON m.coin = r.coin AND m.scan_id = r.scan_id")
    return [dict(r) for r in rows]


def result_for(coin: str, exchange: str | None = None) -> dict | None:
    if exchange:
        row = _row("SELECT * FROM results WHERE coin = ? AND exchange = ? "
                   "ORDER BY scan_id DESC LIMIT 1", (coin, exchange))
    else:
        row = _row("SELECT * FROM results WHERE coin = ? ORDER BY scan_id DESC LIMIT 1",
                   (coin,))
    return dict(row) if row else None


def history_for(coin: str, limit: int = 60) -> list[dict]:
    rows = _rows("SELECT r.scan_id, r.fetched_at, r.verdict, r.score, r.side, s.profile "
        "FROM results r JOIN scans s ON s.id = r.scan_id "
        "WHERE r.coin = ? AND r.verdict IS NOT NULL "
        "ORDER BY r.scan_id DESC LIMIT ?", (coin, limit))
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
    row = _row("SELECT * FROM chart_series WHERE coin = ? AND role = ?",
        (coin, role))
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
    rows = _rows("SELECT check_key, resolved, note, resolved_at FROM manual_checks "
        "WHERE coin = ?", (coin,))
    return {r["check_key"]: dict(r) for r in rows}


def all_manual_checks() -> dict:
    rows = _rows("SELECT coin, check_key, resolved, note, resolved_at FROM manual_checks")
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
                    reason_params: dict | None = None,
                    exchange: str | None = None) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO commentary "
            "(coin, lang, scan_id, exchange, text, model, status, reason, reason_code, "
            " reason_params, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (coin, lang, scan_id, exchange, text, model, status, reason, reason_code,
             json.dumps(reason_params or {}, ensure_ascii=False), now_iso()))


def commentary_for(coin: str, lang: str, exchange: str | None = None,
                   scan_id: int | None = None) -> dict | None:
    """Commentary for this coin, only if it describes the analysis on screen.

    A row from another venue, or from an older scan than the result being rendered,
    is not stale decoration — it is a paragraph that can contradict the verdict above
    it. Those are dropped rather than shown.
    """
    row = _row("SELECT * FROM commentary WHERE coin = ? AND lang = ?", (coin, lang))
    if not row:
        return None
    rec = dict(row)
    if exchange and rec.get("exchange") and rec["exchange"] != exchange:
        return None
    if exchange and not rec.get("exchange"):
        return None  # predates venue tracking; provenance unknown, so not trusted
    if scan_id is not None and rec.get("scan_id") not in (None, scan_id):
        return None
    return rec


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
    row = _row("SELECT value FROM kv WHERE key = ?", (key,))
    return json.loads(row["value"]) if row else default


def set_kv(key: str, value) -> None:
    with tx() as conn:
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                     (key, json.dumps(value, ensure_ascii=False)))


# --------------------------------------------------------------------------------
# Paper trading
# --------------------------------------------------------------------------------


def paper_account() -> dict | None:
    row = _row("SELECT * FROM paper_account WHERE id = 1")
    return dict(row) if row else None


def paper_init(*, exchange: str, capital: float, slots: int,
               heat_cap_pct: float, reset: bool = False) -> dict:
    """Create the account, or reset it to a clean start.

    A reset wipes positions and events as well as the balance. Keeping old trades
    against a fresh balance would silently corrupt every aggregate in the report —
    win rate and total R would describe one account, equity another.
    """
    with tx() as conn:
        if reset:
            conn.execute("DELETE FROM paper_events")
            conn.execute("DELETE FROM paper_positions")
            conn.execute("DELETE FROM paper_account")
        conn.execute(
            "INSERT OR REPLACE INTO paper_account "
            "(id, exchange, starting_capital, balance, slots, heat_cap_pct, "
            " created_at, reset_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (exchange, capital, capital, slots, heat_cap_pct, now_iso(),
             now_iso() if reset else None),
        )
    return paper_account()


def paper_set_balance(balance: float) -> None:
    """Set the balance to an absolute value.

    Prefer `paper_adjust_balance` for anything that credits or debits money — see
    the lost-update note there. This remains for a genuine reset, where an absolute
    value is what is meant.
    """
    with tx() as conn:
        conn.execute("UPDATE paper_account SET balance = ? WHERE id = 1", (balance,))


def paper_adjust_balance(delta: float) -> None:
    """Credit or debit the balance atomically, in the database.

    Every money movement must go through here rather than reading the balance,
    doing arithmetic in Python and writing an absolute value back. That pattern is a
    classic lost update, and it cost real money in this account on 2026-08-22:
    `_open()` debited nine entry fees directly while `cycle()` held a balance read
    from before them and wrote its own total at the end, silently discarding one fee
    of 0.1488 USDT. The balance was then permanently 0.1488 higher than the trade
    records justified.

    `UPDATE ... SET balance = balance + ?` makes the read-modify-write a single
    statement, so concurrent writers serialise instead of clobbering each other.
    """
    with tx() as conn:
        conn.execute("UPDATE paper_account SET balance = balance + ? WHERE id = 1",
                     (float(delta),))


def paper_open_positions() -> list[dict]:
    return [dict(r) for r in _rows(
        "SELECT * FROM paper_positions WHERE status = 'open' ORDER BY opened_ts")]


def paper_closed_positions() -> list[dict]:
    return [dict(r) for r in _rows(
        "SELECT * FROM paper_positions WHERE status = 'closed' "
        "ORDER BY closed_at DESC, id DESC")]


def paper_last_close_times() -> dict[str, str]:
    """Most recent close time per coin, for the re-entry guard."""
    return {r["coin"]: r["closed_at"] for r in _rows(
        "SELECT coin, MAX(closed_at) AS closed_at FROM paper_positions "
        "WHERE status = 'closed' AND closed_at IS NOT NULL GROUP BY coin")}


def paper_position(position_id: int) -> dict | None:
    row = _row("SELECT * FROM paper_positions WHERE id = ?", (position_id,))
    return dict(row) if row else None


# Anything not listed here is silently dropped by paper_open, so a new column has
# to be added in both places or it will read as NULL forever with no error.
_OPEN_FIELDS = (
    "coin", "symbol", "exchange", "side", "slot", "contracts", "entry_price",
    "leverage", "margin", "risk_amount", "stop", "tp1", "tp2", "opened_ts",
    "entry_fee", "scan_id", "score", "verdict", "plan_json", "context_json",
    "plan_entry", "entry_slippage_pct", "btc_bias", "takes_available",
    "limit_price", "placed_ts", "maker", "status",
)


def paper_open(**fields) -> int:
    """Insert a position. `status` may be 'open' or 'pending' for a resting limit."""
    status = fields.pop("status", "open")
    cols = [f for f in _OPEN_FIELDS if f in fields and f != "status"]
    sql = (f"INSERT INTO paper_positions (status, opened_at, placed_at, {', '.join(cols)}) "
           f"VALUES (?, ?, ?, {', '.join('?' for _ in cols)})")
    with tx() as conn:
        cur = conn.execute(sql, (status, now_iso(), now_iso(),
                                 *(fields[c] for c in cols)))
        return int(cur.lastrowid)


def paper_pending_positions() -> list[dict]:
    return [dict(r) for r in _rows(
        "SELECT * FROM paper_positions WHERE status = 'pending' ORDER BY placed_ts")]


def paper_cancel(position_id: int, reason: str) -> None:
    """A limit that never filled leaves no trade, only a record that it was tried."""
    with tx() as conn:
        conn.execute("UPDATE paper_positions SET status = 'cancelled', "
                     "closed_at = ?, exit_reason = ? WHERE id = ?",
                     (now_iso(), reason, position_id))


def paper_update(position_id: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with tx() as conn:
        conn.execute(f"UPDATE paper_positions SET {sets} WHERE id = ?",
                     (*fields.values(), position_id))


def paper_close(position_id: int, *, exit_price: float, exit_reason: str,
                realised_pnl: float, exit_fee: float) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE paper_positions SET status = 'closed', closed_at = ?, "
            "exit_price = ?, exit_reason = ?, realised_pnl = ?, exit_fee = ? "
            "WHERE id = ?",
            (now_iso(), exit_price, exit_reason, realised_pnl, exit_fee, position_id),
        )


def paper_event(position_id: int | None, kind: str, *, action: str | None = None,
                amount: float | None = None, detail: str | None = None) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO paper_events (position_id, at, kind, action, amount, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (position_id, now_iso(), kind, action, amount, detail),
        )


def paper_sample(position_id: int, **fields) -> None:
    """One point on a position's path. Cheap and frequent, so it is a single insert."""
    cols = ("at", "ts", "hours_held", "mark", "unrealised", "r", "margin_ratio")
    with tx() as conn:
        conn.execute(
            f"INSERT INTO paper_samples (position_id, {', '.join(cols)}) "
            f"VALUES (?, {', '.join('?' for _ in cols)})",
            (position_id, now_iso(), *(fields.get(c) for c in cols[1:])),
        )


def paper_samples(position_id: int) -> list[dict]:
    return [dict(r) for r in _rows(
        "SELECT * FROM paper_samples WHERE position_id = ? ORDER BY ts", (position_id,))]


def paper_decision(**fields) -> None:
    cols = ("ts", "scan_id", "coin", "symbol", "side", "score", "rank",
            "action", "code", "mark", "detail")
    with tx() as conn:
        conn.execute(
            f"INSERT OR IGNORE INTO paper_decisions (at, {', '.join(cols)}) "
            f"VALUES (?, {', '.join('?' for _ in cols)})",
            (now_iso(), *(fields.get(c) for c in cols)),
        )


def paper_decisions(limit: int = 500, action: str | None = None) -> list[dict]:
    if action:
        return [dict(r) for r in _rows(
            "SELECT * FROM paper_decisions WHERE action = ? ORDER BY id DESC LIMIT ?",
            (action, limit))]
    return [dict(r) for r in _rows(
        "SELECT * FROM paper_decisions ORDER BY id DESC LIMIT ?", (limit,))]


def paper_events(position_id: int | None = None, limit: int = 200) -> list[dict]:
    if position_id is None:
        return [dict(r) for r in _rows(
            "SELECT * FROM paper_events ORDER BY id DESC LIMIT ?", (limit,))]
    return [dict(r) for r in _rows(
        "SELECT * FROM paper_events WHERE position_id = ? ORDER BY id", (position_id,))]


# --------------------------------------------------------------------------------
# Live positions (real money). See the schema note: these rows annotate, they do not
# decide. The exchange is authoritative for what is open.
# --------------------------------------------------------------------------------

_LIVE_FIELDS = (
    "coin", "symbol", "side", "status", "quantity", "entry_price", "plan_entry",
    "leverage", "risk_amount", "stop", "tp1", "tp2", "order_id",
    "venue_position_id", "sl_tp_set", "tp1_filled", "opened_at", "opened_ts",
    "scan_id", "score", "plan_json",
)


def live_open(**fields) -> int:
    cols = [f for f in _LIVE_FIELDS if f in fields]
    sql = (f"INSERT INTO live_positions ({', '.join(cols)}) "
           f"VALUES ({', '.join('?' for _ in cols)})")
    with tx() as conn:
        return int(conn.execute(sql, [fields[c] for c in cols]).lastrowid)


def live_update(position_id: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with tx() as conn:
        conn.execute(f"UPDATE live_positions SET {sets} WHERE id = ?",
                     [*fields.values(), position_id])


def live_close(position_id: int, *, exit_price, exit_reason, realised_pnl=None) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE live_positions SET status='closed', closed_at=?, exit_price=?, "
            "exit_reason=?, realised_pnl=? WHERE id = ?",
            (now_iso(), exit_price, exit_reason, realised_pnl, position_id))


def live_positions(*statuses: str) -> list[dict]:
    statuses = statuses or ("pending", "open")
    marks = ", ".join("?" for _ in statuses)
    return [dict(r) for r in _rows(
        f"SELECT * FROM live_positions WHERE status IN ({marks}) ORDER BY id",
        statuses)]


def live_closed() -> list[dict]:
    return [dict(r) for r in _rows(
        "SELECT * FROM live_positions WHERE status='closed' "
        "ORDER BY closed_at DESC, id DESC")]


def live_event(position_id, kind: str, detail: str = "") -> None:
    with tx() as conn:
        conn.execute("INSERT INTO live_events (position_id, at, kind, detail) "
                     "VALUES (?, ?, ?, ?)", (position_id, now_iso(), kind, detail))


def live_last_close_times() -> dict[str, str]:
    """Most recent live close per coin, for the live engine's re-entry guard."""
    return {r["coin"]: r["closed_at"] for r in _rows(
        "SELECT coin, MAX(closed_at) AS closed_at FROM live_positions "
        "WHERE status = 'closed' AND closed_at IS NOT NULL GROUP BY coin")}
