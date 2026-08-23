#!/usr/bin/env python3
"""Archive and then drop live trade history older than a cut-off.

Never deletes an OPEN position, and never deletes anything without writing a JSON
archive of it first. These rows are the only record of what the account actually
did — every finding in docs/RESEARCH_LOG.md was reconstructed from them — so
"clean up the list" must not be allowed to mean "destroy the evidence".

    python3 tools/purge_history.py                     # dry run, shows what would go
    python3 tools/purge_history.py --apply             # archive, then delete
    python3 tools/purge_history.py --apply --before 2026-08-23
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import config, store                                # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before",
                    help="ISO date; rows closed before it go. Default: today, UTC.")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete. Without it nothing is written.")
    ap.add_argument("--archive-dir", default=None,
                    help="where the JSON archive lands (default: var/archive)")
    args = ap.parse_args()

    config.ensure_dirs()
    cut = args.before or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    conn = store.connect()

    doomed = [dict(r) for r in conn.execute(
        "SELECT * FROM live_positions "
        "WHERE status = 'closed' AND COALESCE(closed_at, '') < ?", (cut,))]
    kept_closed = conn.execute(
        "SELECT COUNT(*) FROM live_positions "
        "WHERE status = 'closed' AND COALESCE(closed_at, '') >= ?",
        (cut,)).fetchone()[0]
    open_now = conn.execute(
        "SELECT COUNT(*) FROM live_positions WHERE status != 'closed'").fetchone()[0]

    print(f"cut-off        : {cut} (rows closed before this go)")
    print(f"to remove      : {len(doomed)} closed positions")
    print(f"to keep        : {kept_closed} closed, {open_now} open/pending")
    if doomed:
        realised = sum(float(r.get("realised_pnl") or 0) for r in doomed)
        first = min(r.get("closed_at") or "" for r in doomed)
        last = max(r.get("closed_at") or "" for r in doomed)
        print(f"span           : {first}  ->  {last}")
        print(f"realised in it : {realised:+.6f}")

    if not doomed:
        print("nothing to do.")
        return 0
    if not args.apply:
        print("\ndry run — pass --apply to archive and delete.")
        return 0

    ids = [int(r["id"]) for r in doomed]
    marks = ",".join("?" * len(ids))
    events = [dict(r) for r in conn.execute(
        f"SELECT * FROM live_events WHERE position_id IN ({marks})", ids)]
    samples = [dict(r) for r in conn.execute(
        f"SELECT * FROM live_samples WHERE position_id IN ({marks})", ids)]

    dest = Path(args.archive_dir) if args.archive_dir else config.VAR_DIR / "archive"
    dest.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = dest / f"live-history-before-{cut}-{stamp}.json"
    path.write_text(json.dumps(
        {"cut_off": cut, "archived_at": stamp, "positions": doomed,
         "events": events, "samples": samples}, indent=1, default=str))
    print(f"\narchived       : {path}")
    print(f"                 {len(doomed)} positions, {len(events)} events, "
          f"{len(samples)} samples")

    with conn:
        conn.execute(f"DELETE FROM live_samples  WHERE position_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM live_events   WHERE position_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM live_positions WHERE id IN ({marks})", ids)
    print("deleted.")

    print(f"positions left : "
          f"{conn.execute('SELECT COUNT(*) FROM live_positions').fetchone()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
