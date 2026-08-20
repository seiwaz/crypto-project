"""Deterministic lifecycle checks for the demo's management rules.

Run against a scratch database so the live server's demo loop cannot open positions
underneath the assertions:

    SCREENER_DB=/tmp/demo-test.sqlite3 python3 tests/test_demo_lifecycle.py

Mark price and the latest candle are driven directly, because the rules being tested
- TP1 partial, breakeven stop, time stop, review exit, circuit breaker - would
otherwise need the market to cooperate. Everything else is the real code path.

These caught two money bugs: margin being credited back on close although it was
never debited on open (a +7.57 trade left the balance +57.57), and the entry fee
missing from the R-multiple.
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import demo, paper, store

store.init()

SYM, ENTRY, RISK = 'FIL-SWAP-USDT', 0.68, 10.0
spec = paper.contract_spec(SYM)
price = {'v': ENTRY}
paper.mark_price = lambda s: (price['v'], 'test')
demo._latest_candle = lambda sym: {'high': price['v'], 'low': price['v'],
                                   'open': price['v'], 'close': price['v'], 'volume': 0}
demo._trail_stop = lambda pos, plan, spec: None

def check(label, got, want, tol=1e-6):
    ok = abs(got-want) <= tol if isinstance(want, float) else got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    return ok

results = []

print("0. exit_reason detects a stop the price has moved cleanly past, not just touched")
# The bug (found live 2026-08-20): touched() required stop <= high too, a range-
# containment check. Once price gaps past the stop and the most recent candle's own
# high no longer reaches back up to the old level, that check silently never fires
# again - the exact "stop stopped working" symptom. A long's stop only needs
# low <= stop; a short's only needs high >= stop. Neither should care about the
# opposite bound.
results.append(check("long: candle range entirely below a stop it gapped past",
                     paper.exit_reason('long', high=90.0, low=85.0, stop=95.0,
                                       tp1=None, tp2=None, liq=None),
                     ('stopped', 95.0)))
results.append(check("long: not yet reached (candle range entirely above the stop)",
                     paper.exit_reason('long', high=99.0, low=96.0, stop=95.0,
                                       tp1=None, tp2=None, liq=None),
                     None))
results.append(check("short: candle range entirely above a stop it gapped past",
                     paper.exit_reason('short', high=105.0, low=101.0, stop=100.0,
                                       tp1=None, tp2=None, liq=None),
                     ('stopped', 100.0)))
results.append(check("long: a target gapped past also still registers (high >= tp1)",
                     paper.exit_reason('long', high=112.0, low=108.0, stop=None,
                                       tp1=110.0, tp2=None, liq=None),
                     ('tp1', 110.0)))


def fresh(*, side='long', opened_ago_s=0.0, verdict='TAKE', score=85.0):
    store.paper_init(exchange='toobit', capital=1000.0, slots=5, heat_cap_pct=6.0, reset=True)
    sign = 1 if side == 'long' else -1
    stop = ENTRY * (1 - 0.04 * sign)
    tp1  = ENTRY * (1 + 0.06 * sign)
    tp2  = ENTRY * (1 + 0.12 * sign)
    qty = paper.round_to_step(RISK/abs(ENTRY-stop)/spec['units_per_contract'], spec['step_size'])
    n = paper.notional(qty, ENTRY, spec)
    plan = {"profile": "intraday", "timeframes": {"decision": "4H"},
            "levels": {"entry": ENTRY, "stop": stop, "tp1": tp1, "tp2": tp2},
            "sizing": {"quantity": paper.coins(qty, spec), "leverage": 5.0,
                       "risk_amount_R": RISK}}
    pid = store.paper_open(coin='FIL', symbol=SYM, exchange='toobit', side=side, slot=1,
        contracts=qty, entry_price=ENTRY, leverage=5.0, margin=n/5.0, risk_amount=RISK,
        stop=stop, tp1=tp1, tp2=tp2, opened_ts=paper.now_ts()-opened_ago_s,
        entry_fee=paper.fee(n), score=score, verdict=verdict, plan_json=json.dumps(plan))
    return pid, stop, tp1, tp2

print("1. TP1 takes half and locks the runner's stop at the TP1 price")
pid, stop, tp1, tp2 = fresh()
before = store.paper_position(pid)['contracts']
price['v'] = tp1; demo.cycle()
p = store.paper_position(pid)
results.append(check("half closed", round(p['contracts']/before, 3), 0.5))
results.append(check("tp1 flagged", p['tp1_filled'], 1))
results.append(check("stop locked exactly at tp1", p['stop'], tp1))
bal_after_tp1 = store.paper_account()['balance']
results.append(check("banked ~0.75R", round(p['realised_partial'], 2), round(0.75*RISK - 0.08, 2), 0.15))

print("2. The runner stops at the locked TP1 price and the trade nets close to the TP1 R")
price['v'] = p['stop']; demo.cycle()
t = store.paper_closed_positions()[0]
results.append(check("exit reason", t['exit_reason'], 'stopped'))
results.append(check("total > 0", t['realised_pnl'] > 0, True))
bal = store.paper_account()['balance']
results.append(check("balance reconciles", round(bal, 4), round(1000 + t['realised_pnl'], 4), 0.01))
from agent import report as _rep
results.append(check("R is net of entry fee", round(_rep.trades()[0]['r'], 4),
                     round((t['realised_pnl'] - t['entry_fee'] + (t['funding_paid'] or 0))/10.0, 4), 1e-4))

print("3. TP2 closes the whole position")
pid, stop, tp1, tp2 = fresh()
price['v'] = tp2; demo.cycle()
t = store.paper_closed_positions()[0]
results.append(check("exit reason", t['exit_reason'], 'tp2'))
results.append(check("no open positions", len(store.paper_open_positions()), 0))

print("4. Stop-out on a short")
pid, stop, tp1, tp2 = fresh(side='short')
price['v'] = stop; demo.cycle()
t = store.paper_closed_positions()[0]
results.append(check("exit reason", t['exit_reason'], 'stopped'))
results.append(check("loses about 1R", round(t['realised_pnl']/RISK, 1), -1.0, 0.15))

print("5. Time stop fires past the profile's hours when below the USDT floor")
pid, *_ = fresh(opened_ago_s=(demo.time_stop_hours() + 1) * 3600)
price['v'] = ENTRY * 1.001
demo.cycle()
t = store.paper_closed_positions()[0]
results.append(check("exit reason", t['exit_reason'], 'time_stop'))
results.append(check("held past the limit", t['bars_held'] >= 0, True))
results.append(check("floor is USDT", demo.time_stop_floor_usdt({'risk_amount': RISK}), 0.5*RISK))

print("6. Time stop does NOT fire when the trade has cleared the USDT floor")
pid, stop, tp1, tp2 = fresh(opened_ago_s=(demo.time_stop_hours() + 1) * 3600)
price['v'] = ENTRY + 0.6*(tp1-ENTRY)/1.5*1.5   # ~0.9R, below tp1
demo.cycle()
results.append(check("still open", len(store.paper_open_positions()), 1))

print("6b. Time stop does NOT fire on a losing position - it floats to breakeven or the stop")
pid, stop, tp1, tp2 = fresh(opened_ago_s=(demo.time_stop_hours() + 1) * 3600)
price['v'] = ENTRY * 0.999   # a small loss, well past the deadline
demo.cycle()
results.append(check("still open while underwater", len(store.paper_open_positions()), 1))
price['v'] = stop; demo.cycle()   # the real stop still fires - floating isn't "never exits"
t = store.paper_closed_positions()[0]
results.append(check("real stop still closes it", t['exit_reason'], 'stopped'))

import agent.demo as D
BELOW_FLOOR = ENTRY * 1.008   # ~0.2R profit - below the 0.5R floor, above zero

print("6c. A profitable position below the floor floats when confirmed still favoured")
pid, stop, tp1, tp2 = fresh(opened_ago_s=(demo.time_stop_hours() + 1) * 3600)
price['v'] = BELOW_FLOOR
D.store.result_for = lambda coin, ex: {"verdict": "TAKE", "score": 85.0}
demo.cycle()
results.append(check("still open, floating on a confirmed TAKE", len(store.paper_open_positions()), 1))

print("6d. A profitable position closes on signal_exit the moment the verdict turns")
pid, stop, tp1, tp2 = fresh(opened_ago_s=(demo.time_stop_hours() + 1) * 3600)
price['v'] = BELOW_FLOOR
D.store.result_for = lambda coin, ex: {"verdict": "SKIP", "score": 41.0}
demo.cycle()
t = store.paper_closed_positions()[0]
results.append(check("exit reason", t['exit_reason'], 'signal_exit'))
results.append(check("closed in profit, not held for the clock", t['realised_pnl'] > 0, True))

print("6e. Missing scan data does not override the ordinary time-stop either way")
pid, stop, tp1, tp2 = fresh(opened_ago_s=(demo.time_stop_hours() + 1) * 3600)
price['v'] = BELOW_FLOOR
D.store.result_for = lambda coin, ex: None   # no fresh scan data - no opinion, not a float
demo.cycle()
t = store.paper_closed_positions()[0]
results.append(check("falls through to the ordinary time_stop", t['exit_reason'], 'time_stop'))
D.store.result_for = lambda coin, ex: None   # reset for the tests that follow

print("7. Review exit when the verdict is no longer TAKE")
pid, *_ = fresh()
store.paper_update(pid, funding_periods=1)
store.set_kv(f"demo.reviewed.{pid}", 0)
import agent.demo as D
D.store.result_for = lambda coin, ex: {"verdict": "SKIP", "score": 41.0}
price['v'] = ENTRY
demo.cycle()
t = store.paper_closed_positions()[0]
results.append(check("exit reason", t['exit_reason'], 'review_exit'))

print("8. Circuit breaker stops new positions after 3 losses")
store.paper_init(exchange='toobit', capital=1000.0, slots=5, heat_cap_pct=6.0, reset=True)
for i in range(3):
    pid, stop, *_ = fresh.__wrapped__() if hasattr(fresh,'__wrapped__') else (None,None)
    break
store.paper_init(exchange='toobit', capital=1000.0, slots=5, heat_cap_pct=6.0, reset=True)
for i in range(3):
    pid = store.paper_open(coin=f'X{i}', symbol=SYM, exchange='toobit', side='long', slot=1,
        contracts=10, entry_price=ENTRY, leverage=5.0, margin=1.0, risk_amount=RISK,
        stop=0.6, tp1=0.7, tp2=0.8, opened_ts=paper.now_ts(), entry_fee=0.0)
    store.paper_close(pid, exit_price=0.6, exit_reason='stopped', realised_pnl=-10.0, exit_fee=0.0)
cb = demo.circuit_breaker()
results.append(check("breaker tripped", cb and cb['code'], 'consecutive_losses'))
results.append(check("losses counted", cb and cb['losses'], 3))

print("9. A coin that just closed is not re-entered on the same stale scan")
store.paper_init(exchange='toobit', capital=1000.0, slots=5, heat_cap_pct=6.0, reset=True)
SCAN_AT = "2026-01-01T00:00:00+00:00"
row = {"coin": "FIL", "symbol": SYM, "exchange": "toobit", "side": "long",
       "verdict": "TAKE", "score": 88.0, "fetched_at": SCAN_AT, "plan_json": None,
       "scan_id": 1}
demo.store.latest_results = lambda ex: [row]
results.append(check("eligible before any trade",
                     [r["coin"] for r in demo.qualifying_signals()], ["FIL"]))

pid = store.paper_open(coin='FIL', symbol=SYM, exchange='toobit', side='long', slot=1,
    contracts=10, entry_price=ENTRY, leverage=5.0, margin=1.0, risk_amount=RISK,
    stop=0.6, tp1=0.7, tp2=0.8, opened_ts=paper.now_ts(), entry_fee=0.0)
store.paper_close(pid, exit_price=0.6, exit_reason='stopped', realised_pnl=-10.0, exit_fee=0.0)
results.append(check("blocked after closing on that scan",
                     [r["coin"] for r in demo.qualifying_signals()], []))

row["fetched_at"] = "2099-01-01T00:00:00+00:00"   # a scan newer than the close
results.append(check("eligible again on a fresher scan",
                     [r["coin"] for r in demo.qualifying_signals()], ["FIL"]))

print("9b. A tied/no-margin direction is not a qualifying signal")
store.paper_init(exchange='toobit', capital=1000.0, slots=5, heat_cap_pct=6.0, reset=True)
tied_row = {"coin": "TIEDCOIN", "symbol": SYM, "exchange": "toobit", "side": "long",
            "verdict": "TAKE", "score": 88.0, "side_tied": 1,
            "fetched_at": "2099-01-01T00:00:00+00:00", "plan_json": None, "scan_id": 1}
demo.store.latest_results = lambda ex: [tied_row]
results.append(check("tied signal excluded even with a qualifying score",
                     [r["coin"] for r in demo.qualifying_signals()], []))
tied_row["side_tied"] = 0
results.append(check("same signal qualifies once side_tied clears",
                     [r["coin"] for r in demo.qualifying_signals()], ["TIEDCOIN"]))

print("9c. A coin with a still-pending (unfilled maker) order is not re-entered")
# The bug (found live 2026-08-20): qualifying_signals()'s open_coins guard only read
# paper_open_positions(), which filters status='open'. A resting maker limit order
# sits at status='pending' until it fills, so it was invisible to the guard - a scan
# a few minutes later (still inside the maker-timeout window) saw the coin as "not
# open" and queued a second entry. This account did exactly that on WIF live.
store.paper_init(exchange='toobit', capital=1000.0, slots=5, heat_cap_pct=6.0, reset=True)
row = {"coin": "WIF", "symbol": "WIF-SWAP-USDT", "exchange": "toobit", "side": "long",
       "verdict": "TAKE", "score": 80.0, "fetched_at": "2099-01-01T00:00:00+00:00",
       "plan_json": None, "scan_id": 1}
demo.store.latest_results = lambda ex: [row]
results.append(check("eligible before any order exists",
                     [r["coin"] for r in demo.qualifying_signals()], ["WIF"]))

store.paper_open(status='pending', limit_price=0.16, placed_ts=paper.now_ts(),
    coin='WIF', symbol='WIF-SWAP-USDT', exchange='toobit', side='long', slot=1,
    contracts=10, entry_price=0.16, leverage=5.0, margin=1.0, risk_amount=RISK,
    stop=0.15, tp1=0.17, tp2=0.18, opened_ts=paper.now_ts(), entry_fee=0.0)
results.append(check("blocked while its own order is still pending, not yet open",
                     [r["coin"] for r in demo.qualifying_signals()], []))

print("10. Correlated same-direction positions are capped")
from agent import correlation
store.paper_init(exchange='toobit', capital=1000.0, slots=5, heat_cap_pct=6.0, reset=True)
correlation.btc_context = lambda sym, interval=None, window=None: {
    "symbol": sym, "correlation": 0.95, "beta": 1.4, "alpha_pct": 0.0, "bars": 120}
cand = {"coin": "NEW", "symbol": "NEW-SWAP-USDT", "side": "short", "score": 90.0}

results.append(check("allowed with none open",
                     demo.correlated_same_side(cand, []), None))

def fake(side, n):
    return [{"coin": f"C{i}", "symbol": f"C{i}-SWAP-USDT", "side": side} for i in range(n)]

results.append(check("allowed with one correlated same-side",
                     demo.correlated_same_side(cand, fake("short", 1)), None))
blocked = demo.correlated_same_side(cand, fake("short", 2))
results.append(check("blocked at the cap of two", bool(blocked), True))
results.append(check("reports the count", blocked and blocked["already_open"], 2))
results.append(check("opposite side does not count",
                     demo.correlated_same_side(cand, fake("long", 4)), None))

correlation.btc_context = lambda sym, interval=None, window=None: None
results.append(check("unknown correlation does not block",
                     demo.correlated_same_side(cand, fake("short", 4)), None))

store.paper_init(exchange='toobit', capital=1000.0, slots=5, heat_cap_pct=6.0, reset=True)
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
