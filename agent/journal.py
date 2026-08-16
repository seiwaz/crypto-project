"""Plain-text journal of the demo account.

The report on the dashboard and this text are computed from the same rows, so they
reconcile by construction rather than by agreement — there is no running total kept
anywhere that could drift from the trades that produced it.

Numbers print as plain ASCII with fixed precision. This is meant to be diffed, piped
and pasted into a bug report.
"""

from __future__ import annotations

from . import demo, report, store

DASH = "-"


def _n(value, digits: int = 2, dash: str = "no data") -> str:
    if value is None:
        return dash
    return f"{value:,.{digits}f}"


def _signed(value, digits: int = 2) -> str:
    if value is None:
        return "no data"
    return f"{value:+,.{digits}f}"


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
    header = (f"  {'COIN':<7}{'SIDE':<6}{'CONTRACTS':>12}{'ENTRY':>13}{'MARK':>13}"
              f"{'uPNL':>10}{'R':>8}{'MFE':>7}{'MAE':>7}{'MR%':>7}")
    lines.append(header)
    for p in st["positions"]:
        s = p["state"] or {}
        lines.append(
            f"  {p['coin']:<7}{p['side']:<6}{p['contracts']:>12,.1f}"
            f"{p['entry_price']:>13,.6f}"
            f"{(s.get('mark') or 0):>13,.6f}"
            f"{_signed(s.get('unrealised_pnl'), 4):>10}"
            f"{_signed(s.get('unrealised_r'), 3):>8}"
            f"{_signed(p.get('mfe_r'), 2):>7}"
            f"{_signed(p.get('mae_r'), 2):>7}"
            f"{_n(s.get('margin_ratio_pct')):>7}"
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
            f"R {_signed(t['r'], 3)}  net {_signed(t['net_pnl'], 4)}"
        )
        c = t["costs"]
        lines.append(
            f"         opened {t['opened_at']}  closed {t['closed_at']}\n"
            f"         mfe {_signed(t['mfe_r'], 2)}  mae {_signed(t['mae_r'], 2)}  "
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
             f"  win rate         {_n(a['win_rate'], 1)}%",
             f"  avg win / loss   {_signed(a['avg_win_r'], 3)}R / {_signed(a['avg_loss_r'], 3)}R",
             f"  expectancy       {_signed(a['expectancy_r'], 3)}R per trade",
             f"  total R          {_signed(a['total_r'], 2)}",
             f"  max drawdown     {_signed(a['max_drawdown_r'], 2)}R",
             f"  costs paid       {_n(a['costs_paid'], 4)} USDT",
             f"  equity vs start  {_n(a['balance'])} / {_n(a['starting_capital'])}"
             f"  ({_signed(a['return_pct'], 3)}%)"]

    if r["by_exit_reason"]:
        lines += ["", "  BY EXIT REASON",
                  f"    {'reason':<14}{'n':>4}{'share':>9}{'avg R':>9}"
                  f"{'total R':>10}{'avg MFE':>10}"]
        for b in r["by_exit_reason"]:
            lines.append(f"    {b['reason']:<14}{b['count']:>4}"
                         f"{_n(b['share_pct'], 1):>8}%{_signed(b['avg_r'], 3):>9}"
                         f"{_signed(b['total_r'], 2):>10}{_signed(b['avg_mfe_r'], 2):>10}")

    lines += ["", "  BY SCORE BAND",
              f"    {'band':<8}{'n':>4}{'win%':>9}{'expectancy':>13}{'total R':>10}"]
    for b in r["by_score_band"]:
        lines.append(f"    {b['band']:<8}{b['count']:>4}{_n(b['win_rate'], 1):>9}"
                     f"{_signed(b['expectancy_r'], 3):>13}{_signed(b['total_r'], 2):>10}")

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
