#!/usr/bin/env python3
"""
nobitex_api.py - read-only Nobitex API client for the trade-plan skill.

Fetches candles, order books, market stats, margin settings and open positions, and
assembles a `snapshot` containing every indicator the trade plan needs. Standard
library only, plus optional `cryptography`/PyNaCl for faster Ed25519.

SAFETY MODEL
------------
This client is read-only by construction, not by convention:

  * Every request path is checked against an allowlist before the socket opens.
  * Any path matching an order/withdraw/cancel pattern raises immediately, even if
    it somehow appeared in the allowlist.
  * Credentials are read from the environment or a 0600 file - never from argv,
    which would leak them into shell history and the process table.
  * Credentials are redacted from all output, including exceptions.

Placing and closing orders stays with the human. An analysis tool that can also
trade is one prompt injection away from being a trading bot nobody authorised.

USAGE
-----
  export NOBITEX_API_KEY=...        # public key
  export NOBITEX_API_SECRET=...     # privateKey shown once at creation
  # or, for the legacy panel token:
  export NOBITEX_TOKEN=...

  python3 nobitex_api.py auth-check
  python3 nobitex_api.py candles --symbol BTCIRT --resolution 240 --count 300
  python3 nobitex_api.py orderbook --symbol BTCUSDT
  python3 nobitex_api.py snapshot --symbol ETHUSDT --profile intraday --out snap.json
  python3 nobitex_api.py positions
"""

import argparse
import json
import math
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nobitex_ed25519 as ed  # noqa: E402
import trade_plan as tp  # noqa: E402

BASE_URL = os.environ.get("NOBITEX_BASE_URL", "https://apiv2.nobitex.ir")
USER_AGENT = os.environ.get("NOBITEX_USER_AGENT", "TraderBot/ClaudeTradePlan")

# --------------------------------------------------------------------------------
# Read-only guard
# --------------------------------------------------------------------------------

PUBLIC_PATHS = (
    "/v3/orderbook/", "/v2/depth/", "/v2/trades/", "/market/stats",
    "/market/udf/history",
)

PRIVATE_ALLOWLIST = {
    "/users/profile",
    "/users/limitations",
    "/users/wallets/list",
    "/v2/wallets",
    "/users/wallets/balance",
    "/margin/fee-rates",
    "/margin/delegation-limit",
    "/margin/v2/delegation-limit",
    "/positions/list",
    "/positions/active-count",
    "/market/orders/list",
    "/market/trades/list",
}

# Substrings that must never appear in a request path, regardless of anything else.
FORBIDDEN = (
    "orders/add", "orders/batch", "cancel", "withdraw", "/close", "edit-collateral",
    "convert", "update-status", "apikeys", "logout", "login", "transfer",
)


class ReadOnlyViolation(RuntimeError):
    pass


def assert_read_only(path):
    low = path.lower()
    for bad in FORBIDDEN:
        if bad in low:
            raise ReadOnlyViolation(
                f"Refusing to call '{path}': this client is read-only and never "
                f"places, modifies, closes or cancels anything. Execute manually.")
    base = path.split("?")[0]
    if any(base.startswith(p) for p in PUBLIC_PATHS):
        return
    if base in PRIVATE_ALLOWLIST:
        return
    if base.startswith("/positions/") and base.endswith("/status"):
        return
    if base.startswith("/margin/predict/"):
        return
    raise ReadOnlyViolation(
        f"Path '{base}' is not on the read-only allowlist. Add it to "
        f"PRIVATE_ALLOWLIST only if it is genuinely a read endpoint.")


# --------------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------------


class Credentials:
    def __init__(self, key=None, secret=None, token=None, source="none"):
        self.key, self.secret, self.token, self.source = key, secret, token, source

    @property
    def mode(self):
        if self.key and self.secret:
            return "apikey"
        if self.token:
            return "token"
        return "public"

    def redact(self, text):
        for v in (self.key, self.secret, self.token):
            if v and len(str(v)) > 6:
                text = text.replace(str(v), "***REDACTED***")
        return text

    def describe(self):
        def mask(v):
            if not v:
                return None
            v = str(v)
            return f"{v[:4]}...{v[-4:]} ({len(v)} chars)" if len(v) > 12 else "***"
        return {"mode": self.mode, "source": self.source,
                "key": mask(self.key), "secret": "***" if self.secret else None,
                "token": mask(self.token)}


