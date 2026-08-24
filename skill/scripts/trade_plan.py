#!/usr/bin/env python3
"""
trade_plan.py - deterministic trade-plan calculator for leveraged crypto positions.

Two subcommands:

  indicators   Compute ATR/EMA/RSI/RVOL/VWAP/swings from an OHLCV CSV.
  plan         Derive stop, size, leverage, targets, costs and expectancy.

Design note: the arithmetic here is intentionally boring and explicit. The point of
putting it in a script rather than doing it inline is that sizing errors are silent,
expensive, and easy to make when reasoning in prose. Standard library only.

Examples
--------
  python3 trade_plan.py indicators --csv eth_4h.csv
  python3 trade_plan.py plan --profile intraday --side long --entry 3000 \
      --atr 45 --capital 10000 --exchange nobitex --hold-hours 24
"""

import argparse
import csv
import json
import math
import sys
from datetime import datetime, timezone

# --------------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------------

PROFILES = {
    "scalp": {
        # Retimed 2026-08-22 for a 5-20 MINUTE hold.
        #
        # The decision timeframe was 15m, so a trade held 20 minutes acted on barely
        # one fresh bar — the signal could not update inside the life of the position.
        # Decision moves to 5m (four updates per hold) and entry to 1m.
        #
        # ATR deliberately STAYS on 15m. It sets the stop, and a 5m ATR would shrink
        # the stop by roughly 40%, which raises cost_in_R by the same proportion
        # (cost = 2 x fee / stop_pct). At 0.1% a side that is the difference between
        # ~0.13R and ~0.23R of fees per trade. A faster signal is worth having; a
        # tighter stop is not, on this venue.
        "label": "Scalp (5-20m hold)",
        "bias_tf": "1H", "decision_tf": "5m", "entry_tf": "1m", "atr_tf": "15m",
        "atr_mult": 1.5,
        "stop_pct_min": 0.0, "stop_pct_max": 1.5,
        "tp1_r": 1.0, "tp2_r": 2.0,
        "liq_buffer": 3.0,
        "cost_filter": 4.0,
        "default_win_rate": 0.50,
        # 4 x 5m decision candles = the 20-minute ceiling of the intended hold.
        "time_stop_candles": 4,
        # tradability gates
        "atr_pct_min": 0.3, "atr_pct_max": 1.5,
        "max_spread_pct": 0.10, "liquidity_multiple": 3.0,
        # --- target-reachability gates (2026-08-24, Round 19) ---------------------
        #
        # TP1 sits at 1R, so the stop distance IS the target distance. Measured over
        # 28,812 gated entries on real 5m candles, walked forward to the touch:
        #
        #   stop > 2.25%   n=2,718   TP-in-1h 12.3%   meanR 1h -0.0973  2h -0.1500
        #   stop < 1.00%   n=6,641   TP-in-1h 28.2%   meanR 1h -0.1039  2h -0.0248
        #   1H ATR > 2.25% n=8,371   TP-in-1h 18.1%   meanR 1h -0.0929  2h -0.0641
        #
        # Both tails are negative in BOTH halves of the sample, so this is avoiding a
        # measured harm rather than chasing a fitted gain. Keeping only the middle:
        # 47.4% of entries, TP-in-1h 30.5%, meanR 1h +0.0058 (baseline -0.0481) and
        # 2h +0.0562 (baseline +0.0026), positive in both halves.
        #
        # The wide tail is where a target cannot be reached inside the intended hold
        # and only the stop can be; the tight tail is where the 0.2% round trip eats
        # the trade (cost_in_R = 2 x fee / stop_pct, so 0.8-1.0% costs 0.20-0.25R).
        # Live corroboration on 14 closed trades: blocks 4 of 8 stop-outs (both ZECs,
        # AAVE, XRP; -0.674 USDT of realised loss) and 0 of 6 target hits.
        "gate_stop_pct_min": 1.0, "gate_stop_pct_max": 2.25,
        "gate_bias_atr_max": 2.25,
    },
    "intraday": {
        "label": "Intraday (1-4H)",
        "bias_tf": "1D", "decision_tf": "4H", "entry_tf": "1H", "atr_tf": "4H",
        "atr_mult": 2.0,
        "stop_pct_min": 2.0, "stop_pct_max": 5.0,
        "tp1_r": 1.5, "tp2_r": 3.0,
        "liq_buffer": 4.0,
        "cost_filter": 5.0,
        "default_win_rate": 0.40,
        "time_stop_candles": 12,
        "atr_pct_min": 1.0, "atr_pct_max": 6.0,
        "max_spread_pct": 0.30, "liquidity_multiple": 2.0,
    },
    "swing": {
        "label": "Swing (1D+)",
        "bias_tf": "1W", "decision_tf": "1D", "entry_tf": "4H", "atr_tf": "1D",
        "atr_mult": 2.5,
        "stop_pct_min": 5.0, "stop_pct_max": 12.0,
        "tp1_r": 2.0, "tp2_r": 4.0,
        "liq_buffer": 5.0,
        "cost_filter": 5.0,
        "default_win_rate": 0.38,
        "time_stop_candles": 15,
        "atr_pct_min": 2.0, "atr_pct_max": 15.0,
        "max_spread_pct": 0.50, "liquidity_multiple": 1.5,
    },
}

# --------------------------------------------------------------------------------
# Exchange profiles
#
# holding_cost_pct is charged per funding/renewal period, expressed as a percentage
# of notional. These are conservative placeholders - confirm against the venue.
# --------------------------------------------------------------------------------

