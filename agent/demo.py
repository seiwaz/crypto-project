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

from . import config, correlation, paper, skill, store, toobit

log = logging.getLogger("demo")

DEFAULT_SLOTS = 5
DEFAULT_HEAT_CAP_PCT = 6.0
MIN_SCORE = 70.0

# Ceiling on concurrent positions when slots track the signal count. Not a risk
# limit — the heat cap is — but a bound on how thin the account will slice itself,
# and on how many contracts the venue is asked about each cycle.
MAX_SLOTS = 20
# Below this, a position is too small for the venue's minimum lots to accept it
# across most of the watchlist.
MIN_RISK_PCT = 0.2

# How long a tripped breaker holds. Long enough to interrupt a bad run, short enough
# that the account is not halted for a day by a cluster of small losses.
BREAKER_COOLDOWN_HOURS = 6.0

# --- Counter-trend gate -------------------------------------------------------
#
# Stands in for the skill's "trade opposes a strong_trend" and BTC "opposed_strong"
# gates, which live in market_context.py and are not installed. Their absence was
# measured, not assumed: 25 of the first 30 trades were shorts taken while BTC sat
# above its 4H EMA200 and rose 2.66% over 48h. Those shorts lost 5.65 USDT; the five
# longs made 0.74. Direction, not exit timing, was the dominant loss.
#
# Only coins that actually follow BTC are gated. A coin at 0.2 correlation is not
# fighting the trend in any meaningful sense, so vetoing it would cost signals for
# nothing.
COUNTER_TREND_MIN_CORRELATION = 0.45

# --- Give-back exit (OFF by default — the data rejected it) --------------------
#
# The hypothesis was that trades peak early and hand the gain back, so a retrace from
# MFE should be protected. Replayed against the first 30 closed trades it does not
# hold: only 6 reached the 0.35R arm level, and for 5 of those 6 the trade's actual
# close beat what the give-back would have kept — FIL closed +0.467R against a
# protected +0.256R, MORPHO +0.364R against +0.291R.
#
# The losses do not come from surrendering winners. They come from the 24 trades that
# never went anywhere at all: median MFE +0.125R, and not one of 30 reached 1.0R
# against a TP1 set at 1.5R. Left available and off, because the reasoning may hold
# on a different profile or a trending market — but nothing here supports it today.
GIVE_BACK_ENABLED = False
GIVE_BACK_ARM_R = 0.35
GIVE_BACK_FRACTION = 0.6

# Same-direction positions in coins that move together are one position wearing
# several tickers, and the drawdown arrives all at once looking like several
# independent failures.
#
# The brief named 0.9. Measured against this watchlist that threshold never fires:
# across 25 Toobit perps, correlation to BTC is median 0.47 over 20 days of 4H bars
# and median 0.62 over 120 days of daily bars, and the only thing at or above 0.9 is
# BTC against itself. A filter that cannot trigger while the UI reports it as
# enforced is worse than no filter, so the operative default is 0.75 — where four to
# ten coins actually sit, depending on window — and it is settable per deployment.
#
# Correlation is measured on daily bars whatever the trading profile is: it
# describes how two assets relate as a regime, not how they behave on the timeframe
# a signal happens to fire on.
MAX_CORRELATED_SAME_SIDE = 2
CORRELATION_THRESHOLD = 0.75

# Correlation timeframe. Daily is the more stable estimate — measured across 25
# Toobit perps, median correlation to BTC is 0.62 on daily bars against 0.47 on 4H,
# because shorter bars carry more idiosyncratic noise and drag the estimate down.
# 4H is set here by request; it makes the filter weaker, not stronger, so the
# threshold may need lowering to compensate.
CORRELATION_INTERVAL = "4h"

# Timeframe for the instrument's own trend filter. 1H reacts sooner than the 4H
# decision timeframe, so it vetoes earlier and more often.
TREND_FILTER_INTERVAL = "1h"

# Entries are attempted only this often. Management still runs every cycle.
ENTRY_INTERVAL_SECONDS = 1200

