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

from . import config, paper, store, toobit

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
                filled = try_fill_slots()
                if closed or filled["opened"]:
                    log.info("demo cycle: %d closed, %d opened",
                             len(closed), len(filled["opened"]))
            except Exception as exc:                          # noqa: BLE001
                log.warning("demo cycle failed: %s", exc)
        stop_event.wait(cfg.get("cycle_seconds") or DEFAULT_CYCLE_SECONDS)


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
    }


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
        "heat": {
            "used_pct": heat,
            "cap_pct": float(acct["heat_cap_pct"]),
            "headroom_pct": max(0.0, float(acct["heat_cap_pct"]) - heat),
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
    out = []
    for row in store.latest_results(cfg["exchange"]):
        if row.get("verdict") != "TAKE":
            continue
        score = row.get("score")
        if score is None or float(score) < MIN_SCORE:
            continue
        if row["coin"] in open_coins:
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


def _cycle_one(pos: dict) -> dict:
    spec = paper.contract_spec(pos["symbol"])
    mark, mark_source = paper.mark_price(spec)
    if mark is None:
        return {"coin": pos["coin"], "action": "SKIP", "reason": "no mark price"}

    balance_delta = _accrue_funding(pos, spec, mark)
    st = paper.position_state(pos, spec, mark)
    _record_excursions(pos, st)

    candle = _latest_candle(pos["symbol"])
    high = max(candle["high"], mark) if candle else mark
    low = min(candle["low"], mark) if candle else mark

    hit = paper.exit_reason(
        pos["side"], high, low,
        stop=pos.get("stop"), tp1=pos.get("tp1"), tp2=pos.get("tp2"),
        liq=st["liquidation_price"],
    )
    if hit:
        reason, price = hit
        realised = _close(pos, spec, price, reason)
        return {"coin": pos["coin"], "action": "CLOSE", "reason": reason,
                "exit_price": price, "realised": realised,
                "balance_delta": balance_delta + realised}

    return {"coin": pos["coin"], "action": "HOLD", "mark": mark,
            "mark_source": mark_source,
            "unrealised_r": st["unrealised_r"], "balance_delta": balance_delta}


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
    store.paper_close(pos["id"], exit_price=price, exit_reason=reason,
                      realised_pnl=realised, exit_fee=exit_fee)
    store.paper_event(pos["id"], "close", action="CLOSE", amount=realised,
                      detail=reason)
    return float(pos["margin"]) + realised


def _latest_candle(symbol: str) -> dict | None:
    try:
        rows = toobit.klines(symbol, "15m", limit=2)
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