def load_credentials(creds_file=None):
    """Environment first, then an optional JSON file. Never from argv."""
    key = os.environ.get("NOBITEX_API_KEY")
    secret = os.environ.get("NOBITEX_API_SECRET")
    token = os.environ.get("NOBITEX_TOKEN")
    if key or token:
        return Credentials(key, secret, token, "environment")

    path = creds_file or os.environ.get("NOBITEX_CREDS_FILE")
    if path and os.path.exists(path):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            print(f"warning: {path} is readable by others (mode {oct(mode)}). "
                  f"Run: chmod 600 {path}", file=sys.stderr)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return Credentials(
            data.get("apiKey") or data.get("key") or data.get("publicKey"),
            data.get("apiSecret") or data.get("secret") or data.get("privateKey"),
            data.get("token"),
            f"file:{path}")
    return Credentials(source="none")


# --------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------


class NobitexClient:
    def __init__(self, creds, base_url=BASE_URL, timeout=20, min_interval=1.1,
                 verbose=False):
        self.creds = creds
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.min_interval = min_interval   # Nobitex caches sub-second calls anyway
        self.verbose = verbose
        self._last_call = 0.0

    def _headers(self, method, path, body):
        h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if body:
            h["Content-Type"] = "application/json"
        base = path.split("?")[0]
        needs_auth = not any(base.startswith(p) for p in PUBLIC_PATHS)
        if not needs_auth:
            return h
        if self.creds.mode == "apikey":
            ts = str(int(time.time()))
            h["Nobitex-Key"] = self.creds.key
            h["Nobitex-Timestamp"] = ts
            h["Nobitex-Signature"] = ed.sign_b64(
                self.creds.secret, ts, method, path, body or "")
        elif self.creds.mode == "token":
            h["Authorization"] = f"Token {self.creds.token}"
        else:
            raise RuntimeError(
                f"'{base}' needs credentials. Set NOBITEX_API_KEY + "
                f"NOBITEX_API_SECRET, or NOBITEX_TOKEN.")
        return h

    def request(self, method, path, params=None, body=None):
        if params:
            qs = urllib.parse.urlencode(params)
            path = f"{path}?{qs}"
        assert_read_only(path)

        raw_body = json.dumps(body, separators=(",", ":")) if body else ""
        headers = self._headers(method, path, raw_body)

        gap = time.time() - self._last_call
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_call = time.time()

        req = urllib.request.Request(
            self.base_url + path, method=method.upper(), headers=headers,
            data=raw_body.encode() if raw_body else None)
        if self.verbose:
            print(f"  -> {method.upper()} {path}", file=sys.stderr)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(self.creds.redact(
                f"HTTP {e.code} on {path}: {detail}")) from None
        except urllib.error.URLError as e:
            raise RuntimeError(self.creds.redact(
                f"Network error on {path}: {e.reason}")) from None

    # -- endpoints ---------------------------------------------------------------

    def candles(self, symbol, resolution, count=300, end=None):
        end = end or int(time.time())
        data = self.request("GET", "/market/udf/history", {
            "symbol": symbol, "resolution": resolution,
            "to": end, "countback": min(count, 500)})
        if data.get("s") == "no_data":
            return []
        if data.get("s") != "ok":
            raise RuntimeError(f"udf/history: {data.get('errmsg', data)}")
        return [{"timestamp": datetime.fromtimestamp(t, tz=timezone.utc)
                             .strftime("%Y-%m-%dT%H:%M:%S"),
                 "open": float(o), "high": float(h), "low": float(l),
                 "close": float(c), "volume": float(v)}
                for t, o, h, l, c, v in zip(
                    data["t"], data["o"], data["h"], data["l"], data["c"], data["v"])]

    def orderbook(self, symbol):
        return self.request("GET", f"/v3/orderbook/{symbol}")

    def market_stats(self, src=None, dst=None):
        params = {}
        if src:
            params["srcCurrency"] = src
        if dst:
            params["dstCurrency"] = dst
        return self.request("GET", "/market/stats", params or None)

    def profile(self):
        return self.request("GET", "/users/profile")

    def margin_fee_rates(self):
        return self.request("GET", "/margin/fee-rates")

    def delegation_limit(self, symbol=None):
        return self.request("GET", "/margin/v2/delegation-limit",
                            {"symbol": symbol} if symbol else None)

    def positions(self):
        return self.request("GET", "/positions/list")

    def wallets(self):
        return self.request("POST", "/users/wallets/list")


# --------------------------------------------------------------------------------
# Symbols and resolutions
# --------------------------------------------------------------------------------

RESOLUTIONS = {"1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "1H",
               "180": "3H", "240": "4H", "360": "6H", "720": "12H",
               "D": "1D", "2D": "2D", "3D": "3D"}

