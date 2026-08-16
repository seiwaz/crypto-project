"""The demo trading agent: slots, portfolio heat, and the monitoring cycle.

This is the layer between the screener's verdicts and the paper broker. It decides
which qualifying signals become positions, how many may be open at once, and what
happens to each one on every cycle.

Two rules shape almost all of it:

* **An empty slot is a legitimate state.** Filling five slots is not the objective;
  taking only trades that qualify is. If a sixth signal would breach the heat cap, or
  no signal qualifies, the slot stays empty and the UI says why. Relaxing the
  threshold to reach "always 5" would defeat the gates that make the number mean
  anything.
* **Nothing here invents a number.** Marks, funding and candles come from Toobit;
  entries, stops, targets and sizing come from the skill's planner. When an input is
  missing the answer is "no data", never a plausible substitute.

There is no code path in this module that can submit an order anywhere. It reads
market data through the read-only allowlist and writes to the local database.
"""

from __future__ import annotations

import json
import logging

from . import config, paper, skill, store, toobit

log = logging.getLogger("demo")

DEFAULT_SLOTS = 5
DEFAULT_HEAT_CAP_PCT = 6.0
MIN_SCORE = 70.0

# Same-direction positions in coins that move together are one position wearing
# several tickers. Capping them needs a correlation figure, which comes from the
# skill's market_context.py; until that is installed the cap cannot be enforced and
# the UI has to say so rather than quietly filling five correlated slots.
MAX_CORRELATED_SAME_SIDE = 2
CORRELATION_THRESHOLD = 0.9


DEFAULT_CYCLE_SECONDS = 90

# ---------------------------------------------------------------------------
# Management policy, transcribed from the skill's Step 9.
#
# These rules belong in the skill's position_manager.py, which is not installed.
# They are kept here as a single named block, with the skill's own wording quoted,
# so that swapping them for the real script later is a deletion rather than a hunt
# through the cycle logic. Every plan also carries its own `management` strings, and
# a plan's numbers always win over these defaults.
#
#   "On TP1: close 50%, stop to breakeven plus accumulated costs"
#   "Time stop: ~6 decision-TF candles (scalp) or ~12 (intraday) below 0.5R"
#   "Trail behind new swing points on the decision TF, not a tight indicator"
#   "Never widen a stop"
#   "Circuit breaker: 2 losses or -3% equity (scalp), 3 losses or -5% (intraday)"
# ---------------------------------------------------------------------------

TP1_CLOSE_FRACTION = 0.5

# Time stop, in hours per profile.
#
# The skill's guidance is ~12 decision-timeframe candles, which on the intraday
# profile is 48 hours. That was too long to be useful here, so these are shorter by
# request: a quarter of the skill's window on intraday. This is a deliberate departure
# from the skill's number, not an implementation of it, and `demo.time_stop_hours`
# overrides it outright.
TIME_STOP_HOURS = {"scalp": 3.0, "intraday": 12.0, "swing": 48.0}

# The trade must have made at least this much to survive the time stop. Expressed as
# a fraction of the position's own risk so it scales with capital, but every
# comparison happens in USDT — see `time_stop_floor_usdt`.
TIME_STOP_MIN_FRACTION = 0.5

# (consecutive losses, equity drawdown as a fraction of starting capital)
CIRCUIT_BREAKER = {"scalp": (2, 0.03), "intraday": (3, 0.05), "swing": (3, 0.05)}

# Decision-timeframe labels to minutes, for counting bars held.
_TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
               "1H": 60, "2H": 120, "4H": 240, "6H": 360, "12H": 720,
               "1D": 1440, "3D": 4320, "1W": 10080}


def scheduler_loop(stop_event) -> None:
    """Run the monitoring cycle on a timer for as long as the server lives.

    The demo is meant to trade by itself — a position that only gets marked when
    somebody opens the tab would miss the stop it should have exited on, and the
    journal would record an exit at whatever price the page happened to be loaded
    at. The loop is what makes the record honest.

    Errors are logged and swallowed: a dropped VPN or a rate limit must not kill the
    loop and silently freeze every open position.
    """
    while not stop_event.is_set():
        cfg = settings()
        if cfg["enabled"]:
            try:
                out = cycle()
                closed = [r for r in out["results"] if r.get("action") == "CLOSE"]
                # Refill in the same pass, so a freed slot is taken by the current
                # highest-scoring candidate rather than waiting for the next cycle.
                filled = try_fill_slots()
                if closed or filled["opened"]:
                    log.info("demo cycle: %d closed (%s), %d opened (%s)",
                             len(closed), ", ".join(f"{c['coin']}:{c['reason']}"
                                                    for c in closed) or "-",
                             len(filled["opened"]),
                             ", ".join(o["coin"] for o in filled["opened"]) or "-")
                persist_reports()
            except Exception as exc:                          # noqa: BLE001
                log.warning("demo cycle failed: %s", exc)
        stop_event.wait(cfg.get("cycle_seconds") or DEFAULT_CYCLE_SECONDS)