# --- Maker entries ------------------------------------------------------------
#
# Entering with a resting limit slightly better than the market pays the maker fee
# (0.0200%) instead of the taker fee (0.0600%), which matters here: fees were 53.9%
# of gross P&L over the first 30 trades.
#
# The saving is not free, and simulating it as though it were would be the single
# most flattering lie this broker could tell. A limit that does not fill is a trade
# not taken, and the ones that fail to fill are disproportionately the trades that
# ran away in your favour — so the fills you keep are biased toward the trades that
# came back. Worse, as the skill puts it, an unfilled limit "can fill *because* the
# thesis died". Both are modelled: the order rests, fills only if price actually
# trades through it, and is cancelled if it has not filled within the timeout.
MAKER_ENTRY_ENABLED = True
MAKER_OFFSET_PCT = 0.1
MAKER_TIMEOUT_MINUTES = 30.0


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
    last_entry_at = 0.0
    while not stop_event.is_set():
        cfg = settings()
        if cfg["enabled"]:
            try:
                out = cycle()
                closed = [r for r in out["results"] if r.get("action") == "CLOSE"]
                # Two clocks, not one.
                #
                # Managing a position is urgent — a stop or target can be passed in
                # any minute, so that runs every cycle. Opening one is not: the
                # candidate list only changes when a scan completes, so attempting
                # entries every minute re-evaluates the same signals ~20 times and
                # can open a position on a plan whose prices are already stale.
                # Entries therefore run on their own, slower timer aligned with the
                # scan cadence.
                filled = {"opened": [], "declined": [], "slots_free": 0}
                now = paper.now_ts()
                if now - last_entry_at >= cfg["entry_interval_seconds"] or closed:
                    last_entry_at = now
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
        "correlation_threshold": float(demo.get("correlation_threshold")
                                       or CORRELATION_THRESHOLD),
        "max_correlated_same_side": int(demo.get("max_correlated_same_side")
                                        or MAX_CORRELATED_SAME_SIDE),
        "auto_slots": bool(demo.get("auto_slots")),
        "max_slots": int(demo.get("max_slots") or MAX_SLOTS),
        "min_risk_pct": float(demo.get("min_risk_pct") or MIN_RISK_PCT),
        "breaker_cooldown_hours": float(demo.get("breaker_cooldown_hours")
                                       or BREAKER_COOLDOWN_HOURS),
        "counter_trend_gate": demo.get("counter_trend_gate", True),
        "correlation_interval": demo.get("correlation_interval") or CORRELATION_INTERVAL,
        "trend_filter_interval": demo.get("trend_filter_interval") or TREND_FILTER_INTERVAL,
        "entry_interval_seconds": int(demo.get("entry_interval_seconds")
                                      or ENTRY_INTERVAL_SECONDS),
        "maker_entry": demo.get("maker_entry", MAKER_ENTRY_ENABLED),
        "maker_offset_pct": float(demo.get("maker_offset_pct") or MAKER_OFFSET_PCT),
        "maker_timeout_minutes": float(demo.get("maker_timeout_minutes")
                                       or MAKER_TIMEOUT_MINUTES),
        "give_back_enabled": demo.get("give_back_enabled", GIVE_BACK_ENABLED),
        "give_back_arm_r": float(demo.get("give_back_arm_r") or GIVE_BACK_ARM_R),
        "give_back_fraction": float(demo.get("give_back_fraction")
                                    or GIVE_BACK_FRACTION),
        # Gate on /api/demo/reset, checked in server.py. Configured directly in the
        # server's live settings.json only - never via strategy-tuning.json (that
        # file is git-tracked and public) and never returned to the browser (not in
        # server.py's public_settings() allowlist). None means the gate is off.
        "reset_password": demo.get("reset_password"),
    }


def counter_trend(row: dict) -> dict | None:
    """Is this trade fighting the trend of the instrument it is trading?

    The instrument's own trend decides, not BTC's. Gating on correlation to BTC was
    measured to be backwards — it blocked shorts on coins below their own EMA200 with
    alpha of -13% to -36%, while allowing shorts on coins outperforming BTC by 50-60%.
    A weak alt in a rising market is a relative-weakness short, which is a reason to
    take the trade rather than to refuse it.

    BTC still matters, but only where it should: as a veto on a coin with no trend of
    its own that simply tracks a strongly trending BTC.

    Returns the blocking detail, or None to allow. Missing data allows the trade.
    """
    if not settings()["counter_trend_gate"]:
        return None

    own = correlation.coin_regime(row["symbol"],
                                  settings()["trend_filter_interval"])
    if own and own["label"] != "range":
        fighting = (own["label"] == "up" and row["side"] == "short") or \
                   (own["label"] == "down" and row["side"] == "long")
        if fighting:
            return {"reason": "own_trend", "regime": own["label"],
                    "move_pct": own["move_pct"], "side": row["side"]}
        return None            # trading with its own trend — BTC does not override

    # No trend of its own: fall back to BTC, and only for coins that really follow it.
    btc = correlation.btc_regime()
    ctx = correlation.btc_context(row["symbol"])
    if not btc or btc["label"] == "range" or not ctx:
        return None
    if abs(ctx["correlation"]) < CORRELATION_THRESHOLD:
        return None
    fighting = (btc["label"] == "up" and row["side"] == "short") or \
               (btc["label"] == "down" and row["side"] == "long")
    if not fighting:
        return None
    return {"reason": "btc_proxy", "regime": btc["label"],
            "move_pct": btc["move_pct"], "correlation": ctx["correlation"],
            "side": row["side"]}


