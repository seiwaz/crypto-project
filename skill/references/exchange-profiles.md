# Exchange profiles

Read the relevant profile before computing a plan. Leverage caps, liquidation rules,
and holding costs differ enough between venues that a generically-correct plan can be
wrong on a specific exchange.

Anything marked ⚠️ can change without notice. When precision matters, have the user
confirm from the exchange's own fee and margin pages, and say plainly that you're
working from a stored profile rather than live data.

---

## Nobitex — معاملات تعهدی (margin / "commitment" trading)

Iran's largest exchange. Its leveraged product is **not** a true perpetual futures
contract. It's a delegated margin structure backed by a participation pool
(استخر مشارکت), which changes several things that matter for planning.

### Leverage ladder (ضریب)

Available multipliers: **1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5**

| Account level | Max multiplier |
|---|---|
| Level 1 | 2× |
| Higher levels | 5× |

Not every multiplier is enabled for every coin — availability depends on that coin's
participation-pool liquidity. If the user reports a multiplier is unavailable, that's
the reason; recompute with what they actually have.

**Planning consequence:** the 5× ceiling is a real constraint for scalping (where tight
stops invite high leverage) but essentially irrelevant for intraday and swing, where a
1% risk budget with a 3% stop only calls for ~1.5×. If your computed leverage for an
intraday plan is near 5, re-check the sizing chain — something is probably wrong.

### Liquidation — نسبت تعهد (commitment ratio)

Liquidation triggers when the commitment ratio approaches **1**. To absorb sharp price
moves, Nobitex liquidates early — at a ratio of about **1.1** for 1× positions, with
different thresholds at other multipliers. ⚠️

**Always prefer the liquidation price the platform displays** over any formula. Have
the user read it off the order panel before confirming, and verify it sits at least
`buffer ×` the stop distance away (3× scalp, 4× intraday, 5× swing).

### Holding cost — کارمزد تمدید وکالت (renewal fee)

This is the biggest structural difference from a global perp and the one most often
omitted from plans:

- A position is renewed every **8 hours**, and a renewal fee is deducted from the
  locked collateral (وجه تضمین) each time
- Renewal can continue for up to **30 days**, after which the position closes
- There is no funding-rate mechanism; this fee replaces it and it only ever costs you
  — it never pays you, unlike negative funding on a perp

**Planning consequence:** `holding_periods = ceil(hold_hours / 8)`. A 24-hour hold
carries 3 renewals; a 3-day hold carries 9. Feed this into the cost filter. If the
expected hold exceeds ~3 days, this instrument is the wrong tool for the trade.

### Order types

Nobitex supports limit, market, **stop loss**, and **OCO** ordering on both buy and
sell margin positions. There's no excuse for a mental stop — place it with the entry.

### Liquidity and market selection

- Depth comes from the participation pool, so it is thinner than global venues and
  slippage is correspondingly higher
- Prefer high-volume **USDT** pairs for short holds; Toman pairs carry wider spreads
  **and** an implicit exposure to the local USD rate, which is a second variable
  layered onto your trade
- On a 5-minute scalp targeting 0.5% moves, this slippage is a first-order problem.
  On a 3% intraday move it's minor. This is the main reason the intraday profile suits
  Nobitex better than the scalp profile — say so when the user asks for tight scalps.

### Fees ⚠️

Maker/taker fees are tiered by 30-day volume and account level, roughly in the
0.1–0.2% per side range. Don't state a specific number as fact — ask the user for
their tier or use `--fee-pct` with an explicit assumption and label it as one.

### Script usage

```bash
python3 scripts/trade_plan.py plan --exchange nobitex --hold-hours 24 ...
```

The `nobitex` profile applies the 5× cap, the 8-hour renewal period, and the
liquidation-buffer warning automatically. Add `--account-level 1` to cap at 2×.

---

## Generic perpetual futures (Binance, Bybit, OKX, and similar) ⚠️

Use `--exchange generic-perp`. Defaults assume:

- Leverage cap of 20× in the profile (most venues allow far more; the cap here is a
  risk guardrail, not a platform limit — high leverage is available and rarely wise)
- Funding every 8 hours, which can be positive **or negative**, so holding cost may be
  a credit rather than a charge. Ask for the current rate rather than assuming.
- Isolated margin assumed. Cross margin puts the whole account balance behind the
  position, which invalidates the liquidation-buffer check entirely — flag this if the
  user mentions cross.
- Round-trip fees typically ~0.1% total with maker/taker discounts, lower than the
  Nobitex default

Funding is also a *signal*, not just a cost — see the funding rate section in
`indicators.md`.

---

## Adding a profile

Profiles live in the `EXCHANGES` dict at the top of `scripts/trade_plan.py`. Each
entry needs: `leverage_cap`, `level_caps`, `funding_period_hours`, `default_fee_pct`,
`default_holding_cost_pct`, `max_hold_days`, and `notes`. Copy the Nobitex entry and
edit — the calculation chain reads all venue-specific behavior from that dict, so
nothing else needs to change.
