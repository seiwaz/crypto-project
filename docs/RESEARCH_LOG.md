# Research Log

Tracks what changed in `SKILL.md` / `agent/demo.py`, why, and what evidence backs it.
Each round builds on the last; later rounds may reverse earlier conclusions if the
evidence says so — see the "Round N note" lines when that happens.

---

## Round 1 — Foundations (retrospective, 2026-08-18/19)

**Status: already executed**, across five commits before this research log existed
(`459c994` → `a515b5d`). Logged here after the fact so the record is complete, rather
than re-deriving the same conclusions from public sources a second time. Where a
public-source figure was actually used, it's cited; the rest is first-party — replayed
against this account's own trade history, which is a stronger check for *this* system
than a generic backtest would be.

### 1. Regime/trend detection

**What shipped:** `agent/correlation.py: coin_regime()` — price vs EMA200 plus a
12-bar % move, on the instrument's *own* candles (not ADX, not Choppiness Index; see
Round 2 for why that gap matters).

**Finding:** the gate that existed before this was inverted — it gated on *BTC's*
trend + correlation, not the instrument's own. Replayed against 30 closed trades:
25 were shorts taken while BTC sat above its 4H EMA200 and rose 2.66% over 48h. Those
shorts lost 5.65 USDT; the five longs made 0.74 USDT. The gate blocked FIL/DOT/CRO/ATOM
(each below its own EMA200, alpha −6% to −36%) while waving through WLD/INJ/NEAR
(outperforming BTC by 44–62%) — refusing the weak coins and allowing the strong ones,
backwards from what a trend filter should do.

**Fix:** instrument's own EMA200/1H decides first; BTC is consulted only as a veto on a
coin with *no* trend of its own that purely tracks a strongly-trending BTC, gated at
≥0.45 correlation. Missing data allows the trade (fails open, not closed) — a gate that
raises stops the agent trading entirely, which is worse than under-filtering.
Source: `agent/demo.py:302` (`counter_trend`), commit `7356609`.

### 2. ATR-based stop-loss and position sizing

**What shipped:** `stop_distance = atr_mult × ATR`, capped structural override at
`max_struct_mult × ATR-stop` (default 1.5×); `atr_mult` tightened from 2.0 to 1.5 for
the intraday profile; hold window widened from 12h to 48h.

**Finding:** the account was trading a timeframe mismatch — the stop was wider than
the price move available inside the hold window, so trades expired on the clock
instead of reaching a level (84.8% of the first 30 trades did this). A first-touch
simulator was built to replay historical windows and settle stop/hold combinations by
measurement rather than argument (pessimistic on same-bar stop+target ties, matching
the paper broker's own convention):

| Config | Win rate | Undecided | Modelled R |
|---|---|---|---|
| 2.0× ATR, 12h hold (was running) | 3.0% | 84.8% | −0.072R |
| 2.0× ATR, 48h hold | 23.6% | 39.9% | −0.005R |
| 1.0× ATR, 48h hold | 41.5% | 4.7% | +0.066R |
| **1.5× ATR, 48h hold (chosen)** | 35.6% | 18.1% | **+0.058R** |

1.0× scored marginally better in isolation but breaches the intraday profile's own 2%
stop floor on several coins and doubles fee drag as a share of R (a stop half as wide
makes the same fee twice the fraction of the risk unit) — so 1.5× was chosen for
robustness, not because it topped the table. The simulator's prediction (−0.072R for
the old config) tracked the actually-measured −0.055R closely, which is what makes its
comparisons trustworthy for the other rows.

**External anchor** (from the commit that made this change): a published daily
2×-ATR trend-following configuration runs ~46.3% win rate / 1.72 profit factor: the
same rules on hourly bars fall to 32.3% / 0.96 — i.e. the same ATR multiplier degrades
sharply on a faster timeframe, which is the general form of the mismatch found here.
Source: commit `6ad4eef`.

**Structural-stop cap:** an uncapped structural stop was observed pushing a target out
to a distance the instrument couldn't travel in the hold window — WLD's structural
stop was 14.80% against a 2.96% ATR stop; capped at 1.5×, it becomes 6.67%. Every
R-multiple derives from stop distance, so this silently invalidates TP1/TP2 reachability
if left unbounded. Source: commit `6ad4eef`.

**Round 2 note:** this fix targets *stop width vs. hold time*. It does not touch *why*
the trade was entered in a range/dead market to begin with — that's the regime-quality
question Round 2 covers.

### 3. Basic leveraged risk management

**What shipped:** unchanged from the skill's existing model — `R = risk_pct × capital`,
`quantity = R / stop_distance`, leverage derived from stop% and a liquidation-buffer
multiple, portfolio heat capped at 6% of equity spread across slots, a circuit breaker
(consecutive losses / equity drawdown) with a cooldown window.