def clear_breaker() -> dict:
    """Lift the trading halt now, by marking everything closed so far as spent."""
    store.set_kv("demo.breaker_cleared_at", store.now_iso())
    return {"cleared_at": store.get_kv("demo.breaker_cleared_at"),
            "breaker": circuit_breaker()}


def take_count() -> int:
    """Qualifying TAKEs in the most recent scan, whether or not a slot is free."""
    cfg = settings()
    n = 0
    for row in store.latest_results(cfg["exchange"]):
        if row.get("verdict") == "TAKE" and (row.get("score") or 0) >= MIN_SCORE:
            n += 1
    return n


def target_slots() -> int:
    """Capacity: how many positions the account is willing to hold at once.

    This is the *ceiling*, not a prediction of how many will fill. What actually
    limits the board is the supply of TAKE signals and the heat cap, and both are
    already enforced elsewhere — so capacity is a fixed number rather than something
    that tracks the signal count.

    It used to track it, and that was a feedback loop: fewer TAKEs meant fewer slots,
    fewer slots meant a larger share of the heat budget each, larger positions failed
    the liquidation-buffer gate more often, and that produced fewer TAKEs again. The
    live result was risk per trade rising to 1.2% while TAKEs collapsed from 15 to 5,
    and a board reporting "6 of 5" because capacity had shrunk below what was already
    open. Capacity must not depend on anything downstream of capacity.
    """
    cfg = settings()
    return cfg["max_slots"] if cfg["auto_slots"] else cfg["slots"]


def derived_risk_pct() -> float:
    """Risk per trade, so that a full board spends exactly the heat budget.

    Portfolio heat is the risk budget — 6% of equity — and at 1% per trade it is
    exhausted after six positions however many signals qualify. Raising capacity
    alone would only move refusals from "slots_full" to "heat_cap".

    So risk is the budget divided by capacity: 20 slots at 0.30% each fills the same
    6%, spending it across more and less correlated bets instead of fewer larger
    ones. Dividing by *capacity* rather than by the current signal count is what
    keeps it constant between scans, which is what makes plan sizing reproducible.
    The floor stops positions shrinking below what the venue's lot sizes accept.
    """
    cfg = settings()
    if not cfg["auto_slots"]:
        return float(config.load_settings().get("risk_pct") or 1.0)
    return max(cfg["min_risk_pct"], cfg["heat_cap_pct"] / max(1, cfg["max_slots"]))


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
        row["btc_context"] = correlation.btc_context(
            symbol, settings()["correlation_interval"])
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
    # Never report capacity below what is already held; "6 of 5" is not a state.
    slots = max(target_slots(), len(rows))
    if slots != int(acct["slots"]):
        with store.tx() as conn:
            conn.execute("UPDATE paper_account SET slots = ? WHERE id = 1", (slots,))

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
            "auto_slots": settings()["auto_slots"],
            "risk_pct": derived_risk_pct(),
            "takes_available": take_count(),
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


