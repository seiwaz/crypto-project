# Risk math

The part that actually determines whether a trader survives. Read this when the user
questions the sizing model, asks about leverage safety, or wants to measure their own
performance.

---

## 1. The R system

```
1R = the amount lost if the stop is hit
```

Measure every trade in R rather than currency. A +2R trade on $100 of risk is the same
*quality* as +2R on $1000 — expressing results this way separates the merit of a
decision from the size of the account, which is what makes performance review honest
instead of emotional.

---

## 2. Position sizing chain

```
Step 1:  R = risk_pct × capital              (1% standard, 0.5% while learning)
Step 2:  stop_distance = |entry − stop|
Step 3:  quantity = R ÷ stop_distance
Step 4:  notional = quantity × entry
Step 5:  margin = notional ÷ leverage
```

The order matters. Risk and stop are inputs; quantity is derived. Sizing first and
placing the stop afterwards inverts the logic and is the most common cause of
account-ending losses.

### The thing most traders get wrong

> **Leverage does not determine your risk.** Risk is `quantity × stop_distance`.
> Leverage only decides how much collateral is locked and how far liquidation sits.

A $10,000 notional position with a 1% stop risks $100 whether you use 1× or 5×. The
difference is that at 1× you post $10,000 and liquidation is effectively unreachable;
at 5× you post $2,000 and liquidation is roughly 18% away. Same risk at the stop,
different tail risk.

This reframing is worth stating explicitly to users — it's usually the moment leverage
stops being scary-or-exciting and becomes just another parameter.

---

## 3. Leverage and liquidation distance

```
approximate adverse move to liquidation ≈ (100 ÷ leverage) − maintenance_margin%
```

| Leverage | Approx. move to liquidation |
|---|---|
| 1× | effectively unreachable |
| 2× | ~45% |
| 3× | ~30% |
| 5× | ~18% |
| 10× | ~9% |
| 20× | ~4.5% |

**The safety rule the skill enforces:**

```
max_safe_leverage = 100 ÷ (stop_pct × buffer)
    buffer = 3 (scalp) | 4 (intraday) | 5 (swing)
```

The buffer is how many multiples of the stop distance must separate entry from
liquidation. Its purpose is that a single volatile wick shouldn't be able to liquidate
you *before* your stop gets the chance to close the trade at the loss you planned for.
Longer holds get a larger buffer because they're exposed to gaps and news.

Treat the formula as a sanity bound, not a source of truth. Exchanges compute
liquidation from maintenance-margin schedules that vary by tier and position size —
always defer to the number the platform displays.

---

## 4. Reward:risk and breakeven win rate

```
R:R = |TP − entry| ÷ |entry − stop|

Breakeven win rate = 1 ÷ (1 + R:R)
```

| R:R | Win rate needed to break even |
|---|---|
| 1:1 | 50% |
| 1.5:1 | 40% |
| 2:1 | 33% |
| 3:1 | 25% |
| 4:1 | 20% |

This is why the higher-timeframe profiles use wider targets: fewer trades means each
one has to carry more, and a wider R:R buys tolerance for a lower win rate.

---

## 5. Expectancy

```
E = (win_rate × avg_win_R) − ((1 − win_rate) × avg_loss_R)
```

With the intraday profile — TP1 at 1.5R on half the position, TP2 at 3R on the rest,
so avg_win ≈ 2.25R — and a 40% win rate:

```
E = (0.40 × 2.25) − (0.60 × 1.0) = 0.90 − 0.60 = +0.30R per trade
```

Positive. But that's **before costs**, which is where most published strategies stop
and where real accounts diverge from backtests.

---

## 6. Cost drag — the number that decides everything

```
cost_in_R = (round_trip_fee_pct × notional + holding_cost) ÷ R
E_net = E_gross − cost_in_R
```

**Worked example, 5× scalp:** notional $11,110, round-trip fee 0.3% → $33 against an
R of $100 → **0.33R**.

```
E_net = 0.30 − 0.33 = −0.03R      ← winning system, losing trader
```

**Same account, intraday profile:** notional $3,333, round-trip fee 0.3% → $10, plus
~$15 of renewal fees over 24 hours → ~0.25R... but against a wider average win:

```
E_net = 0.30 − 0.10 ≈ +0.20R      ← the same edge, now actually collectible
```

The mechanism is simple and worth spelling out: **notional scales with leverage, and
fees are charged on notional**. High leverage multiplies your costs by exactly the
factor it multiplies your buying power. That is the entire reason high-frequency
leveraged scalping is so much harder than it looks, and it's why the skill's cost
filter (`1R ≥ 4–5 × total_cost`) exists.

---

## 7. Correlation and portfolio heat

```
portfolio_heat = Σ (risk of each open position)
```

Two long positions in highly correlated alts are not two independent 1% risks — under
a market-wide move they behave closer to one 2% risk. Practical limits:

- Max ~2 same-direction positions in correlated assets
- Reduce per-trade risk to ~0.75% when running two positions
- Cap total heat around 3% of capital
- Most alts correlate above 0.8 with BTC, so "diversifying across five altcoins" is
  mostly an illusion of diversification

---

## 8. Circuit breakers

Loss limits exist because judgment degrades measurably after consecutive losses, and
the trades taken in that state are the ones that turn a drawdown into a hole.

| Profile | Stop for the session when |
|---|---|
| Scalp | 2 consecutive losses, or −3% equity in a day |
| Intraday | 3 consecutive losses, or −5% equity |
| Any | −10% from equity peak → stop entirely and review the journal |

---

## 9. Journal fields

Without measurement there's no way to tell an edge from a lucky streak. Record per
trade:

| Field | Why |
|---|---|
| Date, pair, profile | Segmentation |
| Direction and setup score | Does a higher score actually predict better outcomes? |
| Entry pattern (A/B/C) | Which pattern is carrying the results, and which is dead weight? |
| Entry, stop, TP1, TP2 | Reconstruction |
| Stop distance in ATR | Was the multiplier appropriate? |
| Quantity, leverage, margin | Sizing consistency |
| Exit price and reason | TP1 / TP2 / stop / time stop / manual |
| **Result in R** | The only comparable performance unit |
| Fees + holding cost in R | Tracks the drag over time |
| Plan followed? Y/N | **The most important field of all** |

That last field is what separates "my system is bad" from "I didn't follow my system."
They call for completely different fixes, and traders reliably misdiagnose which one
they have.

**Minimum sample before drawing conclusions:** ~30 trades for a rough signal, ~100 for
a win rate you can trust. Below that you're reading noise.
