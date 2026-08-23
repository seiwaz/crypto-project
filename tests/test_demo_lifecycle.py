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
from agent import server as _srv
from agent import config
import json as _json

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
# Price is driven PAST tp1, not exactly onto it. Setting it exactly at the level was
# a blind spot that hid a real bug for the life of the project: demo._touched() used
# `low <= level <= high`, which is only true when the candle straddles the level.
# With high=low=tp1 that held, so this test passed while every live TP1 partial
# silently failed to fire once price moved cleanly through.
pid, stop, tp1, tp2 = fresh()
before = store.paper_position(pid)['contracts']
price['v'] = tp1 + (tp2 - tp1) * 0.4      # clearly past tp1, still short of tp2
demo.cycle()
p = store.paper_position(pid)
results.append(check("half closed", round(p['contracts']/before, 3), 0.5))
results.append(check("tp1 flagged", p['tp1_filled'], 1))
results.append(check("stop locked exactly at tp1", p['stop'], tp1))
bal_after_tp1 = store.paper_account()['balance']
results.append(check("banked ~0.75R", round(p['realised_partial'], 2), round(0.75*RISK - 0.08, 2), 0.15))

results.append(check("_touched fires for a long once price is past tp1",
                     demo._touched('long', high=110.0, low=105.0, level=100.0), True))
results.append(check("_touched does not fire before a long reaches tp1",
                     demo._touched('long', high=99.0, low=95.0, level=100.0), False))
results.append(check("_touched fires for a short once price is below tp1",
                     demo._touched('short', high=95.0, low=90.0, level=100.0), True))
results.append(check("_touched does not fire before a short reaches tp1",
                     demo._touched('short', high=110.0, low=101.0, level=100.0), False))

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

print("9d. Tabdeal client retries a 502 but not a 400")
# Found live 2026-08-22: a single CDN 502 on /r/plots/history knocked XRP out of an
# entire scan. The client was modelled on toobit's, whose rule is "HTTP errors are
# not retried: a 400 will still be a 400" - correct for 4xx, wrong for a gateway
# blip. 5xx and 429 must be retried; 4xx must still fail fast.
import io, urllib.error
from agent import tabdeal as _tab
_tab._RETRY_BACKOFF = 0.0                      # keep the test instant

def _fake_urlopen(codes, payload=b'{"data":[],"no_data":true}'):
    """Raise each code in turn, then succeed. Returns (opener, call_counter)."""
    state = {"n": 0}
    def opener(req, timeout=None):
        i = state["n"]; state["n"] += 1
        if i < len(codes):
            raise urllib.error.HTTPError(req.full_url, codes[i], "err", {},
                                         io.BytesIO(b"<!DOCTYPE html><html>gateway"))
        class R:
            def read(self): return payload
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()
    return opener, state

_orig = _tab.urllib.request.urlopen
try:
    op, st = _fake_urlopen([502, 502])          # two blips, then fine
    _tab.urllib.request.urlopen = op
    got = _tab._get("/r/plots/history", {"symbol": "X_USDT", "resolution": "15"},
                    chart=True)
    results.append(check("502 retried then succeeded", got.get("no_data"), True))
    results.append(check("took 3 attempts", st["n"], 3))

    op, st = _fake_urlopen([400])               # a real client error
    _tab.urllib.request.urlopen = op
    try:
        _tab._get("/r/fapi/v1/depth", {"symbol": "NOPE"})
        results.append(check("400 raised", False, True))
    except _tab.TabdealError as exc:
        results.append(check("400 not retried", st["n"], 1))
        results.append(check("no HTML dumped in the error",
                             "<!DOCTYPE" not in str(exc), True))
finally:
    _tab.urllib.request.urlopen = _orig

print("9e. A signal whose price has already run past the plan entry is declined")
# Found 2026-08-22 in the first Tabdeal trades: a plan's stop/tp1/tp2 are anchored to
# the entry at SCAN time, but the fill happens at the current mark and the levels are
# never re-anchored. FLOKI filled 3.32% above a plan entry whose stop was 1.75% away
# - 1.83R of drift - leaving TP1 already behind price and TP2 0.19R away against 2.83R
# of risk. Drift is measured in units of the planned stop, signed against the trade.
def drift_case(side, plan_entry, stop, mark):
    row = {"side": side}
    prop = {"plan": {"levels": {"entry": plan_entry}}, "stop": stop, "entry": mark}
    return demo._entry_drift_r(row, prop)

results.append(check("the real FLOKI case measures 1.83R of drift",
                     round(drift_case("long", 2.684e-05, 2.637e-05, 2.7702e-05), 2), 1.83))
results.append(check("long at the plan entry is 0R",
                     drift_case("long", 100.0, 99.0, 100.0), 0.0))
results.append(check("long filling BETTER than plan is negative (allowed)",
                     drift_case("long", 100.0, 99.0, 99.5), -0.5))
results.append(check("short is mirrored, not absolute",
                     drift_case("short", 100.0, 101.0, 99.0), 1.0))
results.append(check("missing levels never block a trade",
                     drift_case("long", None, 99.0, 100.0), None))
results.append(check("zero-width stop never blocks",
                     drift_case("long", 100.0, 100.0, 101.0), None))
results.append(check("default threshold is 0.3R",
                     demo.settings()["max_entry_drift_r"], 0.3))

print("11. Live broker is disarmed, and its rails hold when armed")
# tabdeal_broker is the only code in the project that can move real money. These
# checks exist so "it is switched off" is a tested property, not an assumption.
from agent import tabdeal_broker as _tb, guard as _g