def correlated_same_side(row: dict, open_positions: list[dict],
                         interval: str | None = None) -> dict | None:
    """Would taking this trade breach the cap on correlated same-direction risk?

    Five positions at 0.9 correlation in the same direction are one position at five
    times the size. The drawdown arrives all at once and looks like five independent
    failures, which is the most misleading thing a record can contain.

    Returns the blocking detail, or None when the trade is allowed.
    """
    interval = interval or settings()["correlation_interval"]
    ctx = correlation.btc_context(row["symbol"], interval)
    if ctx is None:
        # Unknown is not the same as uncorrelated. Allow the trade — refusing on
        # missing data would quietly stop trading whole coins — but say so.
        return None

    cfg = settings()
    threshold = cfg["correlation_threshold"]
    cap = cfg["max_correlated_same_side"]

    same_side = [p for p in open_positions if p["side"] == row["side"]]
    correlated = 0
    for pos in same_side:
        other = correlation.btc_context(pos["symbol"], interval)
        if other and abs(other["correlation"]) >= threshold:
            correlated += 1

    if abs(ctx["correlation"]) >= threshold and correlated >= cap:
        return {"correlation": ctx["correlation"], "beta": ctx["beta"],
                "already_open": correlated, "cap": cap, "threshold": threshold,
                "side": row["side"]}
    return None