**Finding (circuit breaker only):** the original breaker counted losses with no time
limit and expected a winner to clear it — but a tripped breaker blocks new entries, so
no trade can close to produce that winner. It deadlocked live: seven straight losses
against a limit of three, permanently blocking every TAKE. **Fix:** only losses inside
a rolling cooldown window (default 6h) count, so the breaker self-clears once the
account has sat out that long. Source: `agent/demo.py:1209` (`circuit_breaker`),
commit `3d1ed82` (pre-dates this log but is the same category of fix).

**Round 1 verdict:** the sizing math (`R`, quantity, leverage cap) was not found
broken — the failures were in the *gates around* it (trend direction, stop/hold
matching, breaker logic), not in the arithmetic itself.

### 4. Fee / funding cost model — Nobitex vs Toobit, and its effect on stop distance

**Finding:** fees were **53.9% of gross P&L** over the first 30 trades (commit
`459c994`) — this is the real "fees eating the trade" effect, and it is a cost-drag
problem, not a stop-distance problem. Traced the actual mechanism in
`scripts/trade_plan.py`: `cost_in_R = total_cost / R`, gated at `R ≥ 4×cost` (scalp) /
`5×cost` (intraday/swing), and net expectancy subtracts `cost_in_R` from gross
expectancy. **Fees are never subtracted from stop distance anywhere in the codebase** —
the closest analogue is `agent/demo.py: _reduce_at_tp1`, which moves the post-TP1 stop
to breakeven *plus* accumulated costs, which is a deliberate, correct choice (a bare
breakeven stop still locks in a loss equal to fees paid).

**Where the fee/stop interaction actually bites:** TP1 reachability. Tried lowering
TP1/TP2 to 0.6R/1.2R (more reachable given measured price travel) — the cost gate then
rejected all 47 watchlist coins on net expectancy (−0.264R, breakeven win rate 52.6%).
Measured price travel: these instruments move ~0.27R in 8h and ~0.64R in 48h against a
2.0×-ATR stop; TP1 at 1.5R asks for more than the median trade travels even by the time
stop. **Structural conclusion:** the stop (and therefore R) is wide relative to what
these coins actually move, so *any* target set against that R is either unreachable
(current 1.5R) or too small to clear the fee floor (0.6R). Reverted to profile
defaults; the real fix is likely on the stop-width side (Round 1 §2) rather than the
target side. Source: commit `c6b2c5f`.

**Fee schedule as implemented:** Toobit VIP-0 — maker 0.0200%, taker 0.0600%
round-trip both ways unless a maker fill is recorded (`agent/paper.py:38`). Maker
limit entries (0.1% inside the mark, 30-minute timeout) were added specifically because
of the 53.9% finding — modelled honestly: the order only fills if price actually trades
through it and is cancelled unfilled after 30 minutes, rather than assumed free.
Source: commit `a515b5d`.

**Round 2/3 flag:** the Nobitex-specific 8h renewal-fee model referenced in the skill's
`exchange-profiles.md` was not re-verified in Round 1 — the demo trades on Toobit only.
Confirm in Round 3 whether Nobitex's `default_fee_pct`/`holding_cost_pct` constants in
`trade_plan.py` (lines ~90–125) still match the exchange's current published schedule.

### Round 1 summary

| Area | Verdict |
|---|---|
| Regime detection | Fixed: instrument's own trend, not BTC's |
| ATR stop / hold-window match | Fixed: 1.5×ATR / 48h, capped structural override |
| Basic risk math (R, sizing, leverage) | Not broken |
| Circuit breaker | Fixed: cooldown window instead of permanent counter |
| Fee model correctness | Not broken (costs correctly compared to R, not stop) |
| Fee cost **drag** | Unresolved — 53.9% of gross P&L, TP1 reachable-vs-viable conflict open |

Net modelled effect of all Round 1 fixes together: ~41% win rate, +0.022R expectancy,
against 30% / −0.055R measured before them. **A win rate materially above ~43% is not
supported by the data collected so far** — see `[[demo-trading-experiment]]` memory.

---

## Round 2 — Depth (2026-08-19)

Public-source research this round, since Round 1's fixes were mostly settled by
first-party replay rather than by comparison against published practice. Four findings,
each with a doc change made and, where it touches live gate logic, a note on why the
code change is held rather than applied immediately.

### 1. Correlation is regime-dependent, and the calm-market number is the wrong one

Dynamic tail-dependence research on BTC/ETH (arXiv:2606.16840) measures lower-tail
(crash) correlation around **0.85–0.88** against upper-tail (rally) correlation of only
**0.23–0.25** — roughly 4× higher in a crash than a rally, at the 90th/95th percentile.
This directly undercuts the demo's `CORRELATION_THRESHOLD = 0.75`, calibrated against
this account's own *typical* rolling correlation (median 0.47 on 4H bars, per the
existing code comment in `agent/demo.py`) — a threshold set from calm-period data will
rarely fire under normal conditions and, worse, **lags into exactly the event it exists
to catch**: a rolling 4H/120-bar correlation window only reflects a crash after enough
of it has already happened.

