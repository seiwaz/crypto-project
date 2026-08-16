"""Read-only Toobit client and snapshot builder.

Toobit replaces Nobitex as the default venue. Its REST API is Binance-shaped and,
unlike Nobitex, exposes everything this screener needs without credentials — so the
scanner runs entirely on public endpoints.

This module fetches data. It does not do trading maths: indicators and the direction
score come from the skill's own `compute_indicators` and `score_direction`, called on
Toobit candles, and the plan still comes from `trade_plan.py`. That keeps one
implementation of the risk maths across both venues.

Three Toobit-specific facts drive most of the code here:

* **Contracts are not coins.** `BTC-SWAP-USDT` has `contractMultiplier` 0.001 and
  `CRO-SWAP-USDT` has 10; `1000SHIB` carries its scale in the name instead. The
  skill sizes positions in coins, so the UI must also show the contract count or the
  number next to a Toobit order ticket would be wrong.
* **Leverage and maintenance margin are per contract and per tier**, published in
  `exchangeInfo`. Feeding the real maintenance margin into the planner makes the
  liquidation estimate correct rather than a generic 0.5% assumption.
* **Funding rate is public.** On Nobitex it was an unresolvable MANUAL check; here it
  is a live number, so a TAKE need not be permanently provisional.

Authenticated endpoints are currently geo-blocked for this network (Cloudflare 1010,
"not available in your region" — the VPN exits in the US, which Toobit restricts).
Nothing here depends on them.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, guard, skill

log = logging.getLogger("toobit")

NAME = "toobit"
LABEL = "Toobit — USDT perpetuals"
# The planner's exchange profile. Toobit is a true perp venue, so generic-perp is the
# honest fit; per-contract leverage and maintenance margin are passed in as overrides.
PLAN_EXCHANGE = "generic-perp"

DEFAULT_BASE = "https://api.toobit.com"
USER_AGENT = "LocalScreener/1.0 (read-only)"

# Documented limit is 3000 requests/minute — two orders of magnitude more headroom
# than Nobitex. A small gap still keeps us well-mannered and bounds a runaway loop.
_MIN_GAP_SECONDS = 0.06
_RETRIES = 3
_RETRY_BACKOFF = 1.5
_gap_lock = threading.Lock()
_last_call = 0.0

# Profile timeframe -> Toobit kline interval. Toobit has a native 1w, so unlike
# Nobitex there is no weekly aggregation step.
TF_TO_INTERVAL = {
    "5m": "5m", "15m": "15m", "30m": "30m", "1H": "1h", "4H": "4h",
    "6H": "6h", "12H": "12h", "1D": "1d", "1W": "1w",
}

_SCALE_RE = re.compile(r"^(\d+)(.+)$")


class ToobitError(RuntimeError):
    pass


# --------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------


def base_url() -> str:
    import os
    return (os.environ.get("TOOBIT_BASE_URL") or DEFAULT_BASE).rstrip("/")


def _get(path: str, params: dict | None = None, timeout: int = 20):
    """GET a public endpoint, after the read-only guard has cleared the path."""
    if params:
        path = f"{path}?{urllib.parse.urlencode(params)}"
    guard.assert_toobit_read_only(path, "GET")

    global _last_call
    with _gap_lock:
        wait = _MIN_GAP_SECONDS - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()

    req = urllib.request.Request(
        base_url() + path,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"})

    # This connection runs over a VPN that occasionally drops for a second or two —
    # observed live, taking five coins out of one scan with DNS failures. A transient
    # blip should cost a retry, not a coin's analysis. HTTP errors are not retried:
    # a 400 will still be a 400.
    last = None
    for attempt in range(_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise ToobitError(f"HTTP {exc.code} on {path}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = getattr(exc, "reason", exc)
            if attempt < _RETRIES - 1:
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
                continue
        except json.JSONDecodeError:
            raise ToobitError(f"non-JSON response from {path}") from None
    raise ToobitError(f"network error on {path} after {_RETRIES} attempts: {last}")


# --------------------------------------------------------------------------------
# Contracts
# --------------------------------------------------------------------------------

_contracts_cache: dict | None = None
_contracts_at = 0.0
_CONTRACTS_TTL = 3600


def contracts(refresh: bool = False) -> list[dict]:
    """Live perpetual contracts from /api/v1/exchangeInfo, cached for an hour."""
    global _contracts_cache, _contracts_at
    if (not refresh and _contracts_cache is not None
            and time.time() - _contracts_at < _CONTRACTS_TTL):
        return _contracts_cache
    info = _get("/api/v1/exchangeInfo", timeout=60)
    rows = [c for c in (info.get("contracts") or []) if c.get("status") == "TRADING"]
    _contracts_cache, _contracts_at = rows, time.time()
    return rows


def _split_scale(token: str) -> tuple[int, str]:
    """'1000SHIB' -> (1000, 'SHIB'). 'BTC' -> (1, 'BTC')."""
    m = _SCALE_RE.match(token.upper())
    if not m:
        return 1, token.upper()
    return int(m.group(1)), m.group(2)


def coin_units_per_contract(contract: dict) -> float:
    """How many coins one contract represents.

    Two independent scalings exist and both must be applied: `contractMultiplier`
    (BTC 0.001, CRO 10) and a numeric prefix in the symbol (1000SHIB). Getting this
    wrong misstates position size by orders of magnitude.
    """
    try:
        mult = float(contract.get("contractMultiplier") or 1)
    except (TypeError, ValueError):
        mult = 1.0
    prefix, _ = _split_scale(str(contract.get("underlying") or ""))
    return mult * prefix


def leverage_ceiling(contract: dict) -> float | None:
    """Highest maxLeverage across the contract's risk tiers."""
    values = []
    for tier in contract.get("riskLimits") or []:
        try:
            values.append(float(tier["maxLeverage"]))
        except (KeyError, TypeError, ValueError):
            continue
    return max(values) if values else None