TF_TO_RESOLUTION = {"5m": "5", "15m": "15", "30m": "30", "1H": "60", "3H": "180",
                    "4H": "240", "6H": "360", "12H": "720", "1D": "D", "1W": "D"}


def split_symbol(symbol):
    """'BTCIRT' -> ('btc', 'rls'); 'ETHUSDT' -> ('eth', 'usdt')."""
    s = symbol.upper()
    for quote, cur in (("USDT", "usdt"), ("IRT", "rls"), ("RLS", "rls")):
        if s.endswith(quote):
            return s[:-len(quote)].lower(), cur
    return None, None


def aggregate_weekly(daily):
    """Nobitex has no weekly resolution, so build it from daily candles."""
    out, bucket = [], []
    for row in daily:
        d = datetime.strptime(row["timestamp"], "%Y-%m-%dT%H:%M:%S")
        if bucket and d.isocalendar()[1] != datetime.strptime(
                bucket[0]["timestamp"], "%Y-%m-%dT%H:%M:%S").isocalendar()[1]:
            out.append(_merge(bucket))
            bucket = []
        bucket.append(row)
    if bucket:
        out.append(_merge(bucket))
    return out


def _merge(rows):
    return {"timestamp": rows[0]["timestamp"], "open": rows[0]["open"],
            "high": max(r["high"] for r in rows), "low": min(r["low"] for r in rows),
            "close": rows[-1]["close"], "volume": sum(r["volume"] for r in rows)}


# --------------------------------------------------------------------------------
# Indicators over fetched candles
# --------------------------------------------------------------------------------


def _ichimoku(highs, lows, end_idx, period):
    """(period-high + period-low) / 2, the shared building block for Tenkan/Kijun/
    Senkou B, using only bars up to and including end_idx."""
    if end_idx < period - 1:
        return None
    window_h = highs[end_idx - period + 1: end_idx + 1]
    window_l = lows[end_idx - period + 1: end_idx + 1]
    return (max(window_h) + min(window_l)) / 2


def ichimoku_cloud(highs, lows):
    """Tenkan-sen, Kijun-sen, and the cloud that actually applies to the current bar.

    Senkou Span A/B are computed from data as of 26 bars ago and plotted 26 bars
    *forward* — so the cloud boundary sitting under today's candle was calculated
    using the state of the market 26 bars back, not today's. Getting this backwards
    (using today's high/low window instead) is the single most common Ichimoku
    implementation bug and silently produces a cloud that lags by half a cycle.
    Needs ~78 bars of history (26 lookback + 52 for Senkou B) before it resolves.
    """
    n = len(highs)
    last = n - 1
    tenkan = _ichimoku(highs, lows, last, 9)
    kijun = _ichimoku(highs, lows, last, 26)

    cloud_idx = last - 26
    span_a = span_b = None
    if cloud_idx >= 0:
        ct = _ichimoku(highs, lows, cloud_idx, 9)
        ck = _ichimoku(highs, lows, cloud_idx, 26)
        cb = _ichimoku(highs, lows, cloud_idx, 52)
        if ct is not None and ck is not None and cb is not None:
            span_a, span_b = (ct + ck) / 2, cb

    out = {"tenkan": tenkan, "kijun": kijun,
           "cloud_span_a": span_a, "cloud_span_b": span_b}
    if span_a is not None and span_b is not None:
        out["cloud_top"] = max(span_a, span_b)
        out["cloud_bottom"] = min(span_a, span_b)
        out["cloud_bullish"] = span_a > span_b   # "green" cloud
    return out


def compute_indicators(rows):
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    closes = [r["close"] for r in rows]
    vols = [r["volume"] for r in rows]
    sw_h, sw_l = tp.find_swings(highs, lows)
    a = tp.atr(highs, lows, closes, 14)
    last = closes[-1]
    ichimoku = ichimoku_cloud(highs, lows)

    def structure():
        if len(sw_h) < 2 or len(sw_l) < 2:
            return "unknown"
        hh = sw_h[-1][1] > sw_h[-2][1]
        hl = sw_l[-1][1] > sw_l[-2][1]
        lh = sw_h[-1][1] < sw_h[-2][1]
        ll = sw_l[-1][1] < sw_l[-2][1]
        if hh and hl:
            return "uptrend"
        if lh and ll:
            return "downtrend"
        return "range"

    up = sum(r["volume"] for r in rows[-10:] if r["close"] >= r["open"])
    down = sum(r["volume"] for r in rows[-10:] if r["close"] < r["open"])

    return {
        "candles": len(rows),
        "last_close": last,
        "atr14": a,
        "atr_pct": (a / last * 100) if a else None,
        "ema20": tp.ema(closes, 20),
        "ema50": tp.ema(closes, 50),
        "ema200": tp.ema(closes, 200),
        "rsi14": tp.rsi(closes, 14),
        "rvol20": tp.rvol(vols, 20),
        "session_vwap": tp.session_vwap(rows),
        "last_swing_high": sw_h[-1][1] if sw_h else None,
        "last_swing_low": sw_l[-1][1] if sw_l else None,
        "structure": structure(),
        "volume_bias": ("up" if up > down * 1.1 else
                        "down" if down > up * 1.1 else "balanced"),
        **ichimoku,
    }


