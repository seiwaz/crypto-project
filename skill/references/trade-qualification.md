# Is this coin worth trading?

The plan tells you *how* to trade something. This tells you *whether* to. They're
different questions, and conflating them is why people take technically well-executed
trades on instruments that could never have paid.

A useful way to hold it: the plan is about the trade, qualification is about the
market. You can't fix a bad market with a good entry.

---

## The two layers

**Gates** are properties of the instrument. Binary. A failure means skip, because no
entry price rescues an illiquid book, a flat chart, or a fee structure that eats the
target. Gates are cheap to check and eliminate most candidates fast.

**Score** is graded quality, for ranking whatever cleared the gates. A 0–100 number
that lets you say "of these four, this one deserves the risk budget today."

The order matters: gate first, score second. Scoring an instrument that fails a gate
wastes effort and, worse, produces a number that invites rationalisation.

---

## The gates

### 1. Volatility fit — is there enough movement, and not too much?

```
ATR% = ATR(14) on the profile's ATR timeframe ÷ price × 100
```

| Profile | Acceptable ATR% |
|---|---|
| scalp | 0.3 – 1.5 |
| intraday | 1.0 – 6.0 |
| swing | 2.0 – 15.0 |

**Too low** means the instrument cannot move far enough to clear fees before your time
stop fires. This is the quiet killer — the chart looks clean, the setup looks textbook,
and the move is simply too small to pay for itself.

**Too high** means the stop distance the ATR demands is so wide that either the
position becomes trivially small or the stop sits outside anything you'd tolerate. A
coin that moves 20% a day isn't an opportunity, it's a different game.

*If it fails:* switch to a timeframe profile whose band contains the current ATR%, or
pick a different instrument. Do not shrink the stop to make a volatile coin fit — that
converts a volatility problem into a guaranteed stop-out.

### 2. Spread

```
spread% = (best_ask − best_bid) ÷ mid × 100
```

Limits: 0.1% scalp · 0.3% intraday · 0.5% swing.

You pay the spread twice, and it's invisible in backtests. On a scalp targeting a 0.5%
move, a 0.1% spread is 20% of the target gone before anything happens.

*If it fails:* move to a longer profile where the spread is a smaller share of the
target, or trade the more liquid quote pair. On Nobitex, USDT pairs usually beat Toman
pairs on spread — and Toman pairs add local-rate exposure on top.

### 3. Liquidity depth

```
depth_multiple = value of the top 5 book levels on your side ÷ position notional
```

Required: 3× scalp · 2× intraday · 1.5× swing.

If your order is a meaningful fraction of the visible book, you move the price against
yourself on entry and again on exit. Worse, the same thinness means a stop-loss fill
can slip well past your stop price — the risk you calculated becomes fiction.

*If it fails:* halve the position and re-check, or pick a deeper market. This gate
scales with position size, so a small account may pass where a large one fails on the
identical chart. That's correct behaviour, not a bug.

### 4. Liquidation buffer

```
buffer = distance to liquidation ÷ stop distance
```

Required: 3× scalp · 4× intraday · 5× swing.

Your stop must get to act before the exchange does. If liquidation sits close to the
stop, a single volatile wick liquidates you at a loss far larger than the one you
planned. Longer holds get a bigger buffer because they're exposed to gaps and news.

*If it fails:* reduce leverage. That is the only fix — widening the stop makes it
worse, and reducing size doesn't change the percentage distances at all.

### 5. Cost efficiency

```
cost_in_R = (round-trip fee + holding cost) ÷ R
```

Limit: 0.25R scalp · 0.20R intraday/swing.

The mechanism people miss: **notional scales with leverage, and fees are charged on
notional**. High leverage multiplies costs by exactly the factor it multiplies buying
power. A system with positive expectancy on paper goes negative once this is
subtracted. See the worked example in `risk-math.md` — the same edge is −0.03R at 5×
scalp and +0.20R at 1.5× intraday.

*If it fails:* reduce leverage, widen the target, lengthen the profile, or skip.

### 6. Plan blockers

Weak direction score, hold exceeding the venue's 30-day limit, required leverage above
the cap, market closed. Each names its own remedy in the output.

---

## The score

For candidates that cleared every gate:

| Factor | Weight | Full marks at |
|---|---|---|
| Setup quality | 35 | all automated direction checks agree |
| Net expectancy | 25 | +0.30R per trade after costs |
| Cost drag | 15 | costs under 0.05R |
| Liquidity headroom | 15 | book ≥ 3× the gate requirement |
| Volatility centring | 10 | ATR% at the centre of the profile band |

Setup quality carries the most weight because everything else is a property of the
market that will still be there tomorrow. The setup is the perishable part.

### Verdicts

