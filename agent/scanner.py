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
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

from . import config, exchange, skill, store

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

    venue = exchange.adapter()
    if not watchlist:
        raise RuntimeError(f"no watchlist for {venue.NAME} — run ./run.sh setup first")

    targets = venue.scannable(watchlist)
    if coins:
        wanted = {c.upper() for c in coins}
        targets = [t for t in targets if t["coin"] in wanted]
    if not targets:
        raise RuntimeError("no scannable coins matched")

    usdt_irt = watchlist.get("usdt_irt")
    log.info("venue: %s", venue.LABEL)
    scan_id = store.start_scan(
        profile=settings["profile"], capital=float(settings["capital"]),
        capital_currency=settings.get("capital_currency", "USDT"),
        risk_pct=float(settings["risk_pct"]), total=len(targets), usdt_irt=usdt_irt)

    log.info("scan %s started: %d coins, profile=%s", scan_id, len(targets),
             settings["profile"])
    completed = failed = 0
    started = time.monotonic()

    # Coins are independent, so the scan is only sequential because it was written
    # that way. Toobit allows 3000 requests a minute and one coin costs about six, so
    # a handful of workers is nowhere near the limit and turns a ~290s pass into well
    # under a minute. Nobitex stays at one worker: its limit is ~55/min and the
    # skill's client serialises with a 1.2s gap anyway, so concurrency there would
    # only queue.
    #
    # Four, measured rather than assumed. Toobit throttles concurrent requests from
    # one address, and throughput peaks and then collapses: 18 klines took 4.5s at 4
    # workers, 5.4s at 6, and 9.9s at 10. More threads past that point buy nothing
    # and make every request slower.
    workers = 1 if venue.NAME == exchange.NOBITEX else max(
        1, int(settings.get("scan_workers", 4)))

    # Sizing has to match what the demo will actually trade. When slots track the
    # signal count, risk per trade is derived from the heat cap, so a plan built with
    # the static 1% would be sized for an account holding six positions while the
    # board is holding fifteen.
    from . import demo as demo_mod                             # noqa: PLC0415
    slots = demo_mod.target_slots()
    risk_pct = demo_mod.derived_risk_pct()
    log.info("scan %s sizing: %d slots, %.3f%% risk per trade", scan_id, slots, risk_pct)

    def analyse(entry):
        """Network and CPU for one coin. No database writes — those stay on the
        main thread so progress counters cannot interleave."""
        coin, symbol = entry["coin"], entry["symbol"]
        capital, cap_error = capital_for(entry, settings, usdt_irt)
        if cap_error:
            return entry, capital, None, cap_error
        try:
            return entry, capital, venue.analyze(
                entry, settings["profile"], capital=capital,
                risk_pct=risk_pct,
                count=int(settings.get("candle_count", 300)),
                hold_hours=float(settings.get("hold_hours", 0.0)),
                account_level=settings.get("account_level"),
                slots=slots), None
        except Exception as exc:  # per-coin failure must not end the scan
            log.warning("scan %s: %s failed: %s", scan_id, coin, exc)
            return entry, capital, None, str(exc)[:500]

    def record(entry, capital, result, error):
        nonlocal completed, failed
        coin, symbol = entry["coin"], entry["symbol"]
        if error or result is None:
            store.save_result(scan_id, coin, symbol=symbol, quote=entry.get("quote"),
                              side=None, side_tied=False, snapshot=None, plan=None,
                              capital_used=capital, exchange=venue.NAME, error=error)
            failed += 1
            store.update_scan(scan_id, failed=failed, current_coin=coin)
            return

        snap, plan, candles, side_info = result
        store.save_result(scan_id, coin, symbol=symbol, quote=entry.get("quote"),
                          side=side_info["side"], side_tied=bool(side_info.get("tied")),
                          snapshot=snap, plan=plan, capital_used=capital,
                          exchange=venue.NAME)
        dec = candles.get("decision")
        if dec:
            store.save_series(coin, "decision", scan_id, dec.get("timeframe"),
                              _trim_series(dec, int(settings.get("chart_candles", 180))))
        completed += 1
        store.update_scan(scan_id, completed=completed, current_coin=coin)
        if verbose:
            qual = (plan or {}).get("qualification") or {}
            print(f"  {coin:<7} {symbol:<13} {qual.get('verdict','?'):<10} "
                  f"score {qual.get('score','—')}  side {side_info['side']}")

    if workers == 1:
        for entry in targets:
            if _cancel.is_set():
                store.finish_scan(scan_id, "cancelled", "cancelled by request")
                log.info("scan %s cancelled after %d coins", scan_id, completed)
                return scan_id
            record(*analyse(entry))
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scan") as pool:
            futures = {pool.submit(analyse, e): e for e in targets}
            try:
                for future in as_completed(futures):
                    if _cancel.is_set():
                        for f in futures:
                            f.cancel()
                        store.finish_scan(scan_id, "cancelled", "cancelled by request")
                        log.info("scan %s cancelled after %d coins", scan_id, completed)
                        return scan_id
                    record(*future.result())
            finally:
                for f in futures:
                    f.cancel()

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
        # Gated like the demo loop, so `./run.sh scanner off` can quiet it without
        # stopping the server and taking the dashboard down with it.
        if not settings.get("scanner_enabled", True):
            stop.wait(interval)
            continue
        try:
            scan_once()
        except Exception:
            log.exception("scheduled scan failed")
        # Wake early if asked to stop, rather than sleeping through a shutdown.
        stop.wait(interval)