def weigh_votes(auto_checks):
    """Collapse duplicated checks so one fact cannot cast two votes.

    Each family carries a total weight of 1, split evenly among its members, and
    returns `(long_weight, short_weight, family_count, family_sizes)`.

    Deliberately fractional rather than one-vote-per-family-majority: collapsing to a
    majority makes a family ABSTAIN whenever its members disagree, and tested against
    live data that deflated typical counts to 3-2 of 6 against a threshold of 4 -
    which would have stopped signal generation outright. Weighting keeps the
    granularity (an internally split family contributes 0.5/0.5) while still capping
    a duplicated pair's combined influence at one check's worth.

    Shared, not duplicated, because the venue adapters resolve the manual checks and
    then RE-COUNT the votes. tabdeal.build_snapshot did exactly that with plain
    integer sums and silently discarded the weighting - the families were computed
    and then thrown away, so production scored unchanged while the unit tests passed.
    """
    fams: dict[str, list] = {}
    for c in auto_checks:
        fams.setdefault(c.get("family") or c["check"], []).append(c)
    long_w = short_w = 0.0
    for members in fams.values():
        w = 1.0 / len(members)
        long_w += w * sum(1 for c in members if c["long"])
        short_w += w * sum(1 for c in members if c["short"])
    return (round(long_w, 2), round(short_w, 2), len(fams),
            {k: len(v) for k, v in fams.items()})