| Verdict | Condition | What to do |
|---|---|---|
| **TAKE** | ≥ 70, all gates passed | Execute the plan as written |
| **WATCH** | 50–69, all gates passed | Write down the *one* condition that would upgrade it, and wait for that specific thing |
| **INCOMPLETE** | factor coverage < 80% | Fetch live data before judging — usually means no snapshot was run |
| **SKIP** | any gate failed, or < 50 | No position |

**Why INCOMPLETE exists.** Scoring only the measured factors and grading out of that
subtotal would quietly reward missing data — a plan with no order-book data would
score identically to one with a deep book. Reporting coverage separately and refusing
to issue TAKE below 80% keeps that honest.

**WATCH deserves discipline.** It's the verdict most likely to be rationalised into a
trade. The remedy is to name the upgrade condition out loud before walking away:
"long only if 4H closes above 3120 with RVOL over 1.5." A WATCH without a written
trigger becomes a TAKE within the hour.

---

## What the score is not

The weights are a considered heuristic encoding a priority order. They are **not**
fitted to historical returns and the score is **not** a probability of profit. A 78
does not mean a 78% chance of winning, and an 80 is not meaningfully better than a 75.

Use it to rank candidates against each other and to make the reasoning visible. Don't
use it to size conviction, and don't let a high number override a gate failure or a
manual check you haven't done.

---

## Screening a watchlist

```bash
python3 scripts/nobitex_api.py screen \
  --symbols BTCIRT,ETHIRT,SOLUSDT,XRPIRT,ADAIRT \
  --profile intraday --capital 100000000 --risk-pct 1
```

Output ranks TAKE → WATCH → INCOMPLETE → SKIP, with the failing gate named for each.
Then run a full snapshot and plan only on the TAKE candidates — screening deliberately
uses defaults for fees and win rate so it stays fast and comparable, which makes it a
filter rather than a decision.

Two things screening cannot settle, ever: **BTC alignment** and **funding rate
positioning**. Both need data outside Nobitex. Confirm them manually before executing,
however good the score looks.

---

## Known gaps in the automated score

Four things the score does not check, found in the 2026-08-19 research round and left
undone deliberately rather than silently — each needs validation against live results
before it becomes a real gate, not just a documentation note:

**Correlation is regime-dependent, and the calm-market number is the wrong one to
gate on.** Studies of crypto tail dependence put BTC/alt lower-tail (crash) correlation
around 0.85–0.88 versus 0.23–0.25 in the upper tail (rally) — nearly 4× higher exactly
when a portfolio needs the protection. A correlation filter calibrated on typical/median
readings (~0.5–0.6) will rarely trigger in normal conditions and lags badly into a
selloff, because a rolling correlation window only catches up to the crash after enough
bars of it have already happened. Treat a single fixed correlation threshold as
protection against *normal* co-movement, not crash contagion — a real defense against
the latter needs a market-stress trigger (e.g. a fast BTC drawdown or realized-vol
spike) that caps position count independently of the measured rolling correlation.
Source: dynamic conditional tail-dependence research on BTC/ETH, arXiv:2606.16840.

**Trend direction and trend *quality* are different questions, and only the first is
checked.** `price vs EMA200` plus a recent-move threshold (this skill's and the demo's
regime check) says which way price has drifted, not whether the move is a clean trend
or noise inside a range. The Choppiness Index (built on true range vs. total span) and
Kaufman's Efficiency Ratio (net change ÷ summed move) both answer "is this actually
trending" and would catch cases where a weak EMA-cross reads as a trend. ADX draws the
same distinction but lags more (multi-stage smoothing) and is unreliable below ~20
("dead zone" whipsaws) — strict ADX filtering is reported to cut false trend signals
30–40% in backtests, at the cost of fewer signals. None of these are wired into the
automated score yet; adding one is a Round 3 candidate once there's a live sample to
validate the win-rate/signal-count trade-off against.

**Event risk has no calendar integration.** CPI/FOMC/NFP releases are documented to
cause liquidity gaps and 5–10%+ altcoin moves that threaten leveraged positions
regardless of setup quality. Building calendar integration is nontrivial; a cheaper
proxy already available from the data this skill fetches is a realized-volatility or
ATR spike relative to its own recent history — that catches unscheduled events too,
which a calendar can't.

**No signal has a maximum age.** `qualifying_signals()` in the demo checks a signal is
newer than the position's last close (so it won't re-open on stale evidence) but never
checks the signal against wall-clock time — if the scanner stalls, a TAKE row from
hours ago is still actionable. General signal-decay research shows execution delay
degrades edge measurably even at T+1/T+2, so a live system should have an explicit
staleness ceiling independent of the re-entry check.

---

## A note on the honest answer

If every candidate comes back SKIP, that is the answer. Say it plainly. Traders who
feel obliged to produce a trade from every analysis session are the ones who fund
everyone else's edge. Days with no qualifying setup are normal and expected — a system
that always finds something isn't filtering.