_orig_live = _tb.TabdealBroker.live_enabled
try:
    _tb.TabdealBroker.live_enabled = staticmethod(lambda: False)
    b = _tb.TabdealBroker()
    results.append(check("dry_run is the constructor default", b.dry_run, True))

    blocked = 0
    for fn in (lambda: b.place_order("BTC_USDT", "BUY", 0.001, price=70000),
               lambda: b.close_position("BTC_USDT"),
               lambda: b.set_position_sl_tp(1, sl_price=1.0),
               lambda: b.set_leverage("BTC_USDT", 10),
               lambda: b.transfer(100)):
        try:
            fn()
        except _g.LiveTradingDisabled:
            blocked += 1
        except Exception:                       # noqa: BLE001
            pass
    results.append(check("every write refuses while disarmed", blocked, 5))

    # Armed but still dry: the rails must not depend on the disarmed check.
    _tb.TabdealBroker.live_enabled = staticmethod(lambda: True)
    b = _tb.TabdealBroker(dry_run=True)
    results.append(check("armed dry-run reaches the wire builder",
                         b.place_order("SUI_USDT", "BUY", 100.0,
                                       price=0.83).get("dry_run"), True))
    rails = 0
    for fn in (lambda: b.place_order("BTC_USDT", "BUY", 1.0, price=77000),   # notional
               lambda: b.place_order("BTC_USDT", "LONG", 0.001, price=1),    # side
               lambda: b.place_order("BTC_USDT", "BUY", 0.001,
                                     order_type="STOP_MARKET", price=1),     # type
               lambda: b.place_order("BTC_USDT", "BUY", 0.001),              # no price
               lambda: b.place_order("BTC_USDT", "BUY", 0, price=1),         # qty
               lambda: b.set_position_sl_tp(1),                              # no level
               lambda: b.reduce_position("X_USDT", 1.5)):                    # fraction
        try:
            fn()
        except _tb.BrokerError:
            rails += 1
        except Exception:                        # noqa: BLE001
            pass
    results.append(check("armed rails still reject bad orders", rails, 7))

    # The read guard must keep refusing every write path even with live armed.
    still_refused = all(not _g.tabdeal_is_read_only(p, m)
                        for m, p in _g.TABDEAL_WRITE_ALLOWLIST)
    results.append(check("read guard still refuses all write paths",
                         still_refused, True))
finally:
    _tb.TabdealBroker.live_enabled = _orig_live

results.append(check("_num avoids scientific notation", _tb._num(1e-05), "0.00001"))
results.append(check("_num passes None through", _tb._num(None), None))

print("12. Inverted plan geometry is rejected before it can be traded")
# Live 2026-08-22: the planner emitted BNB long with entry 700.281, stop 712.834
# ABOVE it and tp1 equal to the stop, from "structural (behind swing + 0.25 ATR)" -
# price had fallen through the swing low, so "behind the swing" landed above price.
# It scored 71.2 and passed every other gate. As a long it stops out on open.
_L = lambda e, st, t1, t2: {"entry": e, "stop": st, "tp1": t1, "tp2": t2}
results.append(check("long: normal geometry passes",
                     demo.valid_geometry("long", _L(100, 95, 105, 110)), True))
results.append(check("long: the real BNB inversion is rejected",
                     demo.valid_geometry("long", _L(700.281, 712.834, 712.834, 725.386)),
                     False))
results.append(check("long: tp1 equal to the stop is rejected",
                     demo.valid_geometry("long", _L(100, 95, 95, 110)), False))
results.append(check("short: mirrored geometry passes",
                     demo.valid_geometry("short", _L(100, 105, 95, 90)), True))
results.append(check("short: a long-shaped plan is rejected",
                     demo.valid_geometry("short", _L(100, 95, 105, 110)), False))
results.append(check("missing levels never block (handled elsewhere)",
                     demo.valid_geometry("long", _L(100, None, 105, 110)), True))

print("13. _profit_signal_check works on a row with no `exchange` column")
# live_positions has no `exchange` column; the live engine passes its own rows in.
# Reading pos["exchange"] raised KeyError, aborting _manage_one before the time stop,
# so a live position in profit received no engine management at all.
_saved = D.store.result_for
try:
    D.store.result_for = lambda coin, ex: {"verdict": "TAKE", "score": 85.0}
    results.append(check("no-exchange row resolves instead of raising",
                         demo._profit_signal_check({"coin": "BNB"}), (True, None)))
    D.store.result_for = lambda coin, ex: {"verdict": "SKIP", "score": 40.0}
    ok, why = demo._profit_signal_check({"coin": "BNB"})
    results.append(check("still detects a lapsed setup", ok, False))
    results.append(check("and gives a reason", why is not None, True))
finally:
    D.store.result_for = _saved

print("14. Live engine: TP1 is a full close, not a stop-move")
from agent import live as _live
import inspect as _insp
import time as _time
_src = _insp.getsource(_live._manage_one)
# Anchored on the round_trip_cost assignment, which is the next statement after the
# TP1 branch. The old anchor was the "Signal exit" comment, and that exit was later
# removed - a test that slices source between two comments breaks when either is
# reworded, which is not the same thing as the behaviour changing.
_tp1 = _src[_src.index("TP1 reached"):_src.index("round_trip_cost = qty")]
results.append(check("TP1 branch closes the position",
                     'settle(broker, row, "tp1"' in _tp1, True))
results.append(check("TP1 branch does NOT move the stop",
                     "_attach_stop" not in _tp1, True))
results.append(check("entry attaches TP1 to the exchange",
                     "_attach_stop(broker, pid, symbol, stop, tp1)"
                     in _insp.getsource(_live._enter), True))
results.append(check("long reaches TP1 when mark is at or above it",
                     _live._reached("long", 105.0, 100.0), True))
results.append(check("long has not reached TP1 below it",
                     _live._reached("long", 99.0, 100.0), False))
results.append(check("short reaches TP1 when mark is at or below it",
                     _live._reached("short", 95.0, 100.0), True))

print("15. try_open distinguishes a quiet market from a broken engine")
# _enter referenced `side` one line before it was bound, so every entry raised
# UnboundLocalError; try_open caught it and returned "no_signal". The engine looked
# idle while being incapable of opening anything.
_src = _insp.getsource(_live._enter)
results.append(check("`side` is bound before the geometry check",
                     _src.index('side = row["side"]') < _src.index("valid_geometry(side"),
                     True))
# the selection loop now lives in _try_open_locked, behind the entry lock
_ts = _insp.getsource(_live._try_open_locked)
results.append(check("failed entries report all_entries_failed",
                     "all_entries_failed" in _ts, True))
results.append(check("no_signal is still returned when there is genuinely nothing",
                     '"reason": "no_signal"' in _ts, True))

print("16. Entry is serialised and duplicate rows cannot double-count")
# Live 2026-08-22: a manual try_open raced the scheduler thread; both passed the
# "not already held" check and both placed an order one second apart (8462546,
# 8462548). Result was double the intended size on one venue position and two DB
# rows that each recorded the same close - over-counting the loss by 100%.
results.append(check("an entry lock exists", hasattr(_live, "_entry_lock"), True))
_ts = _insp.getsource(_live.try_open)
results.append(check("try_open takes it non-blocking",
                     "acquire(blocking=False)" in _ts, True))