def score_direction(profile, tfs):
    """Auto-evaluate the checks that OHLCV can settle. The rest stay manual.

    Returning explicit `null` for unresolved checks matters: a plan that silently
    treats an unknown as a pass is exactly the overconfidence this skill exists to
    prevent.
    """
    bias = tfs.get("bias", {}).get("indicators", {})
    dec = tfs.get("decision", {}).get("indicators", {})
    checks = []

    # `family` groups checks that measure the SAME underlying fact. Votes are counted
    # per family, not per check, because a vote count is only evidence if the votes
    # carry different information.
    #
    # Measured over 1,331 historical evaluations on 8 coins (2026-08-23), pairwise
    # agreement between checks averaged 60.5% - but two pairs were near-duplicates:
    #
    #     price vs EMA200 (bias)   <-> EMA50 vs EMA200 (bias)    90.7%
    #     price vs EMA50 (decision) <-> price vs session VWAP     87.7%
    #
    # Both pairs are "is price above its recent average", asked twice. Counting them
    # as four independent votes inflated `direction_ratio` - 35 of the 100 score
    # points - exactly in a trending market, which is when the move is most likely
    # already extended. That shows up in the outcomes: signals with >=8 of 9 votes
    # won 41% of the time over a 4h hold against 52% for 7 votes, and Round 10 found
    # the same shape independently (the 80-89 score band underperformed 70-79).
    #
    # This does not drop a check - all of them stay visible in `checks` with their
    # own reasoning. It stops one fact being counted twice.
    def add(name, long_ok, short_ok, observed, family=None):
        checks.append({"check": name, "long": long_ok, "short": short_ok,
                       "observed": observed, "family": family or name})

    if bias.get("ema200") and bias.get("last_close"):
        above = bias["last_close"] > bias["ema200"]
        add("price vs EMA200 (bias TF)", above, not above,
            f"close {bias['last_close']:.6g} vs EMA200 {bias['ema200']:.6g}",
            family="bias-tf trend")
    if bias.get("ema50") and bias.get("ema200"):
        bull = bias["ema50"] > bias["ema200"]
        add("EMA50 vs EMA200 (bias TF)", bull, not bull,
            f"EMA50 {bias['ema50']:.6g} vs EMA200 {bias['ema200']:.6g}",
            family="bias-tf trend")
    if dec.get("structure") in ("uptrend", "downtrend", "range"):
        st = dec["structure"]
        add("market structure (decision TF)", st == "uptrend", st == "downtrend", st)
    if dec.get("ema50") and dec.get("last_close"):
        above = dec["last_close"] > dec["ema50"]
        add("price vs EMA50 (decision TF)", above, not above,
            f"close {dec['last_close']:.6g} vs EMA50 {dec['ema50']:.6g}",
            family="decision-tf mean")
    if dec.get("rsi14") is not None:
        r = dec["rsi14"]
        if profile == "scalp":
            add("RSI band", 45 <= r <= 65, 35 <= r <= 55, f"RSI {r:.1f}")
        else:
            add("RSI band", 45 <= r <= 70, 30 <= r <= 55, f"RSI {r:.1f}")
    if dec.get("volume_bias"):
        vb = dec["volume_bias"]
        add("volume bias (last 10)", vb == "up", vb == "down", vb)
    if profile == "scalp":
        vwap = tfs.get("decision", {}).get("indicators", {}).get("session_vwap")
        if vwap and dec.get("last_close"):
            above = dec["last_close"] > vwap
            add("price vs session VWAP", above, not above,
                f"close {dec['last_close']:.6g} vs VWAP {vwap:.6g}",
                family="decision-tf mean")

    # Ichimoku cloud (2026-08-20, requested to sharpen direction confirmation). Only
    # scored when price is clearly outside the cloud - inside the cloud is Ichimoku's
    # own definition of "no trade," and forcing a long/short guess there would be
    # exactly the overconfidence this scoring exists to avoid. Threshold left
    # unchanged rather than raised: this adds a vote to the same pool other checks
    # draw from, it doesn't replace one, so the bar to qualify isn't getting harder.
    top, bot = dec.get("cloud_top"), dec.get("cloud_bottom")
    if top is not None and bot is not None and dec.get("last_close") is not None:
        close = dec["last_close"]
        if close > top or close < bot:
            add("price vs Ichimoku cloud", close > top, close < bot,
                f"close {close:.6g} vs cloud [{bot:.6g}, {top:.6g}]"
                f"{' (bullish/green)' if dec.get('cloud_bullish') else ' (bearish/red)'}")

    add("BTC / dominance alignment", None, None, "MANUAL - not derivable from OHLCV")
    if profile != "scalp":
        add("funding rate not crowded against", None, None,
            "MANUAL - read from a global perp venue")

    auto = [c for c in checks if c["long"] is not None]
    manual = [c for c in checks if c["long"] is None]

    long_score, short_score, auto_votes, fam_sizes = weigh_votes(auto)

    # The bar is rescaled with the denominator so grouping does not silently tighten
    # it: 5-of-9 and 6-of-9 keep the same meaning against a smaller total.
    base = 5 if profile == "scalp" else 6
    threshold = round(base / 9.0 * auto_votes, 2) if auto_votes else base
    total = len(checks)

    return {
        "checks": checks,
        "auto_checks": auto_votes,
        "auto_raw_checks": len(auto),
        "families": fam_sizes,
        "manual_checks": len(manual),
        "total_checks": total,
        "long_score": long_score,
        "short_score": short_score,
        "threshold": threshold,
        "note": (f"{long_score}/{auto_votes} independent direction checks favour "
                 f"long, {short_score}/{auto_votes} favour short "
                 f"({len(auto)} raw checks grouped into {auto_votes} families). {len(manual)} check(s) "
                 f"still need manual input before comparing against the "
                 f"{threshold}/{total} threshold."),
    }


# --------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------


def _client(args):
    creds = load_credentials(getattr(args, "creds_file", None))
    return NobitexClient(creds, verbose=getattr(args, "verbose", False))


def cmd_auth_check(args):
    c = _client(args)
    out = {"credentials": c.creds.describe(), "ed25519_backend": ed.backend_name(),
           "base_url": c.base_url}
    fails = ed.self_test()
    out["ed25519_self_test"] = "pass" if not fails else fails
    try:
        c.market_stats("btc", "rls")
        out["public_api"] = "ok"
    except Exception as e:
        out["public_api"] = str(e)
    if c.creds.mode == "public":
        out["private_api"] = "skipped - no credentials configured"
    else:
        try:
            p = c.profile()
            prof = p.get("profile", {})
            out["private_api"] = "ok"
            out["account"] = {"level": prof.get("level"),
                              "verified": prof.get("verifications"),
                              "email": "***REDACTED***"}
        except Exception as e:
            out["private_api"] = str(e)
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_candles(args):
    c = _client(args)
    rows = c.candles(args.symbol, args.resolution, args.count)
    if not rows:
        sys.exit("No candles returned.")
    if args.csv:
        import csv as _csv
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = _csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"Wrote {len(rows)} candles to {args.csv}")
        return
    ind = compute_indicators(rows)
    print(json.dumps({"symbol": args.symbol,
                      "resolution": RESOLUTIONS.get(args.resolution, args.resolution),
                      "indicators": ind}, indent=2, ensure_ascii=False, default=float))


