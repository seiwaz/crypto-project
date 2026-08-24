#!/usr/bin/env python3
"""Report the frozen sample: every trade closed since the configuration was fixed.

No daemon. Everything this needs is already in live_positions, so the only state is
var/sample-baseline.json — which records WHEN the configuration was frozen and the
first position id that belongs to the sample. A background poller would just be a
second copy of the database that can die with an ssh session, as one did.

    ./.venv/bin/python tools/sample_report.py
"""
import calendar, json, sqlite3, statistics, sys, time

DB = "/opt/crypto-screener/var/screener.sqlite3"
BASE = "/opt/crypto-screener/var/sample-baseline.json"


def ep(ts):
    return calendar.timegm(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))


def main():
    base = json.load(open(BASE))
    start = base["from_position_id"]
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "select * from live_positions where status='closed' and id>=? order by id",
        (start,))]
    openn = [dict(r) for r in c.execute(
        "select * from live_positions where status='open' and id>=?", (start,))]

    print(f"baseline frozen {base['frozen_at']}  (from position id {start})")
    cfg = base["config"]
    print("  " + "  ".join(f"{k}={v}" for k, v in list(cfg.items())[:6]))
    print("  " + "  ".join(f"{k}={v}" for k, v in list(cfg.items())[6:]))
    print()
    if not rows:
        print(f"no closed trades yet in this sample.  {len(openn)} open.")
        return

    pnl = [float(r["realised_pnl"] or 0) for r in rows]
    wins = [x for x in pnl if x > 0]
    print(f"CLOSED {len(rows)}   total {sum(pnl):+.5f} USDT   "
          f"wins {len(wins)} ({len(wins)/len(rows)*100:.0f}%)   mean {statistics.mean(pnl):+.6f}")

    rs = [float(r["realised_pnl"])/float(r["risk_amount"])
          for r in rows if r.get("risk_amount")]
    if rs:
        print(f"   in R: total {sum(rs):+.3f}  mean {statistics.mean(rs):+.4f}  "
              f"median {statistics.median(rs):+.4f}")

    gross = fees = 0.0
    for r in rows:
        q = abs(r.get("quantity") or 0); e = r.get("entry_price") or 0
        x = r.get("exit_price") or 0
        if not (q and e and x):
            continue
        gross += (x-e)*q if r.get("side") == "long" else (e-x)*q
        fees += (q*e + q*x) * 0.001
    print(f"   price {gross:+.5f}   fees {-fees:+.5f}")

    by = {}
    for r in rows:
        by.setdefault(r.get("exit_reason"), []).append(float(r["realised_pnl"] or 0))
    print("\n   by exit reason:")
    for k, v in sorted(by.items(), key=lambda kv: sum(kv[1])):
        print(f"     {str(k):<15} n={len(v):<3} sum {sum(v):+.5f}")

    print(f"\n   {'coin':<6} {'side':<6} {'score':>6} {'reason':<15} {'held':>6} "
          f"{'R':>7} {'pnl':>10}")
    for r in rows:
        ra = float(r.get("risk_amount") or 0)
        held = ((ep(r["closed_at"]) - r["opened_ts"])/3600
                if r.get("closed_at") and r.get("opened_ts") else 0)
        rr = f"{float(r['realised_pnl'])/ra:+.3f}" if ra else "—"
        print(f"     {r['coin']:<6} {r['side']:<6} {r.get('score') or 0:6.1f} "
              f"{str(r.get('exit_reason')):<15} {held:5.2f}h {rr:>7} "
              f"{float(r['realised_pnl'] or 0):+10.5f}")

    print(f"\n   still open: {len(openn)}")
    n = len(rows)
    print(f"\n   {'ENOUGH TO JUDGE' if n >= 20 else f'need ~{20-n} more trades before this means anything'}")


main()
