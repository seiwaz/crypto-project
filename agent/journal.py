"""Plain-text journal of the demo account.

The report on the dashboard and this text are computed from the same rows, so they
reconcile by construction rather than by agreement — there is no running total kept
anywhere that could drift from the trades that produced it.

Numbers print as plain ASCII with fixed precision. This is meant to be diffed, piped
and pasted into a bug report.
"""

from __future__ import annotations

from . import demo, report, store


def _risk(trade):
    """The R unit in USDT for a closed trade, from its own numbers."""
    r, net = trade.get('r'), trade.get('net_pnl')
    return None if not r or net is None else net / r

DASH = "-"


def _n(value, digits: int = 2, dash: str = "no data") -> str:
    if value is None:
        return dash
    return f"{value:,.{digits}f}"


def _signed(value, digits: int = 2, dash: str = "no data") -> str:
    """Signed number, or `dash` when absent.

    Fixed-width columns pass a short dash: "no data" is eight characters and silently
    overflowed a seven-wide field, printing "-0.001no datano data" with no separator.
    """
    if value is None:
        return dash
    return f"{value:+,.{digits}f}"


def _unit(text: str, suffix: str) -> str:
    """Append a unit only to an actual figure — "no data USDT" is not a reading."""
    return text if text.strip() in ("no data", "--") else f"{text}{suffix}"


def _cash(r_multiple, risk) -> float | None:
    """An R-multiple back into USDT, using the risk that was on at entry."""
    if r_multiple is None or not risk:
        return None
    return float(r_multiple) * float(risk)


def _rule(width: int = 78) -> str:
    return DASH * width


def account_block() -> list[str]:
    acct = store.paper_account()
    if not acct:
        return ["No demo account. Open the Demo trading tab or POST /api/demo/cycle."]
    st = demo.state()
    a, slots, heat = st["account"], st["slots"], st["heat"]
    lines = [
        "ACCOUNT",
        _rule(),
        f"  venue            {a['exchange']}",
        f"  started with     {_n(a['starting_capital'])} USDT",
        f"  balance          {_n(a['balance'])}",
        f"  equity           {_n(a['equity'])}   ({_signed(a['return_pct'], 3)}%)",
        f"  unrealised       {_signed(a['open_pnl'], 4)}",
        f"  margin used      {_n(a['used_margin'])}   available {_n(a['available_margin'])}",
        f"  slots            {slots['filled']} of {slots['total']}",
    ]
    if slots["reason"]:
        lines.append(f"  slot state       {slots['reason']['code']}")
        detail = slots["reason"].get("detail") or {}
        if detail.get("needs") is not None:
            lines.append(f"                   next candidate {detail.get('coin')} "
                         f"needs {_n(detail['needs'])}, "
                         f"available {_n(detail['available'])}")
    lines.append(f"  portfolio heat   {_n(heat['used_pct'])}% of {_n(heat['cap_pct'], 1)}% cap")
    if not st["correlation_filter"]["available"]:
        lines.append("  correlation      NOT ENFORCED (market_context.py missing)")
    return lines


def open_block() -> list[str]:
    st = demo.state()
    lines = ["", "OPEN POSITIONS", _rule()]
    if not st["positions"]:
        lines.append("  none")
        return lines
    # Every figure in USDT. R is still tracked per position for the score-band
    # analysis, but the board and this table report cash.
    header = (f"  {'COIN':<7}{'SIDE':<6}{'CONTRACTS':>12}{'ENTRY':>13}{'MARK':>13}"
              f"{'LAST':>13}{'uPNL':>10}{'MFE':>10}{'MAE':>10}{'MR%':>7}")
    lines.append(header)
    for p in st["positions"]:
        s = p["state"] or {}
        lines.append(
            f"  {p['coin']:<7}{p['side']:<6}{p['contracts']:>12,.1f}"
            f"{p['entry_price']:>13,.6f}"
            f"{(s.get('mark') or 0):>13,.6f}"
            f"{_n(p.get('last_price'), 6, '--'):>13}"
            f"{_signed(s.get('unrealised_pnl'), 4, '--'):>10}"
            f"{_signed(_cash(p.get('mfe_r'), p.get('risk_amount')), 4, '--'):>10}"
            f"{_signed(_cash(p.get('mae_r'), p.get('risk_amount')), 4, '--'):>10}"
            f"{_n(s.get('margin_ratio_pct'), 2, '--'):>7}"
        )
        lines.append(f"         stop {_n(p.get('stop'), 6)}  tp1 {_n(p.get('tp1'), 6)}  "
                     f"tp2 {_n(p.get('tp2'), 6)}  liq {_n(s.get('liquidation_price'), 6)}  "
                     f"lev {_n(p.get('leverage'))}x  funding {_signed(p.get('funding_paid'), 5)}")
    return lines