def _correlation_filter_status() -> dict:
    """The correlation filter needs `btc_context`, which market_context.py provides.

    Reported rather than silently skipped: five 0.9-correlated same-direction
    positions are one position at five times the size, and it produces a single
    catastrophic drawdown that looks like five independent failures.
    """
    from . import skill  # noqa: PLC0415 — avoids a cycle at import time
    from_skill = (skill.scripts_dir() / "market_context.py").exists()
    local = correlation.available()
    return {
        "available": from_skill or local,
        "threshold": settings()["correlation_threshold"],
        "max_same_side": settings()["max_correlated_same_side"],
        "interval": CORRELATION_INTERVAL,
        # Which implementation is enforcing it matters: the local one is a stand-in
        # with parameters chosen here, not the skill's calibrated context run.
        "source": "market_context.py" if from_skill else ("local" if local else None),
        "reason": None if (from_skill or local) else "unavailable",
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
    # Both open (filled) and pending (resting maker limit, not yet filled) positions
    # block a re-entry into the same coin. `paper_open_positions()` alone missed the
    # pending ones — found 2026-08-20, live: a coin's own resting limit order doesn't
    # make it into `status = 'open'` until it fills, so a scan a few minutes later
    # (still inside the up-to-maker_timeout_minutes pending window) saw the coin as
    # "not open" and queued a second entry for it. This account did exactly that on
    # WIF — two live positions in the same coin at once, doubling its single-name risk
    # beyond the one-slot-per-coin design the slot/heat model assumes.
    open_coins = {p["coin"] for p in store.paper_open_positions()}
    open_coins |= {p["coin"] for p in store.paper_pending_positions()}
    closed_at = store.paper_last_close_times()
    out = []
    for row in store.latest_results(cfg["exchange"]):
        if row.get("verdict") != "TAKE":
            continue
        # A tied/near-tied direction (skill.side_from_direction's DIRECTION_MARGIN)
        # is not a real signal — it's the least-wrong of two options with no real
        # edge, which is exactly how this account traded 213/213 long before this
        # was enforced (2026-08-20). The flag already existed for the UI; it just
        # wasn't stopping anything from actually trading.
        if row.get("side_tied"):
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

    for pending in store.paper_pending_positions():
        try:
            outcome = _work_pending(pending)
        except (paper.PaperError, toobit.ToobitError) as exc:
            log.warning("pending %s failed: %s", pending["coin"], exc)
            continue
        if outcome:
            balance += outcome.pop("balance_delta", 0.0)
            results.append(outcome)

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


def _work_pending(pos: dict) -> dict | None:
    """Fill a resting limit if price traded through it, or cancel it on timeout."""
    cfg = settings()
    spec = paper.contract_spec(pos["symbol"])
    limit = float(pos["limit_price"])
    candle = _latest_candle(pos["symbol"])
    mark, _ = paper.mark_price(spec)
    if mark is None:
        return None

    low = min(candle["low"], mark) if candle else mark
    high = max(candle["high"], mark) if candle else mark
    touched = low <= limit if pos["side"] == "long" else high >= limit

    if touched:
        fee = paper.fee(paper.notional(float(pos["contracts"]), limit, spec),
                        maker=True)
        store.paper_update(pos["id"], status="open", entry_price=limit,
                           opened_ts=paper.now_ts(), entry_fee=fee)
        store.paper_event(pos["id"], "open", amount=-fee,
                          detail=f"maker fill at {limit:.8g}")
        return {"coin": pos["coin"], "action": "FILL", "price": limit,
                "fee": fee, "balance_delta": -fee}

    age_min = (paper.now_ts() - float(pos["placed_ts"] or paper.now_ts())) / 60.0
    if age_min >= cfg["maker_timeout_minutes"]:
        store.paper_cancel(pos["id"], "unfilled")
        store.paper_event(pos["id"], "cancel",
                          detail=f"limit {limit:.8g} unfilled after {age_min:.0f}m")
        return {"coin": pos["coin"], "action": "CANCEL", "reason": "unfilled"}
    return None


def _cycle_one(pos: dict) -> dict:
    spec = paper.contract_spec(pos["symbol"])
    mark, mark_source = paper.mark_price(spec)
    if mark is None:
        return {"coin": pos["coin"], "action": "SKIP", "reason": "no mark price"}

    plan = _plan_of(pos)
    balance_delta = _accrue_funding(pos, spec, mark)
    st = paper.position_state(pos, spec, mark)
    hours_open = (paper.now_ts() - float(pos["opened_ts"])) / 3600.0
    _record_excursions(pos, st, hours_open)

    # One point on this position's path, every cycle.
    store.paper_sample(
        pos["id"], ts=paper.now_ts(), hours_held=round(hours_open, 4), mark=mark,
        unrealised=st.get("unrealised_pnl"), r=st.get("unrealised_r"),
        margin_ratio=st.get("margin_ratio_pct"))

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
    r_now = st.get("unrealised_r")
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

    # Protect a favourable excursion that is decaying, rather than waiting for the
    # time stop to hand it back in full.
    cfg = settings()
    mfe = pos.get("mfe_r")
    if (cfg["give_back_enabled"] and r_now is not None and mfe is not None
            and float(mfe) >= cfg["give_back_arm_r"]
            and r_now <= float(mfe) * (1.0 - cfg["give_back_fraction"])):
        realised = _close(pos, spec, mark, "gave_back")
        return {"coin": pos["coin"], "action": "CLOSE", "reason": "gave_back",
                "mfe_r": float(mfe), "unrealised_r": r_now,
                "exit_price": mark, "realised": realised,
                "balance_delta": balance_delta + realised}

    moved = _trail_stop(pos, plan, spec)

    # Active management for a position currently in profit, at the user's request
    # (2026-08-20): re-check the setup every cycle rather than once per 8h funding
    # period (_review, below) — an 8h cadence can't matter to a 30-minute scalp hold.
    # Still favoured -> let it float past the time-stop deadline instead of cutting a
    # working trade off early. No longer favoured -> take the profit now rather than
    # risk giving it back waiting for a level. Losing positions are untouched here;
    # they already float unconditionally per the time-stop rule below.
    pnl_now = st.get("unrealised_pnl")
    still_favoured = False
    if pnl_now is not None and pnl_now > 0:
        still_favoured, signal_reason = _profit_signal_check(pos)
        if signal_reason is not None:
            realised = _close(pos, spec, mark, "signal_exit")
            return {"coin": pos["coin"], "action": "CLOSE", "reason": "signal_exit",
                    "detail": signal_reason, "unrealised_usdt": pnl_now,
                    "exit_price": mark, "realised": realised,
                    "balance_delta": balance_delta + realised}

    # Time stop, measured in hours and compared in USDT.
    #
    # Floating, at the user's request (2026-08-20): the clock only closes a position
    # that is flat or ahead but not making the required progress — it never locks in
    # a loss on a timer. A position currently underwater is left alone past the
    # deadline, re-checked every cycle, until price either recovers to breakeven+ (at
    # which point the same floor test applies again) or the trade resolves on its own
    # terms — the real stop-loss or a take-profit. This trades one risk for another:
    # a losing position is no longer bounded by time, only by its stop distance, so it
    # can occupy a slot/margin for longer than before. Watch for that trade-off rather
    # than assuming it away. A profitable position whose setup still checks out
    # (still_favoured, just above) is exempted the same way — the clock doesn't cut a
    # trade off early just because it hasn't cleared the fixed USDT floor yet.
    hours_held = (paper.now_ts() - float(pos["opened_ts"])) / 3600.0
    limit_hours = time_stop_hours()
    floor_usdt = time_stop_floor_usdt(pos)
    if (not still_favoured and hours_held >= limit_hours and pnl_now is not None
            and 0 <= pnl_now < floor_usdt):
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
            "floor_usdt": floor_usdt, "floating_on_signal": still_favoured,
            "balance_delta": balance_delta}


