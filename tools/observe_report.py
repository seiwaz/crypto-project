#!/usr/bin/env python3
"""Turn the archived candidates into a study of what the market actually did.

Every scan scores 33 coins and the engine trades about one. `observe.py` keeps all
33; this joins each of them to the candles that followed and asks the questions the
live record is too small to answer:

  * does the score predict anything, or is it decoration?
  * do the gates remove losers, or just trades?
  * which indicator states precede a target, and which precede a stop?
  * what regime were we in, and did the whole watchlist simply follow BTC?
  * where do stops actually get hit relative to how far price then travels?

Outcomes are simulated with the plan's OWN levels — the stop and target that were
computed at that moment — so the answers apply to this strategy rather than to some
idealised one. Costs are charged, and a stop is charged the measured 0.232% of
overshoot, because a stop does not cost exactly 1R.

    ./.venv/bin/python tools/observe_report.py [--hours 12] [--horizon 4]
"""
from __future__ import annotations

import argparse
import calendar
import collections
import json
import os
import statistics
import sys
import time

ROOT = os.environ.get("CS_ROOT", "/opt/crypto-screener")
sys.path.insert(0, ROOT)

OBS = f"{ROOT}/var/observations.jsonl"
ROUND_TRIP = 0.2
OVERSHOOT_PCT = 0.232