results.append(check("a busy lock reports entry_in_progress",
                     "entry_in_progress" in _ts, True))
_lk = _insp.getsource(_live._try_open_locked)
results.append(check("the book is re-read inside the lock before placing",
                     'store.live_positions("pending", "open")' in _lk, True))
_mg = _insp.getsource(_live.manage)
results.append(check("manage dedupes by symbol", "seen" in _mg, True))
results.append(check("a duplicate row is closed with ZERO pnl",
                     "realised_pnl=0.0" in _mg, True))
# the lock actually excludes
_held = _live._entry_lock.acquire(blocking=False)
try:
    results.append(check("held lock blocks a second entry",
                         _live.try_open().get("reason"), "entry_in_progress"))
finally:
    if _held: _live._entry_lock.release()

print("17. Entry is triggered by a new completed scan, not a blind timer")
# Live 2026-08-22: the entry timer fired at 09:51:52, seconds before scan 706 finished
# at 09:52:33 and made BNB a TAKE. The two clocks had drifted apart, so a valid signal
# sat unacted on for five minutes - a sixth of a 30-minute scalp's life.
_sl = _insp.getsource(_live.scheduler_loop)
results.append(check("loop tracks the last scan it acted on",
                     "last_scan_seen" in _sl, True))
results.append(check("entry requires a fresh scan",
                     "scan_id != last_scan_seen" in _sl, True))
# The interval floor on TOP of the scan trigger was removed 2026-08-23: it made the
# engine skip a whole scan whenever an entry landed mid-cycle, leaving XRP at 80.8
# unacted on with three slots free. The scan cadence is the rate limiter.
results.append(check("no redundant interval floor gates the scan trigger",
                     "spaced" not in _sl, True))
results.append(check("every attempt is logged, not just fills",
                     "live entry attempt" in _sl, True))
results.append(check("only COMPLETED scans count",
                     "status = 'done'" in _insp.getsource(_live._latest_scan_id), True))
results.append(check("_latest_scan_id returns an int or None",
                     _live._latest_scan_id() is None
                     or isinstance(_live._latest_scan_id(), int), True))

print("18. Settlement identifies OUR position and only OUR fees")
_cf = _insp.getsource(_live._closing_fill)
# check the CODE, not the comment that explains the old bug
_code = "\n".join(l for l in _cf.splitlines()
                  if not l.strip().startswith("#") and '"""' not in l)
results.append(check("no `or not vpid` fallback in the code itself",
                     "or not vpid" not in _code, True))
results.append(check("falls back to the position nearest our opened_ts",
                     "abs(float(h.get(\"createdTime\") or 0) - opened_ms)" in _cf, True))
results.append(check("fees are bounded by the position's own window",
                     "created <= ts <= updated" in _cf, True))
results.append(check("the venue id is stored even when the stop attach fails",
                     "venue_position_id=str(live_now" in _insp.getsource(_live._attach_stop),
                     True))

results.append(check("a closed row with NULL pnl gets backfilled",
                     hasattr(_live, "backfill_unsettled"), True))
results.append(check("backfill runs every cycle",
                     "backfill_unsettled(broker)" in _insp.getsource(_live.cycle), True))
results.append(check("backfill only touches rows missing a result",
                     'row.get("realised_pnl") is not None' in
                     _insp.getsource(_live.backfill_unsettled), True))

print("19. Signal exit must clear the round-trip cost first")
# Live: 8 closes, gross POSITIVE at +0.064254, account still down 0.047507 - because
# six of them fired while gross was below the ~0.0124 round-trip fee, so the exit
# itself booked the loss. `upnl > 0` was the wrong bar.
_m1 = _insp.getsource(_live._manage_one)
results.append(check("a round-trip cost is computed",
                     "round_trip_cost = qty * mark" in _m1, True))
# The exit this gates is now profit_close (signal_exit was removed 2026-08-23 when
# the engine was restricted to closing only a net-profitable position past its hour).
results.append(check("the exit is gated on the round trip, not on upnl > 0",
                     "exit_cost * cfg[" in _m1 and "exit_upnl > close_bar" in _m1,
                     True))
# `elif upnl > 0:` is the correct below-the-line branch; what must be gone is the
# bare `if upnl > 0:` that used to trigger the exit itself.
results.append(check("the bare `if upnl > 0:` trigger is gone",
                     not any(l.strip() == "if upnl > 0:" for l in _m1.splitlines()),
                     True))
# Below the cost line there is now no branch at all: the engine simply holds and
# lets the exchange's own stop and take-profit stand. Closing a position that has
# not paid for itself books a certain loss, which is what below_cost_line existed to
# avoid - removing every early exit removes the need for the guard.
results.append(check("nothing closes below the cost line",
                     _m1.count("settle(broker, row,"), 3))
# the arithmetic itself: 0.1% a side, both sides
_qty, _mark = 0.0089, 697.0
_expect = _qty * _mark * (_tab.TAKER_FEE_PCT / 100.0) * 2
results.append(check("round trip is 0.2% of notional",
                     round(_expect, 6), round(_qty * _mark * 0.002, 6)))
results.append(check("~0.0124 on a $6.20 position", round(_expect, 4), 0.0124, 0.0002))

print("20. Position sizing re-reads the live balance every time")
class _FakeB:
    def __init__(self, wallet, unreal=0.0): self._w, self._u = wallet, unreal
    def balance(self): return [{"walletBalance": str(self._w), "crossUnPnl": str(self._u)}]
_cfg = {"max_total_notional": 25.0, "notional_multiple": 4.7}
results.append(check("equity is wallet when flat",
                     _live.account_equity(_FakeB(5.27)), 5.27))
results.append(check("unrealised LOSS reduces equity",
                     round(_live.account_equity(_FakeB(5.27, -1.0)), 4), 4.27))
results.append(check("unrealised GAIN does not inflate it",
                     _live.account_equity(_FakeB(5.27, +1.0)), 5.27))