def persist_reports() -> None:
    """Write the journal, the report and one equity sample, every cycle.

    Three files, because they answer different questions and have different
    lifetimes:

      var/journal.txt    the current picture, human-readable, overwritten
      var/report.json    the same figures as data, for the optimisation pass later
      var/history.jsonl  one line per cycle, appended — the equity curve, and the
                         only record of what the account looked like *between*
                         closes. A report built solely from closed trades cannot
                         show a drawdown that recovered before anything exited.

    Failures here are logged and swallowed: reporting must never be able to stop
    the trading loop it is reporting on.
    """
    from . import journal as journal_mod, report as report_mod   # noqa: PLC0415

    try:
        snapshot = state()
        rep = report_mod.build()
        acct = snapshot["account"]
        agg = rep["aggregate"]

        config.VAR_DIR.mkdir(parents=True, exist_ok=True)
        (config.VAR_DIR / "journal.txt").write_text(journal_mod.text() + "\n",
                                                    encoding="utf-8")
        (config.VAR_DIR / "report.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")

        line = {
            "at": store.now_iso(),
            "equity": round(acct["equity"], 4),
            "balance": round(acct["balance"], 4),
            "unrealised": round(acct["open_pnl"], 4),
            "open": len(snapshot["positions"]),
            "closed": agg["closed"],
            "win_rate": agg["win_rate"],
            "net_pnl": agg["net_pnl"],
            "heat_usdt": round(snapshot["heat"]["used_usdt"], 4),
        }
        with (config.VAR_DIR / "history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception as exc:                                  # noqa: BLE001
        log.warning("could not persist reports: %s", exc)


def settings() -> dict:
    s = config.load_settings()
    demo = s.get("demo") or {}
    return {
        "slots": int(demo.get("slots") or DEFAULT_SLOTS),
        "heat_cap_pct": float(demo.get("heat_cap_pct") or DEFAULT_HEAT_CAP_PCT),
        "capital": float(demo.get("capital") or s.get("capital") or 1000.0),
        "exchange": demo.get("exchange") or s.get("exchange") or "toobit",
        "enabled": bool(demo.get("enabled")),
        "cycle_seconds": int(demo.get("cycle_seconds") or DEFAULT_CYCLE_SECONDS),
        # The profile the user has selected, which is the one the whole app trades.
        # Management reads this rather than the profile frozen into an old plan, so
        # switching to scalp in the header shortens the time stop on positions that
        # are already open instead of only on the next scan's.
        "profile": s.get("profile") or "intraday",
        "time_stop_hours": demo.get("time_stop_hours"),
        "time_stop_min_profit_usdt": demo.get("time_stop_min_profit_usdt"),
    }


def time_stop_hours() -> float:
    """How long a position may go nowhere, in hours, for the selected profile."""
    cfg = settings()
    explicit = cfg.get("time_stop_hours")
    if explicit:
        return float(explicit)
    return TIME_STOP_HOURS.get(cfg["profile"], TIME_STOP_HOURS["intraday"])


def time_stop_floor_usdt(pos: dict) -> float:
    """The profit a position must have shown, in USDT, to survive the time stop.

    Every condition the agent acts on is compared in USDT. R is convenient to reason
    about but it is not what the account holds, and mixing the two is how a threshold
    ends up meaning something different from what it reads as.
    """
    cfg = settings()
    explicit = cfg.get("time_stop_min_profit_usdt")
    if explicit is not None:
        return float(explicit)
    return TIME_STOP_MIN_FRACTION * float(pos.get("risk_amount") or 0.0)


def ensure_account() -> dict:
    acct = store.paper_account()
    if acct:
        return acct
    cfg = settings()
    return store.paper_init(exchange=cfg["exchange"], capital=cfg["capital"],
                            slots=cfg["slots"], heat_cap_pct=cfg["heat_cap_pct"])


# --------------------------------------------------------------------------------
# Risk accounting
# --------------------------------------------------------------------------------


def open_risk(pos: dict, spec: dict) -> float:
    """Money still at risk if this position stops out, in USDT.

    Once the stop is at or beyond breakeven the position can no longer lose the
    account anything, so it stops consuming heat. Counting it forever would jam the
    portfolio at its cap with trades that are already safe.
    """
    stop = pos.get("stop")
    if stop is None:
        return 0.0
    entry = float(pos["entry_price"])
    q = paper.coins(float(pos["contracts"]), spec)
    risk = (entry - float(stop)) * q if pos["side"] == "long" else (float(stop) - entry) * q
    return max(0.0, risk)


def portfolio_heat(positions: list[dict], specs: dict, equity: float) -> float:
    """Sum of open risk as a percentage of equity — the number the cap applies to."""
    if equity <= 0:
        return 0.0
    total = sum(open_risk(p, specs[p["symbol"]]) for p in positions
                if p["symbol"] in specs)
    return total / equity * 100.0


# --------------------------------------------------------------------------------
# Account state
# --------------------------------------------------------------------------------


def state() -> dict:
    """Everything the demo tab renders, marked to live Toobit prices."""
    acct = ensure_account()
    positions = store.paper_open_positions()

    specs, rows, stale = {}, [], []
    used_margin = 0.0
    open_pnl = 0.0

    for pos in positions:
        symbol = pos["symbol"]
        try:
            spec = specs.get(symbol) or paper.contract_spec(symbol)
        except paper.PaperError as exc:
            stale.append({"coin": pos["coin"], "reason": str(exc)})
            continue
        specs[symbol] = spec
        mark, source = paper.mark_price(spec)
        used_margin += float(pos["margin"])
        row = dict(pos)
        if mark is None:
            # No mark means no honest PnL. Show the position with its costs and say
            # the mark is missing, rather than valuing it at its entry price — that
            # would read as "flat" when it is really "unknown".
            row["state"] = None
            row["mark_source"] = source
        else:
            st = paper.position_state(pos, spec, mark)
            row["state"] = st
            row["mark_source"] = source
            open_pnl += st["unrealised_pnl"]
        row["open_risk"] = open_risk(pos, spec)
        row["funding"] = paper.funding(symbol)
        rows.append(row)

    try:
        last = toobit.last_prices_for(r["symbol"] for r in rows)
    except toobit.ToobitError:
        last = {}
    for row in rows:
        row["last_price"] = last.get(row["symbol"])

    balance = float(acct["balance"])
    equity = balance + open_pnl
    heat = portfolio_heat(positions, specs, equity)
    slots = int(acct["slots"])

    return {
        "account": {
            "exchange": acct["exchange"],
            "starting_capital": float(acct["starting_capital"]),
            "balance": balance,
            "equity": equity,
            "open_pnl": open_pnl,
            "used_margin": used_margin,
            "available_margin": max(0.0, balance - used_margin),
            "return_pct": ((equity / float(acct["starting_capital"]) - 1.0) * 100.0
                           if float(acct["starting_capital"]) else None),
        },
        "slots": {
            "total": slots,
            "filled": len(rows),
            "empty": max(0, slots - len(rows)),
            "reason": _empty_slot_reason(len(rows), slots, heat, acct),
        },
        # Heat in USDT as well as percent. The cap is defined as a share of equity, so
        # the percentage is the rule; the USDT figure is what it actually means.
        "heat": {
            "used_pct": heat,
            "cap_pct": float(acct["heat_cap_pct"]),
            "headroom_pct": max(0.0, float(acct["heat_cap_pct"]) - heat),
            "used_usdt": sum(open_risk(p, specs[p["symbol"]]) for p in positions
                             if p["symbol"] in specs),
            "cap_usdt": equity * float(acct["heat_cap_pct"]) / 100.0,
        },
        "strategy": {
            "profile": settings()["profile"],
            "time_stop_hours": time_stop_hours(),
            "time_stop_floor_usdt": (time_stop_floor_usdt(positions[0])
                                     if positions else None),
        },
        "positions": rows,
        "stale": stale,
        "correlation_filter": _correlation_filter_status(),
    }


def _empty_slot_reason(filled: int, slots: int, heat: float, acct: dict) -> dict | None:
    """Why a slot is empty — never left to the reader to guess.

    "4 of 5 slots filled" with no explanation invites the assumption that something
    is broken. The distinction that matters is between "nothing qualified" and "the
    heat cap stopped it", because only the first is about the market.
    """
    if filled >= slots:
        return None
    if heat >= float(acct["heat_cap_pct"]):
        return {"code": "heat_cap", "heat": heat, "cap": float(acct["heat_cap_pct"])}
    pool = qualifying_signals()
    if not pool:
        return {"code": "no_qualifying_signal"}

    # There are candidates, so something stopped them. The last fill attempt knows
    # what; recomputing it here would mean a network call per candidate on every page
    # load. Reporting "awaiting fill" when the real answer is "no margin" would send
    # the reader looking at the market instead of at the sizing.
    last = store.get_kv("demo.last_fill") or {}
    if last.get("circuit_breaker"):
        return {"code": "circuit_breaker", "detail": last["circuit_breaker"]}
    blocked = last.get("declined") or []
    if blocked:
        top = blocked[0]
        return {"code": top.get("code", "declined"), "candidates": len(pool),
                "detail": top}
    return {"code": "awaiting_fill", "candidates": len(pool)}


def _correlation_filter_status() -> dict:
    """The correlation filter needs `btc_context`, which market_context.py provides.

    Reported rather than silently skipped: five 0.9-correlated same-direction
    positions are one position at five times the size, and it produces a single
    catastrophic drawdown that looks like five independent failures.
    """
    from . import skill  # noqa: PLC0415 — avoids a cycle at import time
    available = (skill.scripts_dir() / "market_context.py").exists()
    return {
        "available": available,
        "threshold": CORRELATION_THRESHOLD,
        "max_same_side": MAX_CORRELATED_SAME_SIDE,
        "reason": None if available else "market_context_missing",
    }


# --------------------------------------------------------------------------------
# Candidate selection
# --------------------------------------------------------------------------------


def qualifying_signals() -> list[dict]:
    """Signals eligible to take a slot: TAKE, score at or above the floor, not open.

    Ranking is by score. The spec's tie-break on `btc_context.alpha_pct` — coin
    strength rather than a rising tide — needs the context run, so while that is
    missing ties keep their scan order instead of being broken on a guess.
    """
    cfg = settings()
    open_coins = {p["coin"] for p in store.paper_open_positions()}
    closed_at = store.paper_last_close_times()
    out = []
    for row in store.latest_results(cfg["exchange"]):
        if row.get("verdict") != "TAKE":
            continue
        score = row.get("score")
        if score is None or float(score) < MIN_SCORE:
            continue
        if row["coin"] in open_coins:
            continue
        # Re-enter only on fresh evidence.
        #
        # A slot frees the instant a position closes, and the scan row that opened it
        # is still sitting there rated TAKE. Without this the agent would reopen the
        # exact trade that just stopped out, at a worse price, on evidence the market
        # has already falsified — and it would do it every cycle until the next scan.
        # Requiring a scan newer than the close means a re-entry is a new signal, not
        # an echo of the failed one.
        last = closed_at.get(row["coin"])
        if last and str(row.get("fetched_at") or "") <= str(last):
            continue
        out.append(row)
    out.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
    return out


# --------------------------------------------------------------------------------
# Monitoring cycle
# --------------------------------------------------------------------------------


def cycle() -> dict:
    """One monitoring pass over every open position.

    Accrues funding, marks to market, records MFE/MAE, and closes anything that hit a
    level. Exits are detected on the most recent candle's range as well as the current
    mark, because a poll every few minutes would otherwise miss a wick that a real
    venue would have triggered on.
    """
    acct = ensure_account()
    results = []
    balance = float(acct["balance"])

    for pos in store.paper_open_positions():
        try:
            outcome = _cycle_one(pos)
        except (paper.PaperError, toobit.ToobitError) as exc:
            log.warning("cycle failed for %s: %s", pos["coin"], exc)
            results.append({"coin": pos["coin"], "error": str(exc)})
            continue
        balance += outcome.pop("balance_delta", 0.0)
        results.append(outcome)

    store.paper_set_balance(balance)
    return {"checked": len(results), "results": results}


def _plan_of(pos: dict) -> dict:
    try:
        return json.loads(pos["plan_json"]) if pos.get("plan_json") else {}
    except (TypeError, ValueError):
        return {}


def decision_tf(plan: dict) -> str:
    return ((plan.get("timeframes") or {}).get("decision")) or "4H"


def bars_held(pos: dict, plan: dict) -> int:
    minutes = _TF_MINUTES.get(decision_tf(plan), 240)
    elapsed_min = (paper.now_ts() - float(pos["opened_ts"])) / 60.0
    return int(elapsed_min // minutes)


def _cycle_one(pos: dict) -> dict:
    spec = paper.contract_spec(pos["symbol"])
    mark, mark_source = paper.mark_price(spec)
    if mark is None:
        return {"coin": pos["coin"], "action": "SKIP", "reason": "no mark price"}

    plan = _plan_of(pos)
    balance_delta = _accrue_funding(pos, spec, mark)
    st = paper.position_state(pos, spec, mark)
    _record_excursions(pos, st)

    held = bars_held(pos, plan)
    if held != int(pos.get("bars_held") or 0):
        store.paper_update(pos["id"], bars_held=held)
        pos["bars_held"] = held

    candle = _latest_candle(pos["symbol"])
    high = max(candle["high"], mark) if candle else mark
    low = min(candle["low"], mark) if candle else mark

    # Terminal exits first. TP1 is deliberately excluded here — it is a partial, and
    # treating it as an exit would close the whole position at the point the plan
    # says to take half off and let the rest run.
    hit = paper.exit_reason(
        pos["side"], high, low,
        stop=pos.get("stop"), tp1=None, tp2=pos.get("tp2"),
        liq=st["liquidation_price"],
    )
    if hit:
        reason, price = hit
        realised = _close(pos, spec, price, reason)
        return {"coin": pos["coin"], "action": "CLOSE", "reason": reason,
                "exit_price": price, "realised": realised,
                "balance_delta": balance_delta + realised}

    # "On TP1: close 50%, stop to breakeven plus accumulated costs."
    if not int(pos.get("tp1_filled") or 0) and _touched(pos["side"], high, low,
                                                        pos.get("tp1")):
        realised = _reduce_at_tp1(pos, spec, float(pos["tp1"]))
        return {"coin": pos["coin"], "action": "REDUCE", "reason": "tp1",
                "exit_price": pos["tp1"], "realised": realised,
                "balance_delta": balance_delta + realised}

    moved = _trail_stop(pos, plan, spec)

    # Time stop, measured in hours and compared in USDT.
    #
    # Both halves must hold: long enough, and not going anywhere. A position that has
    # made its floor is left alone however long it has been open — the stop is for
    # trades that are idle, not for trades that are merely slow.
    r_now = st.get("unrealised_r")
    hours_held = (paper.now_ts() - float(pos["opened_ts"])) / 3600.0
    limit_hours = time_stop_hours()
    floor_usdt = time_stop_floor_usdt(pos)
    pnl_now = st.get("unrealised_pnl")
    if hours_held >= limit_hours and pnl_now is not None and pnl_now < floor_usdt:
        realised = _close(pos, spec, mark, "time_stop")
        return {"coin": pos["coin"], "action": "CLOSE", "reason": "time_stop",
                "hours_held": round(hours_held, 2), "limit_hours": limit_hours,
                "unrealised_usdt": pnl_now, "floor_usdt": floor_usdt,
                "exit_price": mark, "realised": realised,
                "balance_delta": balance_delta + realised}

    # "Re-evaluate before every 8h renewal/funding charge — would I open this now?"
    verdict = _review(pos)
    if verdict is not None:
        realised = _close(pos, spec, mark, "review_exit")
        return {"coin": pos["coin"], "action": "CLOSE", "reason": "review_exit",
                "detail": verdict, "exit_price": mark, "realised": realised,
                "balance_delta": balance_delta + realised}

    return {"coin": pos["coin"], "action": "HOLD", "mark": mark,
            "mark_source": mark_source, "bars_held": held,
            "hours_held": round(hours_held, 2), "limit_hours": limit_hours,
            "stop_moved": moved, "unrealised_usdt": pnl_now,
            "floor_usdt": floor_usdt, "balance_delta": balance_delta}


def _touched(side: str, high: float, low: float, level: float | None) -> bool:
    return level is not None and low <= float(level) <= high


def _reduce_at_tp1(pos: dict, spec: dict, price: float) -> float:
    """Close half the position at TP1 and move the stop to breakeven plus costs.

    Breakeven *plus accumulated cost*, not bare entry: exiting the remainder at the
    entry price would still lose the fees and funding already paid, so a "breakeven"
    stop set there is a small guaranteed loss.
    """
    qty = float(pos["contracts"])
    closing = paper.round_to_step(qty * TP1_CLOSE_FRACTION, spec["step_size"])
    if closing <= 0 or closing >= qty:
        # Too small to halve on this venue's lot step — take the whole thing at TP1
        # rather than leaving an untradeable remainder behind.
        return _close(pos, spec, price, "tp1")

    remaining = qty - closing
    gross = paper.unrealised_pnl(pos["side"], float(pos["entry_price"]), price,
                                 closing, spec)
    fee = paper.fee(paper.notional(closing, price, spec))
    realised = gross - fee

    entry = float(pos["entry_price"])
    costs = (float(pos.get("entry_fee") or 0.0) + fee
             - float(pos.get("funding_paid") or 0.0))
    coins_left = paper.coins(remaining, spec)
    offset = (costs / coins_left) if coins_left else 0.0
    be_stop = entry + offset if pos["side"] == "long" else entry - offset

    margin_released = float(pos["margin"]) * (closing / qty)   # un-reserved, not cash
    store.paper_update(
        pos["id"],
        contracts=remaining,
        original_contracts=float(pos.get("original_contracts") or qty),
        margin=float(pos["margin"]) - margin_released,
        tp1_filled=1,
        stop_moved_to_be=1,
        stop=be_stop,
        exit_fee=float(pos.get("exit_fee") or 0.0) + fee,
        realised_partial=float(pos.get("realised_partial") or 0.0) + realised,
    )
    store.paper_event(pos["id"], "action", action="REDUCE", amount=realised,
                      detail=f"TP1 {closing:g} of {qty:g} contracts @ {price:g}; "
                             f"stop to breakeven+costs {be_stop:.8g}")
    pos.update(contracts=remaining, tp1_filled=1, stop=be_stop,
               margin=float(pos["margin"]) - margin_released)
    return realised


def _trail_stop(pos: dict, plan: dict, spec: dict) -> float | None:
    """Trail behind the latest swing point on the decision timeframe.

    Swings come from the skill's own `compute_indicators`, not a local reimplementation,
    so the level the demo trails to is the same one the planner would have named.
    A stop is only ever tightened — "never widen a stop" is the one management rule
    with no exceptions.
    """
    if not int(pos.get("tp1_filled") or 0):
        return None                       # trail only the runner, after TP1
    try:
        rows = toobit.klines_cached(pos["symbol"], _tf_to_interval(decision_tf(plan)),
                             limit=120)
    except toobit.ToobitError:
        return None
    if not rows:
        return None
    try:
        ind = skill.compute_indicators(rows)
    except Exception:                                        # noqa: BLE001
        return None

    level = ind.get("swing_low") if pos["side"] == "long" else ind.get("swing_high")
    if not isinstance(level, (int, float)):
        return None
    current = pos.get("stop")
    if current is None:
        return None
    tighter = level > float(current) if pos["side"] == "long" else level < float(current)
    if not tighter:
        return None
    store.paper_update(pos["id"], stop=float(level))
    store.paper_event(pos["id"], "action", action="MOVE_STOP_BE",
                      detail=f"trailed {float(current):.8g} -> {float(level):.8g} "
                             f"behind {decision_tf(plan)} swing")
    pos["stop"] = float(level)
    return float(level)


def _tf_to_interval(tf: str) -> str:
    """Skill timeframe labels to Toobit kline intervals."""
    return {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1H": "1h", "2H": "2h", "4H": "4h", "6H": "6h", "12H": "12h",
            "1D": "1d", "1W": "1w"}.get(tf, "4h")


def _review(pos: dict) -> str | None:
    """"Would I open this now?" — asked once per funding period, not every cycle.

    The demo answers it with the screener's current verdict for that coin. If the
    latest scan no longer rates it a TAKE, the thesis that justified the position is
    gone and the position goes with it. Returns the reason to close, or None to hold.
    """
    periods = int(pos.get("funding_periods") or 0)
    if periods <= 0:
        return None
    last_reviewed = store.get_kv(f"demo.reviewed.{pos['id']}") or 0
    if periods <= int(last_reviewed):
        return None
    store.set_kv(f"demo.reviewed.{pos['id']}", periods)

    row = store.result_for(pos["coin"], pos["exchange"])
    if not row:
        return None
    verdict, score = row.get("verdict"), row.get("score")
    if verdict == "TAKE" and score is not None and float(score) >= MIN_SCORE:
        return None
    return f"verdict is now {verdict} ({score}) at funding period {periods}"


def _accrue_funding(pos: dict, spec: dict, mark: float) -> float:
    """Charge whole funding periods that have elapsed since the last charge."""
    info = paper.funding(pos["symbol"])
    if not info or info.get("rate") is None:
        return 0.0
    elapsed = paper.funding_periods_elapsed(
        float(pos["opened_ts"]), paper.now_ts(), info["period_hours"])
    owed = elapsed - int(pos.get("funding_periods") or 0)
    if owed <= 0:
        return 0.0
    notional = paper.notional(float(pos["contracts"]), mark, spec)
    payment = paper.funding_payment(pos["side"], notional, float(info["rate"])) * owed
    store.paper_update(pos["id"],
                       funding_periods=elapsed,
                       funding_paid=float(pos.get("funding_paid") or 0.0) + payment)
    store.paper_event(pos["id"], "funding", amount=payment,
                      detail=f"{owed} x {info['period']} @ {info['rate']}")
    pos["funding_periods"] = elapsed
    return payment


def _record_excursions(pos: dict, st: dict) -> None:
    """Track the best and worst the trade ever got to, in R."""
    r = st.get("unrealised_r")
    if r is None:
        return
    mfe = pos.get("mfe_r")
    mae = pos.get("mae_r")
    new_mfe = r if mfe is None else max(float(mfe), r)
    new_mae = r if mae is None else min(float(mae), r)
    if new_mfe != mfe or new_mae != mae:
        store.paper_update(pos["id"], mfe_r=new_mfe, mae_r=new_mae)
        pos["mfe_r"], pos["mae_r"] = new_mfe, new_mae


def _close(pos: dict, spec: dict, price: float, reason: str) -> float:
    """Close at `price` and return the cash returned to the balance.

    The balance gets the margin back plus the trade's PnL, minus the exit fee. Entry
    fee and funding were already taken when they occurred, so they are not deducted
    twice here.
    """
    gross = paper.unrealised_pnl(pos["side"], float(pos["entry_price"]), price,
                                 float(pos["contracts"]), spec)
    exit_fee = paper.fee(paper.notional(float(pos["contracts"]), price, spec))
    realised = gross - exit_fee

    # The stored result is the whole trade, including anything banked at TP1 — a
    # position halved at +1.5R and stopped at breakeven is a winner, and recording
    # only the final leg would file it as a scratch.
    partial = float(pos.get("realised_partial") or 0.0)
    store.paper_close(pos["id"], exit_price=price, exit_reason=reason,
                      realised_pnl=realised + partial,
                      exit_fee=float(pos.get("exit_fee") or 0.0) + exit_fee)
    store.paper_event(pos["id"], "close", action="CLOSE", amount=realised,
                      detail=reason)
    # Only the P&L moves the balance.
    #
    # Margin is *reserved*, not spent: `available_margin` is balance minus the margin
    # of open positions, and opening never debited it. Crediting it back on close
    # therefore invented money — a trade that made +7.57 left the balance +57.57.
    # TP1's proceeds were already credited when it filled.
    return realised


def _latest_candle(symbol: str) -> dict | None:
    try:
        rows = toobit.klines_cached(symbol, "15m", limit=2)
    except toobit.ToobitError:
        return None
    return rows[-1] if rows else None


# --------------------------------------------------------------------------------
# Opening positions
# --------------------------------------------------------------------------------


def try_fill_slots() -> dict:
    """Fill empty slots from qualifying signals, respecting the heat cap.

    Returns what it did *and* what it declined to do, so a half-empty board can be
    explained without reading the logs.
    """
    acct = ensure_account()
    snapshot = state()
    opened, declined = [], []

    free = snapshot["slots"]["empty"]
    if free <= 0:
        return {"opened": [], "declined": [], "slots_free": 0}

    # "Circuit breaker: 2 losses or -3% equity (scalp), 3 losses or -5% (intraday)."
    # A breaker that only stops the current trade is not a breaker; this stops the
    # account taking new risk until a human resets it.
    tripped = circuit_breaker()
    if tripped:
        store.set_kv("demo.last_fill",
                     {"opened": [], "declined": [], "slots_free": free,
                      "circuit_breaker": tripped})
        return {"opened": [], "declined": [], "slots_free": free,
                "circuit_breaker": tripped}

    heat = snapshot["heat"]["used_pct"]
    cap = snapshot["heat"]["cap_pct"]
    equity = snapshot["account"]["equity"]
    available = snapshot["account"]["available_margin"]

    for row in qualifying_signals():
        if free <= 0:
            break
        try:
            proposal = _proposal(row, equity)
        except (paper.PaperError, toobit.ToobitError, ValueError) as exc:
            declined.append({"coin": row["coin"], "code": "unavailable",
                             "detail": str(exc)})
            continue
        if proposal is None:
            declined.append({"coin": row["coin"], "code": "no_plan"})
            continue

        # A venue rejects an order it cannot collateralise, and so must this.
        #
        # Without `--slots`, the planner sizes every plan as though it were the only
        # position, so the margin budget is spent five times over: five plans at ~216
        # USDT of margin need 1083 from a 1000 account. Silently opening them would
        # produce an account that could not exist on Toobit, and every downstream
        # figure — equity, heat, return — would be measuring a fiction.
        cost = proposal["margin"] + proposal["entry_fee"]
        if cost > available:
            declined.append({"coin": row["coin"], "code": "insufficient_margin",
                             "needs": cost, "available": available,
                             "leverage": proposal["leverage"]})
            continue

        added_heat = proposal["risk_amount"] / equity * 100.0 if equity > 0 else 0.0
        if heat + added_heat > cap:
            # The sixth signal that would breach the cap is declined, not shrunk to
            # fit. Sizing down to squeeze it in would mean the position no longer
            # matches the plan that qualified it.
            declined.append({"coin": row["coin"], "code": "heat_cap",
                             "would_add_pct": added_heat,
                             "heat_pct": heat, "cap_pct": cap})
            continue

        pid = _open(row, proposal, slot=snapshot["slots"]["filled"] + len(opened) + 1)
        opened.append({"coin": row["coin"], "id": pid,
                       "risk_amount": proposal["risk_amount"]})
        heat += added_heat
        available -= cost
        free -= 1

    outcome = {"opened": opened, "declined": declined, "slots_free": free}
    store.set_kv("demo.last_fill", outcome)
    return outcome


def circuit_breaker() -> dict | None:
    """Has the account taken enough damage today to stop opening new positions?

    Counts only the current run of losses: one winner clears it, which is the point —
    the breaker is for a losing streak, not a lifetime tally.
    """
    acct = store.paper_account()
    if not acct:
        return None
    profile = settings()["profile"]
    max_losses, drawdown_fraction = CIRCUIT_BREAKER.get(profile, (3, 0.05))

    streak = 0
    for trade in store.paper_closed_positions():          # newest first
        pnl = trade.get("realised_pnl")
        if pnl is None or float(pnl) > 0:
            break
        streak += 1

    # In USDT, like every other threshold the agent acts on.
    start = float(acct["starting_capital"])
    lost_usdt = start - float(acct["balance"])
    limit_usdt = start * drawdown_fraction

    if streak >= max_losses:
        return {"code": "consecutive_losses", "losses": streak, "limit": max_losses,
                "profile": profile}
    if lost_usdt >= limit_usdt:
        return {"code": "equity_drawdown", "lost_usdt": lost_usdt,
                "limit_usdt": limit_usdt, "profile": profile}
    return None


def _proposal(row: dict, equity: float) -> dict | None:
    """Turn a stored plan into an executable paper order.

    Sizing is the planner's, not ours — quantity and leverage are read straight out
    of the plan. What happens here is only the translation from coins to Toobit
    contracts, and rounding down to a tradable lot.
    """
    if not row.get("plan_json"):
        return None
    plan = json.loads(row["plan_json"])
    sizing = plan.get("sizing") or {}
    levels = plan.get("levels") or {}

    entry = levels.get("entry")
    qty_coins = sizing.get("quantity")
    leverage = sizing.get("leverage")
    risk_amount = sizing.get("risk_amount_R")
    if not all(isinstance(v, (int, float)) for v in (entry, qty_coins, leverage,
                                                     risk_amount)):
        return None

    spec = paper.contract_spec(row["symbol"])
    mark, _ = paper.mark_price(spec)
    if mark is None:
        raise paper.PaperError("no mark price")

    contracts = paper.round_to_step(qty_coins / spec["units_per_contract"],
                                    spec["step_size"])
    if spec["min_qty"] and contracts < spec["min_qty"]:
        raise ValueError(f"{contracts:g} contracts is below the {spec['min_qty']:g} "
                         f"minimum lot")

    notional = paper.notional(contracts, mark, spec)
    tier = paper.tier_for(spec, notional)
    if leverage > tier["max_leverage"]:
        raise ValueError(f"plan leverage {leverage:g}x exceeds the venue's "
                         f"{tier['max_leverage']:g}x for this size")

    return {
        "spec": spec,
        "entry": mark,
        "contracts": contracts,
        "leverage": float(leverage),
        "margin": notional / float(leverage),
        "risk_amount": float(risk_amount),
        "entry_fee": paper.fee(notional),
        "stop": levels.get("stop"),
        "tp1": levels.get("tp1"),
        "tp2": levels.get("tp2"),
        "plan": plan,
    }


def _open(row: dict, proposal: dict, slot: int) -> int:
    pid = store.paper_open(
        coin=row["coin"], symbol=row["symbol"], exchange=row["exchange"],
        side=row["side"], slot=slot,
        contracts=proposal["contracts"], entry_price=proposal["entry"],
        leverage=proposal["leverage"], margin=proposal["margin"],
        risk_amount=proposal["risk_amount"],
        stop=proposal["stop"], tp1=proposal["tp1"], tp2=proposal["tp2"],
        opened_ts=paper.now_ts(), entry_fee=proposal["entry_fee"],
        scan_id=row.get("scan_id"), score=row.get("score"),
        verdict=row.get("verdict"),
        plan_json=json.dumps(proposal["plan"], ensure_ascii=False),
    )
    # The entry fee leaves the account the moment the position opens, exactly as it
    # would on the venue.
    acct = store.paper_account()
    store.paper_set_balance(float(acct["balance"]) - proposal["entry_fee"])
    store.paper_event(pid, "open", detail=f"slot {slot}, score {row.get('score')}")
    return pid