def ep(ts: str) -> int:
    t = str(ts).replace("Z", "").replace("+00:00", "")
    return calendar.timegm(time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S"))


def load(hours: float) -> list[dict]:
    cut = time.time() - hours * 3600
    out = []
    with open(OBS) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not r.get("at") or not r.get("price"):
                continue
            try:
                r["ts"] = ep(r["at"])
            except ValueError:
                continue
            if r["ts"] >= cut:
                out.append(r)
    return out


def outcome(rec: dict, bars: list[dict], horizon_h: float) -> dict | None:
    """Replay this candidate against its own plan levels."""
    lv = rec.get("levels") or {}
    e, stop, tp = rec.get("price"), lv.get("stop"), lv.get("tp1")
    side = rec.get("side")
    if not (e and stop and tp and side):
        return None
    stop_pct = abs(e - stop) / e * 100
    if stop_pct <= 0:
        return None
    cost_r = ROUND_TRIP / stop_pct
    end = rec["ts"] + horizon_h * 3600
    # The forward window must have FULLY elapsed. A truncated window cannot reach a
    # distant target but can still hit a near stop, so accepting partial windows
    # biases every result toward the stop — the first run of this report read 67%
    # stop-outs against 44% from every other replay for exactly this reason.
    if not bars or bars[-1]["t"] < end:
        return None
    win = [b for b in bars if rec["ts"] < b["t"] <= end]
    if len(win) < horizon_h * 12 * 0.8:        # 5m bars, allow a few gaps
        return None

    hit, when = None, None
    for b in win:
        if side == "long":
            if b["low"] <= stop:
                hit, when = "sl", b["t"]; break
            if b["high"] >= tp:
                hit, when = "tp", b["t"]; break
        else:
            if b["high"] >= stop:
                hit, when = "sl", b["t"]; break
            if b["low"] <= tp:
                hit, when = "tp", b["t"]; break

    if side == "long":
        best = max(b["high"] for b in win); worst = min(b["low"] for b in win)
        mfe = (best / e - 1) * 100; mae = (worst / e - 1) * 100
    else:
        best = min(b["low"] for b in win); worst = max(b["high"] for b in win)
        mfe = (1 - best / e) * 100; mae = (1 - worst / e) * 100

    if hit == "tp":
        r = (lv.get("tp1_r") or 2.0) - cost_r
    elif hit == "sl":
        r = -(1 + OVERSHOOT_PCT / stop_pct) - cost_r
    else:
        last = win[-1]["close"]
        move = (last / e - 1) * 100 * (1 if side == "long" else -1)
        r = move / stop_pct - cost_r
    return {"hit": hit or "open", "R": r, "mfe": mfe, "mae": mae,
            "mfe_r": mfe / stop_pct, "mae_r": mae / stop_pct,
            "stop_pct": stop_pct, "cost_r": cost_r,
            "mins": ((when - rec["ts"]) / 60) if when else None}


def band(rows, key, edges, label, fmt="{:.0f}"):
    print(f"\n  {label:<34}{'n':>7}{'TP%':>7}{'SL%':>7}{'mean R':>9}{'med MFE(R)':>12}")
    for lo, hi in edges:
        g = [r for r in rows if r.get(key) is not None and lo <= r[key] < hi]
        if len(g) < 15:
            continue
        tp = sum(1 for r in g if r["o"]["hit"] == "tp") / len(g) * 100
        sl = sum(1 for r in g if r["o"]["hit"] == "sl") / len(g) * 100
        print(f"    {fmt.format(lo)} to {fmt.format(hi):<22}{len(g):>7}{tp:>6.1f}%{sl:>6.1f}%"
              f"{statistics.mean(r['o']['R'] for r in g):>+9.4f}"
              f"{statistics.median(r['o']['mfe_r'] for r in g):>+12.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=12.0, help="window to study")
    ap.add_argument("--horizon", type=float, default=4.0, help="forward hours per signal")
    args = ap.parse_args()

    from agent import tabdeal

    recs = load(args.hours)
    if not recs:
        print(f"no observations in the last {args.hours}h — let the timer run.")
        return
    span = (max(r["ts"] for r in recs) - min(r["ts"] for r in recs)) / 3600
    scans = len({r["scan"] for r in recs})
    print("=" * 78)
    print(f"MARKET BEHAVIOUR STUDY — {len(recs):,} scored candidates, {scans} scans, "
          f"{span:.1f}h of coverage")
    print(f"forward horizon {args.horizon:.0f}h per signal, plan's own levels, "
          f"costs and {OVERSHOOT_PCT}% stop overshoot charged")
    print("=" * 78)

    # ---- candles, once per symbol ------------------------------------------
    need = sorted({r["symbol"] for r in recs})
    bars = {}
    for sym in need:
        try:
            k = tabdeal.klines(sym, "5", limit=1500)
            bars[sym] = [{"t": ep(b["timestamp"]), "high": b["high"],
                          "low": b["low"], "close": b["close"]} for b in k]
        except Exception:
            bars[sym] = []

    rows = []
    for r in recs:
        o = outcome(r, bars.get(r["symbol"]) or [], args.horizon)
        if o:
            r["o"] = o
            rows.append(r)
    if not rows:
        print("\nnot enough forward history yet — re-run once the window has matured.")
        return
    # Overlap is the thing that makes a big-looking n meaningless here. The scanner
    # re-scores the same 33 coins every ~6 minutes, so consecutive observations of a
    # coin share almost all of their forward window: at a 4h horizon roughly 41 of
    # them are the SAME market move counted 41 times. Report what the sample is
    # actually worth, not how many rows it has.
    coins = len({r["coin"] for r in rows})
    eff = coins * max(1.0, span / args.horizon)
    print(f"\nresolvable: {len(rows):,} of {len(recs):,}  "
          f"({coins} coins over {span:.1f}h)")
    print(f"EFFECTIVE independent observations: ~{eff:.0f}  "
          f"— consecutive scans of one coin share a forward window and are NOT "
          f"independent trades")
    if span < args.horizon * 3:
        print(f"\n  *** WARNING: {span:.1f}h of coverage against a {args.horizon:.0f}h "
              f"horizon. Nearly every observation is measuring the SAME slice of\n"
              f"      market. Treat everything below as one market event, not a study. "
              f"Let the collector run to {args.horizon * 3:.0f}h+ before deciding "
              f"anything. ***")

    # ---- 1. the regime ------------------------------------------------------
    print("\n" + "-" * 78)
    print("1. REGIME — what the market was doing")
    try:
        btc = tabdeal.klines("BTC_USDT", "60", limit=48)
        if btc:
            f, l = btc[0]["close"], btc[-1]["close"]
            hi = max(b["high"] for b in btc); lo = min(b["low"] for b in btc)
            print(f"   BTC over {len(btc)}h: {f:.0f} -> {l:.0f} ({(l/f-1)*100:+.2f}%), "
                  f"range {(hi/lo-1)*100:.2f}%")
    except Exception as exc:
        print("   BTC unavailable:", exc)
    longs = sum(1 for r in rows if r["side"] == "long")
    print(f"   sides scored: {longs} long / {len(rows)-longs} short "
          f"({longs/len(rows)*100:.0f}% long)")
    fwd = [r["o"]["R"] for r in rows]
    print(f"   every candidate, traded blindly: mean {statistics.mean(fwd):+.4f}R  "
          f"median {statistics.median(fwd):+.4f}R")
    tp = sum(1 for r in rows if r["o"]["hit"] == "tp")
    sl = sum(1 for r in rows if r["o"]["hit"] == "sl")
    print(f"   would reach TP {tp/len(rows)*100:.1f}%  hit SL {sl/len(rows)*100:.1f}%  "
          f"neither {(len(rows)-tp-sl)/len(rows)*100:.1f}%")

    # ---- 2. does the score work? -------------------------------------------
    print("\n" + "-" * 78)
    print("2. DOES THE SCORE PREDICT ANYTHING?")
    band(rows, "score", [(0, 55), (55, 60), (60, 65), (65, 70), (70, 73), (73, 100)],
         "score band")

    # ---- 3. do the gates work? ---------------------------------------------
    print("\n" + "-" * 78)
    print("3. DO THE GATES REMOVE LOSERS, OR JUST TRADES?")
    clean = [r for r in rows if not r["gates_failed"]]
    dirty = [r for r in rows if r["gates_failed"]]
    for lab, g in (("passed every gate", clean), ("failed at least one", dirty)):
        if len(g) < 15:
            continue
        t = sum(1 for r in g if r["o"]["hit"] == "tp") / len(g) * 100
        s = sum(1 for r in g if r["o"]["hit"] == "sl") / len(g) * 100
        print(f"   {lab:<24} n={len(g):<6} TP {t:5.1f}%  SL {s:5.1f}%  "
              f"mean {statistics.mean(r['o']['R'] for r in g):+.4f}R")
    each = collections.defaultdict(list)
    for r in dirty:
        for gname in r["gates_failed"]:
            each[gname].append(r["o"]["R"])
    print("\n   what each gate refused (negative mean = the gate earned its place):")
    for gname, v in sorted(each.items(), key=lambda kv: statistics.mean(kv[1])):
        if len(v) < 15:
            continue
        print(f"     {gname:<24} n={len(v):<6} mean {statistics.mean(v):+.4f}R")

    # ---- 4. what precedes a target vs a stop -------------------------------
    print("\n" + "-" * 78)
    print("4. WHAT PRECEDES A TARGET, AND WHAT PRECEDES A STOP")
    won = [r for r in rows if r["o"]["hit"] == "tp"]
    lost = [r for r in rows if r["o"]["hit"] == "sl"]
    def feat(r, path):
        cur = r.get("ind") or {}
        for p in path:
            cur = (cur or {}).get(p) if isinstance(cur, dict) else None
        return cur
    feats = {
        "bias RSI":        ("bias", "rsi14"),
        "decision RSI":    ("decision", "rsi14"),
        "bias ATR%":       ("bias", "atr_pct"),
        "decision ATR%":   ("decision", "atr_pct"),
        "stop distance %": None,
        "cost in R":       None,
        "direction votes": None,
    }
    print(f"   {'feature':<20}{'reached TP':>14}{'hit SL':>14}{'gap':>10}")
    for name, path in feats.items():
        def val(r):
            if name == "stop distance %": return r["o"]["stop_pct"]
            if name == "cost in R":       return r["o"]["cost_r"]
            if name == "direction votes":
                d = r.get("direction") or {}
                return d.get(r["side"]) if d.get(r["side"]) is not None else None
            return feat(r, path)
        a = [val(r) for r in won]; a = [x for x in a if isinstance(x, (int, float))]
        b = [val(r) for r in lost]; b = [x for x in b if isinstance(x, (int, float))]
        if len(a) < 10 or len(b) < 10:
            continue
        ma, mb = statistics.median(a), statistics.median(b)
        print(f"   {name:<20}{ma:>14.3f}{mb:>14.3f}{ma-mb:>+10.3f}")

    # ---- 5. how far do they actually travel? -------------------------------
    print("\n" + "-" * 78)
    print("5. HOW FAR PRICE ACTUALLY TRAVELS  (in R, from the plan's own stop)")
    mfe = sorted(r["o"]["mfe_r"] for r in rows)
    mae = sorted(r["o"]["mae_r"] for r in rows)
    def pc(v, q): return v[min(len(v) - 1, int(len(v) * q))]
    print(f"   favourable  p25 {pc(mfe,.25):+.2f}R  median {pc(mfe,.5):+.2f}R  "
          f"p75 {pc(mfe,.75):+.2f}R  p90 {pc(mfe,.9):+.2f}R")
    print(f"   adverse     p25 {pc(mae,.25):+.2f}R  median {pc(mae,.5):+.2f}R  "
          f"p75 {pc(mae,.75):+.2f}R  p90 {pc(mae,.9):+.2f}R")
    print("\n   share of candidates whose best moment reaches:")
    for t in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
        n = sum(1 for x in mfe if x >= t)
        print(f"     {t:>4.1f}R : {n/len(mfe)*100:5.1f}%")
    print("\n   -> the TP that the most candidates can actually reach, net of costs:")
    best = None
    for t in (0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0):
        tot = 0.0
        for r in rows:
            o = r["o"]
            if o["mae_r"] <= -1:            # the stop would have come first
                tot += -(1 + OVERSHOOT_PCT / o["stop_pct"]) - o["cost_r"]
            elif o["mfe_r"] >= t:
                tot += t - o["cost_r"]
            else:
                tot += o["R"]
        m = tot / len(rows)
        flag = ""
        if best is None or m > best[1]:
            best, flag = (t, m), "  <-- best"
        print(f"     TP {t:>4.2f}R : mean {m:+.4f}R{flag}")
    print(f"\n   NOTE: this ignores the ORDER of the two excursions and so overstates")
    print(f"   every TP. Use it to rank targets against each other, not as a forecast.")
    if span < args.horizon * 3:
        print(f"   And with only {span:.1f}h of coverage this ranking is one market "
              f"slice.")

    # ---- 6. per-coin --------------------------------------------------------
    print("\n" + "-" * 78)
    print("6. BY COIN — where the edge lives")
    bycoin = collections.defaultdict(list)
    for r in rows:
        bycoin[r["coin"]].append(r["o"]["R"])
    ranked = sorted(((statistics.mean(v), k, len(v)) for k, v in bycoin.items()
                     if len(v) >= 15), reverse=True)
    for m, k, n in ranked[:8]:
        print(f"   {k:<8} n={n:<5} mean {m:+.4f}R")
    print("   ...")
    for m, k, n in ranked[-8:]:
        print(f"   {k:<8} n={n:<5} mean {m:+.4f}R")


main()
