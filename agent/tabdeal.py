"""Tabdeal اهرم حرفه‌ای (Professional Leverage) — market data, read-only.

This is the third venue adapter, alongside `toobit.py` and the Nobitex client. It
exists so the screener and the demo can run entirely on Tabdeal data, which matters
because Tabdeal is the venue a real account would actually trade on: a signal scored
on one exchange's prices and filled on another's is measuring the wrong book.

Everything here is GET-only and passes through `guard.assert_tabdeal_read_only`
first. That guard is stricter than the other two on purpose — the credentials this
project holds for Tabdeal carry live trade permission on a funded account, so a
mistake here spends real money rather than leaking a read.

Three things about this venue are genuinely different from Toobit, and the code
below is shaped by them:

* **Candles live on a different host.** `api1.tabdeal.org` (the documented API) has
  no kline endpoint at all — every path 404s and the websocket rejects kline
  subscriptions outright. The OHLCV the web charts draw comes from
  `api-web.tabdeal.org/plots/history`, found in the site's own Nuxt config. It is a
  from/to range query in unix *seconds*, not a `limit` query, so `klines()` converts
  a bar count into a time window.
* **Margin is CROSS with a flat 0.5% maintenance requirement**, not isolated with a
  per-tier ladder. There is no `riskLimits` equivalent to read, and none is needed —
  the figure is the same for every symbol. See `paper.py` for what cross margin does
  to liquidation, which is the part that actually bites.
* **Quantity is in coins, not contracts.** There is no `contractMultiplier` and no
  `1000SHIB`-style scaling, so `units_per_contract` is always 1.0. That removes the
  single largest source of sizing error on Toobit.

Not available from this venue, and handled rather than faked:

* **No funding rate is published for this product** and the product page never
  mentions one. `funding_rate()` returns None, which leaves the skill's funding
  check MANUAL instead of inventing a number. The scalp profile skips that check
  anyway.
* **No ticker endpoint.** `ticker()` is derived from the most recent candle plus the
  order book, and says so, rather than pretending to be a venue 24h stat.
* **No weekly candles.** `1W` returns empty, so the `swing` profile cannot be run
  here; `TF_TO_RESOLUTION` simply has no entry for it and the snapshot reports the
  timeframe as unavailable instead of silently substituting a different one.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import guard, skill

log = logging.getLogger(__name__)

NAME = "tabdeal"
LABEL = "Tabdeal — اهرم حرفه‌ای"
PLAN_EXCHANGE = "tabdeal"

DEFAULT_BASE = "https://api1.tabdeal.org"
DEFAULT_CHART_BASE = "https://api-web.tabdeal.org"

USER_AGENT = "crypto-screener/1.0 (read-only)"

AVAILABLE = "available"
NOT_LISTED = "not_listed"

_MIN_GAP_SECONDS = 0.12
# Four attempts, not three: both Tabdeal hosts sit behind a CDN that returns
# intermittent 502s, and one extra attempt (~9s of total backoff) is cheap against a
# 5-minute scan interval when the alternative is losing a coin from the scan.
_RETRIES = 4
_RETRY_BACKOFF = 1.5
_gap_lock = threading.Lock()
_last_call = 0.0

# Profile timeframe -> Tabdeal chart resolution. Verified live 2026-08-22: 1, 5, 15,
# 30, 60, 120, 240, 360, 720 and 1D all return data; 3, D, W, 1W and 1M return empty.
# `1W` is deliberately absent rather than mapped to something close — the swing
# profile needs a real weekly bar, and substituting 1D would silently change what the
# bias timeframe means.
TF_TO_RESOLUTION = {
    "1m": "1", "5m": "5", "15m": "15", "30m": "30", "1H": "60", "2H": "120",
    "4H": "240", "6H": "360", "12H": "720", "1D": "1D",
}

_RESOLUTION_SECONDS = {
    "1": 60, "5": 300, "15": 900, "30": 1800, "60": 3600, "120": 7200,
    "240": 14400, "360": 21600, "720": 43200, "1D": 86400,
}

# From the product page (tabdeal.org/special-margin), confirmed 2026-08-22:
# leverage is selectable 1..100 and maintenance margin is a flat 0.5% of position
# value for every symbol — «مارجین نگهداری ... ۰.۵ درصد از ارزش کل پوزیشن است».
MAX_LEVERAGE = 100.0
MAINT_MARGIN_PCT = 0.5

# tabdeal.org/commissions, «کارمزد اهرم‌ حرفه‌ای»: 0.001 for both maker and taker,
# i.e. 0.1% a side with no maker discount at all. Flagged as a temporary promotional
# rate on that page, so treat it as a floor that can rise, not a fixed constant.
MAKER_FEE_PCT = 0.1
TAKER_FEE_PCT = 0.1

BTC_SYMBOL = "BTC_USDT"


class TabdealError(RuntimeError):
    pass


# --------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------


def base_url() -> str:
    import os
    return (os.environ.get("TABDEAL_BASE_URL") or DEFAULT_BASE).rstrip("/")


def chart_base_url() -> str:
    import os
    return (os.environ.get("TABDEAL_CHART_BASE_URL")
            or DEFAULT_CHART_BASE).rstrip("/")


def _get(path: str, params: dict | None = None, *, chart: bool = False,
         timeout: int = 20):
    """GET a public endpoint, after the read-only guard has cleared the path.

    `chart=True` routes to `api-web.tabdeal.org` (candles); everything else goes to
    `api1.tabdeal.org`. The guard runs on the path either way — a path being safe on
    one host does not make it safe on the other, so both are checked identically.
    """
    if params:
        path = f"{path}?{urllib.parse.urlencode(params)}"
    guard.assert_tabdeal_read_only(path, "GET")

    global _last_call
    with _gap_lock:
        wait = _MIN_GAP_SECONDS - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()

    host = chart_base_url() if chart else base_url()
    req = urllib.request.Request(
        host + path,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"})

    last = None
    for attempt in range(_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            # 5xx and 429 are transient and MUST be retried. Both Tabdeal hosts sit
            # behind a CDN (ArvanCloud/Cloudflare) that returns a 502 HTML error page
            # under load — seen live 2026-08-22 on /r/plots/history, which dropped
            # XRP out of a whole scan. The Toobit client this was modelled on says
            # "HTTP errors are not retried: a 400 will still be a 400", which is
            # correct for 4xx and wrong for a gateway blip; copying that rule
            # wholesale was the bug.
            if (exc.code >= 500 or exc.code == 429) and attempt < _RETRIES - 1:
                last = f"HTTP {exc.code}"
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
                continue
            raise TabdealError(
                f"HTTP {exc.code} on {path}: {_brief_error(body)}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = getattr(exc, "reason", exc)
            if attempt < _RETRIES - 1:
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
                continue
        except json.JSONDecodeError:
            raise TabdealError(f"non-JSON response from {path}") from None
    raise TabdealError(f"network error on {path} after {_RETRIES} attempts: {last}")


def _brief_error(body: str) -> str:
    """A one-line reason, never a dump of a CDN error page.

    The gateway returns a full HTML document on a 502. Passing that through put 200
    characters of `<!DOCTYPE html><head><meta charset…` into the dashboard's error
    card, which buries the one fact that matters — that the upstream failed.
    """
    text = (body or "").strip()
    if text.startswith("<") or "<html" in text[:200].lower():
        return "upstream returned an HTML error page (gateway/CDN), not JSON"
    return text[:200]


# --------------------------------------------------------------------------------
# Contracts
# --------------------------------------------------------------------------------

_contracts_cache: list | None = None
_contracts_at = 0.0
_CONTRACTS_TTL = 3600


def contracts(refresh: bool = False) -> list[dict]:
    """Live futures symbols from /r/fapi/v1/exchangeInfo, cached for an hour."""
    global _contracts_cache, _contracts_at
    if (not refresh and _contracts_cache is not None
            and time.time() - _contracts_at < _CONTRACTS_TTL):
        return _contracts_cache
    info = _get("/r/fapi/v1/exchangeInfo", timeout=60)
    rows = [c for c in (info.get("symbols") or []) if c.get("status") == "TRADING"]
    _contracts_cache, _contracts_at = rows, time.time()
    return rows


def _precision_step(precision) -> float | None:
    """`pricePrecision: 1` -> 0.1. Tabdeal publishes decimal places, not a step.

    This is the only tick/step information the venue exposes — there is no
    PRICE_FILTER or LOT_SIZE the way Binance-shaped APIs usually carry.
    """
    try:
        return 10.0 ** -int(precision)
    except (TypeError, ValueError):
        return None


def contract_for(symbol: str) -> dict | None:
    return next((c for c in contracts() if c.get("symbol") == symbol), None)


# --------------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------------

_klines_cache: dict = {}
_klines_at: dict = {}
_klines_lock = threading.Lock()
_KLINES_TTL = 45.0


def klines_cached(symbol: str, resolution: str, limit: int = 300) -> list[dict]:
    """Candles, deduplicated across one scan pass. Same contract as toobit's."""
    key = (symbol, resolution, limit)
    now = time.time()
    with _klines_lock:
        hit = _klines_cache.get(key)
        if hit and now - _klines_at.get(key, 0) < _KLINES_TTL:
            return hit
    rows = klines(symbol, resolution, limit)
    with _klines_lock:
        _klines_cache[key] = rows
        _klines_at[key] = time.time()
        if len(_klines_cache) > 512:
            cutoff = time.time() - _KLINES_TTL
            for k, at in list(_klines_at.items()):
                if at < cutoff:
                    _klines_cache.pop(k, None)
                    _klines_at.pop(k, None)
    return rows