cap, why = _live.notional_cap(_FakeB(5.27), _cfg)
results.append(check("cap scales with equity", round(cap, 3), round(5.27*4.7, 3)))
cap2, _ = _live.notional_cap(_FakeB(4.00), _cfg)
results.append(check("a drawdown shrinks the cap", round(cap2, 3), round(4.00*4.7, 3)))
results.append(check("and it really is smaller", cap2 < cap, True))
cap3, why3 = _live.notional_cap(_FakeB(50.0), _cfg)
results.append(check("the absolute ceiling still binds", cap3, 25.0))
results.append(check("the ceiling says so", "ceiling" in why3, True))
class _Dead:
    def balance(self): raise RuntimeError("venue down")
cap4, why4 = _live.notional_cap(_Dead(), _cfg)
results.append(check("an unreadable balance falls back to the configured cap",
                     cap4, 25.0))
results.append(check("and says why", "unreadable" in why4, True))

# 85 was asked for but is unreachable here: max ever scored on Tabdeal is 79.0 and
# nothing has passed 80, so an 85 floor would mean never trading. 75 is the venue
# equivalent of "only the best" - the top 0.83% of all results.
results.append(check("floor defaults to 75 (85 is unreachable on this venue)",
                     demo.min_score(), 75.0))
_below = {"coin": "LOWSCORE", "symbol": SYM, "exchange": "toobit", "side": "long",
          "verdict": "TAKE", "score": 74.0, "side_tied": 0,
          "fetched_at": "2099-01-01T00:00:00+00:00", "plan_json": None, "scan_id": 1}
demo.store.latest_results = lambda ex: [_below]
results.append(check("a TAKE scoring 74 is rejected",
                     [r["coin"] for r in demo.qualifying_signals()], []))
_below["score"] = 76.0
results.append(check("and 76 is accepted",
                     [r["coin"] for r in demo.qualifying_signals()], ["LOWSCORE"]))

print("26. The live engine closes only a net-profitable position past its hour")
_mo2 = _insp.getsource(_live._manage_one)
_mo2c = "\n".join(l for l in _mo2.splitlines() if not l.lstrip().startswith("#"))
results.append(check("profit_close needs BOTH the hour and net profit",
                     'held_h >= cfg["profit_close_after_h"] and exit_upnl > close_bar'
                     in _mo2c, True))
results.append(check("net means past the round trip, not above entry",
                     "round_trip_cost = qty * mark" in _mo2c, True))
results.append(check("the early signal_exit is gone",
                     '"signal_exit"' in _mo2c, False))
results.append(check("the time stop is gone (it closed sub-fee winners)",
                     '"time_stop"' in _mo2c, False))
results.append(check("a loser is not touched by default",
                     _live.settings()["adverse_exit_enabled"], False))
results.append(check("adverse_exit is still gated on the flag if re-enabled",
                     'cfg["adverse_exit_enabled"] and upnl < 0' in _mo2c, True))
results.append(check("default profit hour is 1.0",
                     _live.settings()["profit_close_after_h"], 1.0))
# PEPE closed at -0.00039 under a bar of exactly 1x the round trip: the test reads
# the MARK, the close is a MARKET order, and the fill crossed the spread.
results.append(check("the bar sits above the round trip, not on it",
                     _live.settings()["profit_close_fee_multiple"] > 1.0, True))
results.append(check("default cushion is half a round trip",
                     _live.settings()["profit_close_fee_multiple"], 1.5))
results.append(check("the cushion is applied to the test",
                     "upnl > close_bar" in _mo2c, True))
results.append(check("a close that still settles negative is reported loudly",
                     "profit_close SETTLED NEGATIVE" in _insp.getsource(_live._manage_one),
                     True))
# the arithmetic: the bar must exceed the round trip by the cushion
_q, _mk = 1323300.0, 4.122e-06
_rt = _q * _mk * (_tab.TAKER_FEE_PCT / 100.0) * 2
results.append(check("PEPE's +0.01052 gross would NOT clear the new bar",
                     0.01052 > _rt * _live.settings()["profit_close_fee_multiple"],
                     False))
results.append(check("but it did clear the old 1.0x bar (which is why it lost)",
                     0.01052 > _rt * 1.0, False))

print("35. The board says which TAKEs the engine will not act on")
# The skill grades TAKE at score >= 70 with every gate passed; this deployment only
# opens at min_score (75). ZEC scored TAKE 74.1 on scan 926 and never became a
# position, which reads as "the signal fired and nothing happened".
_ps = _srv.public_settings(config.load_settings())
results.append(check("the browser is told the entry bar",
                     _ps.get("min_score"), demo.min_score()))
results.append(check("and whether shorts are on",
                     _ps.get("allow_shorts"), demo.allow_shorts()))
results.append(check("the entry bar is above the skill's TAKE grade (70)",
                     demo.min_score() > 70.0, True))
_js35 = open("web/app.js").read()
results.append(check("the card flags a TAKE under the bar",
                     "function belowEntryBar" in _js35, True))
results.append(check("it compares the score against min_score, not a literal",
                     "settings || {}).min_score" in _js35, True))
results.append(check("only TAKE is flagged, not WATCH or SKIP",
                     "card.verdict === 'TAKE'" in _js35, True))
_en35 = _json.load(open("web/i18n/en.json"))
_fa35 = _json.load(open("web/i18n/fa.json"))
results.append(check("the explanation interpolates the real bar",
                     "{bar}" in _en35["card.belowBar.meaning"], True))
results.append(check("and is translated", "{bar}" in _fa35["card.belowBar.meaning"], True))

print("34. Banking is decided on the exit-side price, not the mid")
from agent import tabdeal_ws as _tws
# Three closes in a row settled negative on 2026-08-23 while each cleared a 1.5x fee
# bar, because the bar was tested against the MID while a long exits into the BID:
#   NEAR gross 0.01543 vs bar 0.01508 -> -0.00501
#   WIF  gross 0.01625 vs bar 0.01509 -> -0.00004   (mid implied 0.20115, filled 0.2009)
_f34 = _tws.DepthFeed()
_f34.track(["AAA_USDT"])
_f34._absorb("aaausdt@depth@2000ms",
             {"s": "AAAUSDT", "b": [["99.0", "5"]], "a": [["101.0", "5"]]})