def cmd_orderbook(args):
    c = _client(args)
    ob = c.orderbook(args.symbol)
    bids = [(float(p), float(q)) for p, q in ob.get("bids", [])][:args.depth]
    asks = [(float(p), float(q)) for p, q in ob.get("asks", [])][:args.depth]
    if not bids or not asks:
        sys.exit("Empty order book.")
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    out = {
        "symbol": args.symbol,
        "best_bid": best_bid, "best_ask": best_ask, "mid": mid,
        "spread_pct": (best_ask - best_bid) / mid * 100,
        "last_trade_price": float(ob["lastTradePrice"]) if ob.get("lastTradePrice") else None,
        f"bid_value_top{args.depth}": sum(p * q for p, q in bids),
        f"ask_value_top{args.depth}": sum(p * q for p, q in asks),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_positions(args):
    c = _client(args)
    data = c.positions()
    positions = data.get("positions", data)
    print(json.dumps({"count": len(positions) if isinstance(positions, list) else None,
                      "positions": positions}, indent=2, ensure_ascii=False))


def cmd_account(args):
    c = _client(args)
    out = {}
    for name, fn in (("profile", c.profile), ("margin_fee_rates", c.margin_fee_rates),
                     ("delegation_limit", lambda: c.delegation_limit(args.symbol)),
                     ("active_positions", c.positions)):
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = {"error": str(e)}
    if isinstance(out.get("profile"), dict):
        prof = out["profile"].get("profile", {})
        for k in ("email", "mobile", "phone", "nationalCode", "firstName", "lastName"):
            if k in prof:
                prof[k] = "***REDACTED***"
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_snapshot(args):
    c = _client(args)
    prof = tp.PROFILES[args.profile]
    roles = {"bias": prof["bias_tf"], "decision": prof["decision_tf"],
             "entry": prof["entry_tf"], "atr": prof["atr_tf"]}

    tfs, seen = {}, {}
    for role, tf in roles.items():
        res = TF_TO_RESOLUTION.get(tf)
        if not res:
            tfs[role] = {"timeframe": tf, "error": f"no Nobitex resolution for {tf}"}
            continue
        cache_key = (res, tf == "1W")
        if cache_key in seen:
            rows = seen[cache_key]
        else:
            rows = c.candles(args.symbol, res, args.count)
            if tf == "1W":
                rows = aggregate_weekly(rows)
            seen[cache_key] = rows
        if not rows:
            tfs[role] = {"timeframe": tf, "resolution": res, "error": "no data"}
            continue
        tfs[role] = {"timeframe": tf, "resolution": res,
                     "indicators": compute_indicators(rows)}
        if args.save_csv:
            import csv as _csv
            path = f"{args.save_csv}_{role}_{res}.csv"
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = _csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)

    snap = {
        "symbol": args.symbol,
        "profile": args.profile,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "nobitex apiv2",
        "timeframes": tfs,
    }

    entry_ind = tfs.get("entry", {}).get("indicators", {})
    atr_ind = tfs.get("atr", {}).get("indicators", {})
    snap["last_price"] = entry_ind.get("last_close") or atr_ind.get("last_close")
    snap["atr_for_stop"] = atr_ind.get("atr14")
    snap["atr_timeframe"] = roles["atr"]
    snap["swing_low"] = tfs.get("decision", {}).get("indicators", {}).get("last_swing_low")
    snap["swing_high"] = tfs.get("decision", {}).get("indicators", {}).get("last_swing_high")

    try:
        ob = c.orderbook(args.symbol)
        bids = [(float(p), float(q)) for p, q in ob.get("bids", [])][:5]
        asks = [(float(p), float(q)) for p, q in ob.get("asks", [])][:5]
        if bids and asks:
            mid = (bids[0][0] + asks[0][0]) / 2
            snap["orderbook"] = {
                "best_bid": bids[0][0], "best_ask": asks[0][0],
                "spread_pct": (asks[0][0] - bids[0][0]) / mid * 100,
                "bid_value_top5": sum(p * q for p, q in bids),
                "ask_value_top5": sum(p * q for p, q in asks)}
    except Exception as e:
        snap["orderbook"] = {"error": str(e)}

    src, dst = split_symbol(args.symbol)
    if src:
        try:
            snap["market_stats"] = c.market_stats(src, dst).get("stats", {})
        except Exception as e:
            snap["market_stats"] = {"error": str(e)}

    if c.creds.mode != "public":
        try:
            snap["delegation_limit"] = c.delegation_limit(args.symbol)
        except Exception as e:
            snap["delegation_limit"] = {"error": str(e)}
        try:
            snap["margin_fee_rates"] = c.margin_fee_rates()
        except Exception as e:
            snap["margin_fee_rates"] = {"error": str(e)}

    snap["direction_score"] = score_direction(args.profile, tfs)

    text = json.dumps(snap, indent=2, ensure_ascii=False, default=float)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        ds = snap["direction_score"]
        print(f"Snapshot written to {args.out}")
        print(f"  symbol {snap['symbol']}  profile {snap['profile']}  "
              f"last {snap['last_price']}  ATR({snap['atr_timeframe']}) "
              f"{snap['atr_for_stop']}")
        print(f"  {ds['note']}")
    else:
        print(text)


