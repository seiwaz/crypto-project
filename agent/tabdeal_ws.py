"""Live order-book feed over Tabdeal's websocket, used only to price open positions.

Why this exists
---------------
The monitoring loop runs every 3 seconds and marks each open position. Every mark was
a REST call to `/fapi/v1/depth`, so a four-slot book cost ~80 requests a minute for a
number the venue is willing to push for free every 2 seconds.

What is actually available
--------------------------
Probed 2026-08-23 across both hosts and both sockets. On
`wss://api1.tabdeal.org/stream/` exactly one stream works — `<sym>@depth@2000ms` —
and it pushes a **full snapshot**: 100 bids and 100 asks, best first, every 2s.
Everything else is refused with an explicit `INVALID_FORMAT`: `trade`, `aggTrade`,
`trades`, `deal`, `matches`, `kline`, `candle`, `ticker`, `miniTicker`, `bookTicker`,
`markPrice`, `openInterest`. `wss://ws.tabdeal.org/special_margin/stream/` connects
and accepts nothing at all.

So this feed can replace the mark, and nothing else. Candles — and therefore every
indicator in the signal engine — stay on REST, and account/position/order calls stay
on signed REST. There is no "websocket only" configuration of this system available.

What it computes
----------------
The mid of best bid and best ask: deliberately the *same* quantity
`tabdeal.mark_price()` already returns from REST, so moving transport does not
silently shift every P&L figure on the dashboard. The payload also carries `p` (which
tracks a mark) and `f`/`f_bid`/`f_ask` (a fair-price family), but adopting one of
those would change what "mark" means mid-flight, which is a separate decision from
where the number comes from.

Safety
------
This is an optimisation, never a dependency. Any failure — the library missing, the
socket refusing, a stale price, a symbol never seen — returns None so the caller
falls back to REST. A monitoring loop must not stop pricing real positions because a
websocket dropped.
"""
from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger("tabdeal_ws")

WS_URL = "wss://api1.tabdeal.org/stream/"
STREAM_SUFFIX = "@depth@2000ms"

# How old a pushed price may be before it is treated as absent. The stream ticks
# every 2s, so this tolerates a couple of missed frames and no more; past that, a
# REST read is worth more than a stale push.
MAX_AGE_S = 8.0

_RECONNECT_MIN = 2.0
_RECONNECT_MAX = 60.0


def stream_name(symbol: str) -> str:
    """`BTC_USDT` -> `btcusdt@depth@2000ms`.

    The socket is strict about this and it is the opposite of the REST convention:
    REST requires the underscore (`BTC_USDT`), the socket rejects it. `BTC_USDT@depth`
    came back INVALID_FORMAT while `btcusdt@depth@2000ms` streamed fine.
    """
    return symbol.replace("_", "").lower() + STREAM_SUFFIX