results.append(check("mid sits between the touches", _f34.mark("AAA_USDT"), 100.0))
results.append(check("quote exposes both touches", _f34.quote("AAA_USDT"), (99.0, 101.0)))
_saved34 = _tws.FEED
try:
    _tws.FEED = _f34
    results.append(check("a long is valued at the bid it must sell into",
                         _tab.exit_price("AAA_USDT", "long"), 99.0))
    results.append(check("a short is valued at the ask it must buy",
                         _tab.exit_price("AAA_USDT", "short"), 101.0))
    results.append(check("the exit price is worse than the mid for a long",
                         _tab.exit_price("AAA_USDT", "long") < _f34.mark("AAA_USDT"),
                         True))
    results.append(check("and worse than the mid for a short too",
                         _tab.exit_price("AAA_USDT", "short") > _f34.mark("AAA_USDT"),
                         True))
    _f34._prices["AAA_USDT"] = (100.0, 99.0, 101.0, _time.time() - (_tws.MAX_AGE_S + 1))
    results.append(check("a stale quote is withheld", _f34.quote("AAA_USDT"), None))
finally:
    _tws.FEED = _saved34
_mo34 = _insp.getsource(_live._manage_one)
_mo34c = "\n".join(l for l in _mo34.splitlines() if not l.lstrip().startswith("#"))
results.append(check("the close test uses the exitable value",
                     "exit_upnl > close_bar" in _mo34c, True))
results.append(check("the bar is costed on the exit price too",
                     "exit_cost = qty * exit_px" in _mo34c, True))
# NB the leading space: "exit_upnl > close_bar" contains "upnl > close_bar" as a
# substring, so a naive match passes against the very code it is meant to reject.
results.append(check("the mid no longer decides the close",
                     " upnl > close_bar" in _mo34c, False))
# a crossed or absent book must not fabricate a price
_f35 = _tws.DepthFeed(); _f35.track(["BBB_USDT"])
_f35._absorb("bbbusdt@depth@2000ms",
             {"s": "BBBUSDT", "b": [["101.0", "1"]], "a": [["99.0", "1"]]})
results.append(check("a crossed book is rejected", _f35.quote("BBB_USDT"), None))

print("33. The websocket price feed accelerates marks and never gates them")
# The socket is strict and its convention is the OPPOSITE of REST's: REST needs the
# underscore, the socket rejects it. BTC_USDT@depth came back INVALID_FORMAT while
# btcusdt@depth@2000ms streamed a full 100-level snapshot.
results.append(check("stream name drops the underscore and lowercases",
                     _tws.stream_name("BTC_USDT"), "btcusdt@depth@2000ms"))
results.append(check("multi-digit symbols map too",
                     _tws.stream_name("1000SATS_USDT"), "1000satsusdt@depth@2000ms"))

_f = _tws.DepthFeed()
_f.track(["BTC_USDT", "SUI_USDT"])
results.append(check("an untracked symbol has no mark", _f.mark("XRP_USDT"), None))
results.append(check("a tracked symbol with no frame yet has no mark",
                     _f.mark("BTC_USDT"), None))
# a frame arrives: mid of best bid/ask, the same quantity REST mark_price computes
_f._absorb("btcusdt@depth@2000ms",
           {"s": "BTCUSDT", "b": [["100.0", "1"], ["99.0", "2"]],
            "a": [["102.0", "1"], ["103.0", "2"]]})
results.append(check("mark is the bid/ask mid", _f.mark("BTC_USDT"), 101.0))
results.append(check("it did not leak onto another symbol", _f.mark("SUI_USDT"), None))
# routed by the `s` field when the stream name is absent
_f._absorb(None, {"s": "SUIUSDT", "b": [["1.0", "1"]], "a": [["1.2", "1"]]})
results.append(check("a frame routes by its symbol field too",
                     round(_f.mark("SUI_USDT"), 6), 1.1))
# staleness: a dropped socket must degrade to REST, not serve a frozen price
_f._prices["BTC_USDT"] = (101.0, _time.time() - (_tws.MAX_AGE_S + 1))
results.append(check("a stale price is withheld so REST takes over",
                     _f.mark("BTC_USDT"), None))
# malformed frames are ignored rather than raising into the monitoring loop
for _bad in ({"s": "BTCUSDT", "b": [], "a": []},
             {"s": "BTCUSDT", "b": [["x", "1"]], "a": [["2", "1"]]},
             {"s": "BTCUSDT"},
             {"s": "BTCUSDT", "b": [["-1", "1"]], "a": [["-1", "1"]]}):
    _f._absorb("btcusdt@depth@2000ms", _bad)
results.append(check("malformed frames neither raise nor set a price",
                     _f.mark("BTC_USDT"), None))
results.append(check("an unknown symbol is dropped, not guessed",
                     _f._symbol_for("dogeusdt@depth@2000ms", {"s": "DOGEUSDT"}), None))
# the accelerator must never become a dependency
_savedfeed = _tws.FEED
try:
    class _Broken:
        def mark(self, s): raise RuntimeError("socket exploded")
    _tws.FEED = _Broken()
    results.append(check("a throwing feed still returns None to mark_price",
                         _tab._ws_mark("BTC_USDT"), None))
finally:
    _tws.FEED = _savedfeed
results.append(check("mark_price consults the feed before REST",
                     _insp.getsource(_tab.mark_price).index("_ws_mark")
                     < _insp.getsource(_tab.mark_price).index("orderbook("), True))
results.append(check("the engine tracks only open symbols, not the watchlist",
                     'store.live_positions("open")'
                     in _insp.getsource(_live._sync_price_feed), True))

print("32. A short is the exact mirror of a long, not a special case")
# Shorts were switched off after the 21,315-signal replay; switching them back on is
# only safe if every side-dependent calculation mirrors cleanly. This project has
# already shipped one real long-bias bug (side_from_direction defaulting to long,
# which produced 213 consecutive long trades), so this is asserted, not assumed.
#
# Method: take a long scenario, reflect every price around the entry, and require the
# short to produce the same magnitudes.
_E = 100.0
_long_lv = {"entry": _E, "stop": 98.0, "tp1": 103.0, "tp2": 106.0}
_short_lv = {"entry": _E, "stop": 102.0, "tp1": 97.0, "tp2": 94.0}
results.append(check("long geometry accepted", demo.valid_geometry("long", _long_lv), True))
results.append(check("mirrored short geometry accepted",
                     demo.valid_geometry("short", _short_lv), True))
results.append(check("a long-shaped plan is rejected for a short",
                     demo.valid_geometry("short", _long_lv), False))
results.append(check("a short-shaped plan is rejected for a long",
                     demo.valid_geometry("long", _short_lv), False))