EXCHANGES = {
    "nobitex": {
        "label": "Nobitex - معاملات تعهدی",
        "leverage_cap": 5.0,
        "leverage_steps": [1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5],
        "level_caps": {1: 2.0, 2: 5.0, 3: 5.0},
        "funding_period_hours": 8,
        "max_hold_days": 30,
        "default_fee_pct": 0.15,          # per side, tier dependent
        "holding_cost_pct": 0.02,         # per 8h renewal, on notional (assumption)
        "maintenance_margin_pct": 9.0,    # approximates the ~1.1 نسبت تعهد trigger
        "holding_cost_can_be_credit": False,
        "notes": [
            "Delegated margin backed by a participation pool, not a true perpetual.",
            "Renewal fee is deducted from collateral every 8h; max hold 30 days.",
            "Liquidation triggers near نسبت تعهد 1.1 - read the platform's own "
            "liquidation price before confirming.",
            "Stop-loss and OCO orders are supported on both buy and sell positions.",
            "Pool-backed depth means more slippage than global venues.",
        ],
    },
    "toobit": {
        "label": "Toobit - USDT-M perpetuals",
        # Per-contract, from exchangeInfo riskLimits: 75x on FIL tier 0, lower on
        # thinner books, and it falls as position size climbs the tiers. This is the
        # common tier-0 ceiling; pass --leverage-cap with the contract's own figure
        # when it is known.
        "leverage_cap": 50.0,
        "leverage_steps": None,
        "level_caps": {},
        "funding_period_hours": 8,
        "max_hold_days": None,
        # Published VIP-0 futures rates: maker 0.0200%, taker 0.0600%. Taker is the
        # honest default because a market entry pays it.
        "default_fee_pct": 0.06,
        # Funding is read live per contract rather than assumed; this is only the
        # fallback when no rate is available.
        "holding_cost_pct": 0.01,
        # Also per contract and per tier - 0.25% on BTC, 0.667% on FIL, 2.5% on CRO.
        # The generic 0.5% assumption puts liquidation further away than it really is
        # on anything thinner than BTC, so pass the contract's own maintMargin when
        # it is known.
        "maintenance_margin_pct": 0.667,
        "holding_cost_can_be_credit": True,
        "notes": [
            "Contracts are not coins: contractMultiplier is 0.001 on BTC, 10 on CRO, "
            "and symbols like 1000SHIB carry the scale in the name. Size in contracts "
            "when typing an order ticket.",
            "Leverage and maintenance margin come from the contract's own riskLimits "
            "ladder and tighten as notional grows.",
            "Funding is public and settles every 8h, capped at +/-2%.",
            "Minimum lot sizes are large on cheap coins - a small account can be "
            "unable to take a risk-appropriate position at all.",
            "Assumes isolated margin.",
        ],
    },
    "tabdeal": {
        "label": "Tabdeal - اهرم حرفه‌ای (Professional Leverage)",
        # Selectable 1..100 per the product page. Unlike Toobit there is no per-tier
        # ladder to climb, so this ceiling does not tighten as the position grows.
        "leverage_cap": 100.0,
        "leverage_steps": None,
        "level_caps": {},
        "funding_period_hours": 8,
        "max_hold_days": None,
        # tabdeal.org/commissions: 0.001 taker AND 0.001 maker = 0.1% a side, with no
        # maker discount at all. That is 2.5x Toobit's effective round trip, and it is
        # flagged there as a temporary promotional rate, so treat it as a floor.
        "default_fee_pct": 0.1,
        # No funding rate is published for this product and the product page never
        # mentions one. This is the generic placeholder, kept rather than zeroed:
        # "unverified" must not be recorded as "free to hold".
        "holding_cost_pct": 0.01,
        # Flat 0.5% of position value for every symbol - «مارجین نگهداری ... ۰.۵ درصد
        # از ارزش کل پوزیشن». No tier ladder, so unlike Toobit this figure is exact
        # rather than a per-contract approximation.
        "maintenance_margin_pct": 0.5,
        "holding_cost_can_be_credit": True,
        "notes": [
            "CROSS margin, not isolated: the whole wallet backs every position, so "
            "the per-position liquidation buffer below is indicative only. Tabdeal "
            "states one losing position can liquidate the entire account, closing "
            "profitable positions with it.",
            "Maintenance margin is a flat 0.5% of position value on every symbol.",
            "Fees are 0.1% on both maker and taker - there is no maker discount, so "
            "resting a limit order saves nothing, and a high-frequency strategy pays "
            "this on every leg.",
            "No funding rate is published for this product; holding cost here is an "
            "unverified placeholder, not a venue figure.",
            "Order quantity is in coins - there is no contract multiplier.",
            "No weekly candles are available, so the swing profile cannot be planned "
            "on this venue.",
        ],
    },
    "generic-perp": {
        "label": "Generic perpetual futures",
        "leverage_cap": 20.0,
        "leverage_steps": None,
        "level_caps": {},
        "funding_period_hours": 8,
        "max_hold_days": None,
        "default_fee_pct": 0.05,
        "holding_cost_pct": 0.01,
        "maintenance_margin_pct": 0.5,
        "holding_cost_can_be_credit": True,
        "notes": [
            "Funding can be negative (a credit) - confirm the current rate.",
            "Leverage cap here is a risk guardrail, not a platform limit.",
            "Assumes isolated margin. Cross margin invalidates the liquidation "
            "buffer check entirely.",
        ],
    },
}

# --------------------------------------------------------------------------------
# Indicator maths
# --------------------------------------------------------------------------------


def ema(values, period):
    """Exponential moving average, seeded with an SMA of the first `period` values."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    out = sum(values[:period]) / period
    for v in values[period:]:
        out = v * k + out * (1.0 - k)
    return out


def rsi(closes, period=14):
    """Wilder-smoothed RSI."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(highs, lows, closes, period=14):
    """Wilder-smoothed Average True Range."""
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    val = sum(trs[:period]) / period
    for tr in trs[period:]:
        val = (val * (period - 1) + tr) / period
    return val


def rvol(volumes, period=20):
    if len(volumes) < period + 1:
        return None
    avg = sum(volumes[-period - 1:-1]) / period
    if avg == 0:
        return None
    return volumes[-1] / avg