def klines(symbol: str, resolution: str, limit: int = 300) -> list[dict]:
    """OHLCV as the skill's compute_indicators expects it.

    `plots/history` is a time-range query, not a count query, so the requested bar
    count is converted into a window. The window is padded by 20% and a few extra
    bars because the venue skips ranges with no trades — on a thin symbol an exact
    `limit * interval` window comes back short, and an indicator quietly computed on
    120 bars instead of 300 is worse than one that reports the shortfall.
    """
    seconds = _RESOLUTION_SECONDS.get(resolution)
    if not seconds:
        raise TabdealError(f"unsupported Tabdeal resolution {resolution!r}")
    to_ts = int(time.time())
    from_ts = to_ts - int(seconds * (int(limit) * 1.2 + 5))

    raw = _get("/r/plots/history", {"symbol": symbol, "resolution": resolution,
                                    "from": from_ts, "to": to_ts},
               chart=True, timeout=30)
    if not isinstance(raw, dict):
        raise TabdealError(f"unexpected klines payload for {symbol}: {str(raw)[:120]}")
    if raw.get("no_data"):
        return []

    rows = []
    for r in raw.get("data") or []:
        try:
            rows.append({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S",
                                           time.gmtime(int(r["time"]))),
                "open": float(r["open"]), "high": float(r["high"]),
                "low": float(r["low"]), "close": float(r["close"]),
                "volume": float(r.get("volume") or 0.0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda x: x["timestamp"])
    return rows[-int(limit):] if limit else rows


def orderbook(symbol: str, depth: int = 20) -> dict:
    raw = _get("/r/fapi/v1/depth", {"symbol": symbol, "limit": depth})
    bids = [(float(p), float(q)) for p, q in (raw.get("bids") or [])]
    asks = [(float(p), float(q)) for p, q in (raw.get("asks") or [])]
    return {"bids": bids, "asks": asks}


def ticker(symbol: str) -> dict:
    """A stand-in 24h stat, derived rather than reported.

    Tabdeal publishes no ticker endpoint for this product, so this is assembled from
    the last 24h of hourly candles. It is labelled `derived` so nothing downstream
    mistakes it for an exchange-published figure.
    """
    rows = klines_cached(symbol, "60", 24)
    if not rows:
        return {"error": "no candles", "derived": True}
    closes = [r["close"] for r in rows]
    first, last = rows[0]["open"], closes[-1]
    return {
        "last": last,
        "open": first,
        "high": max(r["high"] for r in rows),
        "low": min(r["low"] for r in rows),
        "volume": sum(r["volume"] for r in rows),
        "change_pct": ((last - first) / first * 100.0) if first else None,
        "derived": True,
        "source": "24 x 1H candles from plots/history",
    }


def last_prices_for(symbols) -> dict[str, float]:
    out = {}
    for s in set(symbols):
        try:
            rows = klines_cached(s, "5", 2)
            if rows:
                out[s] = rows[-1]["close"]
        except TabdealError:
            continue
    return out


def mark_price(symbol: str) -> float | None:
    """Best available mark: order-book mid, falling back to the last close.

    There is no index or `edp` equivalent here, so mid is the closest honest thing.
    Mid is preferred over last-close because on a thin book the two can drift, and a
    position should be marked against what it could actually be closed at.

    Three sources, in order of freshness. The websocket pushes a full 100-level book
    snapshot every 2s for the symbols the engine is tracking, and computes the very
    same bid/ask mid as the REST path below — same number, no round trip. It is an
    optimisation and never a dependency: a missing library, a dropped socket or a
    price older than a few seconds all return None here and fall through to REST.
    """
    live = _ws_mark(symbol)
    if live is not None:
        return live
    try:
        ob = orderbook(symbol, 5)
        if ob["bids"] and ob["asks"]:
            return (ob["bids"][0][0] + ob["asks"][0][0]) / 2.0
    except TabdealError:
        pass
    try:
        rows = klines_cached(symbol, "5", 2)
        return rows[-1]["close"] if rows else None
    except TabdealError:
        return None


def _ws_mark(symbol: str) -> float | None:
    """Pushed mid, if the feed has a fresh one. Never raises."""
    try:
        from . import tabdeal_ws                            # noqa: PLC0415
        return tabdeal_ws.FEED.mark(symbol)
    except Exception:                                       # noqa: BLE001
        return None


def funding_rate(symbol: str) -> dict | None:
    """Tabdeal publishes no funding rate for اهرم حرفه‌ای.

    Returning None rather than 0.0 is deliberate: zero would be a claim that the
    product charges nothing to hold, which has not been verified. None leaves the
    skill's funding check MANUAL and keeps the cost model honest about the gap.
    """
    del symbol
    return None


# --------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------


def discover(coins: list[str]) -> dict:
    """Resolve requested coins to Tabdeal futures symbols."""
    from datetime import datetime, timezone

    index: dict[str, dict] = {}
    for c in contracts():
        base = str(c.get("baseAsset") or "").upper()
        if base and str(c.get("quoteAsset") or "").upper() == "USDT":
            index[base] = c

    entries = []
    for coin in coins:
        entry = {"coin": coin, "symbol": None, "quote": "USDT", "status": NOT_LISTED,
                 "reason": None, "market_closed": False, "lot_size": 1,
                 "lot_label": None, "units_per_contract": 1.0,
                 "max_leverage": None, "maint_margin_pct": None}
        c = index.get(coin.upper())
        if not c:
            entry["reason"] = "no USDT futures market on Tabdeal اهرم حرفه‌ای"
            entries.append(entry)
            continue
        entry.update(
            symbol=c["symbol"],
            status=AVAILABLE,
            # No contract multiplier on this venue: order quantity is in coins.
            units_per_contract=1.0,
            max_leverage=MAX_LEVERAGE,
            maint_margin_pct=MAINT_MARGIN_PCT,
            price_step=_precision_step(c.get("pricePrecision")),
            qty_step=_precision_step(c.get("quantityPrecision")),
        )
        entries.append(entry)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exchange": NAME,
        "exchange_label": LABEL,
        "source": "GET /r/fapi/v1/exchangeInfo (symbols)",
        "margin_detection": (
            f"{len(index)} live USDT futures symbols; cross margin, flat "
            f"{MAINT_MARGIN_PCT:g}% maintenance margin, up to {MAX_LEVERAGE:g}x"),
        "requested": len(coins),
        "coins": entries,
    }


