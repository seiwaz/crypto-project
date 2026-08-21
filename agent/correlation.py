"""Correlation and beta against BTC — a stand-in for the skill's market_context.py.

**This is not the skill's implementation.** `market_context.py` is not installed and
neither is `references/signal-quality.md`, which defines its windows and thresholds.
When they arrive, `btc_context` from a real context run should replace everything
here and this module should be deleted rather than kept in parallel.

What justifies writing it anyway: the correlation filter exists to stop five slots
becoming one leveraged bet wearing five tickers, and that risk is live right now —
every position has been the same direction more than once. Pearson correlation and
beta between two return series are textbook statistics, not the skill's proprietary
maths, so this borrows nothing it should not. The skill's own `indicators.md` §14
states the principle it serves: "Most alts correlate above 0.8 with Bitcoin, which
means altcoin analysis that ignores BTC is close to worthless."

Every parameter below is a choice made here, not a value read from the skill:

  window     120 daily bars — long enough for a stable estimate, short enough to
             describe the current regime rather than last year's
  interval   daily, whatever the trading profile is. Correlation describes how two
             assets relate as a regime, not how they behave on the timeframe a
             signal happens to fire on.
  returns    simple bar-to-bar percentage change, aligned by index
  threshold  set by the caller. The brief named 0.9; measured across 25 Toobit
             perps nothing reaches it but BTC against itself, so the operative
             default lives in demo.py and is configurable.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("correlation")

# Toobit's BTC perp. Kept as the module default so existing callers and tests that
# reference `correlation.BTC_SYMBOL` keep working; the live value comes from
# `_market()` below, which follows whichever venue is configured.
BTC_SYMBOL = "BTC-SWAP-USDT"
WINDOW = 120

# Generic interval -> Tabdeal chart resolution. Callers pass intervals in Toobit's
# vocabulary ("4h", "1d") because that is what `settings()["correlation_interval"]`
# has always held, and what the snapshot's own resolution string looks like on
# Toobit. On Tabdeal those have to become "240"/"1D" or the chart API returns an
# empty series — which would read as "not enough history" and silently fail the
# correlation and trend gates open rather than erroring.
_TABDEAL_INTERVALS = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
    "1d": "1D", "1w": None,
}


def _market():
    """(module, btc_symbol, error_class) for the venue currently configured.

    Correlation and trend must be measured on the *same* book the plans are built
    from. Reading them off Toobit while trading Tabdeal would mean the counter-trend
    gate and the correlated-exposure cap were judging a different market than the one
    holding the risk.
    """
    from . import exchange                                    # noqa: PLC0415
    if exchange.current_name() == exchange.TABDEAL:
        from . import tabdeal                                 # noqa: PLC0415
        return tabdeal, tabdeal.BTC_SYMBOL, tabdeal.TabdealError
    from . import toobit                                      # noqa: PLC0415
    return toobit, BTC_SYMBOL, toobit.ToobitError


def _interval_for(mkt, interval: str) -> str | None:
    """Translate a generic interval into the venue's own resolution string."""
    if getattr(mkt, "NAME", "") != "tabdeal":
        return interval
    key = str(interval).strip()
    if key in _TABDEAL_INTERVALS:
        return _TABDEAL_INTERVALS[key]
    # Already a Tabdeal resolution ("15", "240", "1D") — the snapshot passes these
    # straight through from build_snapshot.
    return key if key in mkt._RESOLUTION_SECONDS else None


# Correlation over 120 bars moves slowly; recomputing it every 60s cycle would be
# noise and network traffic for a number that barely changes within an hour.
_TTL = 3600.0

_cache: dict[tuple[str, str, int], tuple[float, dict]] = {}
_lock = threading.Lock()


def _returns(rows: list[dict]) -> list[float]:
    out = []
    for prev, cur in zip(rows, rows[1:]):
        p = float(prev["close"])
        if p:
            out.append((float(cur["close"]) - p) / p)
    return out


def _pearson(xs: list[float], ys: list[float]) -> tuple[float | None, float | None]:
    """Correlation and beta of xs (the coin) against ys (BTC).

    Beta is the slope of the coin's returns on BTC's — cov/var — which is what says
    a 1% BTC move tends to move this coin 1.6%. Correlation says how reliably; beta
    says how much. A filter needs both: 0.95 correlation at beta 0.2 is a different
    exposure from 0.95 at beta 1.8.
    """
    n = min(len(xs), len(ys))
    if n < 30:
        return None, None
    xs, ys = xs[-n:], ys[-n:]
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None, None
    corr = sxy / (sxx ** 0.5 * syy ** 0.5)
    beta = sxy / syy
    return corr, beta


