"""Is the target reachable in the time the plan allows?

Stands in for the `target_reachability` layer of the skill's market_context.py, which
is not installed. The newer SKILL.md specifies it exactly: "historical MFE
distribution over the profile's time window ... hard gate when TP1 hit < 30% of the
time historically."

It exists because of a measured failure. Over the first 30 closed trades not one
reached 1.0R against a TP1 set at 1.5R, the median favourable excursion was +0.125R,
and every exit came from a clock rather than a level. Measuring travel directly
explains why: against a stop of 2.0 x ATR these instruments typically move 0.27R in
8 hours and 0.64R in 48, so TP1 was asking for roughly six times the typical 8-hour
move.

Published practice says the same thing in general terms. A stop far wider than the
move available inside the holding period inverts the risk:reward whatever the chart
looks like, and the fix is to match stop distance to hold time rather than to nudge
the target. The same source set puts a daily 2x-ATR trend system at a 46.3% win rate
and 1.72 profit factor, and the identical rules on hourly bars at 32.3% and 0.96 —
which is the regime this account has been trading in.

The method here is deliberately simple and empirical: replay every historical window
of the intended holding length, record how far price ran in the trade's direction as
a multiple of the plan's own stop distance, and report what fraction of those windows
would have reached TP1.
"""

from __future__ import annotations

import logging
import threading
import time

from . import toobit

log = logging.getLogger("reachability")

# The skill's own threshold: below this, the target is not realistically achievable
# in the time allowed and the trade should not be taken.
MIN_HIT_RATE_PCT = 30.0

_TTL = 3600.0
_cache: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()


def _bars_for(hours: float, interval: str) -> int:
    per_bar = {"5m": 1/12, "15m": 0.25, "30m": 0.5, "1h": 1.0, "4h": 4.0,
               "6h": 6.0, "12h": 12.0, "1d": 24.0}.get(interval, 4.0)
    return max(1, round(hours / per_bar))


def assess(symbol: str, *, side: str, stop_distance: float, entry: float,
           tp1_r: float, hold_hours: float, interval: str = "4h",
           lookback: int = 300) -> dict | None:
    """How often price has historically travelled far enough, in the time allowed.

    `stop_distance` is in price units, so travel converts to R directly — the same R
    the plan sizes and targets in. Returns None when there is not enough history;
    the caller must treat that as unknown rather than as a pass.
    """
    if stop_distance <= 0 or tp1_r <= 0:
        return None

    key = (symbol, side, round(stop_distance, 10), round(tp1_r, 4),
           round(hold_hours, 2), interval)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _TTL:
            return hit[1]

    try:
        rows = toobit.klines_cached(symbol, interval, lookback)
    except toobit.ToobitError:
        return None

    window = _bars_for(hold_hours, interval)
    if len(rows) < window + 20:
        return None

    # Every historical window of the intended length, scored by how far price ran in
    # the trade's favour. Highs for a long, lows for a short — the excursion that
    # would have hit a take-profit, not the close.
    travels = []
    for i in range(len(rows) - window):
        open_px = rows[i]["close"]
        if not open_px:
            continue
        seg = rows[i + 1: i + 1 + window]
        if side == "long":
            best = max(r["high"] for r in seg) - open_px
        else:
            best = open_px - min(r["low"] for r in seg)
        travels.append(best / stop_distance)

    if not travels:
        return None

    travels.sort()
    n = len(travels)
    hit_rate = sum(1 for t in travels if t >= tp1_r) / n * 100.0
    out = {
        "symbol": symbol, "side": side, "tp1_r": tp1_r,
        "hold_hours": hold_hours, "windows": n,
        "hit_rate_pct": hit_rate,
        "median_travel_r": travels[n // 2],
        "p75_travel_r": travels[int(n * 0.75)],
        "p90_travel_r": travels[int(n * 0.90)],
        "reachable": hit_rate >= MIN_HIT_RATE_PCT,
        # What target this instrument would actually reach 30% of the time.
        "tp1_r_for_30pct": travels[int(n * 0.70)],
        "source": "local stand-in for market_context.py target_reachability",
    }
    with _lock:
        _cache[key] = (time.time(), out)
    return out


def first_touch(symbol: str, *, side: str, stop_distance: float, tp1_r: float,
                hold_hours: float, interval: str = "4h",
                lookback: int = 300) -> dict | None:
    """Which level price reaches first — the target or the stop.

    Reachability alone overstates a tight stop. Measuring only the favourable
    excursion says how often price *could* have reached the target, ignoring that a
    nearer stop is hit more often and ends the trade before it gets there. This walks
    each historical window bar by bar and records whichever level is touched first,
    which is the only version of the question a trade actually asks.

    Within a single bar the order of the high and the low is unknowable from OHLC, so
    a bar touching both counts as a stop. That is the pessimistic reading and it is
    the same convention the paper broker uses, so the two agree.
    """
    if stop_distance <= 0 or tp1_r <= 0:
        return None
    try:
        rows = toobit.klines_cached(symbol, interval, lookback)
    except toobit.ToobitError:
        return None
    window = _bars_for(hold_hours, interval)
    if len(rows) < window + 20:
        return None

    wins = losses = neither = 0
    for i in range(len(rows) - window):
        entry = rows[i]["close"]
        if not entry:
            continue
        target = entry + tp1_r * stop_distance if side == "long" else entry - tp1_r * stop_distance
        stop = entry - stop_distance if side == "long" else entry + stop_distance
        outcome = None
        for r in rows[i + 1: i + 1 + window]:
            hit_stop = r["low"] <= stop if side == "long" else r["high"] >= stop
            hit_tp = r["high"] >= target if side == "long" else r["low"] <= target
            if hit_stop:
                outcome = "loss"; break
            if hit_tp:
                outcome = "win"; break
        if outcome == "win":
            wins += 1
        elif outcome == "loss":
            losses += 1
        else:
            neither += 1

    total = wins + losses + neither
    if not total:
        return None
    decided = wins + losses
    return {
        "symbol": symbol, "side": side, "tp1_r": tp1_r, "hold_hours": hold_hours,
        "windows": total,
        "win_pct": wins / total * 100.0,
        "loss_pct": losses / total * 100.0,
        "undecided_pct": neither / total * 100.0,
        "win_rate_of_decided": (wins / decided * 100.0) if decided else None,
        # Expectancy assuming an undecided window exits flat at the time stop.
        "expectancy_r": (wins * tp1_r - losses * 1.0) / total,
    }