def maintenance_margin_pct(contract: dict) -> float | None:
    """Maintenance margin of the first (smallest) risk tier, as a percentage.

    The planner takes a single figure, and the first tier is the one a retail-sized
    position actually sits in. Larger positions face a stricter tier, so this is the
    optimistic end — which is why it is reported alongside the liquidation estimate
    rather than silently baked in.
    """
    tiers = contract.get("riskLimits") or []
    if not tiers:
        return None
    try:
        return float(tiers[0]["maintMargin"]) * 100.0
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------------


_KLINES_TTL = 60.0
_klines_cache: dict[tuple[str, str, int], tuple[float, list]] = {}
_klines_lock = threading.Lock()


def klines_cached(symbol: str, interval: str, limit: int = 300) -> list[dict]:
    """Candles, deduplicated across one scan pass.

    Every profile names four timeframe roles, but they are not four distinct series:
    intraday uses 4H for both `decision` and `atr`, so each coin was fetching the same
    candles twice. The demo's monitoring loop re-fetches the same series every cycle
    as well.

    The TTL is deliberately shorter than any scan interval, so a later pass still sees
    fresh candles — this removes duplicate work inside one pass, it does not serve
    stale data to the next one.
    """
    key = (symbol, interval, limit)
    now = time.time()
    with _klines_lock:
        hit = _klines_cache.get(key)
        if hit and now - hit[0] < _KLINES_TTL:
            return hit[1]
    rows = klines(symbol, interval, limit)
    with _klines_lock:
        _klines_cache[key] = (time.time(), rows)
        if len(_klines_cache) > 512:            # bounded; a scan touches ~150 keys
            cutoff = time.time() - _KLINES_TTL
            for k, (at, _) in list(_klines_cache.items()):
                if at < cutoff:
                    del _klines_cache[k]
    return rows


def klines(symbol: str, interval: str, limit: int = 300) -> list[dict]:
    """OHLCV as the skill's compute_indicators expects it.

    Toobit returns Binance-style arrays:
      [openTime, open, high, low, close, volume, closeTime, quoteVolume, trades, ...]
    """
    raw = _get("/quote/v1/klines",
               {"symbol": symbol, "interval": interval, "limit": min(int(limit), 1000)})
    if not isinstance(raw, list):
        raise ToobitError(f"unexpected klines payload for {symbol}: {str(raw)[:120]}")
    rows = []
    for r in raw:
        try:
            rows.append({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S",
                                           time.gmtime(int(r[0]) / 1000)),
                "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
                "close": float(r[4]), "volume": float(r[5]),
            })
        except (IndexError, TypeError, ValueError):
            continue
    rows.sort(key=lambda x: x["timestamp"])
    return rows


