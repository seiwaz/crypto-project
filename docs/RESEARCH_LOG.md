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

## Round 3 — Critical validation: the BTC-alignment scoring bug (2026-08-20)

Triggered by the watchlist expansion (47 → 69 coins, same day): every coin across two
full scans came back SKIP, several with scores in the high 70s/80s/90s. That pattern —
decent scores, but the direction-confirmation gate (needs ≥6/8 checks agreeing) never
clearing — was suspicious enough on its own to warrant checking whether the gate itself
was miscalibrated rather than the market genuinely offering nothing, so this round did
both: external research on multi-indicator confirmation systems, and a direct code
audit of how the 8 checks are actually computed for the live Toobit scans.

### The bug

`agent/skill.py`'s docstring claims "the Toobit adapter imports [`score_direction`]
rather than growing a second copy... so both venues score a chart identically." True
for 6 of 8 checks — but the 2 the skill marks MANUAL ("BTC / dominance alignment",
"funding rate not crowded") get resolved with Toobit-specific logic in
`agent/toobit.py: resolve_manual_checks()`, and that resolution is where the actual bug
lived. The BTC-alignment check was resolved for **every non-BTC coin using only BTC's
own trend** — `long: btc["bullish"], short: not btc["bullish"]` — regardless of what
that specific coin was doing.

This is the *exact* mistake this project already found and fixed once, in a different
place: `agent/demo.py`'s `counter_trend()` gate used to veto trades based on BTC's trend
alone, and commit `7356609` proved that backwards — it blocked shorts on coins already
in their own downtrend purely because BTC was rising, and passed shorts on coins
outperforming BTC. That fix was never backported to `resolve_manual_checks()`, which
kept applying the disproven logic silently, inside the *score itself*, on every single
scan, for every non-BTC coin — not as an optional gate someone could see and question,
but baked into whether a coin could ever reach the 6/8 threshold at all.

### External confirmation

Two searches, both converging on the same conclusion already reached internally:

**Requiring near-unanimous multi-indicator agreement is a known trap** ("indicator
paralysis") — most practitioners find 2-3 *independent* factors sufficient, and
critically, "the strongest confluence combines factors from genuinely different
methods rather than several versions of the same one." Three of this system's six
automated checks (price vs EMA200, EMA50 vs EMA200, price vs EMA50) are variations on
one trend read, not independent evidence — inflating the effective bar past what "6 of
8" suggests, since a genuinely mixed/transitional period can easily split three
correlated EMA reads against each other.

**Blanket BTC-trend gating for altcoins is a recognized anti-pattern.** Published
practice screens for *relative strength* — "coins showing higher lows while BTC makes
lower lows... being strongest candidates" — treating divergence from BTC as a signal
worth taking, not a disqualifier. A check that forces every altcoin's directional score
to agree with BTC's own bias does the opposite: it structurally can't reward the exact
relative-strength setups professional screening looks for.

### The fix (applied 2026-08-20)

`resolve_manual_checks()` now resolves the BTC-alignment check from the **instrument's
own trend first** (`correlation.coin_regime()`, decision-timeframe), falling back to
BTC's trend only when the coin has no clear trend of its own — the same philosophy as
`demo.counter_trend()`, applied where it was missing. Verified against live data before
deploying: AXS was trending +7.91% on its own 4h chart at the time, while BTC sat below
its 1D EMA200 — under the old code AXS's BTC-check was forced to favour short despite
its own uptrend; under the fix it correctly favours long. FIL and GALA, which had no
clear trend of their own at the same moment, correctly still fall back to BTC's bias —
the fix narrows the bug's blast radius rather than removing the BTC signal outright.

**Effect on the account:** this changes which trades qualify going forward, similar in
kind to the ATR/hold-window fix in Round 1 (`6ad4eef`) that reset the sample. Only 6
trades have closed since the 2026-08-17 reset — worth deciding with the user whether to
reset again now that a real scoring bug (not a parameter tweak) has been fixed, rather
than deciding that unilaterally here.

### Round 3 — still pending

Quantitative/published backtests, liquidation and margin-call mechanics per exchange
documentation, and the Nobitex fee-schedule re-verification flagged in Round 1 §4
remain undone — this round covered the specific issue the watchlist expansion surfaced,
not a full scheduled Round 3 pass.

## Round 4 (user-directed, 2026-08-20) — switch to a 5–30 minute holding strategy

**Change requested:** the user stated the intended strategy holds 5–30 minutes, and
asked for (1) immediate entry on a TAKE signal rather than waiting up to 20 minutes,
(2) position management every 30s instead of 60s, (3) the web UI refreshing every 3s
on every tab without a manual reload.

**Applied via `config/strategy-tuning.json`** (settings-only, no restart):
- `profile: "scalp"` — was `"intraday"`. Switches ATR TF/mult, decision TF (15m vs
  4H), targets (TP1 1.0R/TP2 2.0R vs 1.5R/3.0R), stop range, and the direction-score
  check set (adds session VWAP, narrower RSI band, threshold 5/6 vs 6/8).
- `scan_interval_minutes: 5` (was 20) and `demo.entry_interval_seconds: 300` (was
  1200, kept matched to the scan cadence per the code's own reasoning — entries
  shouldn't act on a scan older than the current one) — as close to "immediate" as
  the batch-scan architecture supports without a deeper redesign to per-coin
  continuous monitoring.
- `demo.cycle_seconds: 30` (was 60) — position management cadence.
- `demo.time_stop_hours: 0.5` (30 minutes, was 48) — the literal cap requested.
- `demo.maker_timeout_minutes: 2` (was 30) — a 30-minute resting limit order made no
  sense against a 30-minute total hold budget; shortened rather than disabling
  maker entries outright, to keep most of the fee saving (fees were 53.9% of gross
  P&L pre-maker-entry, Round 1 §4) while not eating the whole window waiting to fill.

**Web UI (`web/app.js`):** the Demo tab already polled every 3s while visible — the
gap was the Screener tab, which dropped to a 60s idle poll once no scan was running.
Unified both to a 3s `SCREENER_POLL_MS` constant.

**Risk flagged before applying, not blocking on:** this project's very first sample
(pre any fix) failed specifically because the hold window was too short for the
stop/target distance — 84.8% of trades hit a time-stop instead of a real level, and
the fix that resolved it (`6ad4eef`) *widened* the hold to 48h. A 30-minute cap is
far tighter than anything tested since. If the account starts closing everything on
`time_stop` again, this is the first place to look — not a new bug, a repeat of an
already-diagnosed one at a much smaller timescale. No reset needed to apply this: 0
trades had closed since the same-day account reset, so the fresh sample starts
consistently under scalp settings from trade #1.

## Round 5 (user-directed, 2026-08-20) — tighten the stop, keep the 30-minute window

**Live result from Round 4's settings:** 11 trades closed, **all 11 via `time_stop`,
zero via a real stop or TP** — R ranged −0.215 (COMP) to +0.256 (ZRO), with exactly
one survivor (ENA, +0.59R at minute 31, clearing the 0.5R floor). A clean, complete
confirmation of the flagged risk: 1.5× ATR is too wide a distance for these coins to
cover — in either direction — inside 30 minutes.

**Decision:** the user chose to tighten the stop rather than widen the window. Set
`atr_mult: 1.0` (was 1.5, scalp's own profile default) via `config/strategy-tuning.json`
— confirmed this actually overrides the profile (`agent/scanner.py:108` reads the
top-level `settings["atr_mult"]` and passes it to the planner as `--atr-mult`,
logged as "profile default overridden"; Nobitex plans ignore this override, but demo
trading is Toobit-only so that doesn't apply here).

**Why 1.0× specifically, not something untested:** Round 1's own multiplier sweep
(§2 above) already measured 1.0× ATR against a *48h* hold at 41.5% win / 4.7%
undecided — the best undecided-rate of the three multipliers tested, meaning even
at a long hold it resolves fast. Chosen as a previously-validated starting point
over an arbitrary new number. **Explicitly untested: this multiplier against a
30-minute hold specifically** — the compression from 48h to 30min is roughly 96×,
so Round 1's numbers don't transfer directly; this is a real hypothesis, not a
known-good setting. If time-stops still dominate at 1.0×, the next candidate is a
further cut (0.5×) before concluding the 30-minute window itself needs widening.
Watch for the opposite failure mode too: a stop this tight increases how often a
position gets stopped out by ordinary noise rather than a real reversal — none of
the 11 Round-4 trades got close to their stop (worst was COMP at −0.215R against a
−1.0R stop), so that risk wasn't visible yet, but it's the thing a narrower stop
could newly introduce.

## Round 6 (user-directed, 2026-08-20) — floating time-stop: never lock in a loss on the clock

**Change requested:** the fixed 30-minute deadline was closing everything indiscrim-
inately — winners below the 0.5R floor (ZRO +0.256R, ARB +0.239R) and small losses
alike (Round 4/5 data). The user asked for the clock to stop applying to a position
that's currently underwater: wait for breakeven or a real take-profit instead of
force-closing at a loss on a timer.

**Implemented in `agent/demo.py: _cycle_one`**: the time-stop condition changed from
`pnl_now < floor_usdt` to `0 <= pnl_now < floor_usdt` — it now only fires on a
position that is flat-or-ahead but not clearing the profit floor. A position
currently in loss skips the clock entirely from then on; it's governed only by its
real stop-loss (which still fires normally — this is not "never exits") and TP1/TP2,
same as before. Re-checked every cycle, so a position that dips negative, then
recovers to breakeven+, becomes eligible for the ordinary floor test again.

**Trade-off, stated in the code comment and here for visibility:** a losing position
is no longer bounded by time, only by its stop distance — it can occupy a slot and
margin for longer than the old fixed window allowed. That capacity cost is real and
worth watching in the account's slot utilization, not assumed away.

**Tested:** existing suite (`tests/test_demo_lifecycle.py`) passed unchanged
(28/28) — confirms this didn't disturb the winner-side "still open below floor"
case, which was already floating in one direction. Added test 6b for the new
loss-side behavior specifically (position stays open while underwater past the
deadline; its real stop still closes it when hit) — 30/30 passing.

## Round 7 (user-directed, 2026-08-20) — add Ichimoku Cloud to the direction score

**Change requested:** add Ichimoku as another input to the TAKE decision.

**Implemented in `skill/scripts/nobitex_api.py`** (shared by both venues, so this
also reaches the Nobitex-side skill, not just Toobit demo trading):
- `ichimoku_cloud()`: Tenkan (9), Kijun (26), and the cloud (Senkou Span A/B) that
  actually applies to the *current* bar — computed from data as of 26 bars back and
  read forward, not from the current rolling window. Getting the 26-bar displacement
  backwards is the most common Ichimoku implementation bug and silently produces a
  cloud that lags by half a cycle; verified against live BTC/ETH/ORDI data before
  deploying (all three currently trade above a "green" cloud, log below).
- New check in `score_direction()`: "price vs Ichimoku cloud." Only added when price
  is clearly outside the cloud — inside the cloud is Ichimoku's own definition of "no
  signal," and the check is skipped rather than forced, matching how every other
  check here already handles inconclusive data.
- **Threshold left unchanged** (5/scalp, 6/intraday-swing) rather than raised. This
  adds a vote to the pool other checks already draw from; raising the threshold too
  would have made qualifying strictly harder right after Round 5/6 finally got real
  signals flowing again. `auto_checks` goes from up to 8 to up to 9 as a result.
- Documented in `references/indicators.md` §16, including the confluence-quality
  caveat from §15: Tenkan/Kijun are themselves rolling-extreme averages, so they're
  correlated with the existing EMA checks rather than fully independent evidence —
  cloud position is the most distinct signal Ichimoku offers here, which is why
  that's what got wired in rather than a Tenkan/Kijun cross.

**Verified live** (BTC, ETH, ORDI via `toobit.build_snapshot`, scalp profile):
all three resolved a real cloud reading and the check fired correctly, e.g. BTC —
tenkan 71818.95, kijun 70848.15, cloud [68522.9, 69400.25], close 71919.2 → bullish,
`auto_checks` 8→9, threshold unchanged at 5. No crashes on any of the three.

**Not yet measured:** whether this indicator actually improves outcomes here — it's
wired in as one more vote in an existing, validated framework, not separately
backtested against this account's own history the way Round 1's stop-width change
was. Watch score distributions and TAKE rate over the next batch of trades for a
sudden shift, and whether Ichimoku-favorable trades outperform the rest once there's
a large enough sample to split on it.

## Round 8 (user-directed, 2026-08-20) — active management for profitable positions

**Change requested:** for a position currently in profit, re-check the indicators/
conditions every cycle (30s). If the setup still supports more upside, let the
position float past the time-stop deadline instead of cutting it off. If the
conditions have turned, close it — don't wait for price to give the gain back.

**Implemented in `agent/demo.py`**: new `_profit_signal_check(pos)`, called from
`_cycle_one` only when `unrealised_pnl > 0`. Reuses the same latest-scan verdict/
score lookup `_review()` already used for the funding-period check, but on every
cycle instead of once per 8h — an 8h cadence can't matter to a ~30-minute hold.
Three outcomes:
- Confirmed TAKE → position is exempted from the time-stop this cycle (floats).
- Confirmed non-TAKE → closes immediately as `signal_exit`, in profit, without
  waiting for the clock or a real level.
- No scan row at all → falls through to the *unmodified* time-stop logic.

**A real bug caught by the existing test suite before this ever deployed:** the
first version treated "no scan data" the same as "confirmed still favoured" (failed
open to floating). That's wrong and more consequential than it sounds — most cycles
for most positions won't have a scan newer than the position's own age (scans run
every 5 min, cycles every 30s), so failing open would have silently suppressed the
ordinary floor-based time-stop for the common case, not an edge case. Existing test
5 caught this immediately (a position that should have time-stopped stayed open
indefinitely instead). Fixed by making `_profit_signal_check` return a genuine
three-state result (`still_favoured` is only `True` on a *confirmed* TAKE, not on
an absence of data) rather than a two-state one.

**Tested:** `tests/test_demo_lifecycle.py` now covers all three outcomes explicitly
(6c/6d/6e) plus the pre-existing 31 checks — 34/34 passing. `signal_exit` added to
`agent/report.py`'s `EXIT_REASONS` ordering so it groups correctly in reports.

**Scope, as requested:** this only applies to positions currently in profit. Losing
positions are untouched — they already float unconditionally per Round 6, governed
only by their real stop-loss.

## Round 9 (user-directed, 2026-08-20) — password gate on the reset endpoint

**Change requested:** the reset-account button should require a password before it
actually resets. This is the single most destructive action a public,
unauthenticated dashboard exposes — it wipes the entire sample the project exists
to collect (see CLAUDE.md's server-access notes: the dashboard has no auth and no
firewall by design, and *anyone* who can reach the port can currently reset it).

**Deliberately NOT committed to git**: the literal password. This repo is public
(`seiwaz/crypto-project`), and the pattern of password the user gave looked like it
could be reused elsewhere for them — putting it in a public repo's source would
defeat the purpose and expose more than intended. Implemented as a value read from
the server's live `config/settings.json` (`demo.reset_password`), which — like
`config/settings.json` generally — is explicitly never synced from git (see
CLAUDE.md's sync-rule exception) and is set directly on the server, outside this
commit entirely.

**Implemented:**
- `agent/server.py`'s `/api/demo/reset` handler now checks `body["password"]`
  against `demo.settings()["reset_password"]` and returns 403 on a mismatch or
  missing value, before ever touching the account.
- `agent/demo.py: settings()` reads `reset_password` from the `demo` settings block
  (`None` if unset, which leaves the gate off — a deliberate default so existing
  automation/tests aren't broken by this unless the server is actually configured).
- `web/app.js`'s reset button now prompts for the password after the existing
  confirm dialog, sends it with the reset request, and shows the server's error
  message (rather than silently doing nothing) on a wrong password.
- Confirmed `server.py: public_settings()`'s explicit allowlist does not include
  `reset_password` — it cannot leak back out through `/api/settings` or anywhere
  else in the API.

## Round 10 (user-directed, 2026-08-20) — the long-only bias, and locking TP1 profit

**Investigation (213 closed trades, first full-sample review):** win rate 59.2%,
expectancy only +0.054R despite that — thin because `stopped` exits (39% of trades)
split into ~47 genuine full losses (median ≈ −0.85R) against wins from
`time_stop`/`signal_exit` averaging only +0.27–0.29R, capped by the aggressive
30-minute window. Regime split was the more telling number: longs taken while BTC
itself was bearish ran +0.29R expectancy / 71% win rate; longs taken while BTC was
bullish ran **−0.05R expectancy** / 54% win rate — backwards from naive expectation,
consistent with bullish-BTC entries chasing already-extended alt moves.

**The headline finding: zero of 213 trades were short.** Traced to
`agent/skill.py: side_from_direction()` — it picked short only when
`short_score > long_score` strictly, defaulting to long on every tie and every case
where long merely wasn't behind. Confirmed live, same day: the scan showed 66/69
coins scored long, only 3 short, and 100% of TAKE/WATCH verdicts long. Market
conditions this window are a contributing factor, not the sole cause — a tie-break
that can only ever resolve to one side is a code defect regardless of what the
market is doing.

**Fixed:**
- `side_from_direction()` now requires a side to lead by more than
  `DIRECTION_MARGIN` (1) to be chosen; anything closer (including exact ties) is
  `tied`, still returns a side so a card/plan can render, but is meant to be excluded
  from trading.
- `demo.py: qualifying_signals()` now actually enforces `side_tied` — this flag
  already existed and was stored in the DB and shown in the UI, but nothing was
  reading it to block a trade. It was purely cosmetic before this fix.
- Tested: `tests/test_demo_lifecycle.py` 9b covers both states of the flag directly.

**Also requested and implemented: lock the TP1 profit.** `_reduce_at_tp1` used to
move the runner's stop to breakeven-plus-costs; a reversal right after TP1 gave back
almost the whole runner and the trade netted close to zero beyond the banked TP1
half. Now the stop locks at the TP1 price itself — a reversal can only take back the
runner's *further* upside, never the proven gain. Trade-off, stated in the code
comment: the stop sits much closer to price right after TP1 than a breakeven stop
did, so the runner is easier to stop out of on ordinary noise immediately after TP1
fires. Test 1/2 updated to assert the exact new contract (`stop == tp1` precisely,
not just "above entry").

**Full suite**: 36/36 checks passing (34 prior + 2 new for the side-tied filter).

**Also**: added a totals row (sum of count/share/P&L, weighted-average of the
per-bucket averages, not an average of averages) to the "by exit reason" table in
the web dashboard.

## Round 11 (user-reported, 2026-08-20) — stops that stopped working: a real bug, found live

**Report:** user noticed some positions weren't closing on their stop. Verified live:
UNI and ARB were both open with mark clearly past their stop (UNI: stop 3.798,
mark 3.700, long; ARB: stop 0.09103, mark 0.08910, long), both ~170 minutes old.

**Root cause: `agent/paper.py: exit_reason()`'s `touched()` helper used a range-
containment check (`low <= level <= high`) for stop/tp1/tp2, when only a one-sided
comparison is correct.** A long's stop only needs `low <= stop` — did price dip to or
below it at any point in the checked range. Requiring `stop <= high` too silently
breaks the check the moment price moves cleanly past the stop and stays there: once
the most recent candle's own high no longer reaches back up to the old stop level,
`touched()` returns `False` forever, even with the position sitting far past its
stop. `liq` right next to these checks was already written correctly as a one-sided
comparison (`low <= liq` for a long) — `stop`/`tp1`/`tp2` are the ones that had
drifted into the wrong pattern. This is likely as old as the paper broker itself, not
something introduced this session — it just needed a losing streak with no bounce
back up to the old stop level to surface, which the tighter 1.0x ATR stop (Round 5)
and higher trade volume (scalp profile, Round 4) made far more likely to happen.

**Fixed:** replaced the three `touched()` calls with direct one-sided comparisons
matching the `liq` pattern already sitting right there. Long: `low <= stop`,
`high >= tp2`, `high >= tp1`. Short: mirrored.

**Tested:** added test 0 to `tests/test_demo_lifecycle.py`, unit-testing
`paper.exit_reason()` directly against the exact failure shape (a candle range
entirely on the far side of a gapped-past level) for both a stop and a target, both
sides. 40/40 checks passing (36 prior + 4 new).

**Not yet known:** how many other currently-open positions besides UNI/ARB were
affected, or how much this cost historically before being caught — every closed trade
with `exit_reason: stopped` in the existing sample was still a real stop (the bug only
suppresses detection, it never fabricates a false one), so past `stopped` trades are
not in question; the concern is specifically positions that should have stopped but
didn't, and are still sitting open, or that eventually recovered/were caught by some
other exit (signal_exit, time_stop) instead of the real level that should have fired
first. Worth a pass through currently-open positions after this deploys to confirm
UNI/ARB and anything else past its stop closes on the very next cycle.