def btc_context(symbol: str, interval: str = "1d",
                window: int = WINDOW) -> dict | None:
    """Correlation, beta and alpha for one contract against BTC.

    Returns None when there is not enough overlapping history — the caller must treat
    that as "unknown", never as "uncorrelated", or the filter fails open on exactly
    the coins it knows least about.
    """
    mkt, btc_symbol, mkt_error = _market()
    if symbol == btc_symbol:
        return {"symbol": symbol, "correlation": 1.0, "beta": 1.0, "alpha_pct": 0.0,
                "bars": window}

    key = (symbol, interval, window)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _TTL:
            return hit[1]

    native = _interval_for(mkt, interval)
    if native is None:
        log.debug("no %s resolution for interval %s", mkt.NAME, interval)
        return None
    try:
        coin_rows = mkt.klines_cached(symbol, native, window + 1)
        btc_rows = mkt.klines_cached(btc_symbol, native, window + 1)
    except mkt_error as exc:
        log.debug("no candles for %s: %s", symbol, exc)
        return None
    if not coin_rows or not btc_rows:
        return None

    cr, br = _returns(coin_rows), _returns(btc_rows)
    corr, beta = _pearson(cr, br)
    if corr is None:
        return None

    # Alpha: the coin's own move over the window, net of what its beta to BTC would
    # already explain. Positive alpha is coin-specific strength rather than a rising
    # tide, which is the tie-break the brief asks for.
    coin_total = sum(cr) * 100.0
    btc_total = sum(br) * 100.0
    alpha = coin_total - (beta or 0.0) * btc_total

    out = {"symbol": symbol, "correlation": corr, "beta": beta,
           "alpha_pct": alpha, "bars": min(len(cr), len(br)),
           "source": "local stand-in for market_context.py"}
    with _lock:
        _cache[key] = (time.time(), out)
    return out


def available() -> bool:
    """True when correlation can actually be computed for the current venue."""
    try:
        return btc_context(_market()[1]) is not None
    except Exception:                                          # noqa: BLE001
        return False


# --------------------------------------------------------------------------------
# BTC regime
# --------------------------------------------------------------------------------

_REGIME_TTL = 900.0
_regime_cache: tuple[float, dict] = (0.0, {})


def coin_regime(symbol: str, interval: str = "4h",
                window: int = 300) -> dict | None:
    """The instrument's own trend — price against its EMA200 plus the recent move.

    This is the skill's "trend regime" gate, which is about the thing being traded.
    It is a separate question from BTC alignment, and conflating the two produced a
    filter that was exactly backwards: it blocked shorts on coins below their own
    EMA200 with deeply negative alpha (FIL -13.6%, DOT -36.5%, CRO -29.1%) purely
    because they follow BTC, while permitting shorts on coins outperforming it
    (WLD +61.7%, INJ +49.8%). Shorting a weak instrument while BTC rises is a
    relative-weakness trade, not a counter-trend one.
    """
    key = ("regime", symbol, interval, window)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _REGIME_TTL:
            return hit[1]

    mkt, _btc, mkt_error = _market()
    native = _interval_for(mkt, interval)
    if native is None:
        return None
    try:
        rows = mkt.klines_cached(symbol, native, window)
    except mkt_error:
        return None
    if len(rows) < 20:
        return None

    from . import skill                                       # noqa: PLC0415
    try:
        ind = skill.compute_indicators(rows)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("regime unavailable for %s: %s", symbol, exc)
        return None
    close, ema200 = ind.get("last_close"), ind.get("ema200")
    if close is None or ema200 is None:
        return None

    closes = [r["close"] for r in rows]
    look = min(12, len(closes) - 1)
    move_pct = (closes[-1] - closes[-1 - look]) / closes[-1 - look] * 100.0
    above = close > ema200

    if above and move_pct > 0.5:
        label = "up"
    elif not above and move_pct < -0.5:
        label = "down"
    else:
        label = "range"

    out = {"symbol": symbol, "label": label, "above_ema200": above,
           "move_pct": move_pct, "structure": ind.get("structure")}
    with _lock:
        _cache[key] = (time.time(), out)
    return out


def btc_regime(interval: str = "4h", window: int = 300) -> dict | None:
    """Which way BTC is trending, for the gate the missing context run would apply.

    The skill specifies two gates this stands in for — "trade opposes a strong_trend"
    and BTC alignment "opposed_strong" — and both live in market_context.py, which is
    not installed. Their absence is measurable rather than theoretical: with them
    missing the agent took 25 shorts in 30 trades while BTC sat above its 4H EMA200
    and rose 2.66% over 48 hours, and those shorts lost 5.65 USDT against the longs'
    gain of 0.74.

    Direction is taken from price against EMA200 plus the recent move, both from the
    skill's own compute_indicators so "BTC is bullish" means the same thing here as
    everywhere else in the app.
    """
    global _regime_cache
    now = time.time()
    with _lock:
        at, data = _regime_cache
        if data and now - at < _REGIME_TTL:
            return data

    mkt, btc_symbol, mkt_error = _market()
    native = _interval_for(mkt, interval)
    if native is None:
        return None
    try:
        rows = mkt.klines_cached(btc_symbol, native, window)
    except mkt_error:
        return None
    if len(rows) < 20:
        return None

    # Guarded: this feeds a gate in the fill loop, and a gate that raises would stop
    # the agent trading entirely rather than merely failing to veto.
    from . import skill                                       # noqa: PLC0415
    try:
        ind = skill.compute_indicators(rows)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("BTC regime unavailable: %s", exc)
        return None
    close, ema200 = ind.get("last_close"), ind.get("ema200")
    if close is None or ema200 is None:
        return None

    closes = [r["close"] for r in rows]
    look = min(12, len(closes) - 1)
    move_pct = (closes[-1] - closes[-1 - look]) / closes[-1 - look] * 100.0

    above = close > ema200
    # "Up" needs agreement between where price sits and where it has been going. One
    # without the other is a range, and a range should not veto either direction.
    if above and move_pct > 0.5:
        label = "up"
    elif not above and move_pct < -0.5:
        label = "down"
    else:
        label = "range"

    out = {"label": label, "above_ema200": above, "move_pct": move_pct,
           "close": close, "ema200": ema200, "interval": interval,
           "source": "local stand-in for market_context.py"}
    with _lock:
        _regime_cache = (time.time(), out)
    return out
