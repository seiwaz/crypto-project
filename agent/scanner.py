"""The scan loop.

Runs in a background thread so a page load never waits on it. Each coin costs about
six seconds of API time, so a full 36-coin pass takes roughly four minutes; progress
is written to the store continuously and the UI reads it from there.

Failures are per-coin. One dead symbol records its error and the scan carries on —
losing 35 good results to one bad one would be a poor trade.
"""

from __future__ import annotations

import logging
import threading
import time

from . import config, discover, skill, store

log = logging.getLogger("scanner")

_state_lock = threading.Lock()
_thread: threading.Thread | None = None
_cancel = threading.Event()


def is_running() -> bool:
    with _state_lock:
        return _thread is not None and _thread.is_alive()


def request_cancel() -> None:
    _cancel.set()


def capital_for(entry: dict, settings: dict, usdt_irt: float | None) -> tuple[float | None, str | None]:
    """Convert configured capital into the market's quote currency.

    Position size is quantity x price, so capital has to be denominated in the same
    currency the market quotes in. Returns (capital, error) — a missing rate is an
    error, never an assumed conversion.
    """
    capital = float(settings["capital"])
    have = (settings.get("capital_currency") or "USDT").upper()
    want = (entry.get("quote") or "USDT").upper()
    if have == want:
        return capital, None
    if not usdt_irt:
        return None, (f"capital is in {have} but {entry['symbol']} quotes in {want}, "
                      f"and no USDT/IRT rate was available to convert it")
    # Nobitex quotes IRT markets in rials, which is what usdt_irt is expressed in.
    return (capital * usdt_irt, None) if want == "IRT" else (capital / usdt_irt, None)


def scan_once(coins: list[str] | None = None, *, verbose: bool = False) -> int:
    """Run one full pass. Returns the scan id."""
    config.load_dotenv()
    store.init()
    settings = config.load_settings()
    watchlist = config.load_watchlist()

    if not watchlist:
        raise RuntimeError("config/watchlist.json is missing — run ./run.sh setup first")

    targets = discover.scannable(watchlist)
    if coins:
        wanted = {c.upper() for c in coins}
        targets = [t for t in targets if t["coin"] in wanted]
    if not targets:
        raise RuntimeError("no scannable coins matched")

    usdt_irt = watchlist.get("usdt_irt")
    scan_id = store.start_scan(
        profile=settings["profile"], capital=float(settings["capital"]),
        capital_currency=settings.get("capital_currency", "USDT"),
        risk_pct=float(settings["risk_pct"]), total=len(targets), usdt_irt=usdt_irt)

    log.info("scan %s started: %d coins, profile=%s", scan_id, len(targets),
             settings["profile"])
    completed = failed = 0
    started = time.monotonic()

    for entry in targets:
        if _cancel.is_set():
            store.finish_scan(scan_id, "cancelled", "cancelled by request")
            log.info("scan %s cancelled after %d coins", scan_id, completed)
            return scan_id

        coin, symbol = entry["coin"], entry["symbol"]
        store.update_scan(scan_id, current_coin=coin)
        capital, cap_error = capital_for(entry, settings, usdt_irt)

        if cap_error:
            store.save_result(scan_id, coin, symbol=symbol, quote=entry.get("quote"),
                              side=None, side_tied=False, snapshot=None, plan=None,
                              capital_used=None, error=cap_error)
            failed += 1
            store.update_scan(scan_id, failed=failed)
            continue

        try:
            snap, plan, candles, side_info = skill.analyze(
                symbol, settings["profile"], capital=capital,
                risk_pct=float(settings["risk_pct"]),
                count=int(settings.get("candle_count", 300)),
                exchange=settings.get("exchange", "nobitex"),
                hold_hours=float(settings.get("hold_hours", 0.0)),
                account_level=settings.get("account_level"))
        except Exception as exc:  # per-coin failure must not end the scan
            log.warning("scan %s: %s failed: %s", scan_id, coin, exc)
            store.save_result(scan_id, coin, symbol=symbol, quote=entry.get("quote"),
                              side=None, side_tied=False, snapshot=None, plan=None,
                              capital_used=capital, error=str(exc)[:500])
            failed += 1
            store.update_scan(scan_id, failed=failed)
            continue

        store.save_result(scan_id, coin, symbol=symbol, quote=entry.get("quote"),
                          side=side_info["side"], side_tied=bool(side_info.get("tied")),
                          snapshot=snap, plan=plan, capital_used=capital)

        dec = candles.get("decision")
        if dec:
            store.save_series(coin, "decision", scan_id, dec.get("timeframe"),
                              _trim_series(dec, int(settings.get("chart_candles", 180))))

        completed += 1
        store.update_scan(scan_id, completed=completed)
        if verbose:
            qual = (plan or {}).get("qualification") or {}
            print(f"  {coin:<7} {symbol:<13} {qual.get('verdict','?'):<10} "
                  f"score {qual.get('score','—')}  side {side_info['side']}")

    elapsed = time.monotonic() - started
    store.finish_scan(scan_id, "done",
                      f"{completed} ok, {failed} failed in {elapsed:.0f}s")
    store.prune(int(settings.get("keep_scans", 40)))
    log.info("scan %s finished: %d ok, %d failed, %.0fs", scan_id, completed, failed,
             elapsed)
    return scan_id


def _trim_series(dec: dict, limit: int) -> dict:
    """Keep the tail of the series. EMAs are already computed over the full history,
    so trimming the display window does not change any value on the chart."""
    candles = dec["candles"][-limit:]
    ema = {k: v[-limit:] for k, v in (dec.get("ema") or {}).items()}
    return {"timeframe": dec.get("timeframe"), "resolution": dec.get("resolution"),
            "candles": candles, "ema": ema}


def start_background(coins: list[str] | None = None) -> bool:
    """Kick off a scan in a thread. False if one is already running."""
    global _thread
    with _state_lock:
        if _thread is not None and _thread.is_alive():
            return False
        _cancel.clear()

        def _run():
            try:
                scan_once(coins)
            except Exception:
                log.exception("scan thread failed")
                latest = store.running_scan()
                if latest:
                    store.finish_scan(latest["id"], "failed", "scan thread error")

        _thread = threading.Thread(target=_run, name="scanner", daemon=True)
        _thread.start()
        return True


def scheduler_loop(stop: threading.Event) -> None:
    """Scan on the configured interval until told to stop."""
    store.init()
    store.mark_stale_scans()
    while not stop.is_set():
        settings = config.load_settings()
        interval = max(1, int(settings.get("scan_interval_minutes", 15))) * 60
        try:
            scan_once()
        except Exception:
            log.exception("scheduled scan failed")
        # Wake early if asked to stop, rather than sleeping through a shutdown.
        stop.wait(interval)
