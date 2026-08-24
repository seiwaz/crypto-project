#!/usr/bin/env python3
"""Is a signal stable enough to justify the hold it implies?

The engine opens on a TAKE and then holds for at least two hours. That is only
coherent if the signal describes a two-hour thesis. If a coin is TAKE now and WATCH
or SKIP at the next scan six minutes later, the score is not measuring a setup — it
is measuring noise, and the hold has nothing underneath it.

Measured straight from the observation archive, which records every coin's verdict
and score at every scan:

  * how often a TAKE survives to the next scan, and to the 20 scans a 2h hold needs
  * how long a TAKE run actually lasts
  * how far the score moves between consecutive scans of the same coin
  * whether the flicker is the score wandering across a fixed bar, or the underlying
    direction genuinely flipping

    ./.venv/bin/python tools/signal_stability.py [--hours 24]
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
OBS = f"{ROOT}/var/observations.jsonl"


def ep(ts: str) -> int:
    t = str(ts).replace("Z", "").replace("+00:00", "")
    return calendar.timegm(time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--hold-hours", type=float, default=2.0,
                    help="the hold the signal is supposed to justify")
    args = ap.parse_args()

    cut = time.time() - args.hours * 3600
    recs = []
    with open(OBS) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not r.get("at"):
                continue
            try:
                r["ts"] = ep(r["at"])
            except ValueError:
                continue
            if r["ts"] >= cut:
                recs.append(r)
    if not recs:
        print("no observations yet.")
        return

    per = collections.defaultdict(list)
    for r in recs:
        per[r["coin"]].append(r)
    for v in per.values():
        v.sort(key=lambda r: r["scan"])

    scans = sorted({r["scan"] for r in recs})
    gaps = [min(r["ts"] for r in recs if r["scan"] == b)
            - min(r["ts"] for r in recs if r["scan"] == a)
            for a, b in zip(scans, scans[1:])]
    spacing = statistics.median(gaps) / 60 if gaps else 6.0
    need = max(1, round(args.hold_hours * 60 / spacing))
    span = (max(r["ts"] for r in recs) - min(r["ts"] for r in recs)) / 3600

    print("=" * 76)
    print(f"SIGNAL STABILITY — {len(recs):,} observations, {len(scans)} scans, "
          f"{span:.1f}h, {len(per)} coins")
    print(f"scans are {spacing:.1f} min apart, so a {args.hold_hours:.0f}h hold needs a "
          f"signal to survive ~{need} consecutive scans")
    print("=" * 76)

    # ---- 1. transition matrix ------------------------------------------------
    trans = collections.Counter()
    for v in per.values():
        for a, b in zip(v, v[1:]):
            if b["scan"] - a["scan"] > 2:        # a gap; not a real transition
                continue
            trans[(a["verdict"], b["verdict"])] += 1
    states = ["TAKE", "WATCH", "SKIP", "INCOMPLETE", "ERROR"]
    present = [s for s in states if any(k[0] == s for k in trans)]
    print("\n1. WHAT A VERDICT BECOMES AT THE NEXT SCAN")
    print(f"   {'from':<12}{'n':>7}   " + "".join(f"{s:>12}" for s in present))
    for s in present:
        tot = sum(n for k, n in trans.items() if k[0] == s)
        if not tot:
            continue
        cells = "".join(f"{trans[(s, d)]/tot*100:>11.1f}%" for d in present)
        print(f"   {s:<12}{tot:>7}   {cells}")

    take_tot = sum(n for k, n in trans.items() if k[0] == "TAKE")
    stay = trans[("TAKE", "TAKE")]
    if take_tot:
        print(f"\n   a TAKE is still TAKE six minutes later "
              f"{stay/take_tot*100:.1f}% of the time")
        print(f"   it degrades to WATCH/SKIP "
              f"{(take_tot-stay)/take_tot*100:.1f}% of the time")

    # ---- 2. how long does a TAKE run last? -----------------------------------
    runs = []
    for v in per.values():
        cur = 0
        for r in v:
            if r["verdict"] == "TAKE":
                cur += 1
            else:
                if cur:
                    runs.append(cur)
                cur = 0
        if cur:
            runs.append(cur)
    print(f"\n2. HOW LONG A 'TAKE' LASTS")
    if runs:
        runs.sort()
        print(f"   {len(runs)} runs   median {statistics.median(runs):.0f} scans "
              f"({statistics.median(runs)*spacing:.0f} min)   "
              f"longest {max(runs)} ({max(runs)*spacing:.0f} min)")
        for k in (1, 2, 5, 10, need):
            n = sum(1 for x in runs if x >= k)
            note = "  <-- what a 2h hold needs" if k == need else ""
            print(f"   lasted >= {k:>3} scans ({k*spacing:>4.0f} min): "
                  f"{n:>4} of {len(runs)} ({n/len(runs)*100:>5.1f}%){note}")
        one = sum(1 for x in runs if x == 1)
        print(f"\n   {one/len(runs)*100:.0f}% of TAKEs appear for a SINGLE scan and are "
              f"gone at the next one")
    else:
        print("   no TAKE observed in this window.")

    # ---- 3. is it the score wandering, or the setup changing? ----------------
    deltas, flips = [], 0
    pairs = 0
    for v in per.values():
        for a, b in zip(v, v[1:]):
            if b["scan"] - a["scan"] > 2:
                continue
            if a.get("score") is not None and b.get("score") is not None:
                deltas.append(abs(b["score"] - a["score"]))
            pairs += 1
            if a.get("side") and b.get("side") and a["side"] != b["side"]:
                flips += 1
    print(f"\n3. IS IT THE SCORE MOVING, OR THE SETUP?")
    if deltas:
        deltas.sort()
        print(f"   |score change| between consecutive scans: median "
              f"{statistics.median(deltas):.2f}  p90 {deltas[int(len(deltas)*.9)]:.2f}  "
              f"max {max(deltas):.2f}")
    print(f"   direction flipped long<->short in {flips} of {pairs} consecutive pairs "
          f"({flips/pairs*100:.1f}%)" if pairs else "")

    # ---- 4. the bar is the thing that flickers -------------------------------
    try:
        sys.path.insert(0, ROOT)
        from agent import config
        bar = float(config.load_settings().get("min_score") or 73.0)
    except Exception:
        bar = 73.0
    near = [r for r in recs if r.get("score") is not None and abs(r["score"] - bar) <= 3]
    print(f"\n4. HOW MANY LIVE WITHIN 3 POINTS OF THE {bar:g} ENTRY BAR")
    print(f"   {len(near)} of {len(recs)} observations ({len(near)/len(recs)*100:.1f}%)")
    print(f"   a signal this close to the bar crosses it on ordinary score noise "
          f"(median move {statistics.median(deltas):.2f}/scan),")
    print(f"   which is a threshold artefact rather than the setup changing.")

    # ---- verdict --------------------------------------------------------------
    print("\n" + "=" * 76)
    if runs and take_tot:
        surv = sum(1 for x in runs if x >= need) / len(runs) * 100
        keep = stay / take_tot * 100
        print(f"VERDICT: a TAKE survives one scan {keep:.0f}% of the time and lasts the "
              f"~{need} scans\n         a {args.hold_hours:.0f}h hold implies "
              f"{surv:.0f}% of the time.")
        if surv < 20:
            print("\n         The signal does NOT describe a multi-hour thesis. Either the")
            print("         hold is longer than the signal it rests on, or the score is")
            print("         measuring something too fast to hold. This is the operator's")
            print("         point and the data supports it.")
    print("=" * 76)


main()