def _touched(side: str, high: float, low: float, level: float | None) -> bool:
    return level is not None and low <= float(level) <= high


def _reduce_at_tp1(pos: dict, spec: dict, price: float) -> float:
    """Close half the position at TP1 and lock the runner's stop at the TP1 price.

    Changed 2026-08-20, at the user's request: the stop used to move to breakeven
    plus accumulated costs, so a reversal right after TP1 gave back almost the whole
    runner and left the trade netting close to zero beyond the banked TP1 half.
    Locking at the TP1 price instead means a reversal can take back only the runner's
    *further* upside, never the gain already proven at TP1 — the trade's floor
    becomes "roughly the TP1 R-multiple," not "roughly breakeven." This is strictly
    more conservative than breakeven and gives back less on a round-trip, at the cost
    of being easier to stop out of the runner on ordinary noise right after TP1 fires
    (the stop is now much closer to price than a breakeven stop would have been).
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

    runner_stop = price   # the TP1 fill price itself, not breakeven

    margin_released = float(pos["margin"]) * (closing / qty)   # un-reserved, not cash
    store.paper_update(
        pos["id"],
        contracts=remaining,
        original_contracts=float(pos.get("original_contracts") or qty),
        margin=float(pos["margin"]) - margin_released,
        tp1_filled=1,
        stop_moved_to_be=1,
        stop=runner_stop,
        exit_fee=float(pos.get("exit_fee") or 0.0) + fee,
        realised_partial=float(pos.get("realised_partial") or 0.0) + realised,
    )
    store.paper_event(pos["id"], "action", action="REDUCE", amount=realised,
                      detail=f"TP1 {closing:g} of {qty:g} contracts @ {price:g}; "
                             f"stop locked at TP1 {runner_stop:.8g}")
    pos.update(contracts=remaining, tp1_filled=1, stop=runner_stop,
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


def _profit_signal_check(pos: dict) -> tuple[bool, str | None]:
    """For a position currently in profit: is the setup that justified it still there?

    Reuses the same latest-scan verdict/score `_review` checks, but on every cycle
    instead of once per 8h funding period — a 30-minute scalp hold can't wait 8 hours
    for a stale re-check to matter.

    Returns `(still_favoured, close_reason)`. Three outcomes, not two: a confirmed
    TAKE is `(True, None)` and licenses floating past the time-stop deadline; a
    confirmed non-TAKE is `(False, "reason")` and closes immediately. Missing scan
    data is `(False, None)` — deliberately *not* the same as a confirmed TAKE. Failing
    open into "still favoured" here would suppress the ordinary floor-based time-stop
    for every position that simply lacks a fresh scan row, which is the common case,
    not an edge case — this was caught by the existing test suite regressing before
    the fix. Missing data means "no opinion," so it falls through to the unmodified
    time-stop logic below rather than either forcing a close or forcing a float.
    """
    row = store.result_for(pos["coin"], pos["exchange"])
    if not row:
        return False, None
    verdict, score = row.get("verdict"), row.get("score")
    if verdict == "TAKE" and score is not None and float(score) >= MIN_SCORE:
        return True, None
    return False, f"verdict is now {verdict} ({score}) while in profit"


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


def _record_excursions(pos: dict, st: dict, hours_held: float) -> None:
    """Track the best and worst the trade reached, and when it reached them.

    The timing is the point. "+1.4R" says a move existed; "+1.4R after 20 minutes,
    closed 40 hours later" says it was there and was given back, which is a
    management finding. The same number reached on the final bar is not.
    """
    r = st.get("unrealised_r")
    if r is None:
        return
    mfe, mae = pos.get("mfe_r"), pos.get("mae_r")
    updates = {}
    if mfe is None or r > float(mfe):
        updates.update(mfe_r=r, mfe_at=store.now_iso(), mfe_hours=round(hours_held, 4))
    if mae is None or r < float(mae):
        updates.update(mae_r=r, mae_at=store.now_iso(), mae_hours=round(hours_held, 4))
    if updates:
        store.paper_update(pos["id"], **updates)
        pos.update(updates)


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

    tripped = circuit_breaker()

    heat = snapshot["heat"]["used_pct"]
    cap = snapshot["heat"]["cap_pct"]
    equity = snapshot["account"]["equity"]
    available = snapshot["account"]["available_margin"]

    pool = qualifying_signals()

    def record(row, rank, action, code, mark=None, detail=None):
        """Log the decision, taken or not.

        Every candidate is written, including the ones never reached because the
        slots filled first — "slots_full" on a score of 91 is exactly the case worth
        finding later.
        """
        try:
            store.paper_decision(
                ts=paper.now_ts(), scan_id=row.get("scan_id"), coin=row["coin"],
                symbol=row.get("symbol"), side=row.get("side"),
                score=row.get("score"), rank=rank, action=action, code=code,
                mark=mark, detail=detail)
        except Exception as exc:                              # noqa: BLE001
            log.debug("could not record decision for %s: %s", row.get("coin"), exc)

    # A full board and a tripped breaker are still decisions worth recording. The
    # early returns that used to sit here skipped the loop entirely, so the case most
    # worth finding later — a score of 91 that never got a slot — left no trace at
    # all. Both conditions now fall through and are logged per candidate.
    for rank, row in enumerate(pool, start=1):
        if tripped:
            record(row, rank, "declined", "circuit_breaker",
                   detail=json.dumps(tripped, default=str))
            continue
        if free <= 0:
            record(row, rank, "declined", "slots_full")
            continue
        try:
            proposal = _proposal(row, equity)
        except (paper.PaperError, toobit.ToobitError, ValueError) as exc:
            declined.append({"coin": row["coin"], "code": "unavailable",
                             "detail": str(exc)})
            record(row, rank, "declined", "unavailable", detail=str(exc)[:200])
            continue
        if proposal is None:
            declined.append({"coin": row["coin"], "code": "no_plan"})
            record(row, rank, "declined", "no_plan")
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
            record(row, rank, "declined", "insufficient_margin",
                   mark=proposal["entry"],
                   detail=f"needs {cost:.2f}, available {available:.2f}")
            continue

        against = counter_trend(row)
        if against:
            declined.append({"coin": row["coin"], "code": "counter_trend", **against})
            record(row, rank, "declined", "counter_trend",
                   detail=(f"BTC {against['regime']} {against['btc_move_pct']:+.2f}%, "
                           f"{row['side']} at corr {against['correlation']:.2f}"))
            continue

        # Same reasoning as the open_coins guard above: a pending order carries the
        # same committed risk as an open one and must count toward the cap.
        blocked = correlated_same_side(
            row, store.paper_open_positions() + store.paper_pending_positions())
        if blocked:
            declined.append({"coin": row["coin"], "code": "correlated", **blocked})
            record(row, rank, "declined", "correlated", mark=proposal["entry"],
                   detail=(f"corr {blocked['correlation']:.2f} beta {blocked['beta']:.2f}, "
                           f"{blocked['already_open']} same-side already at the "
                           f"{blocked['cap']} cap"))
            continue

        added_heat = proposal["risk_amount"] / equity * 100.0 if equity > 0 else 0.0
        if heat + added_heat > cap:
            record(row, rank, "declined", "heat_cap", mark=proposal["entry"],
                   detail=f"would add {added_heat:.2f}% to {heat:.2f}% of {cap:.1f}%")
            # The sixth signal that would breach the cap is declined, not shrunk to
            # fit. Sizing down to squeeze it in would mean the position no longer
            # matches the plan that qualified it.
            declined.append({"coin": row["coin"], "code": "heat_cap",
                             "would_add_pct": added_heat,
                             "heat_pct": heat, "cap_pct": cap})
            continue

        pid = _open(row, proposal, slot=snapshot["slots"]["filled"] + len(opened) + 1,
                    rank=rank, pool_size=len(pool))
        record(row, rank, "opened", None, mark=proposal["entry"])
        opened.append({"coin": row["coin"], "id": pid,
                       "risk_amount": proposal["risk_amount"]})
        heat += added_heat
        available -= cost
        free -= 1

    outcome = {"opened": opened, "declined": declined, "slots_free": free,
               "candidates": len(pool)}
    if tripped:
        outcome["circuit_breaker"] = tripped
    store.set_kv("demo.last_fill", outcome)
    return outcome


def circuit_breaker() -> dict | None:
    """Has the account taken enough damage recently to stop opening new positions?

    It expires. The first version counted the loss streak with no time limit and
    said a winner would clear it — but a tripped breaker stops new positions, so no
    trade can close, so the streak can never be broken. It deadlocked the account
    permanently: seven straight losses against a limit of three, and every TAKE
    declined for hours with nothing able to change it.

    A trading halt is meant to interrupt a bad run, not end trading. So only losses
    inside the cooldown window count, which means the breaker lifts by itself once
    the account has sat out that long. `./run.sh demo clear-breaker` lifts it sooner.
    """
    acct = store.paper_account()
    if not acct:
        return None
    cfg = settings()
    profile = cfg["profile"]
    max_losses, drawdown_fraction = CIRCUIT_BREAKER.get(profile, (3, 0.05))
    window_h = cfg["breaker_cooldown_hours"]

    cleared_at = store.get_kv("demo.breaker_cleared_at") or ""
    cutoff = paper.now_ts() - window_h * 3600

    streak = 0
    for trade in store.paper_closed_positions():          # newest first
        closed_at = str(trade.get("closed_at") or "")
        if cleared_at and closed_at <= cleared_at:
            break                                          # manually cleared
        ts = _iso_to_ts(closed_at)
        if ts is not None and ts < cutoff:
            break                                          # older than the window
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
                "profile": profile, "cooldown_hours": window_h}
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


def _iso_to_ts(value: str) -> float | None:
    from datetime import datetime                           # noqa: PLC0415
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _btc_bias_label() -> str | None:
    """BTC's own trend at entry, so results can be split by regime later.

    An altcoin short taken while BTC is falling and one taken while BTC is rallying
    are different trades. Without this tag the sample mixes them and every average
    across it is the average of two populations.
    """
    try:
        bias = toobit._btc_bias("1D", 300)
    except Exception:                                         # noqa: BLE001
        return None
    if not bias:
        return None
    return "bullish" if bias.get("bullish") else "bearish"


def _open(row: dict, proposal: dict, slot: int, rank: int = 0,
          pool_size: int = 0) -> int:
    # The plan named an entry; the fill happens at the current mark. The gap is
    # execution drift, and it is signed against the position: a short filled below
    # its planned entry started worse off.
    plan_entry = ((proposal["plan"].get("levels") or {}).get("entry"))
    slippage = None
    if isinstance(plan_entry, (int, float)) and plan_entry:
        raw = (proposal["entry"] - plan_entry) / plan_entry * 100.0
        slippage = raw if row["side"] == "long" else -raw

    cfg = settings()
    status, limit_price, entry_fee = "open", None, proposal["entry_fee"]
    if cfg["maker_entry"]:
        # Better than market: below for a long, above for a short.
        off = cfg["maker_offset_pct"] / 100.0
        limit_price = (proposal["entry"] * (1 - off) if row["side"] == "long"
                       else proposal["entry"] * (1 + off))
        limit_price = paper.round_to_tick(limit_price, proposal["spec"]["tick_size"])
        status = "pending"
        entry_fee = 0.0                      # charged on fill, at the maker rate

    pid = store.paper_open(
        status=status, limit_price=limit_price, placed_ts=paper.now_ts(),
        maker=1 if cfg["maker_entry"] else 0,
        coin=row["coin"], symbol=row["symbol"], exchange=row["exchange"],
        side=row["side"], slot=slot,
        contracts=proposal["contracts"], entry_price=proposal["entry"],
        leverage=proposal["leverage"], margin=proposal["margin"],
        risk_amount=proposal["risk_amount"],
        stop=proposal["stop"], tp1=proposal["tp1"], tp2=proposal["tp2"],
        opened_ts=paper.now_ts(), entry_fee=entry_fee,
        scan_id=row.get("scan_id"), score=row.get("score"),
        verdict=row.get("verdict"),
        plan_json=json.dumps(proposal["plan"], ensure_ascii=False),
        plan_entry=plan_entry,
        entry_slippage_pct=slippage,
        btc_bias=_btc_bias_label(),
        takes_available=pool_size,
    )
    if status == "open":
        # The entry fee leaves the account the moment the position opens.
        acct = store.paper_account()
        store.paper_set_balance(float(acct["balance"]) - entry_fee)
        store.paper_event(pid, "open", detail=f"slot {slot}, score {row.get('score')}")
    else:
        store.paper_event(pid, "place", detail=(
            f"maker limit {limit_price:.8g} ({cfg['maker_offset_pct']:g}% better "
            f"than {proposal['entry']:.8g}), slot {slot}, score {row.get('score')}"))
    return pid
