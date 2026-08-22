"""The live trading engine: open, monitor, and exit real Tabdeal positions.

This is `demo.py`'s management loop pointed at real money. It runs the same strategy
against the same signals, with one deliberate difference in who does what:

    the EXCHANGE owns the downside  — stop loss and take profit are set on the
                                      position itself via `positionSlTp`, so they
                                      survive this process dying, the server
                                      rebooting, or the network dropping
    the ENGINE owns the judgement   — which signal to take, when the setup has
                                      stopped being valid (signal exit), when a
                                      trade has gone nowhere (time stop), and the
                                      TP1 partial

That split is the whole safety design. A monitoring loop is a good place for
"should I still be in this trade"; it is a terrible place for "am I about to lose
more than I planned". The stop never depends on this file running.

## The exchange is the source of truth

`reconcile()` reads `positionRisk` every cycle and treats it as authoritative.
`live_positions` rows are annotations — they carry the plan levels and entry time the
venue does not store. Three cases are handled explicitly rather than assumed away:

* **Ours, still open** — normal, manage it.
* **Ours, gone from the venue** — the exchange closed it, almost always the SL or TP
  we set. Recorded as `exchange_exit` with the real fill read back from `userTrades`.
* **On the venue, not ours** — an orphan. Never silently adopted and never silently
  ignored: it is recorded, logged loudly, and left alone. Real money we did not open
  is a situation for a human, not for a loop that assumes.

## Nothing here runs unless armed

`demo.live_trading` must be true, and `scheduler_loop` exits immediately if it is
not. Every write also passes `guard.assert_tabdeal_write_allowed`. Disarming takes
effect on the next cycle without a restart.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from . import config, demo, store, tabdeal, tabdeal_broker

log = logging.getLogger("live")

DEFAULT_CYCLE_SECONDS = 20
DEFAULT_MAX_SLOTS = 4
DEFAULT_ENTRY_INTERVAL = 300


def settings() -> dict:
    s = config.load_settings()
    d = s.get("demo") or {}
    return {
        "enabled": bool(d.get("live_trading")),
        "capital": float(d.get("capital") or s.get("capital") or 0.0),
        "max_slots": int(d.get("live_max_slots") or DEFAULT_MAX_SLOTS),
        "cycle_seconds": int(d.get("live_cycle_seconds") or DEFAULT_CYCLE_SECONDS),
        "entry_interval_seconds": int(d.get("entry_interval_seconds")
                                      or DEFAULT_ENTRY_INTERVAL),
        "leverage": float(d.get("live_leverage") or 10.0),
        "time_stop_hours": float(d.get("time_stop_hours") or 0.5),
        "max_entry_drift_r": float(d.get("max_entry_drift_r") or 0.3),
        "max_total_notional": float(d.get("live_max_total_notional") or 25.0),
        "dry_run": bool(d.get("live_dry_run", False)),
    }


def _broker() -> tabdeal_broker.TabdealBroker:
    return tabdeal_broker.TabdealBroker(dry_run=settings()["dry_run"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------------
# Reconciliation — the exchange decides what is open
# --------------------------------------------------------------------------------


def reconcile(broker=None) -> dict:
    """Align local records with the venue. Returns what changed."""
    broker = broker or _broker()
    venue = {p["symbol"]: p for p in broker.positions()}
    local = {r["symbol"]: r for r in store.live_positions("open")}
    out = {"open": [], "closed": [], "orphans": []}

    for symbol, row in local.items():
        if symbol in venue:
            out["open"].append(symbol)
            continue
        # Gone from the venue while we still had it open. The exchange closed it —
        # normally the SL or TP we attached. Read the real fill back rather than
        # guessing a price.
        exit_price, pnl = _closing_fill(broker, symbol, row)
        store.live_close(row["id"], exit_price=exit_price,
                         exit_reason="exchange_exit", realised_pnl=pnl)
        store.live_event(row["id"], "close",
                         f"closed by the exchange (SL/TP) at {exit_price}")
        log.warning("live: %s closed by the exchange at %s (pnl %s)",
                    symbol, exit_price, pnl)
        out["closed"].append(symbol)

    for symbol, pos in venue.items():
        if symbol in local:
            continue
        # Real money we did not open. Do not adopt it — adopting means inventing a
        # stop and a plan for a position whose intent is unknown.
        log.error("live: ORPHAN position on %s (%s) — not managed by this engine",
                  symbol, pos.get("positionAmt"))
        out["orphans"].append({"symbol": symbol,
                               "amount": pos.get("positionAmt"),
                               "entry": pos.get("entryPrice")})
    return out


def _closing_fill(broker, symbol: str, row: dict) -> tuple[float | None, float | None]:
    """The price the venue actually closed at, from its own trade record."""
    try:
        trades = broker._get_signed("/r/fapi/v1/userTrades", {"symbol": symbol}) or []
    except Exception as exc:                                   # noqa: BLE001
        log.warning("live: could not read userTrades for %s: %s", symbol, exc)
        return None, None
    opened = float(row.get("opened_ts") or 0) * 1000
    fills = [t for t in trades if float(t.get("time") or 0) >= opened]
    if not fills:
        return None, None
    last = fills[-1]
    price = float(last.get("price") or 0) or None
    pnl = None
    try:
        pnl = sum(float(t.get("realizedPnl") or 0) for t in fills)
    except (TypeError, ValueError):
        pass
    return price, pnl


# --------------------------------------------------------------------------------
# Entry
# --------------------------------------------------------------------------------


def _open_symbols() -> set[str]:
    return {r["symbol"] for r in store.live_positions("pending", "open")}


def total_notional(broker=None) -> float:
    broker = broker or _broker()
    tot = 0.0
    for p in broker.positions():
        try:
            tot += abs(float(p["positionAmt"])) * float(p.get("markPrice") or 0)
        except (TypeError, ValueError, KeyError):
            continue
    return tot


def try_open(broker=None) -> dict:
    """Take the best qualifying signal, if there is room for it.

    Reuses `demo.qualifying_signals()` so the live engine and the paper account are
    choosing from exactly the same pool under exactly the same gates — the point of
    the demo is to predict this, which it cannot do if the selection differs.
    """
    cfg = settings()
    broker = broker or _broker()
    held = _open_symbols()
    slots_free = cfg["max_slots"] - len(held)
    if slots_free <= 0:
        return {"action": "none", "reason": "slots_full", "held": len(held)}

    notional_now = total_notional(broker)
    if notional_now >= cfg["max_total_notional"]:
        return {"action": "none", "reason": "notional_cap",
                "notional": round(notional_now, 2)}

    for row in demo.qualifying_signals():
        if row["symbol"] in held:
            continue
        try:
            return _enter(broker, row, cfg, notional_now)
        except Exception as exc:                               # noqa: BLE001
            log.warning("live: %s entry failed: %s", row["coin"], exc)
            continue
    return {"action": "none", "reason": "no_signal"}


def _enter(broker, row: dict, cfg: dict, notional_now: float) -> dict:
    plan = json.loads(row["plan_json"]) if row.get("plan_json") else {}
    levels = plan.get("levels") or {}
    plan_entry = levels.get("entry")
    stop, tp1, tp2 = levels.get("stop"), levels.get("tp1"), levels.get("tp2")
    if not all(isinstance(v, (int, float)) for v in (plan_entry, stop, tp1, tp2)):
        raise ValueError("plan is missing levels")

    symbol = row["symbol"]
    side = row["side"]
    mark = tabdeal.mark_price(symbol)
    if not mark:
        raise ValueError("no mark price")

    # Same staleness rule as the demo: if price has already run past the plan entry,
    # the levels no longer describe this trade.
    planned_r = abs(float(plan_entry) - float(stop))
    drift = (mark - plan_entry) if side == "long" else (plan_entry - mark)
    drift_r = drift / planned_r if planned_r else 0.0
    if drift_r > cfg["max_entry_drift_r"]:
        return {"action": "declined", "coin": row["coin"], "reason": "stale_signal",
                "drift_r": round(drift_r, 3)}

    # Size from the risk budget, then clamp to whatever notional headroom is left.
    risk_amount = cfg["capital"] * _risk_pct(cfg) / 100.0
    stop_distance = abs(mark - float(stop))
    if stop_distance <= 0:
        raise ValueError("stop distance is zero")
    qty = risk_amount / stop_distance
    spec = _spec(symbol)
    headroom = cfg["max_total_notional"] - notional_now
    if qty * mark > headroom:
        qty = headroom / mark
    qty = tabdeal_broker._round_down(qty, spec["step_size"])
    if qty <= 0:
        return {"action": "declined", "coin": row["coin"], "reason": "size_rounds_to_zero"}

    # Leverage must exist for the symbol before it can be traded — an unconfigured
    # market answers "TraderMarketConfig matching query does not exist".
    try:
        broker.set_leverage(symbol, cfg["leverage"])
    except Exception as exc:                                   # noqa: BLE001
        log.warning("live: set_leverage %s failed: %s", symbol, exc)

    order = broker.place_order(symbol, "BUY" if side == "long" else "SELL", qty,
                               order_type="MARKET", ref_price=mark)
    pid = store.live_open(
        coin=row["coin"], symbol=symbol, side=side, status="open", quantity=qty,
        entry_price=mark, plan_entry=plan_entry, leverage=cfg["leverage"],
        risk_amount=risk_amount, stop=stop, tp1=tp1, tp2=tp2,
        order_id=str(order.get("orderId") or ""), opened_at=_now_iso(),
        opened_ts=time.time(), scan_id=row.get("scan_id"), score=row.get("score"),
        plan_json=row.get("plan_json"))
    store.live_event(pid, "open", f"MARKET {side} {qty:g} @~{mark:.8g}, order "
                                  f"{order.get('orderId')}")
    log.warning("live: OPENED %s %s qty=%g @~%.8g", symbol, side, qty, mark)

    _attach_stop(broker, pid, symbol, stop, tp2)
    return {"action": "opened", "coin": row["coin"], "symbol": symbol,
            "qty": qty, "entry": mark, "order": order.get("orderId")}


def _attach_stop(broker, pid: int, symbol: str, stop, tp) -> None:
    """Hand the downside to the exchange, immediately after the fill.

    This is the most important call in the engine. Until it succeeds the position has
    no protection that survives this process, so a failure is logged as an error and
    recorded on the position rather than swallowed.
    """
    try:
        live = broker.position_for(symbol)
        vpid = (live or {}).get("positionId") or (live or {}).get("id")
        if vpid is None:
            raise ValueError("venue did not report a positionId")
        broker.set_position_sl_tp(vpid, sl_price=stop, tp_price=tp, symbol=symbol)
        store.live_update(pid, sl_tp_set=1, venue_position_id=str(vpid))
        store.live_event(pid, "sltp", f"exchange SL={stop:.8g} TP={tp:.8g}")
        log.warning("live: %s exchange stop set at %.8g", symbol, stop)
    except Exception as exc:                                   # noqa: BLE001
        store.live_event(pid, "sltp_failed", str(exc)[:200])
        log.error("live: %s HAS NO EXCHANGE STOP (%s) — position is unprotected "
                  "except by this loop", symbol, exc)


# --------------------------------------------------------------------------------
# Management — what the engine, not the exchange, decides
# --------------------------------------------------------------------------------


def manage(broker=None) -> list[dict]:
    cfg = settings()
    broker = broker or _broker()
    venue = {p["symbol"]: p for p in broker.positions()}
    out = []
    for row in store.live_positions("open"):
        pos = venue.get(row["symbol"])
        if not pos:
            continue                       # reconcile() handles the vanished case
        try:
            out.append(_manage_one(broker, row, pos, cfg))
        except Exception as exc:                               # noqa: BLE001
            log.warning("live: manage %s failed: %s", row["symbol"], exc)
            out.append({"symbol": row["symbol"], "error": str(exc)})
    return out


def _manage_one(broker, row: dict, pos: dict, cfg: dict) -> dict:
    symbol = row["symbol"]
    mark = float(pos.get("markPrice") or 0) or tabdeal.mark_price(symbol)
    upnl = float(pos.get("unRealizedProfit") or 0)
    risk = float(row.get("risk_amount") or 0)
    r_now = (upnl / risk) if risk else 0.0
    held_h = (time.time() - float(row.get("opened_ts") or time.time())) / 3600.0

    # The stop and the target belong to the exchange. Everything below is judgement.

    # TP1: bank half and let the exchange keep protecting the rest.
    if not int(row.get("tp1_filled") or 0) and _reached(row["side"], mark, row["tp1"]):
        res = broker.reduce_position(symbol, 0.5)
        store.live_update(row["id"], tp1_filled=1, quantity=res.get("remaining",
                                                                   row["quantity"]))
        store.live_event(row["id"], "tp1", f"halved at {mark:.8g}: {res}")
        # Move the exchange stop up to TP1 — the risk-free lock, now enforced by the
        # venue rather than by this loop.
        _attach_stop(broker, row["id"], symbol, row["tp1"], row["tp2"])
        return {"symbol": symbol, "action": "TP1", "mark": mark, **res}

    # Signal exit: in profit, but the setup no longer qualifies.
    if upnl > 0:
        still, reason = demo._profit_signal_check(row)
        if reason is not None:
            broker.close_position(symbol)
            store.live_close(row["id"], exit_price=mark, exit_reason="signal_exit",
                             realised_pnl=upnl)
            store.live_event(row["id"], "close", f"signal_exit: {reason}")
            log.warning("live: %s signal_exit at %.8g (%+.3fR)", symbol, mark, r_now)
            return {"symbol": symbol, "action": "CLOSE", "reason": "signal_exit",
                    "r": r_now}
        if still:
            return {"symbol": symbol, "action": "HOLD", "reason": "still_favoured",
                    "r": r_now}

    # Time stop: only for a trade that is going nowhere. A loser is left to its
    # exchange stop, exactly as in the demo.
    floor = 0.5 * risk
    if held_h >= cfg["time_stop_hours"] and 0 <= upnl < floor:
        broker.close_position(symbol)
        store.live_close(row["id"], exit_price=mark, exit_reason="time_stop",
                         realised_pnl=upnl)
        store.live_event(row["id"], "close", f"time_stop after {held_h:.2f}h")
        log.warning("live: %s time_stop at %.8g (%+.3fR)", symbol, mark, r_now)
        return {"symbol": symbol, "action": "CLOSE", "reason": "time_stop", "r": r_now}

    return {"symbol": symbol, "action": "HOLD", "r": round(r_now, 3),
            "held_h": round(held_h, 2)}


def _reached(side: str, mark: float, level) -> bool:
    """One-sided, never range containment. See demo._touched and Round 11."""
    if level is None:
        return False
    return mark >= float(level) if side == "long" else mark <= float(level)


def _risk_pct(cfg: dict) -> float:
    """Risk per position, derived so a full board sits at the notional cap."""
    per_notional = cfg["max_total_notional"] / max(1, cfg["max_slots"])
    # notional = R / stop_pct, and the scalp stop is ~1.5%
    return (per_notional * 0.015) / cfg["capital"] * 100.0 if cfg["capital"] else 0.0


def _spec(symbol: str) -> dict:
    from . import paper                                        # noqa: PLC0415
    return paper.contract_spec(symbol)


# --------------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------------


def cycle() -> dict:
    broker = _broker()
    rec = reconcile(broker)
    managed = manage(broker)
    return {"reconcile": rec, "managed": managed}


def scheduler_loop(stop_event) -> None:
    """Run the engine for as long as the server lives and live trading is armed.

    Entries are attempted on their own slower timer, for the same reason the demo
    separates them: the candidate set only changes when a scan completes, so trying
    every cycle re-evaluates the same signals and risks acting on a stale plan.
    """
    last_entry = 0.0
    while not stop_event.is_set():
        cfg = settings()
        if cfg["enabled"]:
            try:
                out = cycle()
                if out["reconcile"]["closed"] or out["reconcile"]["orphans"]:
                    log.warning("live reconcile: %s", out["reconcile"])
                now = time.time()
                if now - last_entry >= cfg["entry_interval_seconds"]:
                    last_entry = now
                    opened = try_open()
                    if opened.get("action") == "opened":
                        log.warning("live entry: %s", opened)
                store.set_kv("live.last_cycle", {"at": _now_iso(), **out})
            except Exception as exc:                           # noqa: BLE001
                log.warning("live cycle failed: %s", exc)
        stop_event.wait(cfg["cycle_seconds"])


def state() -> dict:
    """What the dashboard needs to show the live account."""
    broker = _broker()
    try:
        bal = broker.balance()
        positions = broker.positions()
    except Exception as exc:                                   # noqa: BLE001
        return {"error": str(exc), "enabled": settings()["enabled"]}
    cfg = settings()
    return {
        "enabled": cfg["enabled"],
        "dry_run": cfg["dry_run"],
        "balance": bal,
        "venue_positions": positions,
        "tracked": store.live_positions("pending", "open"),
        "closed": store.live_closed()[:50],
        "slots": {"used": len(positions), "max": cfg["max_slots"]},
        "notional": {"used": round(total_notional(broker), 2),
                     "cap": cfg["max_total_notional"]},
    }
