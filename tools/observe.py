#!/usr/bin/env python3
"""Archive every scored candidate before the scanner prunes it.

Why this exists
---------------
The engine trades roughly one coin per scan and learns from that one. Every scan
actually scores **33**, and each of those is a labelled observation waiting for an
outcome — a score, a side, a full gate result and a complete indicator snapshot,
followed by a price series that says what happened next. Thrown away, the system
learns at 1/33 of the rate the data allows.

And they are thrown away: `store.prune(keep_scans=40)` keeps about four hours. A
twelve-hour study would lose two thirds of itself before anyone read it.

This appends each new scan's candidates to a JSONL file, compactly, and is safe to
run on a short timer: it records the highest scan_id it has already written and only
ever moves forward. Read-only against the database.

    ./.venv/bin/python tools/observe.py            # one pass
    (a systemd timer runs it every 5 minutes)

`tools/observe_report.py` turns the file into the actual study by joining each
observation to the candles that came after it.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

ROOT = os.environ.get("CS_ROOT", "/opt/crypto-screener")
DB = f"{ROOT}/var/screener.sqlite3"
OUT = f"{ROOT}/var/observations.jsonl"
STATE = f"{ROOT}/var/observations.state"


def _last_written() -> int:
    """Highest scan_id already archived.

    Kept in its own file rather than derived by re-reading the whole JSONL: the file
    is append-only and grows all day, and re-parsing it every five minutes to learn
    one integer is the kind of thing that quietly becomes the slowest part of a box.
    """
    try:
        with open(STATE) as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _indicators(snap: dict) -> dict:
    """The handful of indicator values worth keeping per timeframe.

    Not the whole snapshot — that is 5KB a coin and would be 700MB across a twelve
    hour study. These are the inputs the direction checks actually read, plus the
    swing levels, so a later pass can ask which states preceded a target and which
    preceded a stop.
    """
    out = {}
    for role in ("bias", "decision", "entry"):
        ind = ((snap.get("timeframes") or {}).get(role) or {}).get("indicators") or {}
        if not ind:
            continue
        out[role] = {k: ind.get(k) for k in (
            "last_close", "ema20", "ema50", "ema200", "rsi14", "atr_pct",
            "structure", "volume_bias", "session_vwap",
            "last_swing_high", "last_swing_low",
            "cloud_top", "cloud_bottom", "cloud_bullish") if ind.get(k) is not None}
    return out


def collect() -> int:
    if not os.path.exists(DB):
        print(f"no database at {DB}", file=sys.stderr)
        return 0
    since = _last_written()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "select * from results where scan_id > ? order by scan_id, coin", (since,))]
    if not rows:
        return 0

    written, highest = 0, since
    with open(OUT, "a") as fh:
        for r in rows:
            highest = max(highest, int(r["scan_id"]))
            try:
                snap = json.loads(r["snapshot_json"]) if r["snapshot_json"] else {}
            except (ValueError, TypeError):
                snap = {}
            try:
                plan = json.loads(r["plan_json"]) if r["plan_json"] else {}
            except (ValueError, TypeError):
                plan = {}
            q = plan.get("qualification") or {}
            lv = plan.get("levels") or {}
            # `economics`, not `expectancy` — there is no top-level "expectancy" key
            # and reading one silently produced null costs on every record.
            ec = plan.get("economics") or {}
            ds = snap.get("direction_score") or {}
            rec = {
                "scan": r["scan_id"],
                "at": r["fetched_at"] or snap.get("fetched_at"),
                "coin": r["coin"],
                "symbol": r["symbol"],
                "side": r["side"],
                "side_tied": r["side_tied"],
                "verdict": r["verdict"],
                "score": r["score"],
                # the live futures mid the plan was anchored to — the price every
                # forward outcome has to be measured from
                "price": snap.get("last_price"),
                "candle_close": snap.get("candle_close"),
                "gates_failed": q.get("gates_failed") or [],
                "score_parts": {p["factor"]: p["points"]
                                for p in (q.get("score_breakdown") or [])},
                "levels": {k: lv.get(k) for k in
                           ("entry", "stop", "tp1", "stop_pct", "tp1_r")},
                "cost_in_R": ec.get("cost_in_R"),
                "expectancy_net_R": ec.get("expectancy_net_R"),
                "avg_win_R": ec.get("avg_win_R"),
                "rr_tp1": ec.get("rr_tp1"),
                "direction": {"long": ds.get("long_score"), "short": ds.get("short_score"),
                              "of": ds.get("auto_checks"), "threshold": ds.get("threshold")},
                "ind": _indicators(snap),
            }
            fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
            written += 1

    with open(STATE, "w") as fh:
        fh.write(str(highest))
    return written


if __name__ == "__main__":
    n = collect()
    if n:
        size = os.path.getsize(OUT) / 1e6 if os.path.exists(OUT) else 0
        print(f"{time.strftime('%H:%M:%S')} archived {n} observations "
              f"(file now {size:.1f} MB)")
