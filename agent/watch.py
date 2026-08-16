"""Timed observation run over the demo account.

Started and stopped by `./run.sh watch`, never by hand. It samples the account at a
fixed cadence and writes a full journal when the window closes, so "let it run for an
hour and show me the journal" is a supervised process with a PID file rather than a
stray shell loop that survives `./run.sh stop`.

It only reads. Positions are opened and managed by the demo loop inside the server;
this watches and records.
"""

from __future__ import annotations

import signal
import sys
import time
from datetime import datetime, timezone

from . import config, demo, journal, store

SAMPLE_SECONDS = 300
_stop = False


def _handle_signal(_signum, _frame) -> None:
    """Finish the current sample, write the journal, exit — rather than vanishing.

    `./run.sh watch stop` sends TERM. Dying instantly would lose the observation, so
    the run ends the way a completed one does: with a journal on disk.
    """
    global _stop
    _stop = True


def sample() -> str:
    st = demo.state()
    acct = st["account"]
    closed = store.paper_closed_positions()
    actions = [e for e in store.paper_events(limit=500)
               if e["kind"] in ("close", "action")]
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    return (f"{stamp}  equity {acct['equity']:.2f}  "
            f"unrealised {acct['open_pnl']:+.4f}  "
            f"open {len(st['positions'])}  closed {len(closed)}  "
            f"actions {len(actions)}")


def run(minutes: float) -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    config.ensure_dirs()
    store.init()
    out_path = config.VAR_DIR / "watch-journal.txt"
    deadline = time.monotonic() + minutes * 60

    print(f"watch: {minutes:g} minutes, sampling every {SAMPLE_SECONDS}s", flush=True)
    print(sample(), flush=True)

    while not _stop and time.monotonic() < deadline:
        # Wake often enough that a stop request is honoured promptly, rather than
        # sleeping through it for five minutes.
        slice_end = min(time.monotonic() + SAMPLE_SECONDS, deadline)
        while not _stop and time.monotonic() < slice_end:
            time.sleep(1)
        if _stop:
            break
        try:
            print(sample(), flush=True)
        except Exception as exc:                              # noqa: BLE001
            print(f"sample failed: {exc}", flush=True)

    text = journal.text()
    out_path.write_text(text + "\n", encoding="utf-8")
    print(f"watch: {'stopped early' if _stop else 'complete'} — journal at {out_path}",
          flush=True)
    print(text, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run(float(sys.argv[1]) if len(sys.argv) > 1 else 60.0))