def cmd_screen(args):
    """Rank several symbols by whether they are worth trading right now.

    This is the 'which of these deserves my risk budget today' question. It runs the
    same gates and score as a full plan, using a nominal position derived from the
    supplied capital, so results are comparable across symbols.
    """
    c = _client(args)
    prof = tp.PROFILES[args.profile]
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    rows = []

    for sym in symbols:
        entry_res = TF_TO_RESOLUTION[prof["entry_tf"]]
        atr_res = TF_TO_RESOLUTION[prof["atr_tf"]]
        try:
            atr_rows = c.candles(sym, atr_res, args.count)
            if not atr_rows:
                rows.append({"symbol": sym, "verdict": "NO DATA", "score": None})
                continue
            atr_ind = compute_indicators(atr_rows)
            dec_rows = (atr_rows if TF_TO_RESOLUTION[prof["decision_tf"]] == atr_res
                        else c.candles(sym, TF_TO_RESOLUTION[prof["decision_tf"]],
                                       args.count))
            bias_rows = (atr_rows if TF_TO_RESOLUTION[prof["bias_tf"]] == atr_res
                         else c.candles(sym, TF_TO_RESOLUTION[prof["bias_tf"]],
                                        args.count))
            tfs = {"bias": {"indicators": compute_indicators(bias_rows)},
                   "decision": {"indicators": compute_indicators(dec_rows)},
                   "atr": {"indicators": atr_ind},
                   "entry": {"indicators": atr_ind if entry_res == atr_res else
                             compute_indicators(c.candles(sym, entry_res, args.count))}}
            ds = score_direction(args.profile, tfs)

            ob = c.orderbook(sym)
            bids = [(float(p), float(q)) for p, q in ob.get("bids", [])][:5]
            asks = [(float(p), float(q)) for p, q in ob.get("asks", [])][:5]
            mid = (bids[0][0] + asks[0][0]) / 2 if bids and asks else None
            spread = (asks[0][0] - bids[0][0]) / mid * 100 if mid else None

            entry = atr_ind["last_close"]
            a = atr_ind["atr14"]
            stop_distance = prof["atr_mult"] * a
            R = args.risk_pct / 100.0 * args.capital
            notional = (R / stop_distance) * entry

            auto = ds["auto_checks"]
            side = "long" if ds["long_score"] >= ds["short_score"] else "short"
            ratio = (ds[f"{side}_score"] / auto) if auto else None
            book = (sum(p * q for p, q in (asks if side == "long" else bids))
                    if bids and asks else None)

            fee = tp.EXCHANGES["nobitex"]["default_fee_pct"]
            cost = 2 * fee / 100.0 * notional
            cost_r = cost / R
            avg_win = (prof["tp1_r"] + prof["tp2_r"]) / 2
            wr = prof["default_win_rate"]
            e_net = wr * avg_win - (1 - wr) - cost_r

            q = tp.qualify(prof, atr_pct=atr_ind["atr_pct"], spread_pct=spread,
                           book_value=book, notional=notional,
                           direction_ratio=ratio, expectancy_net=e_net,
                           cost_in_r=cost_r, liq_buffer_ratio=None)
            rows.append({
                "symbol": sym, "side": side, "verdict": q["verdict"],
                "score": q["score"], "coverage": q["score_coverage"],
                "last": entry, "atr_pct": round(atr_ind["atr_pct"], 2),
                "spread_pct": round(spread, 3) if spread else None,
                "structure": tfs["decision"]["indicators"]["structure"],
                "rsi": round(tfs["decision"]["indicators"]["rsi14"], 1)
                       if tfs["decision"]["indicators"]["rsi14"] else None,
                "direction": f"{ds[side + '_score']}/{auto}",
                "cost_in_R": round(cost_r, 3),
                "gates_failed": q["gates_failed"],
            })
        except Exception as e:
            rows.append({"symbol": sym, "verdict": "ERROR", "score": None,
                         "error": str(e)[:160]})

    order = {"TAKE": 0, "WATCH": 1, "INCOMPLETE": 2, "SKIP": 3, "NO DATA": 4,
             "ERROR": 5}
    rows.sort(key=lambda r: (order.get(r["verdict"], 9), -(r["score"] or 0)))

    if args.json:
        print(json.dumps({"profile": args.profile, "results": rows}, indent=2,
                         ensure_ascii=False, default=float))
        return

    print(f"\n Screening {len(symbols)} symbols - profile {args.profile}, "
          f"risk {args.risk_pct}% of {args.capital:,.0f}\n")
    print(f" {'symbol':<10} {'verdict':<11} {'score':>6} {'side':<6} {'dir':<6} "
          f"{'ATR%':>6} {'spread%':>8} {'cost R':>7}  structure")
    print(" " + "-" * 84)
    for r in rows:
        if r["verdict"] in ("ERROR", "NO DATA"):
            print(f" {r['symbol']:<10} {r['verdict']:<11} {r.get('error', '')[:50]}")
            continue
        print(f" {r['symbol']:<10} {r['verdict']:<11} {r['score']:>6} "
              f"{r['side']:<6} {r['direction']:<6} {r['atr_pct']:>6} "
              f"{str(r['spread_pct']):>8} {r['cost_in_R']:>7}  {r['structure']}")
        if r["gates_failed"]:
            print(f" {'':<10} failed: {', '.join(r['gates_failed'])}")
    print("\n TAKE candidates deserve a full plan:")
    print("   python3 nobitex_api.py snapshot --symbol <SYM> --profile "
          f"{args.profile} --out snap.json")
    print("   python3 trade_plan.py plan --snapshot snap.json --side <side> "
          f"--capital {args.capital:.0f}")
    print("\n Screening never settles BTC alignment or funding - confirm those "
          "manually before executing.\n")