def scannable(watchlist: dict) -> list[dict]:
    return [c for c in watchlist.get("coins", [])
            if c["status"] == AVAILABLE and not c.get("market_closed")]


# --------------------------------------------------------------------------------
# Manual-check resolution
# --------------------------------------------------------------------------------

_btc_cache: dict = {}
_BTC_TTL = 120.0
_btc_lock = threading.Lock()


def _btc_bias(bias_tf: str, count: int) -> dict | None:
    """BTC's own trend on the bias timeframe, fetched once per scan pass."""
    resolution = TF_TO_RESOLUTION.get(bias_tf)
    if not resolution:
        return None
    key = (resolution, count)
    now = time.time()
    with _btc_lock:
        hit = _btc_cache.get(key)
        if hit and now - hit[0] < _BTC_TTL:
            return hit[1]
        try:
            rows = klines_cached(BTC_SYMBOL, resolution, count)
        except TabdealError:
            return None
        out = None
        if rows:
            ind = skill.compute_indicators(rows)
            close, ema200 = ind.get("last_close"), ind.get("ema200")
            if close and ema200:
                out = {"timeframe": bias_tf, "close": close, "ema200": ema200,
                       "bullish": close > ema200}
        _btc_cache[key] = (now, out)
        return out


def resolve_manual_checks(checks: list[dict], *, coin: str, symbol: str,
                          decision_interval: str, funding: dict | None,
                          btc: dict | None) -> list[dict]:
    """Fill in the checks Tabdeal can settle; leave the rest MANUAL.

    Same shape and same reasoning as the Toobit version: the BTC-alignment leg
    resolves from the *instrument's own* trend first and only falls back to BTC's
    when the coin has no trend of its own. Gating every coin on BTC's direction was
    measured backwards in this codebase (`demo.counter_trend`, commit 7356609) and
    later found to have leaked into the scoring path too (Round 3) — it must not be
    reintroduced here just because this is a new venue.

    The funding check is always left MANUAL, because this venue publishes no funding
    rate (see `funding_rate`).
    """
    out = []
    for check in checks:
        name = check.get("check", "")
        if check.get("long") is not None:
            out.append(check)
            continue

        if name.startswith("BTC / dominance"):
            if coin.upper() == "BTC":
                out.append({**check, "long": True, "short": True,
                            "resolved_by": NAME,
                            "observed": "this is BTC — alignment is self-referential"})
                continue
            from . import correlation  # noqa: PLC0415 - avoids an import cycle
            own = correlation.coin_regime(symbol, decision_interval)
            if own and own["label"] != "range":
                up = own["label"] == "up"
                out.append({**check, "long": up, "short": not up,
                            "resolved_by": NAME,
                            "observed": (f"{coin} own {decision_interval} trend: "
                                         f"{own['label']} ({own['move_pct']:+.2f}%)")})
            elif btc:
                out.append({**check, "long": btc["bullish"], "short": not btc["bullish"],
                            "resolved_by": NAME,
                            "observed": (
                                f"{coin} has no clear trend of its own; falling back to "
                                f"BTC {'above' if btc['bullish'] else 'below'} EMA200 on "
                                f"{btc['timeframe']} ({btc['close']:.6g} vs "
                                f"{btc['ema200']:.6g}); dominance not covered")})
            else:
                out.append(check)
            continue

        out.append(check)
    return out