def closed_block() -> list[str]:
    rows = report.trades()
    lines = ["", "CLOSED TRADES", _rule()]
    if not rows:
        lines.append("  none yet")
        return lines
    for t in rows:
        lines.append(
            f"  {t['coin']:<7}{t['side']:<6}{t['exit_reason'] or '?':<12}"
            f"entry {_n(t['entry_price'], 6)}  exit {_n(t['exit_price'], 6)}  "
            f"net {_signed(t['net_pnl'], 4)} USDT"
        )
        c = t["costs"]
        lines.append(
            f"         opened {t['opened_at']}  closed {t['closed_at']}\n"
            f"         mfe {_signed(_cash(t['mfe_r'], _risk(t)), 4)}  "
            f"mae {_signed(_cash(t['mae_r'], _risk(t)), 4)}  "
            f"lev {_n(t['leverage'])}x  score {_n(t['score_at_entry'], 1)}  "
            f"fees {_n(c['entry_fee'], 4)}+{_n(c['exit_fee'], 4)}  "
            f"funding {_signed(c['funding'], 5)}"
        )
    return lines


def report_block() -> list[str]:
    r = report.build()
    a = r["aggregate"]
    lines = ["", "REPORT", _rule(),
             f"  closed trades    {a['closed']}",
             f"  win rate         {_unit(_n(a['win_rate'], 1), '%')}",
             f"  avg win / loss   {_unit(_signed(a['avg_win_usdt'], 4), ' USDT')}"
             f" / {_unit(_signed(a['avg_loss_usdt'], 4), ' USDT')}",
             f"  expectancy       {_unit(_signed(a['expectancy_usdt'], 4), ' USDT per trade')}",
             f"  net P&L          {_unit(_signed(a['net_pnl'], 4), ' USDT')}",
             f"  max drawdown     {_unit(_signed(a['max_drawdown_usdt'], 4), ' USDT')}",
             f"  costs paid       {_n(a['costs_paid'], 4)} USDT",
             f"  equity vs start  {_n(a['balance'])} / {_n(a['starting_capital'])}"
             f"  ({_signed(a['return_pct'], 3)}%)"]

    if r["by_exit_reason"]:
        lines += ["", "  BY EXIT REASON",
                  f"    {'reason':<14}{'n':>4}{'share':>9}{'avg P&L':>11}"
                  f"{'total P&L':>12}{'avg MFE':>11}"]
        for b in r["by_exit_reason"]:
            lines.append(f"    {b['reason']:<14}{b['count']:>4}"
                         f"{_n(b['share_pct'], 1, '--'):>8}%{_signed(b['avg_pnl'], 4, '--'):>11}"
                         f"{_signed(b['total_pnl'], 4, '--'):>12}"
                         f"{_signed(b['avg_mfe_usdt'], 4, '--'):>11}")

    lines += ["", "  BY SCORE BAND",
              f"    {'band':<8}{'n':>4}{'win%':>9}{'expectancy R':>14}{'total R':>10}"]
    for b in r["by_score_band"]:
        lines.append(f"    {b['band']:<8}{b['count']:>4}{_n(b['win_rate'], 1, '--'):>9}"
                     f"{_signed(b['expectancy_r'], 3, '--'):>13}"
                     f"{_signed(b['total_r'], 2, '--'):>10}")

    s = r["sample"]
    lines.append("")
    if not s["sufficient"]:
        lines.append(f"  INSUFFICIENT SAMPLE — {s['closed']} of {s['minimum']} closed "
                     f"trades. No conclusions are drawn, and no findings are shown.")
    elif r["findings"]:
        lines.append("  FINDINGS (evidence for a human decision — nothing was changed)")
        for f in r["findings"]:
            lines.append(f"    - {f['code']}: {f['evidence']}")
    else:
        lines.append("  Sample is sufficient; no finding met its threshold.")
    return lines


def events_block(limit: int = 40) -> list[str]:
    rows = store.paper_events(limit=limit)
    lines = ["", f"EVENT LOG (most recent {min(limit, len(rows))})", _rule()]
    if not rows:
        lines.append("  none")
        return lines
    by_id = {p["id"]: p["coin"] for p in
             store.paper_open_positions() + store.paper_closed_positions()}
    for e in rows:
        coin = by_id.get(e["position_id"], "?")
        amount = f"  {_signed(e['amount'], 5)}" if e["amount"] is not None else ""
        lines.append(f"  {e['at']}  {coin:<7}{e['kind']:<9}"
                     f"{(e['action'] or ''):<14}{(e['detail'] or '')}{amount}")
    return lines


def text() -> str:
    parts = (account_block() + open_block() + closed_block()
             + report_block() + events_block())
    return "\n".join(parts)


if __name__ == "__main__":
    print(text())