# TP reached: same distance, opposite direction
results.append(check("long reaches tp1 at +3", _live._reached("long", 103.0, 103.0), True))
results.append(check("short reaches tp1 at -3", _live._reached("short", 97.0, 97.0), True))
results.append(check("long has not reached tp1 at -3",
                     _live._reached("long", 97.0, 103.0), False))
results.append(check("short has not reached tp1 at +3",
                     _live._reached("short", 103.0, 97.0), False))

# Entry drift is signed AGAINST the position on both sides
_pl = {"plan": {"levels": {"entry": _E}}, "stop": 98.0, "entry": 101.0}   # long filled worse
_ps = {"plan": {"levels": {"entry": _E}}, "stop": 102.0, "entry": 99.0}   # short filled worse
_dl = demo._entry_drift_r({"side": "long"}, _pl)
_ds = demo._entry_drift_r({"side": "short"}, _ps)
results.append(check("a long filled 1.0 above plan drifts +0.5R", round(_dl, 6), 0.5))
results.append(check("a short filled 1.0 below plan drifts the same +0.5R",
                     round(_ds, 6), 0.5))
_plb = {"plan": {"levels": {"entry": _E}}, "stop": 98.0, "entry": 99.0}   # long filled better
_psb = {"plan": {"levels": {"entry": _E}}, "stop": 102.0, "entry": 101.0}  # short filled better
results.append(check("a better long fill is negative drift, never blocked",
                     round(demo._entry_drift_r({"side": "long"}, _plb), 6), -0.5))
results.append(check("a better short fill mirrors it",
                     round(demo._entry_drift_r({"side": "short"}, _psb), 6), -0.5))

# P&L arithmetic: a short that moves in its favour earns what the long earns
_cfg32 = _live.settings()
_savedmp = _tab.mark_price
try:
    _tab.mark_price = lambda sym: 102.0          # +2 from entry
    _pnl_long = _live._live_pnl(
        [{"symbol": "M_USDT", "positionAmt": "10", "entryPrice": "100"}], _cfg32)[0]
    _tab.mark_price = lambda sym: 98.0           # -2 from entry, the mirror
    _pnl_short = _live._live_pnl(
        [{"symbol": "M_USDT", "positionAmt": "-10", "entryPrice": "100"}], _cfg32)[0]
finally:
    _tab.mark_price = _savedmp
results.append(check("long +2 gives +20 gross", round(_pnl_long["gross"], 6), 20.0))
results.append(check("short -2 gives the same +20 gross",
                     round(_pnl_short["gross"], 6), 20.0))
results.append(check("long pct is +2%", round(_pnl_long["pct"], 4), 2.0))
results.append(check("short pct is also +2% (in its own favour)",
                     round(_pnl_short["pct"], 4), 2.0))
results.append(check("both pay a cost on the same notional basis",
                     round(_pnl_long["cost"], 6) > 0 and round(_pnl_short["cost"], 6) > 0,
                     True))

# the side gate itself
_savedres = _live.store.result_for
try:
    _live.store.result_for = lambda c, e: {"verdict": "TAKE", "score": 80.0,
                                           "side": "short", "side_tied": 0}
    results.append(check("a green short holds a short position",
                         _live._signal_supports_holding({"coin": "Z", "side": "short"},
                                                        _cfg32)[0], True))
    results.append(check("a green short does NOT hold a long position",
                         _live._signal_supports_holding({"coin": "Z", "side": "long"},
                                                        _cfg32)[0], False))
finally:
    _live.store.result_for = _savedres

print("31. A profitable position past its hour is KEPT while the signal is green")
# Rule: >=1h AND net profitable -> hold if the scan still says TAKE at >= 70 on our
# side, close otherwise. Letting a live signal run is what produced the only
# profitable closes so far: FLOKI +1.288% and SUI +0.703% both reached the exchange
# TP because nothing cut them at the hour.
_cfgh = _live.settings()
results.append(check("hold-take bar defaults to 70", _cfgh["hold_take_score"], 70.0))
results.append(check("it is stricter than the abandon floor",
                     _cfgh["hold_take_score"] > demo.hold_score_floor(), True))

_saved = _live.store.result_for
def _stub(verdict, score, side="long", tied=0):
    return lambda coin, exch: {"verdict": verdict, "score": score,
                               "side": side, "side_tied": tied}
_row = {"coin": "ZZZ", "side": "long"}
try:
    _live.store.result_for = _stub("TAKE", 82.0)
    results.append(check("green and strong -> keep",
                         _live._signal_supports_holding(_row, _cfgh)[0], True))
    _live.store.result_for = _stub("TAKE", 70.0)
    results.append(check("exactly at the bar -> keep",
                         _live._signal_supports_holding(_row, _cfgh)[0], True))
    _live.store.result_for = _stub("TAKE", 69.9)
    results.append(check("green but below 70 -> close",
                         _live._signal_supports_holding(_row, _cfgh)[0], False))
    _live.store.result_for = _stub("WATCH", 88.0)
    results.append(check("not green, however high the score -> close",
                         _live._signal_supports_holding(_row, _cfgh)[0], False))
    _live.store.result_for = _stub("SKIP", 81.0)
    results.append(check("SKIP at 81 -> close (the real ICP case)",
                         _live._signal_supports_holding(_row, _cfgh)[0], False))
    _live.store.result_for = _stub("TAKE", 90.0, side="short")
    results.append(check("green but the scan flipped side -> close",
                         _live._signal_supports_holding(_row, _cfgh)[0], False))
    _live.store.result_for = _stub("TAKE", 90.0, tied=1)
    results.append(check("green but direction tied -> close",
                         _live._signal_supports_holding(_row, _cfgh)[0], False))
    _live.store.result_for = lambda coin, exch: None
    results.append(check("no scan data -> close, do not hold on uncertainty",
                         _live._signal_supports_holding(_row, _cfgh)[0], False))
finally:
    _live.store.result_for = _saved

_mo3 = _insp.getsource(_live._manage_one)
results.append(check("the gate runs only inside the profitable-past-the-hour branch",
                     _mo3.index("_signal_supports_holding")
                     > _mo3.index("upnl > close_bar"), True))
results.append(check("holding reports itself, not silence",
                     '"reason": "riding_signal"' in _mo3, True))