# --------------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------------


def build_snapshot(entry: dict, profile: str, count: int = 300) -> tuple[dict, dict]:
    """Assemble a snapshot in exactly the shape trade_plan.py --snapshot expects."""
    from datetime import datetime, timezone

    tp = skill._load_trade_plan_module()
    prof = tp.PROFILES[profile]
    symbol = entry["symbol"]

    roles = {"bias": prof["bias_tf"], "decision": prof["decision_tf"],
             "entry": prof["entry_tf"], "atr": prof["atr_tf"]}

    tfs, candles, fetched = {}, {}, {}
    for role, tf in roles.items():
        resolution = TF_TO_RESOLUTION.get(tf)
        if not resolution:
            # 1W is the real case here; the swing profile cannot run on this venue.
            tfs[role] = {"timeframe": tf,
                         "error": f"Tabdeal has no {tf} chart resolution"}
            continue
        if resolution not in fetched:
            fetched[resolution] = klines_cached(symbol, resolution, count)
        rows = fetched[resolution]
        if not rows:
            tfs[role] = {"timeframe": tf, "resolution": resolution,
                         "error": "no data"}
            continue
        tfs[role] = {"timeframe": tf, "resolution": resolution,
                     "indicators": skill.compute_indicators(rows)}
        candles[role] = {"timeframe": tf, "resolution": resolution, "candles": rows}

    entry_ind = tfs.get("entry", {}).get("indicators", {})
    atr_ind = tfs.get("atr", {}).get("indicators", {})
    dec_ind = tfs.get("decision", {}).get("indicators", {})

    # قیمت لحظه‌ای — the live futures book, not the last candle close.
    #
    # The candle series is Tabdeal's general chart feed (spot), and its most recent
    # close can be a whole bar old. Anchoring a plan's entry to it means the levels
    # are computed against a price that no longer exists, which on a 5-20 minute hold
    # is a meaningful part of the trade — and it is what the stale-signal drift guard
    # kept having to reject. The order book mid from /r/fapi/v1/depth is the real,
    # current price of the instrument actually being traded.
    #
    # Indicators still come from the candles: EMA/ATR/RSI need a series, and the two
    # track within 0.02-0.17%. Only the ENTRY anchor moves to the live book.
    live_px = mark_price(symbol)
    candle_px = entry_ind.get("last_close") or atr_ind.get("last_close")
    last_price = live_px or candle_px

    snap = {
        "symbol": symbol,
        "coin": entry["coin"],
        "profile": profile,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "tabdeal اهرم حرفه‌ای",
        "exchange": NAME,
        "timeframes": tfs,
        "last_price": last_price,
        "price_source": ("fapi orderbook mid (live)" if live_px
                         else "last candle close (book unavailable)"),
        "candle_close": candle_px,
        "atr_for_stop": atr_ind.get("atr14"),
        "atr_timeframe": roles["atr"],
        "swing_low": dec_ind.get("last_swing_low"),
        "swing_high": dec_ind.get("last_swing_high"),
        "contract": {
            # Quantity is in coins on this venue, so there is no multiplier to apply
            # anywhere downstream.
            "units_per_contract": 1.0,
            "max_leverage": entry.get("max_leverage") or MAX_LEVERAGE,
            "maint_margin_pct": entry.get("maint_margin_pct") or MAINT_MARGIN_PCT,
            "margin_mode": "cross",
        },
    }

    try:
        ob = orderbook(symbol, 20)
        bids, asks = ob["bids"][:5], ob["asks"][:5]
        if bids and asks:
            mid = (bids[0][0] + asks[0][0]) / 2
            snap["orderbook"] = {
                "best_bid": bids[0][0], "best_ask": asks[0][0],
                "spread_pct": (asks[0][0] - bids[0][0]) / mid * 100,
                "bid_value_top5": sum(p * q for p, q in bids),
                "ask_value_top5": sum(p * q for p, q in asks),
                "quantities_in": "coins",
            }
    except TabdealError as exc:
        snap["orderbook"] = {"error": str(exc)}

    try:
        snap["market_stats"] = ticker(symbol)
    except TabdealError as exc:
        snap["market_stats"] = {"error": str(exc)}

    snap["funding"] = funding_rate(symbol)
    btc = _btc_bias(prof["bias_tf"], count)
    snap["btc_bias"] = btc

    ds = skill.score_direction(profile, tfs)
    ds["checks"] = resolve_manual_checks(
        ds["checks"], coin=entry["coin"], symbol=symbol,
        decision_interval=tfs.get("decision", {}).get("resolution") or "240",
        funding=snap["funding"], btc=btc)
    auto = [c for c in ds["checks"] if c["long"] is not None]
    manual = [c for c in ds["checks"] if c["long"] is None]
    # Re-weigh, do not re-count. Resolving the manual checks adds votes, so the
    # totals must be rebuilt - but rebuilding them with plain integer sums threw the
    # family weighting away, and production scored exactly as before while the unit
    # tests on score_direction still passed. The families were being computed and
    # then discarded one function later.
    ds["long_score"], ds["short_score"], votes, fams = skill.weigh_votes(auto)
    ds["auto_checks"], ds["manual_checks"] = votes, len(manual)
    ds["auto_raw_checks"] = len(auto)
    ds["families"] = fams
    ds["threshold"] = round((5 if profile == "scalp" else 6) / 9.0 * votes, 2) or 1
    ds["note"] = (f"{ds['long_score']}/{votes} independent direction checks favour "
                  f"long, {ds['short_score']}/{votes} favour short "
                  f"({len(auto)} raw checks in {votes} families). "
                  + (f"{len(manual)} still need manual input."
                     if manual else "All checks resolved from live data."))
    snap["direction_score"] = ds

    dec = candles.get("decision")
    if dec:
        closes = [r["close"] for r in dec["candles"]]
        dec["ema"] = {f"ema{p}": skill.ema_series(closes, p) for p in (20, 50, 200)}
    return snap, candles