# --------------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(
        description="Read-only Nobitex client for the trade-plan skill",
        epilog="Credentials come from NOBITEX_API_KEY/NOBITEX_API_SECRET, "
               "NOBITEX_TOKEN, or --creds-file. Never pass them as arguments.")
    p.add_argument("--creds-file", help="JSON file (chmod 600) with key/secret/token")
    p.add_argument("--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("auth-check", help="Verify credentials and connectivity")
    s.set_defaults(func=cmd_auth_check)

    s = sub.add_parser("candles", help="Fetch OHLCV and compute indicators")
    s.add_argument("--symbol", required=True, help="e.g. BTCIRT, ETHUSDT")
    s.add_argument("--resolution", default="240", choices=list(RESOLUTIONS))
    s.add_argument("--count", type=int, default=300, help="max 500 per request")
    s.add_argument("--csv", help="Write candles to this CSV instead of printing")
    s.set_defaults(func=cmd_candles)

    s = sub.add_parser("orderbook", help="Spread and top-of-book depth")
    s.add_argument("--symbol", required=True)
    s.add_argument("--depth", type=int, default=5)
    s.set_defaults(func=cmd_orderbook)

    s = sub.add_parser("positions", help="List open margin positions (read-only)")
    s.set_defaults(func=cmd_positions)

    s = sub.add_parser("account", help="Profile, margin fees, delegation limit")
    s.add_argument("--symbol")
    s.set_defaults(func=cmd_account)

    s = sub.add_parser("screen", help="Rank symbols by whether they are worth trading")
    s.add_argument("--symbols", required=True,
                   help="Comma-separated, e.g. BTCIRT,ETHIRT,SOLUSDT")
    s.add_argument("--profile", choices=list(tp.PROFILES), default="intraday")
    s.add_argument("--capital", type=float, required=True)
    s.add_argument("--risk-pct", type=float, default=1.0)
    s.add_argument("--count", type=int, default=300)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_screen)

    s = sub.add_parser("snapshot", help="Full multi-timeframe snapshot for a plan")
    s.add_argument("--symbol", required=True)
    s.add_argument("--profile", choices=list(tp.PROFILES), default="intraday")
    s.add_argument("--count", type=int, default=300)
    s.add_argument("--out", help="Write JSON here (recommended)")
    s.add_argument("--save-csv", help="Prefix for per-timeframe CSV dumps")
    s.set_defaults(func=cmd_snapshot)

    args = p.parse_args()
    try:
        args.func(args)
    except ReadOnlyViolation as e:
        sys.exit(f"BLOCKED: {e}")
    except RuntimeError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