def orderbook(symbol: str, depth: int = 20) -> dict:
    raw = _get("/quote/v1/depth", {"symbol": symbol, "limit": depth})
    bids = [(float(p), float(q)) for p, q in (raw.get("b") or [])]
    asks = [(float(p), float(q)) for p, q in (raw.get("a") or [])]
    return {"bids": bids, "asks": asks}


def ticker(symbol: str) -> dict:
    raw = _get("/quote/v1/ticker/24hr", {"symbol": symbol})
    row = raw[0] if isinstance(raw, list) and raw else raw
    return row if isinstance(row, dict) else {}


# Batched market data.
#
# Both endpoints return every symbol in one response — 744 index prices, 778 funding
# rates — so the cost of watching a portfolio is one request, not one per position.
# Fetching per symbol made demo.state() take 8.3s with four positions open, on every
# page poll, and it grew linearly with the number of slots.
#
# The TTLs differ because the data does: a mark moves continuously and is cached only
# long enough to stop one operation fetching it repeatedly, while funding is fixed for
# the whole 8-hour period and was being re-fetched every 60 seconds.
_INDEX_TTL = 2.0
_FUNDING_TTL = 300.0

_index_cache: tuple[float, dict] = (0.0, {})
_funding_cache: tuple[float, dict] = (0.0, {})
_batch_lock = threading.Lock()


def index_prices(refresh: bool = False) -> dict[str, dict]:
    """Every index token -> {"edp": float, "index": float}, in one request."""
    global _index_cache
    with _batch_lock:
        at, data = _index_cache
        if not refresh and data and time.time() - at < _INDEX_TTL:
            return data
    raw = _get("/quote/v1/index")
    out: dict[str, dict] = {}
    if isinstance(raw, dict):
        for field in ("index", "edp"):
            block = raw.get(field)
            if not isinstance(block, dict):
                continue
            for token, value in block.items():
                try:
                    out.setdefault(token, {})[field] = float(value)
                except (TypeError, ValueError):
                    continue
    with _batch_lock:
        _index_cache = (time.time(), out)
    return out


def funding_all(refresh: bool = False) -> dict[str, dict]:
    """Every contract's funding rate, in one request."""
    global _funding_cache
    with _batch_lock:
        at, data = _funding_cache
        if not refresh and data and time.time() - at < _FUNDING_TTL:
            return data
    raw = _get("/api/v1/futures/fundingRate", {"limit": 1})
    out: dict[str, dict] = {}
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict) or "rate" not in row:
            continue
        try:
            rate = float(row["rate"])
        except (TypeError, ValueError):
            continue
        out[str(row.get("symbol"))] = {
            "rate": rate,
            "rate_pct": rate * 100.0,
            "period": row.get("period"),
            "next_funding_time": row.get("nextFundingTime"),
            "rate_cap": row.get("fundingRateCap"),
            "rate_floor": row.get("fundingRateFloor"),
        }
    with _batch_lock:
        _funding_cache = (time.time(), out)
    return out


def funding_rate(symbol: str) -> dict | None:
    """Current funding rate. Public on Toobit — the check Nobitex could never settle."""
    try:
        return funding_all().get(symbol)
    except ToobitError as exc:
        log.debug("funding rate unavailable for %s: %s", symbol, exc)
        return None


# --------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------

AVAILABLE = "available"
NOT_LISTED = "not-listed"
AMBIGUOUS = "ambiguous"


def _index_contracts() -> dict[str, list[dict]]:
    """coin -> contracts, resolving the scale prefix so 1000SHIB indexes under SHIB."""
    out: dict[str, list[dict]] = {}
    for c in contracts():
        if str(c.get("quoteAsset", "")).upper() != "USDT" or c.get("inverse"):
            continue
        _, base = _split_scale(str(c.get("underlying") or ""))
        out.setdefault(base, []).append(c)
    return out


