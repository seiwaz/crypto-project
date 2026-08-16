"""Closed-trade reporting for the demo.

Two design rules, both about not overstating what a small sample can tell you:

* **Breakdown by exit reason is the headline, not an appendix.** If `time_stop` and
  `review_exit` dominate, the entries are fine and the timing is wrong. If `stopped`
  dominates while MFE was routinely above 1R, the entries are fine and the management
  is wrong. Those two look identical on an equity curve and have opposite fixes.
* **Under 30 closed trades, no conclusions.** The analyses still render, because
  watching them form is useful, but every one of them carries the insufficient-sample
  label and the recommendation section stays empty.

Everything here is arithmetic over stored rows. It reconciles with the journal by
construction — the aggregates are computed from the same `paper_positions` rows the
per-trade table lists, never from a running total kept elsewhere.
"""

from __future__ import annotations

import json
from collections import defaultdict

from . import store

# Below this, the spread on any win rate or expectancy estimate is wider than the
# differences anyone would act on.
MIN_SAMPLE = 30

EXIT_REASONS = ("tp2", "tp1", "stopped", "liquidated", "time_stop", "review_exit")


def _net_pnl(trade: dict) -> float | None:
    """P&L after every cost the trade actually incurred.

    `realised_pnl` is already net of exit fees, because those are taken at the moment
    each leg closes. The entry fee is not: it leaves the balance when the position
    opens, so it has to be subtracted here or a 1R winner reports as slightly better
    than 1R. Funding is a signed cash-flow, negative when the position paid.
    """
    pnl = trade.get("realised_pnl")
    if pnl is None:
        return None
    return (float(pnl)
            - float(trade.get("entry_fee") or 0.0)
            + float(trade.get("funding_paid") or 0.0))


def _r_multiple(trade: dict) -> float | None:
    """Result in R: net P&L over the risk that was on at entry."""
    risk = trade.get("risk_amount")
    net = _net_pnl(trade)
    if not risk or net is None:
        return None
    return net / float(risk)


def _costs(trade: dict) -> dict:
    entry_fee = float(trade.get("entry_fee") or 0.0)
    exit_fee = float(trade.get("exit_fee") or 0.0)
    funding = float(trade.get("funding_paid") or 0.0)
    return {
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "funding": funding,
        "total": entry_fee + exit_fee - funding,
    }