# --------------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------------

# The planner's `tabdeal` profile carries the venue's real maintenance margin, so
# unlike the Toobit path there is no correction to apply — the figure the plan is
# built with and the figure the venue uses are the same 0.5%.
PROFILE_MAINT_PCT = MAINT_MARGIN_PCT


def analyze(entry: dict, profile: str, *, capital: float, risk_pct: float,
            count: int = 300, hold_hours: float = 0.0,
            slots: int = 1, tp1_r: float | None = None,
            tp2_r: float | None = None,
            atr_mult: float | None = None) -> tuple[dict, dict, dict, dict]:
    """Snapshot, then plan, for one Tabdeal futures symbol.

    Returns (snapshot, plan, candles_by_role, side_info).

    The leverage-rounding repair and margin-budget helpers are imported from
    `toobit` rather than duplicated. They are pure arithmetic over a plan dict with
    no network access and nothing venue-specific in them — the `generic-perp`
    rounding bug they work around is a property of the shared skill, not of Toobit —
    and keeping one copy avoids the two drifting apart.
    """
    import tempfile
    from pathlib import Path

    from .toobit import _repair_leverage_rounding, margin_budget_pct

    snap, candles = build_snapshot(entry, profile, count)
    if not snap.get("last_price") or not snap.get("atr_for_stop"):
        raise TabdealError(f"insufficient candle data for {entry['symbol']}")

    side, side_info = skill.side_from_direction(snap)
    venue_cap = entry.get("max_leverage") or MAX_LEVERAGE

    with tempfile.TemporaryDirectory(prefix="tabdeal-") as tmp:
        path = Path(tmp) / "snap.json"
        path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        budget_pct = margin_budget_pct(slots)

        def build(cap, forced=None):
            return skill.plan(str(path), side, capital, profile=profile,
                              risk_pct=risk_pct, exchange=PLAN_EXCHANGE,
                              hold_hours=hold_hours, leverage_cap=cap,
                              leverage=forced, max_margin_pct=budget_pct,
                              tp1_r=tp1_r, tp2_r=tp2_r, atr_mult=atr_mult)

        plan = build(venue_cap)
        plan = _repair_leverage_rounding(plan, build, capital, venue_cap)

    plan.setdefault("venue", {})
    plan["venue"] = {
        "exchange": NAME,
        "label": LABEL,
        "symbol": entry["symbol"],
        "units_per_contract": 1.0,
        "max_leverage": venue_cap,
        "maint_margin_pct": MAINT_MARGIN_PCT,
        "planner_maint_margin_pct": PROFILE_MAINT_PCT,
        "leverage_correction": None,
        "margin_mode": "cross",
        "funding": None,
        "fees": {"maker_pct": MAKER_FEE_PCT, "taker_pct": TAKER_FEE_PCT,
                 "note": "0.1% both sides, no maker discount; promotional rate"},
    }
    qty = (plan.get("sizing") or {}).get("quantity")
    if qty is not None:
        # Coins, not contracts — but the field name is kept so the UI and the demo
        # can read one key across all venues.
        plan["venue"]["contracts"] = round(qty, 8)
    return snap, plan, candles, side_info