def discover(coins: list[str]) -> dict:
    """Resolve the requested coins to Toobit perpetual contracts."""
    from datetime import datetime, timezone

    index = _index_contracts()
    entries = []
    for coin in coins:
        entry = {"coin": coin, "symbol": None, "quote": "USDT", "status": NOT_LISTED,
                 "reason": None, "market_closed": False, "lot_size": 1,
                 "lot_label": None, "units_per_contract": 1.0,
                 "max_leverage": None, "maint_margin_pct": None}
        found = index.get(coin.upper()) or []

        if not found:
            entry["reason"] = "no USDT perpetual contract on Toobit"
            entries.append(entry)
            continue
        if len(found) > 1:
            # Two contracts claim the same base (Toobit lists PUMPBTC and PUMP2).
            # Guessing which one the user meant is exactly the kind of silent
            # decision that puts a position on the wrong instrument.
            entry["status"] = AMBIGUOUS
            entry["reason"] = ("several contracts match: "
                               + ", ".join(sorted(c["symbol"] for c in found))
                               + " — name the exact one in config/coins.txt")
            entries.append(entry)
            continue

        c = found[0]
        units = coin_units_per_contract(c)
        entry.update(
            symbol=c["symbol"],
            status=AVAILABLE,
            units_per_contract=units,
            lot_size=units,
            lot_label=(f"{units:g}" if units != 1 else None),
            max_leverage=leverage_ceiling(c),
            maint_margin_pct=maintenance_margin_pct(c),
        )
        if units != 1:
            entry["reason"] = (f"1 contract = {units:g} {coin} "
                               f"(Toobit lists it as {c['symbol']})")
        entries.append(entry)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exchange": NAME,
        "exchange_label": LABEL,
        "source": "GET /api/v1/exchangeInfo (contracts)",
        "margin_detection": (f"{len(contracts())} live perpetual contracts; leverage "
                             f"and maintenance margin read per contract"),
        "requested": len(coins),
        "coins": entries,
    }


def scannable(watchlist: dict) -> list[dict]:
    return [c for c in watchlist.get("coins", [])
            if c["status"] == AVAILABLE and not c.get("market_closed")]


# --------------------------------------------------------------------------------
# Manual-check resolution
# --------------------------------------------------------------------------------

BTC_SYMBOL = "BTC-SWAP-USDT"
# Above this, funding is crowded enough to count against the side paying it.
# 0.05% per 8h is ~0.15%/day, which is a real drag on a multi-period hold.
FUNDING_CROWDED_PCT = 0.05


# BTC bias is identical for every coin in a pass, so it is fetched once and reused.
# Without this a 47-coin scan pulled the same BTC candles 47 times.
_btc_cache: dict[tuple[str, int], tuple[float, dict | None]] = {}
_BTC_TTL = 120.0
# Held across the check-fetch-store, not just the dict access. With a parallel scan
# every worker starts at once, so an unlocked cache means all of them miss together
# and fetch the same BTC candles — the cache would save nothing on the one pass where
# it matters most.
_btc_lock = threading.Lock()


def _btc_bias(bias_tf: str, count: int) -> dict | None:
    """BTC trend on the bias timeframe, from Toobit's own BTC perp.

    Uses the skill's indicators so 'BTC is bullish' means the same thing here as
    everywhere else in the app. Cached briefly — the reading cannot meaningfully
    change within one scan, and refetching it per coin is pure waste.
    """
    interval = TF_TO_INTERVAL.get(bias_tf)
    if not interval:
        return None
    key = (interval, count)
    with _btc_lock:
        hit = _btc_cache.get(key)
        if hit and time.time() - hit[0] < _BTC_TTL:
            return hit[1]
        try:
            rows = klines(BTC_SYMBOL, interval, count)
        except ToobitError:
            return None
        if not rows:
            return None
        ind = skill.compute_indicators(rows)
        close, ema200 = ind.get("last_close"), ind.get("ema200")
        if close is None or ema200 is None:
            return None
        result = {"close": close, "ema200": ema200, "bullish": close > ema200,
                  "timeframe": bias_tf}
        _btc_cache[key] = (time.time(), result)
        return result


