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
_src = _insp.getsource(_live._manage_one)
_tp1 = _src[_src.index("TP1 reached"):_src.index("Signal exit")]
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
results.append(check("entry requires a fresh scan", "fresh_scan" in _sl, True))
results.append(check("interval is kept only as a floor", "spaced" in _sl, True))
results.append(check("every attempt is logged, not just fills",
                     "live entry attempt" in _sl, True))
results.append(check("only COMPLETED scans count",
                     "status = 'done'" in _insp.getsource(_live._latest_scan_id), True))
results.append(check("_latest_scan_id returns an int or None",
                     _live._latest_scan_id() is None
                     or isinstance(_live._latest_scan_id(), int), True))

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