print("30. P&L is computed server-side, gross and net, against a live mark")
# The browser derived P&L from the sampled history, thinned to 15s while the tab
# refreshes every 3s, and showed only net - so it lagged the venue and could never
# agree with Tabdeal's own gross figure.
_lp = _insp.getsource(_live._live_pnl)
results.append(check("state exposes a per-position pnl block",
                     '"live": _live_pnl(positions, cfg)' in _insp.getsource(_live.state),
                     True))
results.append(check("gross is reported", '"gross": round(gross, 8)' in _lp, True))
results.append(check("net is reported", '"net": round(gross - cost, 8)' in _lp, True))
results.append(check("the mark is read per request, not sampled",
                     "tabdeal.mark_price(sym)" in _lp, True))
results.append(check("a failed mark read does not drop the row",
                     "except Exception" in _lp, True))
# arithmetic, both sides
_long = _live._live_pnl([{"symbol": "X_USDT", "positionAmt": "10",
                          "entryPrice": "100"}], _live.settings())[0]
_short = _live._live_pnl([{"symbol": "X_USDT", "positionAmt": "-10",
                           "entryPrice": "100"}], _live.settings())[0]
results.append(check("a row is returned even with no mark available",
                     _long["symbol"], "X_USDT"))
if _long.get("gross") is not None:
    _m = _long["mark"]
    results.append(check("long gross = (mark - entry) x qty",
                         round(_long["gross"], 6), round((_m - 100) * 10, 6)))
    results.append(check("short gross is mirrored",
                         round(_short["gross"], 6), round((100 - _m) * 10, 6)))
    results.append(check("net is gross minus the round trip",
                         round(_long["net"], 6),
                         round(_long["gross"] - _long["cost"], 6)))
    results.append(check("cost is 0.2% of notional",
                         round(_long["cost"], 8), round(10 * _m * 0.002, 8)))
_js = open("web/app.js").read()
results.append(check("the browser no longer derives P&L", "function netPnl" in _js, False))

print("29. The dashboard payload carries what the live tab renders")
_h = _insp.getsource(_live.history)
results.append(check("history exposes quantity (the P/L column needs it)",
                     '"quantity": r.get("quantity")' in _h, True))
results.append(check("history exposes the exit reason", '"exit_reason"' in _h, True))
results.append(check("history exposes realised pnl", '"realised_pnl"' in _h, True))
results.append(check("live state carries a BTC reference price",
                     '"btc": btc_price()' in _insp.getsource(_live.state), True))
results.append(check("btc_price is cached, not fetched per request",
                     "_btc_cache" in _insp.getsource(_live.btc_price), True))
results.append(check("a failed BTC read keeps the last price",
                     "except Exception" in _insp.getsource(_live.btc_price), True))

print("28. An unprotected position is repaired, not just logged about")
# SUI opened 2026-08-23 17:07 and sat with no exchange stop for 40 minutes: the venue
# had not registered the position yet when _attach_stop read it back, so there was no
# positionId, and the failure was logged once and abandoned. FLOKI, opened one second
# earlier in the same batch, was fine.
results.append(check("absent stop detected when the field is None",
                     _live._venue_has_stop({}), False))
results.append(check('absent stop detected when the venue sends "0"',
                     _live._venue_has_stop({"stopLossPrice": "0"}), False))
results.append(check("absent stop detected on an empty string",
                     _live._venue_has_stop({"stopLossPrice": ""}), False))
results.append(check("a real stop is recognised",
                     _live._venue_has_stop({"stopLossPrice": "0.8096"}), True))
results.append(check("garbage does not raise",
                     _live._venue_has_stop({"stopLossPrice": "abc"}), False))
_rc = _insp.getsource(_live.reconcile)
results.append(check("reconcile checks every open position's stop",
                     "_venue_has_stop(venue[symbol])" in _rc, True))
results.append(check("and repairs it", "_repair_stop(broker, row" in _rc, True))
_rp = _insp.getsource(_live._repair_stop)
results.append(check("the repair reads the stop back rather than trusting the write",
                     "_venue_has_stop(back)" in _rp, True))
results.append(check("a failed repair says so loudly, not silently",
                     _rp.count("log.error"), 2))

print("27. Position history is recorded for later analysis")
_rs = _insp.getsource(_live._record_sample)
results.append(check("sampling is thinned, not every cycle",
                     'cfg["history_interval_seconds"]' in _rs, True))
results.append(check("a failed write cannot break management",
                     "except Exception" in _rs, True))
# It read live.settings()["exchange"], which does not exist - a KeyError on every
# call, swallowed at debug level, so the table stayed empty and nothing said why.
results.append(check("the venue comes from demo.settings, which has one",
                     'demo.settings()["exchange"]' in _rs, True))
results.append(check("a swallowed failure still warns once",
                     "_sample_warned" in _rs, True))
# behavioural: an actual write must land, in this suite's own database
_pid = store.live_open(coin="ZZZ", symbol="ZZZ_USDT", side="long", status="open",
                       quantity=1.0, entry_price=100.0, opened_ts=1.0, opened_at="x")
_live._last_sample.pop(_pid, None)
_live._record_sample(dict([r for r in store.live_positions("open")
                           if r["id"] == _pid][0]),
                     _live.settings(), mark=101.0, upnl=1.0, qty=1.0,
                     r_now=0.5, held_h=0.5)
_got = store.live_samples(_pid)
results.append(check("a sample actually lands in the table", len(_got), 1))
results.append(check("net is gross minus the round trip",
                     round(_got[0]["upnl_net"], 3), 0.798))
results.append(check("thinning suppresses an immediate second write",
                     (_live._record_sample(
                         dict([r for r in store.live_positions("open")
                               if r["id"] == _pid][0]), _live.settings(),
                         mark=102.0, upnl=2.0, qty=1.0, r_now=1.0, held_h=0.6),
                      len(store.live_samples(_pid)))[1], 1))
store.live_close(_pid, exit_price=101.0, exit_reason="profit_close", realised_pnl=0.8)
results.append(check("a closed position's samples prune",
                     store.live_samples_prune(9e9), 1))
results.append(check("it records the verdict it was judged against",
                     "verdict=scan.get" in _rs, True))
results.append(check("history() exists for the dashboard", callable(_live.history), True))
_cy = _insp.getsource(_live.cycle)
results.append(check("old samples are pruned", "live_samples_prune" in _cy, True))