**Doc change:** `indicators.md` §14 and `trade-qualification.md` ("Known gaps") now
state this asymmetry explicitly and reject "low measured correlation" as evidence of
safety during a fast BTC move. **Code change held:** the fix implied — a market-stress
trigger (fast BTC drawdown or realized-vol spike) that escalates the correlated-same-
side cap independent of measured correlation — is a real change to the demo's live gate
logic. Not applied yet: the account is mid-way through the post-reset 10-trade sample
you asked to see a report on, and this would be a second confound stacked on Round 1's
already-pending fee/TP1 question. Proposed for after that checkpoint (see summary below).

### 2. Trend direction and trend quality are different questions

The demo's and skill's regime check (`price > EMA200` + `move% over 12 bars > 0.5`) is
a direction filter with a loose quality bar — a small, noisy move clears it either way.
Public sources agree the standard alternatives measure something genuinely different:
Choppiness Index reads range-congestion from true range directly; Kaufman's Efficiency
Ratio (net change ÷ summed move, bounded 0–1) reacts fast and rewards a clean path;
ADX measures trend *strength* specifically, lags more due to multi-stage smoothing, and
is unreliable below ~20 ("dead zone" whipsaws) — but a strict ADX>20 filter is reported
to cut false trend signals 30–40% in backtests, at a real cost in signal count.

**Doc change:** new `indicators.md` §15, marked explicitly advisory/not-yet-automated.
**Code change held:** deliberately not wired into `coin_regime()` — adding a
choppiness/efficiency gate changes which trades qualify, and that needs testing against
a live sample the way Round 1's stop-width change was (the first-touch simulator), not
shipped on the strength of general backtests from other instruments.

### 3. Volume confirmation was already numerically defined but not automated

`references/indicators.md` §6 already specifies RVOL ≥ 1.5 as "required for a breakout
trade," and `rvol20` is already computed by both `trade_plan.py` and the snapshot in
`nobitex_api.py` — but grep confirms it is **never read by the automated scoring**, only
reported as a number. SKILL.md's Step 4 checklist listed "volume confirms" without the
number, which papers over the gap. External research on RVOL breakout filters (single
study, small sample at the high end) reports win rate improving 37%→61% and profit
factor 0.92→1.58 at a 1.5× filter, 72%/1.84 at 2.0× but on only 18 signals over three
years — directionally consistent with the skill's own existing 1.5× threshold, though
the small sample at 2.0× is not strong enough to justify raising the bar further.

**Doc change:** SKILL.md Step 4 now names the RVOL ≥ 1.5 threshold explicitly and flags
that it isn't auto-gated. **Code change held:** wiring an automated RVOL gate into
`nobitex_api.py`'s scoring is a change to the live-trading skill script, not just docs —
out of scope for a documentation-only round; flagged for Round 3 or a dedicated pass.

### 4. Two gaps with no automation at all: event risk and signal freshness

Macro releases (CPI/FOMC/NFP) are documented to cause liquidity gaps and moves large
enough to threaten leveraged positions (5–10%+ altcoin moves cited around adverse CPI
prints) regardless of setup quality — and there is no economic-calendar integration
anywhere in this codebase. Building one is nontrivial; a realized-volatility/ATR spike
relative to its own recent history is a cheaper proxy already available from data this
system already fetches, and catches unscheduled events a calendar can't.

Separately, general signal-decay research (execution-delay backtests showing Sharpe
2.88→2.25 from T+0 to T+2) supports what the demo already does *partially*:
`qualifying_signals()` checks a signal is newer than the position's last close, but
never checks it against wall-clock time — a stalled scanner could leave an hours-old
TAKE actionable indefinitely.

**Doc change:** both documented in `trade-qualification.md`'s new "Known gaps" section.
**Code change held:** the freshness ceiling is a small, low-risk addition (a max-age
check next to the existing re-entry check) and could reasonably ship without waiting
for the 10-trade checkpoint if you'd like — flagged separately in the summary below
rather than applied silently, per your instruction to flag changes to core code first.

### Round 2 summary — code changes proposed, none applied yet

| Change | Risk if applied now | Recommendation |
|---|---|---|
| Market-stress correlation override | Confounds the in-flight 10-trade sample | Hold until checkpoint |
| Choppiness/Efficiency Ratio regime gate | Same — changes which trades qualify | Hold; needs its own replay test like Round 1's stop fix |
| Automated RVOL gate in `nobitex_api.py` | Live-skill script change, untested | Hold for a dedicated pass |
| Signal max-age ceiling | Low — doesn't change which trades qualify, only guards against a stalled scanner | Low-risk; could ship independently |

## Round 3 — Expert validation (pending)

To cover: quantitative/published backtests, liquidation and margin-call mechanics per
exchange documentation, and a critical re-check of Round 1 and 2 assumptions against
more specialized sources — including the Nobitex fee-schedule flag raised in Round 1 §4.
