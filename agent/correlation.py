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

from . import toobit

log = logging.getLogger("correlation")

BTC_SYMBOL = "BTC-SWAP-USDT"
WINDOW = 120
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
    if symbol == BTC_SYMBOL:
        return {"symbol": symbol, "correlation": 1.0, "beta": 1.0, "alpha_pct": 0.0,
                "bars": window}

    key = (symbol, interval, window)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _TTL:
            return hit[1]

    try:
        coin_rows = toobit.klines_cached(symbol, interval, window + 1)
        btc_rows = toobit.klines_cached(BTC_SYMBOL, interval, window + 1)
    except toobit.ToobitError as exc:
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
        return btc_context(BTC_SYMBOL) is not None
    except Exception:                                          # noqa: BLE001
        return False