def resolve_manual_checks(checks: list[dict], *, coin: str, funding: dict | None,
                          btc: dict | None) -> list[dict]:
    """Fill in the two checks the skill marks MANUAL, where Toobit can settle them.

    Anything not genuinely settled stays MANUAL. In particular the skill's check is
    "BTC / dominance alignment" and Toobit gives no dominance figure, so the observed
    string says plainly that only the BTC leg was resolved.
    """
    out = []
    for check in checks:
        name = check.get("check", "")
        if check.get("long") is not None:
            out.append(check)
            continue

        if name.startswith("BTC / dominance") and btc:
            if coin.upper() == "BTC":
                out.append({**check, "long": True, "short": True,
                            "resolved_by": "toobit",
                            "observed": "this is BTC — alignment is self-referential"})
            else:
                out.append({**check, "long": btc["bullish"], "short": not btc["bullish"],
                            "resolved_by": "toobit",
                            "observed": (
                                f"BTC {'above' if btc['bullish'] else 'below'} EMA200 on "
                                f"{btc['timeframe']} ({btc['close']:.6g} vs "
                                f"{btc['ema200']:.6g}); dominance not covered")})
            continue

        if name.startswith("funding rate") and funding:
            pct = funding["rate_pct"]
            # Positive funding: longs pay shorts, i.e. the book is crowded long.
            out.append({**check,
                        "long": pct <= FUNDING_CROWDED_PCT,
                        "short": pct >= -FUNDING_CROWDED_PCT,
                        "resolved_by": "toobit",
                        "observed": (f"funding {pct:+.4f}% per {funding.get('period') or '8H'} "
                                     f"(crowded beyond ±{FUNDING_CROWDED_PCT}%)")})
            continue

        out.append(check)
    return out


# --------------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------------