def _hours_held(row: dict) -> float | None:
    """Wall-clock hours a closed position was open, from its own timestamps."""
    from datetime import datetime
    try:
        opened = datetime.fromisoformat(row["opened_at"])
        closed = datetime.fromisoformat(row["closed_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return (closed - opened).total_seconds() / 3600.0


def _risk_of(trade: dict) -> float | None:
    """The R unit in USDT for a trade, recovered from its own numbers."""
    r, net = trade.get("r"), trade.get("net_pnl")
    if not r or net is None:
        return None
    return net / r


def trades() -> list[dict]:
    """Every closed trade, with the fields the report needs, newest first."""
    out = []
    for row in store.paper_closed_positions():
        plan = json.loads(row["plan_json"]) if row.get("plan_json") else None
        qual = (plan or {}).get("qualification") or {}
        out.append({
            "id": row["id"],
            "coin": row["coin"],
            "symbol": row["symbol"],
            "side": row["side"],
            "entry_price": row["entry_price"],
            "exit_price": row["exit_price"],
            "opened_at": row["opened_at"],
            "closed_at": row["closed_at"],
            "exit_reason": row["exit_reason"],
            "r": _r_multiple(row),
            "gross_pnl": row.get("realised_pnl"),
            "costs": _costs(row),
            "net_pnl": _net_pnl(row),
            "mfe_r": row.get("mfe_r"),
            "mae_r": row.get("mae_r"),
            "bars_held": row.get("bars_held"),
            "hours_held": _hours_held(row),
            "mfe_hours": row.get("mfe_hours"),
            "mae_hours": row.get("mae_hours"),
            "entry_slippage_pct": row.get("entry_slippage_pct"),
            "btc_bias": row.get("btc_bias"),
            "takes_available": row.get("takes_available"),
            "leverage": row.get("leverage"),
            "score_at_entry": row.get("score"),
            "verdict_at_entry": row.get("verdict"),
            "gates_at_entry": qual.get("gates"),
        })
    return out


def _drawdown(rs: list[float]) -> float:
    """Max peak-to-trough decline of the cumulative R curve."""
    peak = 0.0
    equity = 0.0
    worst = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return worst


def aggregate(rows: list[dict]) -> dict:
    """Win rate, expectancy, totals, drawdown, costs — from the trade rows alone.

    Reported in USDT. R is still computed per trade and kept in the rows, because it
    is the unit that compares trades of different sizes and the learning-loop
    analyses group by it — but the headline figures are cash, which is what the
    account actually gained or lost.
    """
    scored = [t for t in rows if t["r"] is not None]
    rs = [t["r"] for t in scored]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]

    nets = [t["net_pnl"] for t in rows if t["net_pnl"] is not None]
    net_wins = [n for n in nets if n > 0]
    net_losses = [n for n in nets if n <= 0]

    acct = store.paper_account() or {}
    start = float(acct.get("starting_capital") or 0.0)
    balance = float(acct.get("balance") or 0.0)

    return {
        "closed": len(rows),
        "scored": len(scored),
        "win_rate": (len(wins) / len(scored) * 100.0) if scored else None,
        "avg_win_r": (sum(wins) / len(wins)) if wins else None,
        "avg_loss_r": (sum(losses) / len(losses)) if losses else None,
        "expectancy_r": (sum(rs) / len(rs)) if rs else None,
        "avg_win_usdt": (sum(net_wins) / len(net_wins)) if net_wins else None,
        "avg_loss_usdt": (sum(net_losses) / len(net_losses)) if net_losses else None,
        "expectancy_usdt": (sum(nets) / len(nets)) if nets else None,
        "net_pnl": sum(nets) if nets else None,
        "max_drawdown_usdt": _drawdown(nets) if nets else None,
        # None, not 0.0, when nothing has closed. A total of "0R" is a measured
        # result meaning the wins cancelled the losses; an empty journal has no
        # result at all, and the UI must be able to say "no data" instead.
        "total_r": sum(rs) if rs else None,
        "max_drawdown_r": _drawdown(rs) if rs else None,
        "costs_paid": sum(t["costs"]["total"] for t in rows),
        "starting_capital": start,
        "balance": balance,
        "return_pct": ((balance / start - 1.0) * 100.0) if start else None,
    }


def by_exit_reason(rows: list[dict]) -> list[dict]:
    """The most useful view and the least common one.

    Grouping by how a trade ended separates a timing problem from a management
    problem — including the MFE of stopped trades, which is what says whether the
    move was ever there to capture.
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    for t in rows:
        buckets[t["exit_reason"] or "unknown"].append(t)

    out = []
    for reason, group in buckets.items():
        rs = [t["r"] for t in group if t["r"] is not None]
        mfes = [t["mfe_r"] for t in group if t["mfe_r"] is not None]
        nets = [t["net_pnl"] for t in group if t["net_pnl"] is not None]
        # MFE in cash needs the risk that was on at entry, which differs per trade,
        # so it is converted per trade and then averaged — not averaged in R and
        # scaled, which would be wrong the moment two trades risked different amounts.
        mfe_cash = [t["mfe_r"] * t_risk for t in group
                    if t["mfe_r"] is not None
                    and (t_risk := _risk_of(t)) is not None]
        out.append({
            "reason": reason,
            "count": len(group),
            "share_pct": len(group) / len(rows) * 100.0 if rows else 0.0,
            "total_r": sum(rs) if rs else None,
            "avg_r": (sum(rs) / len(rs)) if rs else None,
            "avg_mfe_r": (sum(mfes) / len(mfes)) if mfes else None,
            "total_pnl": sum(nets) if nets else None,
            "avg_pnl": (sum(nets) / len(nets)) if nets else None,
            "avg_mfe_usdt": (sum(mfe_cash) / len(mfe_cash)) if mfe_cash else None,
            "mfe_above_1r": sum(1 for m in mfes if m >= 1.0),
        })
    order = {r: i for i, r in enumerate(EXIT_REASONS)}
    out.sort(key=lambda b: (order.get(b["reason"], 99), -b["count"]))
    return out


def by_score_band(rows: list[dict]) -> list[dict]:
    """Does a 90 actually beat a 72? If not, the weights are decoration."""
    bands = (("70-79", 70, 80), ("80-89", 80, 90), ("90+", 90, 1e9))
    out = []
    for label, lo, hi in bands:
        group = [t for t in rows
                 if t["score_at_entry"] is not None
                 and lo <= float(t["score_at_entry"]) < hi
                 and t["r"] is not None]
        rs = [t["r"] for t in group]
        out.append({
            "band": label,
            "count": len(group),
            "win_rate": (sum(1 for r in rs if r > 0) / len(rs) * 100.0) if rs else None,
            "expectancy_r": (sum(rs) / len(rs)) if rs else None,
            # None rather than 0.0 for an empty band, for the same reason the overall
            # total is: "+0.00R" reads as a measured wash, not as "nothing here yet".
            "total_r": sum(rs) if rs else None,
        })
    return out


def selection_quality(limit: int = 2000) -> dict:
    """What the filler passed over, and why.

    A trade declined for `slots_full` at rank 1 is a very different signal from one
    declined for `insufficient_margin`: the first says the pool was richer than the
    account could hold, the second says the sizing is wrong. Both are invisible
    unless the declines are counted.
    """
    rows = store.paper_decisions(limit=limit)
    if not rows:
        return {"decisions": 0}
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        key = r["action"] if r["action"] == "opened" else (r["code"] or "declined")
        buckets[key].append(r)

    out = {"decisions": len(rows), "by_outcome": []}
    for key, group in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        scores = [r["score"] for r in group if r["score"] is not None]
        ranks = [r["rank"] for r in group if r["rank"] is not None]
        out["by_outcome"].append({
            "outcome": key,
            "count": len(group),
            "avg_score": (sum(scores) / len(scores)) if scores else None,
            "best_score": max(scores) if scores else None,
            "avg_rank": (sum(ranks) / len(ranks)) if ranks else None,
        })
    return out


def timing(rows: list[dict]) -> dict:
    """How long trades took to reach their best and worst points.

    Separates a management problem from a thesis problem on the time axis: a trade
    that peaked early and was held for hours afterwards was given back, and one that
    never moved was never right.
    """
    with_mfe = [t for t in rows if t.get("mfe_hours") is not None]
    if not with_mfe:
        return {"trades": 0}
    peaked_early = [t for t in with_mfe
                    if t.get("hours_held") and t["mfe_hours"] < t["hours_held"] / 2]
    return {
        "trades": len(with_mfe),
        "avg_hours_to_mfe": sum(t["mfe_hours"] for t in with_mfe) / len(with_mfe),
        "peaked_in_first_half": len(peaked_early),
        "peaked_in_first_half_pct": len(peaked_early) / len(with_mfe) * 100.0,
    }


def execution(rows: list[dict]) -> dict:
    """Fill quality against the plan, and what regime trades were taken in."""
    slips = [t["entry_slippage_pct"] for t in rows
             if t.get("entry_slippage_pct") is not None]
    regimes: dict[str, int] = defaultdict(int)
    for t in rows:
        regimes[t.get("btc_bias") or "unknown"] += 1
    return {
        "trades_with_slippage": len(slips),
        "avg_slippage_pct": (sum(slips) / len(slips)) if slips else None,
        "worst_slippage_pct": (max(slips) if slips else None),
        "by_btc_regime": dict(regimes),
    }


def build() -> dict:
    """The whole report, with an explicit statement of what the sample can support."""
    rows = trades()
    n = len(rows)
    return {
        "trades": rows,
        "aggregate": aggregate(rows),
        "by_exit_reason": by_exit_reason(rows),
        "by_score_band": by_score_band(rows),
        # Captured while running, for the optimisation pass: what was passed over,
        # how quickly trades peaked, and how well fills matched their plans.
        "selection": selection_quality(),
        "timing": timing(rows),
        "execution": execution(rows),
        "sample": {
            "closed": n,
            "minimum": MIN_SAMPLE,
            "sufficient": n >= MIN_SAMPLE,
            "remaining": max(0, MIN_SAMPLE - n),
        },
        # Findings are evidence for a human decision, never an auto-tuner. Nothing in
        # this project edits the skill's thresholds or scoring weights: an agent that
        # retunes itself on 20 trades overfits to noise and destroys the only clean
        # record of how the original system performed.
        "findings": [] if n < MIN_SAMPLE else _findings(rows),
        "findings_withheld": n < MIN_SAMPLE,
    }


def _findings(rows: list[dict]) -> list[dict]:
    """Observations with their evidence attached. Recommendations, never actions."""
    out = []
    exits = {b["reason"]: b for b in by_exit_reason(rows)}

    stopped = exits.get("stopped")
    if stopped and stopped["share_pct"] >= 40 and stopped["mfe_above_1r"]:
        out.append({
            "code": "stops_after_run",
            "evidence": {
                "stopped_share_pct": stopped["share_pct"],
                "stopped_count": stopped["count"],
                "mfe_above_1r": stopped["mfe_above_1r"],
                "sample": len(rows),
            },
        })

    timed = (exits.get("time_stop", {}).get("count", 0)
             + exits.get("review_exit", {}).get("count", 0))
    if timed and timed / len(rows) >= 0.4:
        out.append({
            "code": "timing_dominates",
            "evidence": {"time_and_review_exits": timed, "sample": len(rows)},
        })

    bands = [b for b in by_score_band(rows) if b["count"] >= 5]
    if len(bands) >= 2:
        best = max(bands, key=lambda b: b["expectancy_r"] or -99)
        worst = min(bands, key=lambda b: b["expectancy_r"] or 99)
        if best["band"] < worst["band"]:
            out.append({
                "code": "score_not_monotonic",
                "evidence": {"bands": bands},
            })
    return out