def find_swings(highs, lows, left=2, right=2):
    """Fractal swings. A swing is only confirmed `right` candles after it forms."""
    sw_highs, sw_lows = [], []
    for i in range(left, len(highs) - right):
        window_h = highs[i - left:i + right + 1]
        window_l = lows[i - left:i + right + 1]
        if highs[i] == max(window_h) and window_h.count(highs[i]) == 1:
            sw_highs.append((i, highs[i]))
        if lows[i] == min(window_l) and window_l.count(lows[i]) == 1:
            sw_lows.append((i, lows[i]))
    return sw_highs, sw_lows


def _management(prof, ex, args):
    """How the position is actually managed once it is open.

    Two sets of rules exist and only one of them runs. The generic ones below —
    a 50% partial at TP1, a stop moved up behind it, a time stop after N decision
    candles — describe a venue that supports a partial close. **Tabdeal does not.**
    It has no `reduceOnly` and no partial close, so a half-exit would have to be an
    opposing MARKET order that can FLIP the position rather than trim it, and the
    live engine therefore closes TP1 outright and carries no time stop at all.

    Emitting the generic rules on a Tabdeal plan is not a documentation nicety: the
    plan is what a human reads before entering, and it was telling them to bank half
    at TP1 and bail after four candles when the engine does neither. This project has
    already had SKILL.md drift the same way (Round 12). Keep this in step with
    `agent/live.py`.
    """
    if (args.exchange or "").lower() == "tabdeal":
        return {
            "on_tp1": "FULL close at TP1 — the exchange holds it as a take-profit on "
                      "the position itself. Tabdeal supports neither reduceOnly nor a "
                      "partial close, so there is no TP2 and no runner.",
            "stop": "Held by the exchange on the position, so it survives the engine "
                    "process dying. Never moved by the engine.",
            "engine_exit": "ONE exit only: held >= 1h AND profitable at the EXIT-SIDE "
                           "price by more than 1.5x the round trip AND the scan no "
                           "longer calls it TAKE at >= 70. A position in loss is never "
                           "touched at any hold.",
            "time_stop": "None. A time stop fired on positions in profit but below "
                         "the round trip, which books a certain loss.",
            "review_cadence": f"Re-scored every scan ({prof['decision_tf']} decision "
                              f"candles); the engine re-reads the verdict every cycle.",
        }
    return {
        "on_tp1": "Close 50%, move stop to breakeven + accumulated cost",
        "trail": f"Behind new swing points on the {prof['decision_tf']} chart",
        "time_stop": f"Exit after ~{prof['time_stop_candles']} {prof['decision_tf']} "
                     f"candles if price has not reached 0.5R",
        "review_cadence": f"Re-evaluate before every {ex['funding_period_hours']}h "
                          f"renewal/funding charge",
    }


def session_vwap(rows):
    """VWAP for the most recent UTC day present in the data.

    Returns None when timestamps can't be parsed - a VWAP that doesn't reset at the
    session boundary is not VWAP, so it's better to return nothing than a wrong number.
    """
    def parse(ts):
        ts = str(ts).strip()
        try:
            n = float(ts)
            if n > 1e11:
                n /= 1000.0
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(ts.replace("+00:00", ""), fmt)
            except ValueError:
                continue
        return None

    parsed = [(parse(r["timestamp"]), r) for r in rows if r.get("timestamp")]
    parsed = [(d, r) for d, r in parsed if d is not None]
    if not parsed:
        return None
    last_day = parsed[-1][0].date()
    num = den = 0.0
    for d, r in parsed:
        if d.date() != last_day:
            continue
        tp = (r["high"] + r["low"] + r["close"]) / 3.0
        num += tp * r["volume"]
        den += r["volume"]
    return num / den if den else None