def build_snapshot(entry: dict, profile: str, count: int = 300) -> tuple[dict, dict]:
    """Assemble a snapshot in exactly the shape trade_plan.py --snapshot expects.

    Returns (snapshot, candles_by_role); candles feed the chart.
    """
    from datetime import datetime, timezone

    tp = skill._load_trade_plan_module()
    prof = tp.PROFILES[profile]
    symbol = entry["symbol"]
    units = float(entry.get("units_per_contract") or 1)

    roles = {"bias": prof["bias_tf"], "decision": prof["decision_tf"],
             "entry": prof["entry_tf"], "atr": prof["atr_tf"]}

    tfs, candles, fetched = {}, {}, {}
    for role, tf in roles.items():
        interval = TF_TO_INTERVAL.get(tf)
        if not interval:
            tfs[role] = {"timeframe": tf, "error": f"no Toobit interval for {tf}"}
            continue
        if interval not in fetched:
            fetched[interval] = klines(symbol, interval, count)
        rows = fetched[interval]
        if not rows:
            tfs[role] = {"timeframe": tf, "resolution": interval, "error": "no data"}
            continue
        tfs[role] = {"timeframe": tf, "resolution": interval,
                     "indicators": skill.compute_indicators(rows)}
        candles[role] = {"timeframe": tf, "resolution": interval, "candles": rows}

    entry_ind = tfs.get("entry", {}).get("indicators", {})
    atr_ind = tfs.get("atr", {}).get("indicators", {})
    dec_ind = tfs.get("decision", {}).get("indicators", {})

    snap = {
        "symbol": symbol,
        "coin": entry["coin"],
        "profile": profile,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "toobit perpetuals",
        "exchange": NAME,
        "timeframes": tfs,
        "last_price": entry_ind.get("last_close") or atr_ind.get("last_close"),
        "atr_for_stop": atr_ind.get("atr14"),
        "atr_timeframe": roles["atr"],
        "swing_low": dec_ind.get("last_swing_low"),
        "swing_high": dec_ind.get("last_swing_high"),
        "contract": {
            "units_per_contract": units,
            "max_leverage": entry.get("max_leverage"),
            "maint_margin_pct": entry.get("maint_margin_pct"),
        },
    }

    # Order book. Quantities are in contracts, so depth value needs the multiplier —
    # without it BTC's book would read 1000x thinner than it is.
    try:
        ob = orderbook(symbol, 20)
        bids, asks = ob["bids"][:5], ob["asks"][:5]
        if bids and asks:
            mid = (bids[0][0] + asks[0][0]) / 2
            snap["orderbook"] = {
                "best_bid": bids[0][0], "best_ask": asks[0][0],
                "spread_pct": (asks[0][0] - bids[0][0]) / mid * 100,
                "bid_value_top5": sum(p * q * units for p, q in bids),
                "ask_value_top5": sum(p * q * units for p, q in asks),
                "quantities_in": "contracts",
            }
    except ToobitError as exc:
        snap["orderbook"] = {"error": str(exc)}

    try:
        snap["market_stats"] = ticker(symbol)
    except ToobitError as exc:
        snap["market_stats"] = {"error": str(exc)}

    funding = funding_rate(symbol)
    snap["funding"] = funding
    btc = _btc_bias(prof["bias_tf"], count)
    snap["btc_bias"] = btc

    ds = skill.score_direction(profile, tfs)
    ds["checks"] = resolve_manual_checks(ds["checks"], coin=entry["coin"],
                                         funding=funding, btc=btc)
    # Recount: checks the venue just settled are now automated, not manual.
    auto = [c for c in ds["checks"] if c["long"] is not None]
    manual = [c for c in ds["checks"] if c["long"] is None]
    ds["auto_checks"], ds["manual_checks"] = len(auto), len(manual)
    ds["long_score"] = sum(1 for c in auto if c["long"])
    ds["short_score"] = sum(1 for c in auto if c["short"])
    ds["note"] = (f"{ds['long_score']}/{len(auto)} automated checks favour long, "
                  f"{ds['short_score']}/{len(auto)} favour short. "
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

# generic-perp assumes this maintenance margin. Toobit publishes the real per-tier
# figure, which is lower for majors (BTC/ETH 0.25%) but much higher for small caps
# (CRO 2.5%). Where the real figure is higher, the profile's assumption puts
# liquidation further away than it actually is — the one direction that must not be
# left uncorrected.
GENERIC_PERP_MAINT_PCT = 0.5


def safe_leverage_for(maint_pct: float, stop_pct: float, buffer: float) -> float:
    """Highest leverage that still keeps liquidation `buffer` x the stop away.

    This is the skill's own relation from SKILL.md — max_safe_lev = 100 / (stop_pct x
    buffer) — with the venue's real maintenance margin added to the denominator,
    because liquidation triggers that much before the margin is actually exhausted.
    It is used only to produce a `--leverage-cap` input; all sizing still happens
    inside trade_plan.py.
    """
    denominator = (stop_pct * buffer) + maint_pct
    return 100.0 / denominator if denominator > 0 else 0.0


# The planner's own default: no single position may tie up more than a quarter of
# capital as margin.
BASE_MAX_MARGIN_PCT = 25.0


def margin_budget_pct(slots: int) -> float:
    """Share the margin budget across the positions the account must carry.

    This is what the skill's `--slots` flag does, expressed with the flag the
    installed planner actually has. The skill is explicit that a leverage stuck at 1x
    is almost always this: "the margin budget defaults to a single position, so on a
    small account the notional never exceeds it and no leverage is needed".

    It does not increase risk. Risk is quantity times stop distance, and neither
    changes here — the same 1% of capital is at stake either way. What changes is how
    much collateral is locked to hold that position, and therefore how far away
    liquidation sits. The planner's own safety cap, 100 / (stop% x liq buffer), still
    binds, so leverage can only rise until liquidation reaches the buffer the profile
    demands - 4x the stop distance for intraday. That cap is what keeps this safe,
    and it is the skill's, not ours.
    """
    return BASE_MAX_MARGIN_PCT / max(1, int(slots))


def analyze(entry: dict, profile: str, *, capital: float, risk_pct: float,
            count: int = 300, hold_hours: float = 0.0,
            slots: int = 1) -> tuple[dict, dict, dict, dict]:
    """Snapshot, then plan, for one Toobit contract.

    Returns (snapshot, plan, candles_by_role, side_info).

    The plan may be built twice. The first pass establishes the stop distance; if the
    resulting leverage would not survive Toobit's real maintenance margin for this
    contract, it is re-run with a corrected leverage cap so the liquidation buffer
    the gate checks is one that actually holds on this venue.
    """
    import tempfile
    from pathlib import Path

    tp = skill._load_trade_plan_module()
    prof = tp.PROFILES[profile]

    snap, candles = build_snapshot(entry, profile, count)
    if not snap.get("last_price") or not snap.get("atr_for_stop"):
        raise ToobitError(f"insufficient candle data for {entry['symbol']}")

    side, side_info = skill.side_from_direction(snap)
    venue_cap = entry.get("max_leverage")

    with tempfile.TemporaryDirectory(prefix="toobit-") as tmp:
        path = Path(tmp) / "snap.json"
        path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")

        budget_pct = margin_budget_pct(slots)

        def build(cap, forced=None):
            return skill.plan(str(path), side, capital, profile=profile,
                              risk_pct=risk_pct, exchange=PLAN_EXCHANGE,
                              hold_hours=hold_hours, leverage_cap=cap,
                              leverage=forced, max_margin_pct=budget_pct)

        plan = build(venue_cap)
        plan = _repair_leverage_rounding(plan, build, capital, venue_cap)

        maint = entry.get("maint_margin_pct")
        correction = None
        if maint and maint > GENERIC_PERP_MAINT_PCT:
            stop_pct = (plan.get("levels") or {}).get("stop_pct")
            leverage = (plan.get("sizing") or {}).get("leverage")
            if stop_pct and leverage:
                safe = safe_leverage_for(maint, stop_pct, prof["liq_buffer"])
                if leverage > safe:
                    correction = {
                        "reason": (f"Toobit's maintenance margin for this contract is "
                                   f"{maint:g}%, above the {GENERIC_PERP_MAINT_PCT:g}% the "
                                   f"planner's generic-perp profile assumes. Leverage was "
                                   f"re-capped from {leverage:g}x to {safe:.2f}x so the "
                                   f"liquidation buffer holds at the real figure."),
                        "was": leverage, "now": round(safe, 2),
                        "maint_margin_pct": maint,
                    }
                    capped = min(safe, venue_cap) if venue_cap else safe
                    plan = _repair_leverage_rounding(build(capped), build, capital,
                                                     capped)

    plan.setdefault("venue", {})
    plan["venue"] = {
        "exchange": NAME,
        "label": LABEL,
        "symbol": entry["symbol"],
        "units_per_contract": entry.get("units_per_contract"),
        "max_leverage": venue_cap,
        "maint_margin_pct": entry.get("maint_margin_pct"),
        "planner_maint_margin_pct": GENERIC_PERP_MAINT_PCT,
        "leverage_correction": correction,
        "funding": snap.get("funding"),
    }
    # Position size in the unit the exchange actually takes orders in.
    qty = (plan.get("sizing") or {}).get("quantity")
    units = float(entry.get("units_per_contract") or 1)
    if qty is not None and units:
        plan["venue"]["contracts"] = round(qty / units, 4)
    return snap, plan, candles, side_info


_ROUNDING_BLOCKER = "cannot fund a notional"


def _repair_leverage_rounding(plan: dict, build, capital: float,
                              cap: float | None) -> dict:
    """Work around a rounding bug in the skill's `snap_leverage`.

    Its docstring promises "smallest allowed leverage >= value", but for a venue with
    no discrete leverage steps — which `generic-perp`, and therefore Toobit, is — it
    does `round(value, 2)`. That rounds *down*: a required 1.2845x becomes 1.28x,
    which then fails the script's own `leverage < needed` check and raises a spurious
    "cannot fund a notional" blocker. Nobitex never hit it because its leverage steps
    are discrete, so every generic-perp plan is affected and roughly half trip it.

    Rather than patch the shared skill, this re-runs the plan with leverage pinned to
    the ceiling of what is needed — and only when that value still sits inside the
    caps the skill itself calculated, so the safety clamp is never widened. A
    genuinely unfundable position keeps its blocker.
    """
    import math

    if not any(_ROUNDING_BLOCKER in b for b in (plan.get("blockers") or [])):
        return plan
    sizing = plan.get("sizing") or {}
    notional = sizing.get("notional")
    caps = [v for v in (sizing.get("leverage_caps") or {}).values()
            if isinstance(v, (int, float))]
    if cap:
        caps.append(cap)
    if not notional or not caps:
        return plan

    # 25% is the planner's own default --max-margin-pct; we never override it.
    needed = notional / (0.25 * capital)
    forced = math.ceil(needed * 100) / 100
    if forced > min(caps) + 1e-9:
        return plan
    repaired = build(cap, forced)
    still_blocked = any(_ROUNDING_BLOCKER in b for b in (repaired.get("blockers") or []))
    return plan if still_blocked else repaired