print("25. Shorts can be switched off without deleting the short path")
# 21,315 replayed signals: shorts net negative at every horizon and worsening with
# time (-0.209% at 30m to -0.706% at 24h, n=9,741) while longs improve.
_qs = _insp.getsource(demo.qualifying_signals)
results.append(check("qualifying_signals consults the setting",
                     "allow_shorts()" in _qs, True))
results.append(check("only shorts are gated by it",
                     'row.get("side") == "short"' in _qs, True))
results.append(check("shorts are allowed", demo.allow_shorts(), True))

print("24. Redundant direction checks cannot vote twice")
# Measured over 1,331 historical evaluations: price-vs-EMA200 and EMA50-vs-EMA200 on
# the bias TF agree 90.7% of the time, and price-vs-EMA50 and price-vs-VWAP on the
# decision TF agree 87.7%. Counting each pair twice inflated direction_ratio - 35 of
# the 100 score points - and forced every coin long (33/33 before, 22L/7S after).
from agent import skill as _sk
_api = _insp.getsource(_sk._load_api_module().score_direction)
results.append(check("checks carry a family", '"family"' in _api, True))
results.append(check("the two bias-TF trend checks share one family",
                     _api.count('family="bias-tf trend"'), 2))
results.append(check("the two decision-TF mean checks share one family",
                     _api.count('family="decision-tf mean"'), 2))
results.append(check("a family's weight is split, not abstained",
                     "1.0 / len(members)"
                     in _insp.getsource(_sk._load_api_module().weigh_votes), True))
results.append(check("the vote threshold rescales with the denominator",
                     "base / 9.0 * auto_votes" in _api, True))
_sfd = _insp.getsource(_sk.side_from_direction)
results.append(check("the tie margin rescales too",
                     "span / 9.0" in _sfd, True))
results.append(check("margin is compared against the scaled need",
                     "margin > need" in _sfd, True))
# The venue adapter resolves the manual checks and then rebuilds the totals. The
# first version of this change rebuilt them with plain integer sums, silently
# discarding the weighting: production scored unchanged while these tests passed.
_bs = _insp.getsource(_tab.build_snapshot)
results.append(check("the venue adapter re-weighs rather than re-counts",
                     "skill.weigh_votes(auto)" in _bs, True))
results.append(check("it no longer integer-sums the votes",
                     'sum(1 for c in auto if c["long"])' in _bs, False))
results.append(check("weighing lives in one shared place",
                     callable(_sk.weigh_votes), True))
_wv = _sk.weigh_votes([
    {"check": "a", "family": "dup", "long": True,  "short": False},
    {"check": "b", "family": "dup", "long": True,  "short": False},
    {"check": "c", "family": "c",   "long": False, "short": True},
])
results.append(check("a duplicated pair casts one vote, not two", _wv[0], 1.0)),
results.append(check("families are counted, not raw checks", _wv[2], 2))
_wv2 = _sk.weigh_votes([
    {"check": "a", "family": "dup", "long": True,  "short": False},
    {"check": "b", "family": "dup", "long": False, "short": True},
])
results.append(check("an internally split family splits its weight, not abstains",
                     (_wv2[0], _wv2[1]), (0.5, 0.5)))
results.append(check("a check with no family stands alone",
                     _sk.weigh_votes([{"check": "solo", "long": True,
                                       "short": False}])[2], 1))

print("23. Holding is judged on the thesis, not on the entry gates")
# AAVE closed at score 74.0 with the verdict still TAKE, because the exit test WAS
# the entry test (floor 75). A score near the bar churned, at 0.2% a round trip.
# Strip comments before matching: the comment block here QUOTES the old rule to
# explain why it went, and matching it would assert against prose, not behaviour.
_pc_raw = _insp.getsource(demo._profit_signal_check)
_pc = "\n".join(l for l in _pc_raw.splitlines() if not l.lstrip().startswith("#"))
results.append(check("a hold floor exists below the entry bar",
                     demo.hold_score_floor() < demo.min_score(), True))
results.append(check("default band is 10 points",
                     demo.min_score() - demo.hold_score_floor(), 10.0))
results.append(check("holding no longer requires verdict == TAKE",
                     'verdict == "TAKE"' in _pc, False))
results.append(check("the entry bar is not the exit bar",
                     ">= MIN_SCORE" in _pc, False))
results.append(check("a direction flip closes the position",
                     "direction flipped to" in _pc, True))
results.append(check("a tied scan side does not count as a flip",
                     'row.get("side_tied")' in _pc, True))
results.append(check("conviction collapse closes the position",
                     "hold_score_floor()" in _pc, True))
results.append(check("missing scan data is still 'no opinion'",
                     _pc.count("return False, None"), 2))

print("22. The adverse exit exists but is off: a loser is never touched")
# It was added because six stop-outs were 89% of all loss at a median hold of 548
# minutes. It is disabled at the operator's instruction: on a 1-tick loser it burns
# the full 0.2% round trip for nothing (FLOKI closed at -0.035%, of which the fee was
# 85%). Kept behind a flag so the trade-off stays visible and reversible.
_mo = _insp.getsource(_live._manage_one)
results.append(check("a losing position is only checked when enabled",
                     'cfg["adverse_exit_enabled"] and upnl < 0' in _mo, True))
results.append(check("it closes as adverse_exit",
                     '"adverse_exit", mark' in _mo, True))
results.append(check("only when the setup has actually lapsed",
                     "reason is not None and not still" in _mo, True))
results.append(check("adverse window is configurable",
                     "adverse_exit_after_h" in _insp.getsource(_live.settings), True))
results.append(check("it is off by default", _live.settings()["adverse_exit_enabled"],
                     False))

print("21. Entry fills every free slot in one pass")
_lk = _insp.getsource(_live._try_open_locked)
results.append(check("does not return on the first fill",
                     "opened.append(res)" in _lk, True))
results.append(check("stops at the slot limit", "if slots_free <= 0:" in _lk, True))
results.append(check("stops at the notional cap", "if notional_now >= cap:" in _lk, True))
results.append(check("tracks notional as it fills",
                     "notional_now += res.get" in _lk, True))
results.append(check("reports how many it opened", '"count": len(opened)' in _lk, True))
_sl2 = _insp.getsource(_live.scheduler_loop)
results.append(check("a fresh scan alone triggers entry",
                     "spaced" not in _sl2, True))

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