class DepthFeed:
    """Keeps the latest book mid per symbol, fed by a background websocket."""

    def __init__(self) -> None:
        self._prices: dict[str, tuple[float, float]] = {}   # symbol -> (mid, ts)
        self._wanted: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ws = None
        self._subscribed: set[str] = set()
        self._connected = False
        self._last_error: str | None = None
        self._msgs = 0
        # The retry runs every cycle; the complaint should not.
        self._warned_unavailable = False

    # -- public ------------------------------------------------------------------

    def start(self) -> bool:
        """Begin streaming. Returns False if the feed cannot run at all."""
        try:
            import websocket                                # noqa: F401,PLC0415
        except Exception as exc:                            # noqa: BLE001
            # Not just ImportError, and not a canned message. The library raises
            # ImportError from inside itself on some installs, and reporting every
            # such case as "not installed" sent a real diagnosis chasing a package
            # that was in fact present and importable from the same user, cwd and
            # sandbox. Say what actually happened.
            self._last_error = f"{type(exc).__name__}: {exc}"[:200]
            if not self._warned_unavailable:
                self._warned_unavailable = True
                log.warning("tabdeal_ws: cannot start (%s) — marks will use REST",
                            self._last_error)
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tabdeal-ws",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:                               # noqa: BLE001
                pass

    def track(self, symbols) -> None:
        """Set the symbols worth streaming — normally whatever is open right now."""
        want = {s for s in symbols if s}
        with self._lock:
            if want == self._wanted:
                return
            self._wanted = want
        self._sync_subscriptions()

    def mark(self, symbol: str) -> float | None:
        """Latest mid for `symbol`, or None if absent or stale."""
        with self._lock:
            hit = self._prices.get(symbol)
        if not hit:
            return None
        price, ts = hit
        if time.time() - ts > MAX_AGE_S:
            return None
        return price

    def status(self) -> dict:
        with self._lock:
            ages = {s: round(time.time() - ts, 1) for s, (_, ts) in self._prices.items()}
        return {
            "connected": self._connected,
            "tracking": sorted(self._wanted),
            "subscribed": sorted(self._subscribed),
            "ages_s": ages,
            "messages": self._msgs,
            "last_error": self._last_error,
        }

    # -- internals ---------------------------------------------------------------

    def _sync_subscriptions(self) -> None:
        ws = self._ws
        if ws is None or not self._connected:
            return                              # picked up on the next connect
        with self._lock:
            want = set(self._wanted)
        add = want - self._subscribed
        drop = self._subscribed - want
        try:
            if add:
                ws.send(json.dumps({"method": "SUBSCRIBE",
                                    "params": [stream_name(s) for s in sorted(add)],
                                    "id": int(time.time()) % 100000}))
            if drop:
                ws.send(json.dumps({"method": "UNSUBSCRIBE",
                                    "params": [stream_name(s) for s in sorted(drop)],
                                    "id": int(time.time()) % 100000 + 1}))
        except Exception as exc:                            # noqa: BLE001
            self._last_error = f"subscribe failed: {exc}"
            return
        self._subscribed = want

    def _run(self) -> None:
        import websocket                                    # noqa: PLC0415

        backoff = _RECONNECT_MIN
        while not self._stop.is_set():
            try:
                self._ws = websocket.create_connection(
                    WS_URL, timeout=20, header={"Origin": "https://tabdeal.org"})
                self._connected = True
                self._subscribed = set()
                self._last_error = None
                backoff = _RECONNECT_MIN
                log.warning("tabdeal_ws: connected")
                self._sync_subscriptions()
                self._pump()
            except Exception as exc:                        # noqa: BLE001
                self._last_error = str(exc)[:200]
            finally:
                self._connected = False
                try:
                    if self._ws is not None:
                        self._ws.close()
                except Exception:                           # noqa: BLE001
                    pass
                self._ws = None
            if self._stop.is_set():
                break
            # Prices go stale on their own via MAX_AGE_S, so a disconnect degrades to
            # REST rather than serving a frozen number.
            log.warning("tabdeal_ws: disconnected (%s) — retry in %.0fs",
                        self._last_error or "closed", backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, _RECONNECT_MAX)

    def _pump(self) -> None:
        ws = self._ws
        while not self._stop.is_set() and ws is not None:
            ws.settimeout(30)
            raw = ws.recv()
            if not raw:
                return
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            data = msg.get("data") if isinstance(msg, dict) else None
            if not isinstance(data, dict):
                continue                        # subscribe acks and control frames
            self._absorb(msg.get("stream"), data)
            # Re-sync opportunistically: `track()` may have been called while the
            # socket was mid-frame.
            with self._lock:
                drifted = self._wanted != self._subscribed
            if drifted:
                self._sync_subscriptions()

    def _absorb(self, stream: str | None, data: dict) -> None:
        bids, asks = data.get("b") or [], data.get("a") or []
        if not bids or not asks:
            return
        try:
            mid = (float(bids[0][0]) + float(asks[0][0])) / 2.0
        except (TypeError, ValueError, IndexError):
            return
        if mid <= 0:
            return
        symbol = self._symbol_for(stream, data)
        if not symbol:
            return
        with self._lock:
            self._prices[symbol] = (mid, time.time())
            self._msgs += 1

    def _symbol_for(self, stream: str | None, data: dict) -> str | None:
        """Map a frame back to our underscored symbol.

        The socket answers in its own convention (`BTCUSDT` in `s`, `btcusdt@…` in
        `stream`), so match against what we asked for rather than trying to guess
        where the underscore belongs — `1000SATS_USDT` and friends make that guess
        wrong.
        """
        with self._lock:
            wanted = set(self._wanted)
        if stream:
            for sym in wanted:
                if stream_name(sym) == stream:
                    return sym
        raw = (data.get("s") or "").replace("_", "").upper()
        for sym in wanted:
            if sym.replace("_", "").upper() == raw:
                return sym
        return None


FEED = DepthFeed()