def read_ohlcv(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for raw in csv.DictReader(fh):
            keys = {k.strip().lower(): v for k, v in raw.items() if k}
            try:
                rows.append({
                    "timestamp": keys.get("timestamp") or keys.get("time") or keys.get("date"),
                    "open": float(keys["open"]),
                    "high": float(keys["high"]),
                    "low": float(keys["low"]),
                    "close": float(keys["close"]),
                    "volume": float(keys.get("volume") or 0.0),
                })
            except (KeyError, TypeError, ValueError):
                continue
    if not rows:
        sys.exit("No usable rows. Expected columns: timestamp,open,high,low,close,volume")
    return rows


def cmd_indicators(args):
    rows = read_ohlcv(args.csv)
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    closes = [r["close"] for r in rows]
    vols = [r["volume"] for r in rows]

    sw_h, sw_l = find_swings(highs, lows)
    a = atr(highs, lows, closes, args.atr_period)
    last = closes[-1]

    result = {
        "candles": len(rows),
        "last_close": round(last, 8),
        "atr": round(a, 8) if a else None,
        "atr_pct_of_price": round(a / last * 100, 3) if a else None,
        "ema20": round(ema(closes, 20), 8) if ema(closes, 20) else None,
        "ema50": round(ema(closes, 50), 8) if ema(closes, 50) else None,
        "ema200": round(ema(closes, 200), 8) if ema(closes, 200) else None,
        "rsi14": round(rsi(closes, 14), 2) if rsi(closes, 14) else None,
        "rvol20": round(rvol(vols, 20), 2) if rvol(vols, 20) else None,
        "session_vwap": None,
        "last_swing_high": round(sw_h[-1][1], 8) if sw_h else None,
        "last_swing_low": round(sw_l[-1][1], 8) if sw_l else None,
    }
    v = session_vwap(rows)
    if v:
        result["session_vwap"] = round(v, 8)

    # Derived read-outs so the caller doesn't have to eyeball comparisons.
    e20, e50, e200 = result["ema20"], result["ema50"], result["ema200"]
    result["structure_notes"] = {
        "above_ema200": (last > e200) if e200 else None,
        "above_ema50": (last > e50) if e50 else None,
        "above_ema20": (last > e20) if e20 else None,
        "perfect_order_bull": (e20 > e50 > e200) if (e20 and e50 and e200) else None,
        "perfect_order_bear": (e20 < e50 < e200) if (e20 and e50 and e200) else None,
        "above_vwap": (last > result["session_vwap"]) if result["session_vwap"] else None,
    }

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    print(f"\nIndicators  ({result['candles']} candles)")
    print("-" * 52)
    for k in ("last_close", "atr", "atr_pct_of_price", "ema20", "ema50", "ema200",
              "rsi14", "rvol20", "session_vwap", "last_swing_high", "last_swing_low"):
        print(f"  {k:<20} {result[k] if result[k] is not None else '-'}")
    print("\nStructure")
    print("-" * 52)
    for k, v2 in result["structure_notes"].items():
        mark = "-" if v2 is None else ("yes" if v2 else "no")
        print(f"  {k:<20} {mark}")
    if result["session_vwap"] is None:
        print("\n  note: VWAP skipped (timestamps unparseable or absent).")
    print()


# --------------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------------


def snap_leverage(value, steps, cap):
    """Smallest allowed leverage >= value, respecting the cap.

    On a stepless venue this rounds *up*, not to nearest. `round()` was used here and
    it rounds down half the time, which returns a leverage below the one asked for
    and then fails the caller's own `leverage < needed` check. It affected every
    generic-perp plan; roughly half of a 47-coin Toobit pass tripped it.

    Rounding up can exceed the cap by less than a cent of leverage, so the result is
    clamped afterwards rather than before.
    """
    value = max(value, 1.0)
    if steps:
        allowed = [s for s in steps if s <= cap + 1e-9]
        for s in allowed:
            if s >= value - 1e-9:
                return float(s)
        return float(allowed[-1]) if allowed else None
    if value > cap:
        return None
    snapped = math.ceil(value * 100 - 1e-9) / 100
    return round(min(max(snapped, 1.0), cap), 2)


# --------------------------------------------------------------------------------
# Trade-worthiness qualification
#
# The plan answers "how would I trade this". This answers the prior question:
# "is this instrument, right now, worth risking anything on at all?"
#
# Two layers, deliberately separated:
#   GATES  - properties of the instrument. Any failure means skip, because no
#            entry price fixes an illiquid book or a dead-flat chart.
#   SCORE  - graded quality of the opportunity, for ranking candidates against
#            each other once they have cleared the gates.
#
# The weights below are a considered heuristic, not an empirically fitted model.
# They encode a priority order - setup quality first, then whether the economics
# survive costs - and should be treated as a consistent ranking device rather than
# a probability of profit.
# --------------------------------------------------------------------------------


def qualify(prof, *, atr_pct=None, spread_pct=None, book_value=None, notional=None,
            direction_ratio=None, expectancy_net=None, cost_in_r=None,
            liq_buffer_ratio=None, rr_tp2=None, blockers=None, market_closed=None,
            stop_pct=None, bias_atr_pct=None):
    """Return a TAKE / WATCH / SKIP verdict with the reasoning that produced it."""
    gates, score_parts = [], []

    def gate(name, passed, detail):
        gates.append({"gate": name, "passed": passed, "detail": detail})

    # --- gates ------------------------------------------------------------------
    if market_closed:
        gate("market open", False, "market is closed")
    if atr_pct is not None:
        lo, hi = prof["atr_pct_min"], prof["atr_pct_max"]
        ok = lo <= atr_pct <= hi
        why = ("too quiet - moves will not cover costs" if atr_pct < lo
               else "too volatile - stops get hit at random" if atr_pct > hi
               else "in range")
        gate("volatility fit", ok, f"ATR {atr_pct:.2f}% vs band {lo}-{hi}% ({why})")
    if spread_pct is not None:
        ok = spread_pct <= prof["max_spread_pct"]
        gate("spread", ok,
             f"{spread_pct:.3f}% vs max {prof['max_spread_pct']}%")
    if book_value is not None and notional:
        mult = book_value / notional if notional else 0
        ok = mult >= prof["liquidity_multiple"]
        gate("liquidity depth", ok,
             f"top-of-book {mult:.1f}x position size "
             f"(need {prof['liquidity_multiple']}x)")
    if liq_buffer_ratio is not None:
        ok = liq_buffer_ratio >= prof["liq_buffer"]
        gate("liquidation buffer", ok,
             f"{liq_buffer_ratio:.1f}x stop (need {prof['liq_buffer']}x)")
    if cost_in_r is not None:
        ok = cost_in_r <= 1.0 / prof["cost_filter"]
        gate("cost efficiency", ok,
             f"costs are {cost_in_r:.2f}R (max {1.0 / prof['cost_filter']:.2f}R)")

    # Target reachability. TP1 is 1R, so the stop distance is also how far price must
    # travel to win — and the existing cost gate only bounds it from ONE side, which
    # is why a 2.876% stop (ZEC, 2026-08-23) passed every gate at 0.08R of cost and
    # then took a full -1.32R. See the profile block for the measurement.
    lo, hi = prof.get("gate_stop_pct_min"), prof.get("gate_stop_pct_max")
    if stop_pct is not None and lo is not None and hi is not None:
        ok = lo <= stop_pct <= hi
        why = ("target is unreachable in the intended hold; only the stop is"
               if stop_pct > hi else
               "the round trip eats too much of R" if stop_pct < lo else "in range")
        gate("stop reachability", ok,
             f"stop {stop_pct:.2f}% vs band {lo}-{hi}% ({why})")

    # Regime volatility, read on the BIAS timeframe rather than the ATR timeframe.
    # The existing "volatility fit" gate reads the 15m ATR that sets the stop; this
    # one asks whether the instrument's whole hourly regime is orderly enough for a
    # level to mean anything. They disagree often: ZEC passed volatility fit at 1.24%
    # while its 1H ATR was 2.58%.
    cap = prof.get("gate_bias_atr_max")
    if bias_atr_pct is not None and cap is not None:
        gate("regime volatility", bias_atr_pct <= cap,
             f"{prof['bias_tf']} ATR {bias_atr_pct:.2f}% vs max {cap}%")
    for b in (blockers or []):
        gate("plan blocker", False, b)

    failed = [g for g in gates if not g["passed"]]

    # --- score ------------------------------------------------------------------
    def part(name, weight, value, detail):
        pts = max(0.0, min(1.0, value)) * weight
        score_parts.append({"factor": name, "weight": weight,
                            "points": round(pts, 1), "detail": detail})
        return pts

    total = 0.0
    if direction_ratio is not None:
        total += part("setup quality", 35, direction_ratio,
                      f"{direction_ratio:.0%} of automated direction checks agree")
    if expectancy_net is not None:
        total += part("net expectancy", 25, expectancy_net / 0.30,
                      f"{expectancy_net:+.3f}R per trade after costs "
                      f"(+0.30R scores full marks)")
    if cost_in_r is not None:
        total += part("cost drag", 15, 1.0 - cost_in_r / 0.25,
                      f"costs consume {cost_in_r:.1%} of R (under 0.05R is excellent)")
    if book_value is not None and notional:
        mult = book_value / notional
        total += part("liquidity headroom", 15, mult / (prof["liquidity_multiple"] * 3),
                      f"book is {mult:.1f}x position size")
    if atr_pct is not None:
        lo, hi = prof["atr_pct_min"], prof["atr_pct_max"]
        mid = (lo + hi) / 2
        centred = 1.0 - abs(atr_pct - mid) / ((hi - lo) / 2) if hi > lo else 0
        total += part("volatility centring", 10, centred,
                      f"ATR {atr_pct:.2f}% vs band centre {mid:.2f}%")

    max_points = sum(p["weight"] for p in score_parts)
    normalised = (total / max_points * 100) if max_points else 0.0
    coverage = max_points / 100.0

    missing = []
    if direction_ratio is None:
        missing.append("setup quality (run nobitex_api.py snapshot, or score the "
                       "direction checks manually)")
    if book_value is None:
        missing.append("order-book liquidity (needs a live snapshot)")

    # --- verdict ----------------------------------------------------------------
    # Scoring only the factors we measured then grading out of that subtotal would
    # quietly reward missing data. A gate failure is still decisive, but a high
    # score built on half the inputs is reported as INCOMPLETE rather than TAKE.
    if failed:
        verdict, action = "SKIP", "Gate failure - do not open a position."
    elif coverage < 0.8:
        verdict = "INCOMPLETE"
        action = ("Not enough live data to judge this properly. Missing: "
                  + "; ".join(missing) + ". The partial score below covers only "
                  f"{coverage:.0%} of the factors - treat it as provisional.")
    elif normalised >= 70:
        verdict, action = "TAKE", "Qualifies. Execute the plan as written."
    elif normalised >= 50:
        verdict, action = "WATCH", ("Marginal. Put it on the watchlist, write down "
                                    "the single condition that would upgrade it, "
                                    "and wait for that instead of forcing an entry.")
    else:
        verdict, action = "SKIP", "Score too low - the edge is not there today."

    return {
        "verdict": verdict,
        "score": round(normalised, 1),
        "score_coverage": round(coverage, 2),
        "missing_factors": missing,
        "action": action,
        "gates": gates,
        "gates_failed": [g["gate"] for g in failed],
        "score_breakdown": score_parts,
        "scored_out_of": max_points,
        "caveat": "Weights are a heuristic ranking device, not a fitted model or a "
                  "probability of profit. Use the score to compare candidates, not "
                  "to size conviction.",
    }


def apply_snapshot(args):
    """Fill entry / ATR / swings from a nobitex_api.py snapshot.

    Explicit flags always win: the snapshot is a convenience, not an override. A
    trader who typed --entry meant that entry, even if the market has since moved.
    """
    with open(args.snapshot, encoding="utf-8") as fh:
        snap = json.load(fh)
    used = []
    if snap.get("profile") and args.profile == "intraday" and "--profile" not in sys.argv:
        args.profile = snap["profile"]
        used.append(f"profile={args.profile}")
    if args.entry is None and snap.get("last_price"):
        args.entry = float(snap["last_price"])
        used.append(f"entry={args.entry}")
    if args.atr is None and snap.get("atr_for_stop"):
        args.atr = float(snap["atr_for_stop"])
        used.append(f"atr={args.atr} ({snap.get('atr_timeframe')})")
    if args.swing_low is None and snap.get("swing_low"):
        args.swing_low = float(snap["swing_low"])
        used.append(f"swing_low={args.swing_low}")
    if args.swing_high is None and snap.get("swing_high"):
        args.swing_high = float(snap["swing_high"])
        used.append(f"swing_high={args.swing_high}")
    if args.entry is None:
        sys.exit("Snapshot has no usable price; pass --entry explicitly.")
    return snap, used


def cmd_plan(args):
    snapshot, snapshot_used = (None, [])
    if args.snapshot:
        snapshot, snapshot_used = apply_snapshot(args)
    if args.entry is None:
        sys.exit("--entry is required (or supply --snapshot).")

    prof = PROFILES[args.profile]
    ex = EXCHANGES[args.exchange]
    warnings, blockers = [], []

    if snapshot:
        ds = snapshot.get("direction_score", {})
        auto = ds.get("auto_checks") or 0
        side_score = ds.get(f"{args.side.lower()}_score")
        if auto and side_score is not None:
            if side_score < auto * 0.7:
                blockers.append(
                    f"Direction score: only {side_score}/{auto} automated checks "
                    f"favour {args.side}. The setup does not qualify - no trade.")
            elif ds.get("manual_checks"):
                warnings.append(
                    f"{side_score}/{auto} automated checks favour {args.side}, but "
                    f"{ds['manual_checks']} check(s) still need manual confirmation "
                    f"(BTC alignment, funding) before the {ds.get('threshold')}/"
                    f"{ds.get('total_checks')} threshold is settled.")
        ob = snapshot.get("orderbook") or {}
        if isinstance(ob, dict) and ob.get("spread_pct", 0) > 0.1 and args.profile == "scalp":
            warnings.append(
                f"Order-book spread is {ob['spread_pct']:.3f}% - above the 0.1% "
                f"scalp threshold. Slippage will eat a meaningful share of R.")

    side = args.side.lower()
    sign = 1.0 if side == "long" else -1.0
    entry = args.entry

    # --- stop -------------------------------------------------------------------
    stop_source = None
    if args.stop is not None:
        stop = args.stop
        stop_source = "user-specified"
        if (side == "long" and stop >= entry) or (side == "short" and stop <= entry):
            sys.exit("Stop is on the wrong side of entry for a %s position." % side)
    elif args.atr is not None:
        atr_mult = args.atr_mult if getattr(args, "atr_mult", None) else prof["atr_mult"]
        dist = atr_mult * args.atr
        stop = entry - sign * dist
        stop_source = f"{atr_mult:g} x ATR"
        # Widen behind structure if a swing was supplied and sits further out.
        pad = 0.25 * args.atr
        struct = None
        if side == "long" and args.swing_low is not None:
            struct = args.swing_low - pad
        elif side == "short" and args.swing_high is not None:
            struct = args.swing_high + pad
        if struct is not None and abs(entry - struct) > abs(entry - stop):
            # Cap how far structure may widen the stop beyond the ATR stop.
            #
            # Every R-multiple derives from the stop distance, so an uncapped
            # structural stop silently pushes the target out of reach: a swing three
            # ATRs away turns a 1.5R TP1 into a 4.5-ATR move, which the instrument may
            # never travel in the time the plan allows. Observed live on Toobit — a
            # 14.8% structural stop against a 2.96% ATR, with a target nothing could
            # reach and every trade exiting on a clock instead of a level.
            max_mult = getattr(args, "max_struct_mult", None) or 1.5
            limit = max_mult * dist
            if abs(entry - struct) > limit:
                stop = entry - sign * limit
                stop_source = (f"structural, capped at {max_mult:g} x the "
                               f"{atr_mult:g} x ATR stop")
                warnings.append(
                    f"Swing structure sits {abs(entry - struct) / dist:.1f} x the ATR "
                    f"stop away; capped at {max_mult:g} x so the target stays "
                    f"reachable. Consider a higher-timeframe profile instead.")
            else:
                stop = struct
                stop_source = "structural (behind swing + 0.25 ATR)"
    else:
        sys.exit("Provide --atr (preferred) or --stop.")

    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        sys.exit("Stop distance is zero.")
    stop_pct = stop_distance / entry * 100.0

    if stop_pct < prof["stop_pct_min"]:
        warnings.append(
            f"Stop is {stop_pct:.2f}% - tighter than the {prof['label']} floor of "
            f"{prof['stop_pct_min']}%. Expect noise stop-outs; consider a lower timeframe profile.")
    if stop_pct > prof["stop_pct_max"]:
        warnings.append(
            f"Stop is {stop_pct:.2f}% - wider than the {prof['label']} ceiling of "
            f"{prof['stop_pct_max']}%. Volatility is elevated; halve size or stand aside.")

    # --- size -------------------------------------------------------------------
    R = args.risk_pct / 100.0 * args.capital
    quantity = R / stop_distance
    notional = quantity * entry

    # --- leverage ---------------------------------------------------------------
    max_safe = 100.0 / (stop_pct * prof["liq_buffer"])
    caps = [ex["leverage_cap"], max_safe]
    cap_reasons = {"exchange": ex["leverage_cap"], "safety": round(max_safe, 2)}
    if args.account_level and args.account_level in ex["level_caps"]:
        caps.append(ex["level_caps"][args.account_level])
        cap_reasons["account_level"] = ex["level_caps"][args.account_level]
    if args.leverage_cap:
        caps.append(args.leverage_cap)
        cap_reasons["user"] = args.leverage_cap
    effective_cap = min(caps)

    needed = notional / (args.max_margin_pct / 100.0 * args.capital)
    leverage = args.leverage if args.leverage else snap_leverage(
        needed, ex["leverage_steps"], effective_cap)

    if leverage is None:
        blockers.append(
            f"Required leverage {needed:.2f}x exceeds the effective cap "
            f"{effective_cap:.2f}x. Lower --risk-pct, widen the stop, or raise "
            f"--max-margin-pct.")
        leverage = effective_cap
    elif leverage < needed - 1e-9:
        blockers.append(
            f"Leverage {leverage}x cannot fund a notional of {notional:,.2f} within "
            f"{args.max_margin_pct}% of capital (needs {needed:.2f}x).")
    if leverage > effective_cap + 1e-9:
        warnings.append(
            f"Leverage {leverage}x is above the safe/exchange cap {effective_cap:.2f}x.")

    margin = notional / leverage
    margin_pct_of_capital = margin / args.capital * 100.0

    # --- liquidation ------------------------------------------------------------
    adverse_pct = 100.0 / leverage - ex["maintenance_margin_pct"]
    if adverse_pct <= 0:
        liq_price = entry
        liq_buffer_actual = 0.0
        blockers.append("Leverage is too high for this venue's maintenance margin.")
    else:
        liq_price = entry * (1.0 - sign * adverse_pct / 100.0)
        liq_buffer_actual = adverse_pct / stop_pct

    if liq_buffer_actual < prof["liq_buffer"]:
        warnings.append(
            f"Liquidation is only {liq_buffer_actual:.1f}x the stop distance away "
            f"({prof['liq_buffer']}x required). Reduce leverage.")

    # --- targets ----------------------------------------------------------------
    tp1_r = args.tp1_r if args.tp1_r else prof["tp1_r"]
    tp2_r = args.tp2_r if args.tp2_r else prof["tp2_r"]
    tp1 = entry + sign * tp1_r * stop_distance
    tp2 = entry + sign * tp2_r * stop_distance

    # --- costs ------------------------------------------------------------------
    fee_pct = args.fee_pct if args.fee_pct is not None else ex["default_fee_pct"]
    hold_pct = (args.holding_cost_pct if args.holding_cost_pct is not None
                else ex["holding_cost_pct"])
    round_trip_fee = 2.0 * fee_pct / 100.0 * notional
    periods = math.ceil(args.hold_hours / ex["funding_period_hours"]) if args.hold_hours else 0
    holding_cost = periods * hold_pct / 100.0 * notional
    total_cost = round_trip_fee + holding_cost
    cost_in_r = total_cost / R if R else 0.0

    if R < prof["cost_filter"] * total_cost:
        blockers.append(
            f"Cost filter failed: 1R ({R:,.2f}) < {prof['cost_filter']}x total cost "
            f"({total_cost:,.2f}). Fees eat too much of the edge - widen the target, "
            f"cut leverage, or skip the trade.")

    if ex["max_hold_days"] and args.hold_hours > ex["max_hold_days"] * 24:
        blockers.append(
            f"Planned hold exceeds this venue's {ex['max_hold_days']}-day limit.")

    # --- expectancy -------------------------------------------------------------
    win_rate = args.win_rate if args.win_rate is not None else prof["default_win_rate"]
    avg_win_r = (tp1_r + tp2_r) / 2.0          # 50/50 scale-out
    e_gross = win_rate * avg_win_r - (1.0 - win_rate) * 1.0
    e_net = e_gross - cost_in_r
    breakeven_wr = 1.0 / (1.0 + avg_win_r)

    if e_net <= 0:
        warnings.append(
            f"Net expectancy is {e_net:+.3f}R at a {win_rate:.0%} win rate - not "
            f"positive after costs.")

    # --- is this worth trading at all? -----------------------------------------
    snap_ob = (snapshot or {}).get("orderbook") or {}
    snap_ds = (snapshot or {}).get("direction_score") or {}
    auto_checks = snap_ds.get("auto_checks") or 0
    side_score = snap_ds.get(f"{side}_score")
    direction_ratio = (side_score / auto_checks) if (auto_checks and side_score is not None) else None
    book_value = None
    if isinstance(snap_ob, dict):
        book_value = (snap_ob.get("bid_value_top5") if side == "short"
                      else snap_ob.get("ask_value_top5"))
    closed = None
    stats = (snapshot or {}).get("market_stats")
    if isinstance(stats, dict):
        for v in stats.values():
            if isinstance(v, dict) and "isClosed" in v:
                closed = bool(v["isClosed"])
                break

    qualification = qualify(
        prof,
        atr_pct=(args.atr / entry * 100.0) if args.atr else None,
        spread_pct=snap_ob.get("spread_pct") if isinstance(snap_ob, dict) else None,
        book_value=book_value,
        notional=notional,
        direction_ratio=direction_ratio,
        expectancy_net=e_net,
        cost_in_r=cost_in_r,
        liq_buffer_ratio=liq_buffer_actual,
        rr_tp2=tp2_r,
        blockers=blockers,
        market_closed=closed,
        stop_pct=stop_pct,
        bias_atr_pct=(((snapshot or {}).get("timeframes") or {})
                      .get("bias", {}).get("indicators", {}).get("atr_pct")),
    )

    plan = {
        "profile": args.profile,
        "profile_label": prof["label"],
        "timeframes": {
            "bias": prof["bias_tf"], "decision": prof["decision_tf"],
            "entry": prof["entry_tf"], "atr": prof["atr_tf"],
        },
        "exchange": ex["label"],
        "side": side,
        "levels": {
            "entry": round(entry, 8),
            "stop": round(stop, 8),
            "stop_source": stop_source,
            "stop_distance": round(stop_distance, 8),
            "stop_pct": round(stop_pct, 3),
            "tp1": round(tp1, 8), "tp1_r": tp1_r,
            "tp2": round(tp2, 8), "tp2_r": tp2_r,
            "liquidation_price_estimate": round(liq_price, 8),
        },
        "sizing": {
            "risk_pct": args.risk_pct,
            "risk_amount_R": round(R, 2),
            "quantity": round(quantity, 8),
            "notional": round(notional, 2),
            "leverage": leverage,
            "leverage_caps": cap_reasons,
            "leverage_needed": round(needed, 2),
            "margin": round(margin, 2),
            "margin_pct_of_capital": round(margin_pct_of_capital, 2),
            "liq_buffer_x_stop": round(liq_buffer_actual, 2),
            "liq_buffer_required": prof["liq_buffer"],
        },
        "economics": {
            "fee_pct_per_side": fee_pct,
            "round_trip_fee": round(round_trip_fee, 2),
            "holding_periods": periods,
            "holding_period_hours": ex["funding_period_hours"],
            "holding_cost": round(holding_cost, 2),
            "total_cost": round(total_cost, 2),
            "cost_in_R": round(cost_in_r, 3),
            "rr_tp1": round(tp1_r, 2), "rr_tp2": round(tp2_r, 2),
            "avg_win_R": round(avg_win_r, 2),
            "breakeven_win_rate": round(breakeven_wr, 3),
            "assumed_win_rate": win_rate,
            "expectancy_gross_R": round(e_gross, 3),
            "expectancy_net_R": round(e_net, 3),
        },
        "management": _management(prof, ex, args),
        "data_source": ({"snapshot": args.snapshot,
                         "fetched_at": snapshot.get("fetched_at"),
                         "fields_used": snapshot_used,
                         "direction_score": snapshot.get("direction_score", {}).get("note")}
                        if snapshot else {"snapshot": None,
                                          "note": "Inputs supplied manually"}),
        "qualification": qualification,
        "verdict": "BLOCKED" if blockers else ("CAUTION" if warnings else "OK"),
        "blockers": blockers,
        "warnings": warnings,
        "exchange_notes": ex["notes"],
        "disclaimer": "Mechanical output of a stated method. Not financial advice, "
                      "and not a prediction. Leveraged positions can lose the entire "
                      "margin.",
    }

    if args.json:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return

    L = plan["levels"]
    S = plan["sizing"]
    E = plan["economics"]
    Q = plan["qualification"]
    print(f"\n{'=' * 62}")
    print(f" {side.upper()}  |  {prof['label']}  |  {ex['label']}")
    print(f" WORTH TRADING? {Q['verdict']}   score {Q['score']}/100")
    print(f" {Q['action']}")
    print(f"{'=' * 62}")
    print(f"\n QUALIFICATION GATES")
    for g in Q["gates"]:
        print(f"   [{'ok' if g['passed'] else ' X'}] {g['gate']:<20} {g['detail']}")
    if Q["score_breakdown"]:
        print(f"\n SCORE  ({Q['score']}/100, covering {Q['score_coverage']:.0%} "
              f"of factors)")
        for s in Q["score_breakdown"]:
            print(f"   {s['points']:>5.1f}/{s['weight']:<3} {s['factor']:<20} {s['detail']}")
        for m in Q["missing_factors"]:
            print(f"     - /   not measured: {m}")
    print(f"\n LEVELS                        price          dist        R")
    print(f"   entry                {L['entry']:>14}")
    print(f"   stop loss            {L['stop']:>14}  {L['stop_distance']:>12}   1.00R"
          f"   ({L['stop_pct']}%)")
    print(f"   TP1                  {L['tp1']:>14}  "
          f"{abs(L['tp1'] - L['entry']):>12.6g}   {L['tp1_r']:.2f}R")
    print(f"   TP2                  {L['tp2']:>14}  "
          f"{abs(L['tp2'] - L['entry']):>12.6g}   {L['tp2_r']:.2f}R")
    print(f"   liquidation (est.)   {L['liquidation_price_estimate']:>14}")
    print(f"   stop basis: {L['stop_source']}")
    print(f"\n SIZING")
    print(f"   risk (1R)            {S['risk_amount_R']:>14,.2f}  ({S['risk_pct']}% of capital)")
    print(f"   quantity             {S['quantity']:>14}")
    print(f"   notional             {S['notional']:>14,.2f}")
    print(f"   leverage             {S['leverage']:>14}  (needed {S['leverage_needed']}x, "
          f"caps {S['leverage_caps']})")
    print(f"   margin               {S['margin']:>14,.2f}  ({S['margin_pct_of_capital']}% of capital)")
    print(f"   liq buffer           {S['liq_buffer_x_stop']:>14}x stop  "
          f"(need {S['liq_buffer_required']}x)")
    print(f"\n ECONOMICS")
    print(f"   round-trip fee       {E['round_trip_fee']:>14,.2f}  ({E['fee_pct_per_side']}%/side)")
    print(f"   holding cost         {E['holding_cost']:>14,.2f}  "
          f"({E['holding_periods']} x {E['holding_period_hours']}h)")
    print(f"   total cost           {E['total_cost']:>14,.2f}  = {E['cost_in_R']}R")
    print(f"   avg win / breakeven  {E['avg_win_R']:>14}R  / {E['breakeven_win_rate']:.1%} win rate")
    print(f"   expectancy           {E['expectancy_net_R']:>14}R net  "
          f"(gross {E['expectancy_gross_R']}R at {E['assumed_win_rate']:.0%})")
    if blockers:
        print(f"\n BLOCKERS")
        for b in blockers:
            print(f"   [X] {b}")
    if warnings:
        print(f"\n WARNINGS")
        for w in warnings:
            print(f"   [!] {w}")
    print(f"\n NOTES")
    for n in ex["notes"]:
        print(f"   - {n}")
    print(f"\n {plan['disclaimer']}\n")


# --------------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(
        description="Leveraged crypto trade-plan calculator",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("indicators", help="Compute indicators from an OHLCV CSV")
    pi.add_argument("--csv", required=True,
                    help="CSV with timestamp,open,high,low,close,volume")
    pi.add_argument("--atr-period", type=int, default=14)
    pi.add_argument("--json", action="store_true")
    pi.set_defaults(func=cmd_indicators)

    pp = sub.add_parser("plan", help="Build a full trade plan")
    pp.add_argument("--profile", choices=list(PROFILES), default="intraday")
    pp.add_argument("--side", choices=["long", "short"], required=True)
    pp.add_argument("--entry", type=float,
                    help="Entry price (optional when --snapshot is given)")
    pp.add_argument("--snapshot",
                    help="JSON from 'nobitex_api.py snapshot' - fills entry, ATR "
                         "and swing levels, and applies the direction score")
    pp.add_argument("--atr", type=float, help="ATR on the profile's ATR timeframe")
    pp.add_argument("--max-struct-mult", type=float, default=1.5,
                    help="Cap on how far a structural stop may widen beyond the ATR "
                         "stop (default 1.5). Uncapped structural stops are the usual "
                         "cause of targets that are never reached.")
    pp.add_argument("--atr-mult", type=float,
                    help="Override the profile's ATR multiplier for the stop. The "
                         "profile default suits its intended holding period; a "
                         "shorter hold needs a tighter stop or the target is never "
                         "reached before the clock runs out.")
    pp.add_argument("--stop", type=float, help="Override the ATR-derived stop")
    pp.add_argument("--swing-low", type=float, help="Last swing low (widens a long stop)")
    pp.add_argument("--swing-high", type=float, help="Last swing high (widens a short stop)")
    pp.add_argument("--capital", type=float, required=True)
    pp.add_argument("--risk-pct", type=float, default=1.0)
    pp.add_argument("--exchange", choices=list(EXCHANGES), default="nobitex")
    pp.add_argument("--account-level", type=int, help="Verification tier (caps leverage)")
    pp.add_argument("--leverage", type=float, help="Force a specific leverage")
    pp.add_argument("--leverage-cap", type=float, help="Additional user-imposed cap")
    pp.add_argument("--max-margin-pct", type=float, default=25.0,
                    help="Max %% of capital locked as margin (default 25)")
    pp.add_argument("--fee-pct", type=float, help="Fee per side, %% of notional")
    pp.add_argument("--holding-cost-pct", type=float,
                    help="Renewal/funding cost per period, %% of notional")
    pp.add_argument("--hold-hours", type=float, default=0.0)
    pp.add_argument("--tp1-r", type=float)
    pp.add_argument("--tp2-r", type=float)
    pp.add_argument("--win-rate", type=float, help="Assumed win rate, 0-1")
    pp.add_argument("--json", action="store_true")
    pp.set_defaults(func=cmd_plan)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
