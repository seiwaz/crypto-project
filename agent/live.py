"""The live trading engine: open, monitor, and exit real Tabdeal positions.

This is `demo.py`'s management loop pointed at real money. It runs the same strategy
against the same signals, with one deliberate difference in who does what:

    the EXCHANGE owns both levels   — stop loss AND TP1 are set on the position itself
                                      via `positionSlTp`, so a stop-out or a target
                                      is honoured even if this process dies, the
                                      server reboots, or the network drops.
    the ENGINE owns the judgement   — which signal to take, when a setup that has not
                                      reached its target has stopped being valid
                                      (signal exit), and when a trade has gone nowhere
                                      (time stop). There is no TP2: TP1 is the target,
                                      and reaching it closes the position outright.

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
import threading
import time
from datetime import datetime, timezone

from . import config, demo, store, tabdeal, tabdeal_broker

log = logging.getLogger("live")

# 3 seconds. The exit that matters most here is signal_exit, and on a 5-20 minute
# hold a 20-second cadence could miss a fifth of the trade. The stop and TP live on
# the exchange, so this cadence governs judgement, not safety.
DEFAULT_CYCLE_SECONDS = 3
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
        # Retained for the paper engine and for history; the live manager no longer
        # has a time stop - see _manage_one.
        "time_stop_hours": float(d.get("time_stop_hours") or 0.5),
        # How long a losing trade is given before a lapsed setup closes it. Not zero:
        # a position needs a little room to breathe past entry noise before "the
        # signal changed" means anything. Defaults to the time stop's own window.
        "adverse_exit_after_h": float(d.get("adverse_exit_after_h")
                                      or d.get("time_stop_hours") or 0.5),
        # Whether a losing position may be closed by the engine at all. Turned OFF at
        # the operator's instruction 2026-08-23 ("do not touch positions in loss") -
        # a loser then has exactly one exit, its exchange stop. Worth knowing before
        # flipping this: adding the adverse exit was measured as the single largest
        # loss reduction available (six stop-outs were 89% of all loss at a median
        # hold of 548 minutes), and switching it off restores that exposure.
        "adverse_exit_enabled": bool(d.get("adverse_exit_enabled", False)),
        # A position held this long that is NET profitable - after the round trip, not
        # merely above entry - is banked, whatever the setup says.
        "profit_close_after_h": float(d.get("profit_close_after_h") or 1.0),
        # How far past the round trip the gross must be before closing counts as
        # profitable. Not 1.0: the test uses the MARK and the close is a MARKET order,
        # so the fill crosses the spread and lands below what was measured. PEPE
        # cleared the fee by 0.0004 at the mark on 2026-08-23 and settled at -0.00039.
        # 1.5 leaves half a round trip of headroom for that slippage.
        "profit_close_fee_multiple": float(d.get("profit_close_fee_multiple") or 1.5),
        # A winner past its hour is KEPT while the scan still says TAKE at or above
        # this score, and banked otherwise. Higher than the abandon floor on purpose:
        # "still worth riding" is a stronger claim than "not yet dead".
        "hold_take_score": float(d.get("hold_take_score") or 70.0),
        # How often the monitoring loop writes a position sample. The loop itself runs
        # every `cycle_seconds`; recording at that rate would be ~1.2M rows a day for
        # four positions, so the series is thinned to something a chart still reads
        # smoothly.
        "history_interval_seconds": float(d.get("history_interval_seconds") or 15.0),
        "history_keep_days": float(d.get("history_keep_days") or 14.0),
        "max_entry_drift_r": float(d.get("max_entry_drift_r") or 0.3),
        # An absolute ceiling, and the multiple of live equity that normally binds.
        # The multiple is what keeps risk proportional as the account moves; the
        # ceiling is a blast-radius limit that does not grow with a winning streak.
        "max_total_notional": float(d.get("live_max_total_notional") or 25.0),
        "notional_multiple": float(d.get("live_notional_multiple") or 4.7),
        # Ceiling on the leverage a signal may ask for.
        "max_leverage": float(d.get("live_max_leverage") or 20.0),
        "dry_run": bool(d.get("live_dry_run", False)),
    }


def _broker() -> tabdeal_broker.TabdealBroker:
    return tabdeal_broker.TabdealBroker(dry_run=settings()["dry_run"])


def account_equity(broker) -> float | None:
    """Live equity from the venue, read fresh. None if it cannot be read.

    Unrealised losses count against it, unrealised gains do not. Sizing off paper
    profit would let a position that has merely not been closed yet justify a larger
    one next to it, which is how a drawdown compounds under cross margin.
    """
    try:
        row = (broker.balance() or [{}])[0]
        wallet = float(row.get("walletBalance") or 0)
        unreal = float(row.get("crossUnPnl") or 0)
        return wallet + min(0.0, unreal)
    except Exception as exc:                                   # noqa: BLE001
        log.warning("live: could not read the account balance: %s", exc)
        return None


def notional_cap(broker, cfg: dict) -> tuple[float, str]:
    """How much total notional the account may carry RIGHT NOW.

    Re-read before every entry rather than taken from config. A fixed cap silently
    becomes a larger multiple of equity as the account draws down — $25 against 5.27
    is 4.7x, against 4.00 it is 6.3x — and under cross margin that walks liquidation
    closer with every loss, exactly when it should be walking away.

    Returns (cap, why) so the reason appears in the decision log.
    """
    equity = account_equity(broker)
    if equity is None or equity <= 0:
        # Never size off a number we could not read. The configured ceiling is the
        # conservative fallback because it cannot be larger than the intended cap.
        return cfg["max_total_notional"], "balance unreadable, using configured cap"
    scaled = equity * cfg["notional_multiple"]
    if scaled <= cfg["max_total_notional"]:
        return scaled, f"{cfg['notional_multiple']:g}x equity {equity:.4f}"
    return cfg["max_total_notional"], f"ceiling (equity {equity:.4f} would allow {scaled:.2f})"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------------
# Reconciliation — the exchange decides what is open
# --------------------------------------------------------------------------------


def backfill_unsettled(broker=None) -> list[dict]:
    """Fill in any closed row still missing its realised P&L.

    `settle()` reads the fill back from the venue, but the venue does not always have
    it published yet at that instant — it returns None and the row is stored with a
    NULL result. A NULL in a money record is not acceptable: it silently drops that
    trade out of every total, which is exactly how the live record came to disagree
    with the account by 0.0169. This runs every cycle and closes the gap as soon as
    the venue catches up.
    """
    broker = broker or _broker()
    out = []
    for row in store.live_closed():
        if row.get("realised_pnl") is not None:
            continue
        price, pnl = _closing_fill(broker, row["symbol"], row)
        if pnl is None:
            continue
        store.live_update(row["id"], realised_pnl=pnl,
                          exit_price=row.get("exit_price") or price)
        store.live_event(row["id"], "settled",
                         f"backfilled from the venue: net {pnl}")
        log.warning("live: backfilled %s (id %s) net %s", row["symbol"], row["id"], pnl)
        out.append({"id": row["id"], "symbol": row["symbol"], "realised_pnl": pnl})
    return out


def reconcile(broker=None) -> dict:
    """Align local records with the venue. Returns what changed."""
    broker = broker or _broker()
    venue = {p["symbol"]: p for p in broker.positions()}
    local = {r["symbol"]: r for r in store.live_positions("open")}
    out = {"open": [], "closed": [], "orphans": []}

    for symbol, row in local.items():
        if symbol in venue:
            out["open"].append(symbol)
            # Repair a position the venue is holding without our stop on it.
            #
            # _attach_stop reads the position back to get its positionId, and
            # immediately after a market order the venue has sometimes not registered
            # it yet. It then logged "HAS NO EXCHANGE STOP" and never tried again -
            # so SUI sat unprotected for 40 minutes on 2026-08-23 while FLOKI, opened
            # one second earlier in the same batch, got its stop fine. Filling several
            # slots per scan makes that race more likely, not less.
            #
            # Reconcile already runs every cycle and already reads the venue, so it is
            # the natural place to notice and fix it. The venue is the authority here:
            # if it reports no stopLossPrice, there is no stop, whatever our row says.
            if not _venue_has_stop(venue[symbol]) and row.get("stop"):
                if _repair_stop(broker, row, venue[symbol]):
                    out.setdefault("stops_repaired", []).append(symbol)
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


def _signal_supports_holding(row: dict, cfg: dict) -> tuple[bool, str]:
    """Is the signal still actively strong enough to keep riding a WINNER?

    Deliberately a different question from `_profit_signal_check`, which asks "is the
    thesis dead" and answers with a floor 10 points UNDER the entry bar. That floor is
    the right test for whether to abandon a trade. It is the wrong test for whether to
    bank one: a position can be well above the abandon floor and still be a signal
    that is fading, and holding a fading winner past its hour just re-exposes a profit
    that has already paid for its own fees.

    Holding requires a positive, current reason: the verdict is TAKE, the score is at
    or above `hold_take_score`, and the scan still favours the side we are on. Missing
    or stale scan data is NOT a reason to hold - with the profit already clear of the
    round trip, banking it is the safe side of that uncertainty.
    """
    try:
        scan = store.result_for(row["coin"], demo.settings()["exchange"])
    except Exception as exc:                                   # noqa: BLE001
        return False, f"scan unreadable ({exc})"
    if not scan:
        return False, "no current scan"
    verdict, score = scan.get("verdict"), scan.get("score")
    side = (scan.get("side") or "").lower()
    if verdict != "TAKE":
        return False, f"verdict {verdict}, not TAKE"
    if score is None or float(score) < cfg["hold_take_score"]:
        return False, (f"score {score} below the {cfg['hold_take_score']:g} "
                       f"hold-take bar")
    if side and side != (row.get("side") or "").lower():
        return False, f"scan now favours {side}"
    if scan.get("side_tied"):
        return False, "direction tied"
    return True, f"TAKE {score}"


def _venue_has_stop(pos: dict) -> bool:
    """Does the venue actually hold a stop on this position?

    Tabdeal reports an absent level as None, "" or the string "0" depending on the
    endpoint, so a plain truthiness test on the raw field is not enough.
    """
    try:
        return float(pos.get("stopLossPrice") or 0) > 0
    except (TypeError, ValueError):
        return False


def _repair_stop(broker, row: dict, pos: dict) -> bool:
    """Re-attach a missing exchange stop to an already-open position."""
    vpid = pos.get("positionId") or pos.get("id") or row.get("venue_position_id")
    if vpid is None:
        return False
    try:
        broker.set_position_sl_tp(vpid, sl_price=row["stop"], tp_price=row.get("tp1"),
                                  symbol=row["symbol"])
    except Exception as exc:                                   # noqa: BLE001
        log.error("live: %s stop repair FAILED (%s) — still unprotected",
                  row["symbol"], exc)
        return False
    # Never trust the write; read it back. "success" from positionSlTp is not proof,
    # and the first two live positions of this account ran unprotected because that
    # distinction was not made.
    try:
        back = broker.position_for(row["symbol"]) or {}
    except Exception:                                          # noqa: BLE001
        back = {}
    if not _venue_has_stop(back):
        log.error("live: %s stop repair did not stick — still unprotected",
                  row["symbol"])
        return False
    store.live_update(row["id"], sl_tp_set=1, venue_position_id=str(vpid))
    store.live_event(row["id"], "sltp_repaired",
                     f"re-attached SL={row['stop']:.8g}")
    log.warning("live: %s exchange stop REPAIRED at %.8g (was missing)",
                row["symbol"], row["stop"])
    return True


def settle(broker, row: dict, reason: str, fallback_price: float) -> dict:
    """Close a position and record what the VENUE says it cost, not our estimate.

    The first live close made the need obvious: CAKE exited flat at 1.7801 with
    0.00623 of commission each side, a real result of about -0.0125 USDT, but the
    engine recorded +0.00175 because it stored the unrealised PnL computed from our
    own mark at decision time and never subtracted fees. Left alone, the live record
    would drift from the account exactly the way the paper account's did — and this
    one is real money.

    Falls back to the estimate only when the venue has not yet published the fill,
    and says so in the event log rather than silently passing an estimate off as
    settled.
    """
    symbol = row["symbol"]
    broker.close_position(symbol)
    price, pnl = _closing_fill(broker, symbol, row)
    estimated = price is None
    store.live_close(row["id"], exit_price=price if price else fallback_price,
                     exit_reason=reason, realised_pnl=pnl)
    store.live_event(row["id"], "close",
                     f"{reason} at {price if price else fallback_price} "
                     f"(net {pnl if pnl is not None else 'unknown'})"
                     + (" [ESTIMATED — venue fill not yet available]" if estimated else ""))
    return {"exit_price": price or fallback_price, "realised_pnl": pnl,
            "estimated": estimated}


def _closing_fill(broker, symbol: str, row: dict) -> tuple[float | None, float | None]:
    """The venue's own exit price and NET result, after commission.

    Two things this must not do, both learned from real closes:

    * **Do not read `realizedPnl` from `userTrades`.** Tabdeal's fill records carry
      only symbol/price/qty/quoteQty/commission/time/buyer/maker — there is no
      `realizedPnl` field at all, so summing it silently produced 0 and reported a
      trade's whole result as just its fees. The authoritative gross figure is on the
      *position* record from `/r/fapi/v1/position`.
    * **Do not filter fills by our own `opened_ts`.** It is recorded after the order
      returns, and on BNB it landed a fraction of a second *after* the entry fill's
      own timestamp — so the entry was excluded and only one side's commission was
      counted, halving the reported cost. Filter by the venue's `createdTime` for the
      position instead, which cannot race with it.
    """
    try:
        history = broker._get_signed("/r/fapi/v1/position") or []
        trades = broker._get_signed("/r/fapi/v1/userTrades", {"symbol": symbol}) or []
    except Exception as exc:                                   # noqa: BLE001
        log.warning("live: could not read venue records for %s: %s", symbol, exc)
        return None, None

    # Identify OUR position, not merely one with the same symbol.
    #
    # The first version matched `str(id) == vpid or not vpid`, so whenever
    # venue_position_id was missing — which it is whenever the stop attach failed —
    # `not vpid` matched the FIRST entry for that symbol, i.e. the oldest. On BNB that
    # took the gross from a position closed half an hour earlier: row 7 was recorded
    # as -0.012409 (its fees alone) when the real net was -0.005547, because the
    # +0.006853 gross belonged to it and was read off the wrong record.
    vpid = str(row.get("venue_position_id") or "")
    same = [h for h in history if h.get("symbol") == symbol]
    pos = next((h for h in same if str(h.get("id")) == vpid), None) if vpid else None
    if pos is None:
        opened_ms = float(row.get("opened_ts") or 0) * 1000
        pos = min(same, key=lambda h: abs(float(h.get("createdTime") or 0) - opened_ms),
                  default=None)
    if pos is None:
        return None, None

    # Fees belong to THIS position's window only. Summing everything after
    # `createdTime` swept in every later trade's fills too — harmless while this was
    # the newest position, and silently wrong the moment it was not.
    created = float(pos.get("createdTime") or 0)
    updated = float(pos.get("updateTime") or 0) or float("inf")
    fees = 0.0
    for t in trades:
        try:
            ts = float(t.get("time") or 0)
            if created <= ts <= updated + 2000:      # small grace for the closing fill
                fees += float(t.get("commission") or 0)
        except (TypeError, ValueError):
            continue
    try:
        gross = float(pos.get("realizedPnl") or 0)
    except (TypeError, ValueError):
        gross = 0.0
    price = None
    try:
        price = float(pos.get("avgExitPrice") or 0) or None
    except (TypeError, ValueError):
        pass
    return price, gross - fees


# --------------------------------------------------------------------------------
# Entry
# --------------------------------------------------------------------------------


# Entry is serialised. Without this, two callers — the scheduler thread and a manual
# try_open, or two overlapping cycles — can both pass the "not already held" check
# and both place an order. That happened live on 2026-08-22: orders 8462546 and
# 8462548 went in a second apart, giving double the intended size on one venue
# position and two DB rows that each recorded the same close, over-counting the loss
# by 100%.
_entry_lock = threading.Lock()


def _open_symbols() -> set[str]:
    return {r["symbol"] for r in store.live_positions("pending", "open")}


def total_notional(broker=None) -> float:
    broker = broker or _broker()
    tot = 0.0
    for p in broker.positions():
        try:
            px = float(p.get("markPrice") or 0) or (tabdeal.mark_price(p["symbol"]) or 0)
            tot += abs(float(p["positionAmt"])) * px
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
    # Non-blocking: if another caller is already placing an order, say so rather than
    # queueing up behind it to place a second one moments later.
    if not _entry_lock.acquire(blocking=False):
        return {"action": "none", "reason": "entry_in_progress"}
    try:
        return _try_open_locked(broker, cfg)
    finally:
        _entry_lock.release()


def _try_open_locked(broker, cfg: dict) -> dict:
    held = _open_symbols()
    slots_free = cfg["max_slots"] - len(held)
    if slots_free <= 0:
        return {"action": "none", "reason": "slots_full", "held": len(held)}

    cap, why = notional_cap(broker, cfg)
    notional_now = total_notional(broker)
    if notional_now >= cap:
        return {"action": "none", "reason": "notional_cap",
                "notional": round(notional_now, 2), "cap": round(cap, 2),
                "basis": why}
    cfg = {**cfg, "max_total_notional": cap, "cap_basis": why}

    # Pass our own book: the paper account's positions must not gate the live one.
    held_coins = {r["coin"] for r in store.live_positions("pending", "open")}
    errors, opened = [], []
    for row in demo.qualifying_signals(held_coins=held_coins,
                                       closed_times=store.live_last_close_times()):
        if slots_free <= 0:
            break
        if notional_now >= cap:
            break
        if row["symbol"] in held:
            continue
        # Re-read our book right before committing: the venue is authoritative and a
        # position may have appeared since the loop started.
        if row["coin"] in {r["coin"] for r in store.live_positions("pending", "open")}:
            continue
        try:
            res = _enter(broker, row, cfg, notional_now)
        except Exception as exc:                               # noqa: BLE001
            log.warning("live: %s entry failed: %s", row["coin"], exc)
            errors.append({"coin": row["coin"], "error": str(exc)[:200]})
            continue
        if res.get("action") != "opened":
            # declined for a per-signal reason (stale, inverted, rounds to zero) —
            # keep going, the next candidate may be fine
            errors.append(res)
            continue
        # Fill every free slot this pass, not just one.
        #
        # It used to `return` on the first fill, so one slot opened per completed
        # scan. Scans are ~5-6 minutes apart, so a four-slot board took twenty
        # minutes to fill — longer than the entire 5-20 minute hold it was filling
        # for, and by then the other signals had gone stale. Seen live with XRP 80.8,
        # AAVE 76.9 and SHIB 75.2 all qualifying and three slots free.
        opened.append(res)
        held.add(row["symbol"])
        slots_free -= 1
        notional_now += res.get("qty", 0) * res.get("entry", 0)

    if opened:
        return {"action": "opened", "count": len(opened), "positions": opened,
                "declined": errors or None}
    if errors:
        return {"action": "none", "reason": "all_entries_failed", "errors": errors}
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
    # Checked again here, not only in qualifying_signals: this is the last point
    # before real money moves, and an inverted plan stops out the instant it opens.
    # Must come AFTER `side` is bound — it referenced it one line too early and threw
    # UnboundLocalError on every single entry, which `try_open` caught and reported as
    # the innocuous-looking "no_signal". The engine could not open a position at all.
    if not demo.valid_geometry(side, levels):
        return {"action": "declined", "coin": row["coin"],
                "reason": "inverted_plan_geometry",
                "levels": {"entry": plan_entry, "stop": stop, "tp1": tp1, "tp2": tp2}}

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

    # Size to a notional target, and let risk fall out of the real stop distance.
    #
    # Not from a `capital` x `risk_pct` budget, deliberately. Under cross margin the
    # binding constraint is TOTAL notional — liquidation is computed on the whole
    # book, not per position — so notional is the thing to control directly. It also
    # decouples the live engine from the top-level `capital` setting, which the
    # planner uses to build plans: setting that to the real 5.27 balance made every
    # plan unfundable (84x leverage required against a 17x cap) and turned all 33
    # coins into SKIP, starving this engine of the very signals it needs.
    stop_distance = abs(mark - float(stop))
    if stop_distance <= 0:
        raise ValueError("stop distance is zero")
    spec = _spec(symbol)
    target_notional = min(cfg["max_total_notional"] / max(1, cfg["max_slots"]),
                          cfg["max_total_notional"] - notional_now)
    if target_notional <= 0:
        return {"action": "declined", "coin": row["coin"], "reason": "notional_cap"}
    qty = tabdeal_broker._round_down(target_notional / mark, spec["step_size"])
    if qty <= 0:
        return {"action": "declined", "coin": row["coin"],
                "reason": "size_rounds_to_zero",
                "target_notional": round(target_notional, 4)}
    risk_amount = qty * stop_distance          # 1R, a consequence of the real stop

    # Leverage comes from the SIGNAL, not from a fixed setting.
    #
    # The planner derives it per coin from that coin's own stop distance and the
    # profile's liquidation buffer — roughly 100 / (stop_pct x buffer) — so a wide
    # stop gets low leverage and a tight one gets more, and liquidation stays the
    # same multiple of the stop either way. Sending a blind 5x ignored all of that:
    # `plan["sizing"]["leverage"]` was never read anywhere in this file.
    #
    # Clamped to the venue ceiling and to `live_max_leverage` so a bad plan cannot
    # ask for something extreme.
    plan_lev = ((plan.get("sizing") or {}).get("leverage"))
    if isinstance(plan_lev, (int, float)) and plan_lev >= 1:
        leverage = min(float(plan_lev), cfg["max_leverage"], 100.0)
        lev_source = f"signal ({plan_lev:g}x)"
    else:
        leverage = cfg["leverage"]
        lev_source = f"fallback ({leverage:g}x — plan carried no leverage)"
    leverage = max(1.0, round(leverage))
    try:
        broker.set_leverage(symbol, leverage)
    except Exception as exc:                                   # noqa: BLE001
        log.warning("live: set_leverage %s failed: %s", symbol, exc)

    order = broker.place_order(symbol, "BUY" if side == "long" else "SELL", qty,
                               order_type="MARKET", ref_price=mark)
    # A dry run must leave no trace in live_positions. Recording one produced phantom
    # rows the venue had never heard of, which reconcile() then correctly reported as
    # "closed by the exchange" — churning the same coin open and shut and polluting
    # the record with trades that never existed.
    if order.get("dry_run"):
        return {"action": "dry_run", "coin": row["coin"], "symbol": symbol,
                "qty": qty, "entry": mark, "would_send": order.get("params")}
    pid = store.live_open(
        coin=row["coin"], symbol=symbol, side=side, status="open", quantity=qty,
        entry_price=mark, plan_entry=plan_entry, leverage=leverage,
        risk_amount=risk_amount, stop=stop, tp1=tp1, tp2=tp2,
        order_id=str(order.get("orderId") or ""), opened_at=_now_iso(),
        opened_ts=time.time(), scan_id=row.get("scan_id"), score=row.get("score"),
        plan_json=row.get("plan_json"))
    store.live_event(pid, "open", f"MARKET {side} {qty:g} @~{mark:.8g} at {leverage:g}x "
                                  f"[{lev_source}], order {order.get('orderId')}")
    log.warning("live: OPENED %s %s qty=%g @~%.8g", symbol, side, qty, mark)

    # Both levels go to the exchange: the stop for the downside, and TP1 as the
    # take-profit. Having the venue own both means a target or a stop is honoured
    # even if this process is down — the engine's own checks are the fallback, not
    # the mechanism.
    _attach_stop(broker, pid, symbol, stop, tp1)
    return {"action": "opened", "coin": row["coin"], "symbol": symbol,
            "qty": qty, "entry": mark, "leverage": leverage,
            "leverage_source": lev_source, "order": order.get("orderId")}


def _attach_stop(broker, pid: int, symbol: str, stop, tp=None) -> None:
    """Hand the stop, and normally TP1, to the exchange right after the fill.

    Both matter: the stop bounds the loss and TP1 takes the profit, and neither
    should depend on this process being alive. Passing `tp=None` clears any existing
    venue take-profit — verified against the live account.

    This is the most important call in the engine. Until it succeeds the position has
    no protection that survives this process, so a failure is logged as an error and
    recorded on the position rather than swallowed.
    """
    try:
        live = broker.position_for(symbol)
        vpid = (live or {}).get("positionId")
        if vpid is None:
            raise ValueError("venue did not report a positionId")
        broker.set_position_sl_tp(vpid, sl_price=stop, tp_price=tp, symbol=symbol)
        store.live_update(pid, sl_tp_set=1, venue_position_id=str(vpid))
        store.live_event(pid, "sltp", f"exchange SL={stop:.8g} TP={tp:.8g}")
        log.warning("live: %s exchange stop set at %.8g", symbol, stop)
    except Exception as exc:                                   # noqa: BLE001
        # Record whatever id we did manage to read: settlement needs it to identify
        # this position later, and losing it is what made the wrong record get read.
        try:
            live_now = broker.position_for(symbol)
            if live_now and live_now.get("positionId") is not None:
                store.live_update(pid, venue_position_id=str(live_now["positionId"]))
        except Exception:                                      # noqa: BLE001
            pass
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
    # One venue position per symbol, so manage one row per symbol. Duplicates close
    # the same position twice — the second attempt fails with "position not found"
    # and, worse, records the same realised PnL again.
    seen: set[str] = set()
    for row in store.live_positions("open"):
        if row["symbol"] in seen:
            store.live_close(row["id"], exit_price=None, exit_reason="duplicate",
                             realised_pnl=0.0)
            store.live_event(row["id"], "close",
                             "duplicate row for a symbol already managed; "
                             "closed with zero PnL so it cannot be double-counted")
            log.error("live: duplicate row for %s — closed as duplicate", row["symbol"])
            out.append({"symbol": row["symbol"], "action": "DEDUPE"})
            continue
        seen.add(row["symbol"])
        pos = venue.get(row["symbol"])
        if not pos:
            continue                       # reconcile() handles the vanished case
        try:
            out.append(_manage_one(broker, row, pos, cfg))
        except Exception as exc:                               # noqa: BLE001
            log.warning("live: manage %s failed: %s", row["symbol"], exc)
            out.append({"symbol": row["symbol"], "error": str(exc)})
    return out


_last_sample: dict[int, float] = {}
_sample_warned = False


def _record_sample(row: dict, cfg: dict, *, mark, upnl, qty, r_now, held_h) -> None:
    """Write one point of a position's history, thinned to the configured interval.

    Why keep this at all: a closed trade records only its endpoints, so every
    post-mortem in this project so far has had to reconstruct what happened in the
    middle from exchange candles - which shows what the market did, but not what the
    engine could see or what it was judging against. This records our own view:
    the mark we acted on, the unrealised P&L net of the round trip, and the verdict
    and score live at that instant.

    Never allowed to break management: a monitoring loop must not stop managing real
    money because a write failed.
    """
    pid = row.get("id")
    if not pid:
        return
    now = time.time()
    if now - _last_sample.get(pid, 0.0) < cfg["history_interval_seconds"]:
        return
    _last_sample[pid] = now
    try:
        # demo.settings(), not settings(): the venue key lives on the demo config,
        # and live.settings() has no "exchange". Reading it here raised KeyError on
        # every single sample - the identical mistake that once aborted
        # _profit_signal_check and left profitable live positions unmanaged.
        scan = store.result_for(row["coin"], demo.settings()["exchange"]) or {}
        round_trip = qty * mark * (tabdeal.TAKER_FEE_PCT / 100.0) * 2
        store.live_sample_add(
            pid, ts=now,
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            mark=round(float(mark), 10), upnl=round(float(upnl), 8),
            upnl_net=round(float(upnl) - round_trip, 8), r=round(float(r_now), 4),
            held_h=round(float(held_h), 4),
            verdict=scan.get("verdict"), score=scan.get("score"))
    except Exception as exc:                                   # noqa: BLE001
        # Warn, and only once per process. This was debug-level and a KeyError on
        # every call was therefore invisible: the table stayed empty and nothing
        # said why. A swallowed exception still has to announce itself.
        global _sample_warned
        if not _sample_warned:
            _sample_warned = True
            log.warning("live: position sampling is failing (%s: %s) - history will "
                        "be empty until this is fixed", type(exc).__name__, exc)
        else:
            log.debug("live: sample write failed for %s: %s", row.get("symbol"), exc)


def _manage_one(broker, row: dict, pos: dict, cfg: dict) -> dict:
    symbol = row["symbol"]
    # The venue reports markPrice and unRealizedProfit as "0" on a live position, so
    # both are computed here from our own mark and the venue's entry price. Trusting
    # its zeros would leave the manager believing every trade is exactly flat —
    # signal exit would never fire, and the time stop would fire on everything.
    mark = tabdeal.mark_price(symbol)
    if not mark:
        return {"symbol": symbol, "action": "SKIP", "reason": "no mark price"}
    entry = float(pos.get("entryPrice") or row.get("entry_price") or 0)
    qty = abs(float(pos.get("positionAmt") or 0))
    upnl = (mark - entry) * qty if row["side"] == "long" else (entry - mark) * qty
    risk = float(row.get("risk_amount") or 0)
    r_now = (upnl / risk) if risk else 0.0
    held_h = (time.time() - float(row.get("opened_ts") or time.time())) / 3600.0

    _record_sample(row, cfg, mark=mark, upnl=upnl, qty=qty, r_now=r_now,
                   held_h=held_h)

    # The stop and the target belong to the exchange. Everything below is judgement.

    # TP1 reached: CLOSE. It is the take-profit, not a stop-move.
    #
    # The exchange also carries TP1 as a venue-side take-profit, so in practice the
    # venue usually closes first and reconcile() records it. This check is the
    # fallback for the window where price has traded through TP1 but the venue has
    # not yet acted — without it a target could be passed and given back while the
    # engine looked on.
    if _reached(row["side"], mark, row["tp1"]):
        out = settle(broker, row, "tp1", float(row["tp1"]))
        log.warning("live: %s TP1 hit at %s — closed (net %s)", symbol,
                    out["exit_price"], out["realised_pnl"])
        return {"symbol": symbol, "action": "CLOSE", "reason": "tp1", **out}

    round_trip_cost = qty * mark * (tabdeal.TAKER_FEE_PCT / 100.0) * 2

    # The ONLY exit this engine takes: held an hour or more, and net profitable.
    #
    # "Net" is after the round trip, not merely above entry. At 0.1% a side a
    # position up 0.15% of notional is still a loss once closed, and nine of the
    # first 23 live trades moved in our favour and lost money exactly that way.
    #
    # There used to be a `signal_exit` here that banked a winner as soon as profit
    # cleared the fee, whatever the clock said. It fired at a median hold of ELEVEN
    # minutes for a mean +0.230% gross - about +0.11R - while losers ran to the full
    # -1.0R. Cutting winners at a tenth of R and losers at a whole one is roughly 1:9
    # against us, and needs a ~90% win rate to break even. The hour is what lets a
    # winner become worth more than its own fee.
    #
    # Everything below the hour, and everything in loss at any hour, is left to the
    # exchange's own stop and take-profit. That is deliberate: the venue holds both
    # levels on the position itself, so they are honoured even if this process dies.
    close_bar = round_trip_cost * cfg["profit_close_fee_multiple"]
    if held_h >= cfg["profit_close_after_h"] and upnl > close_bar:
        # Past the hour and genuinely in profit. Now the only question left is
        # whether the signal still earns the risk of staying in.
        keep, why = _signal_supports_holding(row, cfg)
        if keep:
            return {"symbol": symbol, "action": "HOLD", "reason": "riding_signal",
                    "detail": why, "r": round(r_now, 3),
                    "held_h": round(held_h, 2), "net": round(upnl - round_trip_cost, 8)}

        out = settle(broker, row, "profit_close", mark)
        log.warning("live: %s profit_close after %.2fh at %.3fR (gross %.5f vs bar "
                    "%.5f, net %s) - %s", symbol, held_h, r_now, upnl, close_bar,
                    out["realised_pnl"], why)
        # An engine-initiated close must never reduce the balance. The bar above is
        # measured at the mark and the close is a MARKET order, so slippage can still
        # in principle land it under water - if it ever does, say so loudly instead of
        # letting it disappear into the ledger the way PEPE's -0.00039 did.
        settled = out.get("realised_pnl")
        if settled is not None and float(settled) <= 0:
            log.error("live: %s profit_close SETTLED NEGATIVE (%.6f) - the %.2fx fee "
                      "cushion did not cover slippage; raise "
                      "demo.profit_close_fee_multiple",
                      symbol, float(settled), cfg["profit_close_fee_multiple"])
        return {"symbol": symbol, "action": "CLOSE", "reason": "profit_close",
                "held_h": round(held_h, 2), "r": round(r_now, 3),
                "detail": why, **out}

    # Adverse exit: the setup has lapsed and the trade is DOWN.
    #
    # DISABLED by default since 2026-08-23 at the operator's instruction ("do not
    # touch positions in loss"). A losing position is left entirely to its exchange
    # stop.
    #
    # The measurement that motivated this branch, kept because switching the flag
    # back on is a one-line change and the trade-off should be visible at the point
    # of decision: across the first 23 live trades a winner was re-checked every
    # cycle and closed once its setup lapsed, while a loser was checked against
    # nothing. Winners banked +0.11R, losers realised the full -1.0R, and six
    # stop-outs were 89% of all loss at a median hold of 548 minutes. Leaving losers
    # alone restores that asymmetry.
    if (cfg["adverse_exit_enabled"] and upnl < 0
            and held_h >= cfg["adverse_exit_after_h"]):
        still, reason = demo._profit_signal_check(row)
        if reason is not None and not still:
            out = settle(broker, row, "adverse_exit", mark)
            log.warning("live: %s adverse_exit at %.3fR after %.2fh (%s)",
                        symbol, r_now, held_h, reason)
            return {"symbol": symbol, "action": "CLOSE", "reason": "adverse_exit",
                    "detail": reason, "r": round(r_now, 3),
                    "held_h": round(held_h, 2), **out}

    # No time stop.
    #
    # It used to close anything held past `time_stop_hours` with `0 <= upnl < floor`.
    # That range includes a position in profit but BELOW the round trip, so firing it
    # booked a certain loss on a trade that had simply not paid for itself yet - the
    # same mistake the old signal_exit made, just later. Anything genuinely net
    # profitable is already taken by profit_close at the one-hour mark, so all this
    # could still catch was the cases where closing costs money.
    #
    # A position that is flat or down now waits for the exchange's own stop or
    # take-profit. Both sit on the position at the venue, so neither depends on this
    # process being alive.

    return {"symbol": symbol, "action": "HOLD", "r": round(r_now, 3),
            "held_h": round(held_h, 2)}


def _reached(side: str, mark: float, level) -> bool:
    """One-sided, never range containment. See demo._touched and Round 11."""
    if level is None:
        return False
    return mark >= float(level) if side == "long" else mark <= float(level)


def _risk_pct(cfg: dict) -> float:
    """Indicative risk per position as a % of the live balance. Reporting only.

    Sizing does not use this — `_enter` sizes to a notional target and derives 1R
    from the actual stop distance. This exists so the dashboard can express that as
    a percentage of the account.
    """
    per_notional = cfg["max_total_notional"] / max(1, cfg["max_slots"])
    return (per_notional * 0.015) / cfg["capital"] * 100.0 if cfg["capital"] else 0.0


def _spec(symbol: str) -> dict:
    from . import paper                                        # noqa: PLC0415
    return paper.contract_spec(symbol)


# --------------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------------


_last_prune = 0.0


def cycle() -> dict:
    global _last_prune
    broker = _broker()
    rec = reconcile(broker)
    managed = manage(broker)
    filled = backfill_unsettled(broker)
    out = {"reconcile": rec, "managed": managed}
    if filled:
        out["backfilled"] = filled

    # Bound the sample table. Hourly is often enough for a table that grows a few
    # rows a minute, and keeps this off the hot path of a 3-second loop.
    cfg = settings()
    now = time.time()
    if now - _last_prune > 3600:
        _last_prune = now
        try:
            dropped = store.live_samples_prune(now - cfg["history_keep_days"] * 86400)
            if dropped:
                out["pruned_samples"] = dropped
        except Exception as exc:                               # noqa: BLE001
            log.debug("live: sample prune failed: %s", exc)
    return out


def history(include_closed: int = 5) -> dict:
    """Per-position price history for the dashboard chart.

    Returns open positions first, each with its sampled series, plus the most
    recently closed ones so a chart does not lose a line the instant a trade ends.
    """
    open_rows = store.live_positions("open")
    closed_rows = store.live_closed()[:max(0, include_closed)]
    rows = list(open_rows) + list(closed_rows)
    series = store.live_samples_for([r["id"] for r in rows])
    out = []
    for r in rows:
        pts = series.get(int(r["id"]), [])
        out.append({
            "id": r["id"], "coin": r["coin"], "symbol": r["symbol"],
            "side": r["side"], "status": r["status"],
            "entry": r.get("entry_price"), "quantity": r.get("quantity"),
            "stop": r.get("stop"),
            "tp1": r.get("tp1"), "leverage": r.get("leverage"),
            "score": r.get("score"), "opened_at": r.get("opened_at"),
            "opened_ts": r.get("opened_ts"),
            "closed_at": r.get("closed_at"), "exit_price": r.get("exit_price"),
            "exit_reason": r.get("exit_reason"),
            "realised_pnl": r.get("realised_pnl"),
            "points": [{"ts": p["ts"], "mark": p["mark"], "upnl": p["upnl"],
                        "upnl_net": p["upnl_net"], "r": p["r"],
                        "verdict": p["verdict"], "score": p["score"]}
                       for p in pts],
        })
    return {"positions": out, "server_ts": time.time()}


def scheduler_loop(stop_event) -> None:
    """Run the engine for as long as the server lives and live trading is armed.

    Entry is triggered by a NEW COMPLETED SCAN, not by a fixed timer.

    The timer version fired every `entry_interval_seconds` regardless of whether the
    candidate set had changed, and the two clocks drifted against each other. Seen
    live on 2026-08-22: the engine attempted entry at 09:51:52, seconds before scan
    706 finished at 09:52:33 and made BNB a TAKE — so a valid signal sat unacted on
    for the next five minutes. On a 30-minute scalp that is a sixth of the trade's
    life, and by the time the timer came round the drift guard may reject the plan as
    stale. Acting on scan completion removes the drift entirely: a new candidate set
    is exactly when there is something new to decide.

    `entry_interval_seconds` is kept as a floor so a burst of scans cannot produce a
    burst of entries.
    """
    last_entry = 0.0
    last_scan_seen = None
    while not stop_event.is_set():
        cfg = settings()
        if cfg["enabled"]:
            try:
                out = cycle()
                if out["reconcile"]["closed"] or out["reconcile"]["orphans"]:
                    log.warning("live reconcile: %s", out["reconcile"])

                scan_id = _latest_scan_id()
                now = time.time()
                # A new completed scan IS the rate limiter — scans are minutes apart
                # and each one is a fresh candidate set. The extra `entry_interval`
                # floor on top of that just made the engine skip whole scans whenever
                # an entry happened to land mid-cycle, which is how an 80.8-scoring
                # XRP sat unacted on with three slots free.
                if scan_id is not None and scan_id != last_scan_seen:
                    last_scan_seen, last_entry = scan_id, now
                    opened = try_open()
                    # Log every outcome, not just a fill. A silent non-entry is what
                    # made the timer drift above take so long to spot.
                    log.warning("live entry attempt (scan %s): %s", scan_id, opened)
                out["scan_id"] = scan_id
                store.set_kv("live.last_cycle", {"at": _now_iso(), **out})
            except Exception as exc:                           # noqa: BLE001
                log.warning("live cycle failed: %s", exc)
        stop_event.wait(cfg["cycle_seconds"])


def _latest_scan_id() -> int | None:
    """The most recent COMPLETED scan. A running scan is a partial candidate set."""
    try:
        row = store.latest_scan_done() if hasattr(store, "latest_scan_done") else None
        if row:
            return int(row["id"])
    except Exception:                                          # noqa: BLE001
        pass
    try:
        with store.tx() as conn:
            r = conn.execute("SELECT MAX(id) AS m FROM scans "
                             "WHERE status = 'done'").fetchone()
            return int(r["m"]) if r and r["m"] is not None else None
    except Exception as exc:                                   # noqa: BLE001
        log.warning("live: could not read the latest scan id: %s", exc)
        return None


_btc_cache: dict = {"ts": 0.0, "price": None}


def btc_price() -> float | None:
    """BTC mark, cached briefly.

    Shown in the header on every page, so it is read by both the 3s live poll and
    the main board poll. Without the cache that is a venue round trip per request
    per tab, for a number that does not need to be fresher than a few seconds.
    """
    now = time.time()
    if now - _btc_cache["ts"] < 5.0:
        return _btc_cache["price"]
    try:
        _btc_cache["price"] = tabdeal.mark_price("BTC_USDT")
    except Exception:                                          # noqa: BLE001
        pass                                    # keep the last good price
    _btc_cache["ts"] = now
    return _btc_cache["price"]


def _live_pnl(positions: list[dict], cfg: dict) -> list[dict]:
    """Per-position mark and P&L, computed here rather than in the browser.

    Two reasons this moved server-side.

    The dashboard was deriving P&L from `quantity` and a mark taken from the sampled
    history, which is thinned to one point every 15 seconds while the tab refreshes
    every 3 - so it showed a figure up to 15s behind the venue's own. It also showed
    only the net number, which can never agree with Tabdeal's «سود و زیان ناخالص»
    because that figure is gross.

    Both are returned now. `gross` is (mark - entry) x qty, which is exactly what the
    venue displays and should tie out against it. `net` subtracts the round trip -
    what the position is worth if closed right now - and is the number the engine's
    own exit rule tests, so the two columns explain each other.

    The venue reports markPrice as "0" on a live position, so the mark is read
    per symbol; mark_price() is cached briefly upstream, so repeated symbols and
    repeated polls do not each cost a request.
    """
    out = []
    fee_rt = (tabdeal.TAKER_FEE_PCT / 100.0) * 2
    for p in positions:
        sym = p.get("symbol")
        try:
            mark = tabdeal.mark_price(sym)
        except Exception:                                      # noqa: BLE001
            mark = None
        entry = float(p.get("entryPrice") or 0)
        qty = abs(float(p.get("positionAmt") or 0))
        row = {"symbol": sym, "mark": mark, "entry": entry or None, "quantity": qty}
        if mark and entry and qty:
            sgn = 1 if float(p.get("positionAmt") or 0) > 0 else -1
            gross = (mark - entry) * qty * sgn
            cost = qty * mark * fee_rt
            row.update({
                "gross": round(gross, 8),
                "cost": round(cost, 8),
                "net": round(gross - cost, 8),
                "pct": round((mark - entry) / entry * 100 * sgn, 4),
            })
        out.append(row)
    return out


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
        "btc": btc_price(),
        "balance": bal,
        "venue_positions": positions,
        "live": _live_pnl(positions, cfg),
        "tracked": store.live_positions("pending", "open"),
        "closed": store.live_closed()[:50],
        "slots": {"used": len(positions), "max": cfg["max_slots"]},
        "equity": account_equity(broker),
        "notional": {"used": round(total_notional(broker), 2),
                     "cap": round(notional_cap(broker, cfg)[0], 2),
                     "basis": notional_cap(broker, cfg)[1]},
    }
