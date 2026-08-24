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

## Round 12 (user-requested, 2026-08-20) — research "risk-free position" properly, reconcile skill docs with the live Round 10 behavior

**Ask:** research the "risk-free trade" concept from external sources and make sure
the skill teaches it and the live behavior actually implements it — this had already
been implemented in code (Round 10's `_reduce_at_tp1` TP1-price stop lock) but never
formally researched/cited, and `skill/SKILL.md` still described the *old*
breakeven-plus-costs behavior, so the documented plan a user reads and the code
actually running had drifted apart.

**Research (WebSearch, both queries logged):**
- Moving the stop to breakeven after a partial exit is the textbook definition of a
  "risk-free trade" — the position can no longer produce a net loss from that point
  forward. Sources:
  [Trading Heroes](https://www.tradingheroes.com/move-stoploss-breakeven/),
  [Trade-Guard](https://www.trade-guard.info/en/blog/risk-free-stop-loss-strategy),
  [MondFX](https://mondfx.com/what-is-risk-free-in-forex),
  [FeneFX](https://fenefx.com/en/blog/risk-free-in-forex).
- The canonical partial-exit-then-move-stop pattern is attributed to Mark Minervini:
  scale out half once gain equals the original risk (1R), move the remaining stop to
  breakeven. Widely cited as the baseline hybrid approach across practitioner sources.
- Futures/scalping-specific sources ([TradeZella](https://www.tradezella.com/blog/scalping-strategies),
  [FeneFX](https://fenefx.com/en/blog/risk-free-in-forex)) confirm the same
  partial-then-lock-stop mechanism is standard for short-hold scalp strategies
  specifically, not just swing trading — directly relevant since this system runs a
  5-30 minute scalp profile.
- No source described locking the runner's stop at the *partial-exit price itself*
  (rather than breakeven) under a specific named term — it is a strictly more
  conservative variant of the same breakeven mechanism, not a distinct published
  strategy, so it's documented here as a variant with its own explicit trade-off
  rather than attributed to a source that doesn't describe it.

**Conclusion: the Round 10 implementation is correct and well-grounded**, and is a
*more* conservative form of the standard risk-free mechanism (floor = TP1's R-multiple
instead of floor = 0), which fits this system's own finding (Round 10's 213-trade
review) that reversals right after TP1 were giving back nearly the whole runner under
the old breakeven-plus-costs stop. No code change needed — `agent/demo.py:
_reduce_at_tp1` already does this correctly (`runner_stop = price`, the TP1 fill
price).

**Fixed: `skill/SKILL.md` and `skill/references/risk-math.md` were out of date.**
`SKILL.md`'s Step 8 management rules still told the reader "on TP1: close 50%, stop to
breakeven plus accumulated costs" — the *old*, pre-Round-10 behavior. Updated to state
the actual live rule (stop locked at the TP1 fill price) and point to a new §10 in
`risk-math.md` that lays out the three variants (breakeven only / breakeven+costs /
locked-at-TP1-price), their worst-case floors and trade-offs, the citations above, and
why this system deliberately picked the most conservative of the three. Synced to both
copies (`skill/` in this repo and `~/.claude/skills/crypto-leverage-trade-plan/`) per
the sync rule. No code, no test, and no deploy needed — this was a documentation-only
round; the compile-check/restart deploy pipeline doesn't touch `SKILL.md` or
`references/*.md`, which are read fresh from disk, so no server-side action is
required for this to take effect for anyone reading the skill.

## Round 13 (user-reported, 2026-08-20) — the same coin could be opened twice at once

**Report:** while checking open positions live, found two separate WIF positions open
simultaneously (ids 436 and 438, different slots, opened 2.5 minutes apart from
consecutive scans 380 and 381) — doubling WIF's single-name risk beyond the
one-slot-per-coin design the slot/heat model assumes.

**Root cause: `qualifying_signals()`'s re-entry guard (`agent/demo.py`, the
`open_coins` set) only read `store.paper_open_positions()`, which filters
`status = 'open'`.** This account trades with `maker_entry` on, so a qualifying signal
doesn't fill immediately — it places a resting limit order at `status = 'pending'`
(`agent/store.py: paper_open`/`paper_pending_positions`) that only flips to `'open'`
once price actually trades through it (`_work_pending`), or gets cancelled after
`maker_timeout_minutes` (2 min) unfilled. While a coin's own order sits in that
pending window, it is invisible to `open_coins` — so a fresh scan a few minutes later
(inside that same 2-minute window) saw the coin as "not open" and queued a second
entry for it. `correlated_same_side()`'s cap check (`agent/demo.py`, called from
`try_fill_slots`) had the identical gap — it was also passed only
`store.paper_open_positions()`.

Both gaps share one cause: a pending position already carries the real committed risk
(`margin`/`risk_amount`/`stop`/`tp1`/`tp2` are all set at placement time in `_open()`,
identically for `status='open'` and `status='pending'` — only the fill/fee timing
differs) but neither guard treated it as "already have exposure to this coin/cohort"
until it filled.

**Fixed:** both guards now union `paper_open_positions()` with
`paper_pending_positions()` before checking. `qualifying_signals()`'s `open_coins`
and the `correlated_same_side()` call in `try_fill_slots()` both changed.

**Known, deliberately not fixed this round:** `state()`'s `slots.filled` and the
displayed `heat.used_pct` still count `paper_open_positions()` only, not pending. This
means capacity/heat can still be briefly undercounted *across separate
`try_fill_slots()` calls* while a maker order is pending (bounded to the 2-minute
maker-timeout window, and only ever an undercount, never an overcount, since heat is
correctly added within a single call before any placement in that call). This is a
narrower, lower-severity version of the same class of gap — worth a look if it's ever
seen to matter in practice, but the fix in this round already closes the specific,
demonstrated failure (the same coin doubled up), and widening `state()`'s displayed
position list to include unfilled orders is a separate, UI-facing change with its own
trade-offs (showing a resting order as if it were a filled position with
mark-to-market PnL would be misleading) that wasn't part of what broke here.

**Tested:** added test 9c to `tests/test_demo_lifecycle.py` — a coin with a pending
(not yet open) order is correctly excluded from `qualifying_signals()`. 42/42 checks
passing (40 prior + 2 new, since 9c has two assertions).

## Round 15 (2026-08-23) — why the live account loses: the exits are asymmetric

**Issue.** 23 closed live Tabdeal trades, net **−0.544 USDT** on a ~5.3 USDT account
(−10%). User asked why the loss rate is so high, and whether the skill is at fault.

**Evidence.** Grouped by exit reason:

| exit_reason | n | median hold | mean move | sum net | stop dist |
|---|---|---|---|---|---|
| exchange_exit (stop fired) | 6 | **548 min** | −1.136% | **−0.482** | 1.99% |
| signal_exit | 13 | 11 min | +0.230% | −0.029 | 2.06% |
| time_stop | 3 | 33 min | +0.050% | −0.032 | 2.42% |

Six stop-outs are **89% of all loss**, at a median hold of nine hours on a strategy
whose intended hold is 5–20 minutes. **TP1 never fired once in 23 trades.**

Two hypotheses tested and **rejected**:
- *Chasing extended moves* (the Round 10 suspicion). Pre-entry run-up is
  indistinguishable between winners and losers: 30m −0.118% vs +0.008%, 60m −0.015%
  vs −0.092%. Not chasing.
- *Wrong-way direction.* The market was rising throughout — 0 of 7 majors falling
  over 60m while all three open longs were red. Direction was right.

**Root cause.** `_manage_one` checks a *winner* against the latest verdict every
cycle and closes it the moment the setup lapses. A *loser* is checked against
nothing — `_profit_signal_check` runs only inside the in-profit branch, and the
time stop is guarded by `0 <= upnl`. So a losing trade has exactly one exit, the
exchange stop, however many hours that takes.

Realised: winners banked **+0.11R** (signal_exit fires as soon as profit clears the
0.2% round trip, ≈0.25R), losers realised **−1.0R**. About **1:9 against us** —
breakeven would need a ~90% win rate.

**Research.** Confirmed the reward:risk reading against published guidance: the
honest ceiling for scalping is 1:1 to 1:1.5, compensated by a 60–75% win rate, and
round-trip cost on a small target is 20–40% of intended profit
([For Traders](https://fortraders.com/blog/scalping-strategies-maximizing-profits-in-short-term-trades),
[SM Developers](https://smdevs.in/resources/blogs/best-risk-reward-ratio-scalping)).
Also checked the ATR-timeframe rule — stops should use the ATR of the timeframe that
triggered the entry, not a higher one
([Traders Second Brain](https://traderssecondbrain.com/guides/stop-loss-placement-methods),
[QuantStock](https://quantstock.org/blog/atr-stop-loss-strategy-guide)).

**The ATR timeframe is NOT the bug**, and this is worth recording because it is the
intuitive fix and it is wrong. Measured across 14 coins:

| stop basis | stop dist | cost_in_R | breakeven win rate @1:1 |
|---|---|---|---|
| 1.5×ATR15m (current) | 0.788% | 0.254R | 63% |
| 1.5×ATR5m | 0.403% | 0.496R | 75% |
| 1.0×ATR5m | 0.269% | 0.744R | 87% |

Moving ATR to the entry timeframe *tightens* the stop, which makes the fee a larger
share of R and pushes the required win rate to 75–87%. The line-40 comment in
`trade_plan.py`'s scalp profile was right to keep ATR on 15m.

**Change.** Added `adverse_exit` to `_manage_one`: a losing trade past
`adverse_exit_after_h` (default = the time-stop window) whose setup has lapsed is
closed. The exchange stop stays where it is as the backstop — this only stops us
waiting for a stop that was sized for a signal which no longer exists. Also fixed
entry filling one slot per scan (`_try_open_locked` returned on its first fill), so
a four-slot board took 20+ minutes to fill. Tests 21/22; 132/132.

**Status: shipped, live at head `62eed35`.** Verified against the live book at the
moment of the fix: ICP −1.054% with a current verdict of **SKIP** — dead setup,
1.2% above its stop, and under the old code entitled to no check at all.

**Open, and larger than this fix.** The measured edge is roughly the size of the
fee: the one-day replay of 2,885 signals gave fwd30 **+0.105%** and fwd60
**+0.221%** against a round trip of **0.200%**. At a 30-minute hold the strategy
does not clear its own costs even when the signal is right. This fix removes an
unforced 1:9 asymmetry; it does not manufacture edge. See the Tabdeal fee blocker.

## Round 16 (2026-08-23) — the direction score counted the same fact twice

**Issue.** User: "check signaling it may work bad, do not use only skill, research
about signalling, so skill may be incorrect." The direction score is a vote count
over 9 checks, and a vote count is only evidence if the votes differ.

**Research.** Multicollinearity in technical analysis — "the unknowing use of the
same type of information more than once" — is a documented failure mode, and
overlapping indicators "create a false sense of confirmation"
([StockCharts](https://chartschool.stockcharts.com/table-of-contents/overview/multicollinearity),
[Earn2Trade](https://www.earn2trade.com/blog/avoiding-indicator-overlap/),
[LuxAlgo](https://www.luxalgo.com/blog/common-mistakes-traders-using-indicators/)).
The prescription is one indicator each for trend, momentum and volatility, not
several of the same type.

**Measurement** (`/tmp/votes.py`, 1,331 evaluations, 8 coins, real
`skill.score_direction`, votes recorded per check):

Mean pairwise agreement 60.5% (50% = independent). Two pairs are near-duplicates:

| pair | agreement |
|---|---|
| price vs EMA200 (bias) ↔ EMA50 vs EMA200 (bias) | **90.7%** |
| price vs EMA50 (decision) ↔ price vs session VWAP | **87.7%** |

Both pairs ask "is price above its recent average", twice.

**Hypothesis rejected along the way:** I expected near-unanimity to be the norm,
from a live scan showing 33/33 coins long. It is not — unanimous votes are 11.1%
of cases and ≥80% one-sided 38.5%. That snapshot was a strongly trending moment,
not the general case.

**Conviction is not monotonic** — this is the actionable part:

| votes | n | mean 4h | net of 0.2% | win |
|---|---|---|---|---|
| ≥5 | 1153 | +0.480% | +0.280% | 51% |
| ≥6 | 883 | +0.517% | +0.317% | 53% |
| ≥7 | 486 | +0.568% | +0.368% | 52% |
| **≥8** | **134** | **+0.219%** | **+0.019%** | **41%** |

A near-unanimous signal is *worse* than a 7-of-9. Round 10 found the same shape
independently (the 80-89 score band underperformed 70-79). Double-counting trend
inflates the score hardest in a trending market — i.e. when the move is already
extended.

**Change.** Checks now carry a `family`; a family shares one vote's weight,
split among its members. No check is dropped — all stay visible with their own
reasoning. `direction_ratio` (35 of the 100 score points) is now honest.

**Two calibration traps, both caught by testing against live data before deploy:**
- Collapsing a family to its *majority* makes it abstain when members disagree.
  That deflated typical counts to 3-2 of 6 against a threshold of 4 and would have
  stopped signal generation outright. Fractional weighting keeps the granularity.
- `DIRECTION_MARGIN` was calibrated for integer votes out of 9. A typical
  4.00-3.00 weighted split is a margin of exactly 1.00, which the unscaled `> 1`
  test reads as TIED — almost every trade blocked. Both the vote threshold and the
  tie margin now rescale with the denominator.

**Verified on the full 33-coin watchlist:** 29 tradable, 4 tied, **22 long / 7
short** — against 33/33 long before. Part of the long bias was this double count.

**Also this round.** Hold extended from 20 minutes to 4 hours
(`time_stop_hours` 0.3333 → 4.0, `adverse_exit_after_h` 1.0), on the same
measurement: the filter nets +0.28% to +0.37% at 4h and is *negative* at 30
minutes, where forward return (~+0.105%) does not cover the 0.200% round trip.

**Not the bug, recorded so it is not retried:** the ATR timeframe. See Round 15.

Tests 24; 147/147.

## Round 22 (2026-08-24) — asked to fix the entry point; one was already fixed, the other does not survive testing

**Instruction:** fix the two defects Round 21 found.

### Defect 1 was already fixed, and Round 21 mis-stated it

`agent/tabdeal.py: build_snapshot()` has anchored the plan entry to the **live
futures order-book mid** (`mark_price`) since the Tabdeal cutover, falling back to
the candle close only when the book is unavailable. The lag therefore never touched
the entry price — measured fill-vs-plan-entry is **+0.0037%**. It touches only the
indicator values, and those cannot be moved: Tabdeal publishes no futures candles
anywhere, and a series built forward from the depth feed has no history for a 200-bar
1H EMA. Round 21's entry has been corrected in place rather than left to mislead.

### Defect 2: the entry timeframe stays unused, because no use of it survives

Four separate tests, all on real candles:

| use of the 1m timeframe | n | result |
|---|---|---|
| require 1m close > 1m EMA20 | 7,681 | **backwards** — above 1.04, below 1.14 |
| both 1m confirmations together | 7,681 | 1.05 vs a 1.07 baseline |
| refuse entries in the top quarter of the last 5x1m range | 7,813 | see below |

The spike gate looked like the one worth having — kept set ratio 1.10 against a 1.04
baseline, refused tail 0.96, and it improves 23 of 33 symbols. **It fails on
stability, which is the test that matters:**

| | first half | second half |
|---|---|---|
| baseline | 1.15 | 0.94 |
| keep spike < 75 | 1.30 | 0.95 |
| refused tail | 0.87 | 0.99 |

By chronological quarter the benefit decays straight to nothing: **+0.200, +0.149,
+0.007, -0.041**. In the most recent quarter the refused entries are *better* than
the kept ones. An effect that is strong early, absent late, and monotonically
decaying is a regime artefact, not a structural edge — and the whole baseline swings
1.15 -> 0.94 across the same span, which is what it is riding on.

(One symbol, XAUT, reports 1.42 -> 28.01. Its median MAE collapses to near zero
because it is gold-backed and barely moves — ATR 0.16%, already flagged in
`coins.txt`. A degenerate ratio, not a result.)

**Not shipped.** Putting a filter that works in one half of a four-day sample in
front of real money is precisely the overfit Round 16 committed and Round 17 had to
undo. The entry timeframe stays fetched-but-unread, which is honest waste rather than
a fitted rule.

### What the evidence does support, and is NOT being done unilaterally

Every measurement taken since Round 17 puts the **two-hour** horizon well ahead of
the one-hour horizon the engine actually uses:

| source | 1h | 2h (or longer) |
|---|---|---|
| Round 17, 21,315 signals | +0.086% @4h | **+0.348% @8h** |
| Round 18, 19,855 entries | +0.053% | **+0.302%** |
| Round 19, post-gate kept set | +0.0058R | **+0.0562R** |

`profit_close_after_h` is **1.0**. Three independent samples say the geometry needs
longer, and this is far better supported than anything tested in this round. It is
left alone because the one-hour rule was an explicit operator instruction, not a
default — changing it is a decision to put to the operator, not to infer.


## Round 20b (2026-08-24) — re-asked: why does a new position go negative?

Re-measured with more history (40 positions with recorded samples, up from 32).
**The answer splits cleanly in two, and the first part is almost the whole of it.**

### It does not go negative. It STARTS negative, by exactly the round trip.

The board shows **net**, which subtracts the full 0.2% of notional the moment the
position exists. Live proof from one row, FLOKI at 32 minutes: **gross +0.00436**
— price moving our way — against a 0.00882 round trip, so **net −0.00445**. A
winning position displaying red.

| within | ever GROSS positive | ever NET positive |
|---|---|---|
| 1 min | **58%** | **6%** |
| 2 min | 69% | 25% |
| 5 min | 72% | 28% |
| 10 min | 87% | 49% |
| 30 min | 90% | 68% |
| 60 min | 92% | 70% |

Time to the first positive reading: **gross median 0.6 minutes, net median 7.8
minutes** — thirteen times longer. Price goes our way almost immediately in most
trades; what takes eight minutes is earning back the fee. A position must move
**+0.200%** simply to display zero, against a median ten-minute best case of
+0.206% (Round 20). That is why almost every position looks red early even when
nothing is wrong with it.

### The residual effect from Round 20 is still there, and got stronger

Permutation test, 20,000 draws, each position matched against random bars on the
**same coin in the same hour**:

| | live | null | p |
|---|---|---|---|
| MAE (10 min) | **-0.540%** | -0.310% | **0.0003** (was 0.0019 at n=32) |
| MFE (10 min) | +0.335% | +0.363% | 0.27 |

Same upside, ~74% deeper drawdown, now significant at p=0.0003. **Cause still
unidentified after seven tests** (Round 21's table). It is comparable in size to
the fee — 0.230pp of excess drawdown against 0.200% of round trip — but unlike the
fee it is a distribution property, not something visible in the first minute. For
the specific experience of "it goes red the instant I open it", the fee is the
entire story.


## Round 21 (2026-08-24) — the entry point, audited against candles and indicators

**Asked:** there may be a mistake in the entry point — verify carefully against
candles, charts and indicator history.

### Verified CORRECT — these are not the problem

| check | result |
|---|---|
| bar alignment | spacing exactly 300s / 3600s; at production limits the phase is stable |
| indicator reproducibility | two fetches 40s apart: ATR/EMA identical to **0.000%** |
| data freshness at entry | snapshot fetched -> fill median **22s**, max 39s |
| fill vs plan entry | median **+0.0037%** |
| fill vs the bar it landed in | median **+0.0043%**, above the close exactly 50% of the time |

An early reading suggested bars were off-boundary (BTC 5m starting at 04:48:27).
That was an artefact of probing with `limit=6`: `klines()` derives `from_ts` from the
requested count, and a short window makes the venue bucket from `from` instead of a
canonical origin. At the limits production actually uses, bars are aligned. **Not a
bug** — but worth knowing before anyone probes this API again with a small limit.

### Genuinely wrong, and small

**1. The chart feed lags the futures book, directionally.** 85 paired samples across
5 symbols: correlation(60s price move, chart-vs-mid gap) = **-0.475**. While price is
falling the chart reads **+0.049%** above the market; while rising, **+0.005%**.
Median gap is otherwise tiny (-0.03% to +0.05%), so this is lag, not basis.

> **CORRECTION (Round 22).** This entry first said "the decision price is stale-high
> into declines". That is **wrong**, and the fix it implied was already in the code.
> `tabdeal.build_snapshot()` anchors `last_price` to `mark_price(symbol)` — the live
> futures order-book mid — and falls back to the candle close only when the book is
> unavailable, with a comment saying exactly why. The plan's ENTRY is live; that is
> why fill-vs-plan-entry measures +0.0037%. The lag touches only the INDICATOR
> values, which need a series and have no futures source: no futures klines exist on
> any Tabdeal host, verified across 2 hosts, 2 sockets and 10 REST paths, and candles
> built forward from the depth feed would have no history for a 200-bar 1H EMA. So
> the indicator lag is real, ~0.05%, and not fixable.

**2. `entry_tf` is computed in full and then discarded.** The scalp profile declares
`entry_tf: 1m`. `compute_indicators` produces EMA20/50/200, RSI, VWAP, Ichimoku,
swings and structure for it, and **exactly one field survives**:
`snap["last_price"]`. All nine direction checks read the bias (1H) or decision (5m)
timeframe. A strategy whose entire difficulty is *when* to enter throws away the only
timeframe fine enough to time it. ZEC is the illustration: at entry its 1m close
(872.746) sat under its own 1m EMA20 (873.76) and inside the 1m cloud.

### Seven hypotheses now tested and REJECTED

| # | hypothesis | n | result |
|---|---|---|---|
| 1 | prior 15m run-up | 39,303 | MFE/MAE flat 1.03-1.26 across every band |
| 2 | extension over 1H EMA200 | 28,812 | flat; least-extended band is the worst |
| 3 | position in the 2h range | 38,709 | flat 1.08-1.19 even in the top decile |
| 4 | entry slippage | 53 | fill +0.004% vs bar close, 50/50 either side |
| 5 | "buys the top of a 5m push" | 53 | median in-bar fill at the **56.5th** percentile, not ~100; 67% of the next hour trades above the fill; only 8% never exceeded |
| 6 | 1m close above its 1m EMA20 | 7,681 | **backwards** — above 1.04, below **1.14** |
| 7 | both of the 1m confirmations | 7,681 | 1.05, no better than the 1.07 baseline |

**#5 deserves a note.** Two hand-picked trades (NEAR filling at 2.06 against a bar
high of 2.0629, TAO at 242.24 against a spot bar high of 242.00) looked like proof
that the engine buys the exact local top. Measured across all 53 entries it is not
true. Two examples are not evidence; this is the third time this session that a
pattern visible in a handful of trades dissolved at scale.

### The one gradient that survived

Where price sits inside the **last five 1m bars** does grade monotonically:

| position in the 5x1m range | n | MFE/MAE |
|---|---|---|
| 0-25% | 2,430 | **1.19** |
| 25-50% | 1,407 | 1.09 |
| 50-75% | 1,365 | 1.10 |
| 75-90% | 757 | 0.95 |
| 90-100% | 1,722 | 0.98 |

Real and in the intuitive direction, but modest (1.19 vs a 1.07 baseline) and it
keeps only a third of entries. Not shipped: it is a candidate, not a finding, and
combining it with the EMA20 check destroyed it (1.05).

### Standing conclusion

**No bug in the entry point.** The price is right, the data is fresh, the indicators
are reproducible. The Round 20 result stands — entries take a significantly deeper
drawdown than a random moment on the same coin in the same hour (p=0.0019) for
identical upside (p=0.40) — and after seven tests the cause is still unidentified.
The two real defects found here (a ~0.05% directional feed lag, and an unused entry
timeframe) are together too small to account for it.


## Round 20 (2026-08-24) — why a fresh position shows red, and what the stops really cost

**Asked:** positions almost always go to loss right after opening — why? And
re-check the stops.

### Part 1: the red is mostly arithmetic, and partly real

**The arithmetic, which is most of it.** The board's net P/L subtracts the whole
0.2% round trip, so a position is *born* showing -0.2% of notional and has to rise
0.2% before it reads zero. Median best-case in the first ten minutes is +0.33%.
Almost every position therefore shows red for its first minutes **even when price
is moving our way**. That is not a signal fault; it is the fee being shown honestly.

**The dip is also transient.** Gross price move from entry, 32 positions with
recorded samples:

| after | median | negative |
|---|---|---|
| ~15s | -0.001% | 50% |
| 1 min | -0.051% | 61% |
| 3 min | -0.112% | 68% |
| 5 min | **-0.211%** | **71%** |
| 10 min | -0.028% | 55% |
| 30 min | **+0.145%** | 44% |

It bottoms around five minutes and recovers by thirty.

**But there IS a real effect underneath, and it is significant.** Permutation test,
20,000 draws, each live position matched against random bars on **the same coin in
the same hour**:

| | live | null (same coin, same hour) | p |
|---|---|---|---|
| MAE (10 min) | **-0.540%** | -0.315% [-0.429, -0.193] | **0.0019** |
| MFE (10 min) | +0.335% | +0.353% [+0.227, +0.478] | 0.40 |

**Same upside, 70% deeper drawdown.** Regime is controlled (BTC moved -0.54% over
the whole 17h span, and the peers come from the same hours on the same coins).

### Four mechanisms tested and REJECTED — do not retry these

1. **Prior 15m run-up** (n=39,303). MFE/MAE is flat at 1.03-1.26 across every
   band from -1% to +2%. A bigger run-up raises MAE *and* MFE together — that is
   volatility, not adverse timing. This also re-kills the "chasing extended moves"
   idea from Round 10 with a much larger sample.
2. **Extension over the 1H EMA200** (n=28,812). Already rejected in Round 19.
3. **Position in the 2h range** (n=38,709). Flat at 1.08-1.19 even in the top
   decile. Live entries *do* sit high in the range (median 78th percentile, 38% in
   the top fifth vs a 29% baseline) — but since range position does not predict a
   deeper drawdown, that skew explains nothing.
4. **Entry slippage** (n=32). The fill is **+0.0043%** against the close of the 5m
   bar it landed in, above it exactly 50% of the time, and +0.0037% against the plan
   entry. Execution is clean; it accounts for ~2% of the gap.

**Conclusion: the effect is real and the cause is not identified.** n=32 is enough
to establish it and not enough to explain it. No fix is being shipped on a guess —
this project has already had Round 16 overturned by Round 17 for exactly that.

### Part 2: the stops

**Placement is correct.** Both open positions carry a venue stop matching the plan
to within 0.1%, `sl_tp_set=1`, at 2.026% and 1.874% — inside Round 19's new band.
Across the whole history exactly **one** position ever ran with no stop attached
(HYPE, 08-23), the race `reconcile()` now repairs every cycle.

**Most exits are not stop-outs.** Of 70 closed trades only **12** filled at or
through their stop; 57 exited while still inside it (engine closes and TP hits).
An earlier read of this conflated the two — several 08-22 rows show a fill 2-3%
*above* the stop on a long, which is not a stop-out at all.

**Fills are good; one real gap.** Overshoot beyond the level: median **+0.232%**,
mean +0.472%, worst **+2.972%** (XRP, 08-23). 11 of 12 filled at or through.

**The number that matters: a stop-out realises -1.29R, not -1R** (median -1.292,
mean -1.351). Roughly 0.12R of overshoot plus 0.15R of round trip on top of the
-1R the plan assumes.

**What that does to the geometry.** TP1 is +1R gross, so a win nets about +0.85R
against a loss of -1.29R. Breakeven win rate is **60.5%**, and Round 19 measured
TP-in-1h at 30.5% after the new gates. **The TP1=1R geometry cannot pay for itself
on target hits alone.** Whatever edge exists has to come from the engine's >=1h
`profit_close` on the 52.8% of positions that touch neither level (Round 18) — and
Round 18 measured those at +0.049R mean. This is the central economic problem and
no filter fixes it.


## Round 19 (2026-08-24) — why ZEC was a mistake, and the two gates that catch it

**Asked:** ZEC hit its stop — was the signal wrong? And prevent the AAVE / TAO /
NEAR stop-outs.

### What actually happened to ZEC

Entry 873.545 at 18:46, stop 848.418 (**2.876%**), stopped at 842.2 for **-1.32R**.
ZEC had run **+2.81% in ten minutes** (18:03->18:13) and topped at 885.4 by 18:18.
The engine bought at 18:46 — **28 minutes after the top, on the way down** — and it
never made a new high afterwards. BTC was flat (+0.10% to +0.29%) the whole time,
so this was ZEC-specific.

Every gate passed. Nothing was broken. The failure was that **TP1 sits at 1R, so the
stop distance IS the target distance, and only ONE side of it was bounded.** The cost
gate rejects a stop that is too TIGHT (cost_in_R = 2 x fee / stop_pct). Nothing
rejected one too WIDE — and a 2.876% stop scored *well* on cost drag (0.08R, 10.5/15
points). The scoring was rewarding exactly the geometry that cannot resolve.

### The test — 28,812 gated entries, 33 symbols, real 5m candles, no lookahead

Each 5m bar taken as an entry with the stop the planner would set, walked forward to
the first touch, scored in R net of the round trip. Bias-TF figures read only from
1H bars already closed; swing highs via production's own `find_swings`, which
confirms a swing only `right` bars later.

**Tails, measured (the thing to exclude):**

| excluded tail | n | TP in 1h | mean R 1h | mean R 2h |
|---|---|---|---|---|
| stop > 2.25% | 2,718 | 12.3% | -0.0973 | -0.1500 |
| stop < 1.00% | 6,641 | 28.2% | -0.1039 | -0.0248 |
| 1H ATR > 2.25% | 8,371 | 18.1% | -0.0929 | -0.0641 |

All negative in **both** halves of the sample.

**Keeping the middle** (`1.0 <= stop <= 2.25` and `1H ATR <= 2.25`): 47.4% of
entries, TP-in-1h **30.5%** (baseline 26.3%), mean R 1h **+0.0058** (baseline
-0.0481), 2h **+0.0562** (baseline +0.0026), positive in both halves.

### Two hypotheses TESTED AND REJECTED — do not retry

1. **Extension over the 1H EMA200 does not matter.** ZEC was +36.9% extended and it
   looked decisive on the live record (5 of 8 stop-outs above +20%, 0 of 6 winners).
   The replay says otherwise: ext < 20% gives 2h +0.0033, ext >= 20% gives -0.0002,
   and the *least* extended band (0-5%) is the worst of all at -0.1013 (1h). This is
   a 14-trade pattern that 28,812 entries do not support.
2. **Buying under a swing high is worse — but the filter is not worth having.**
   The direction is real: price already ABOVE the last confirmed 1H swing high gives
   TP-in-1h 36.3% vs 21.9% below it. But stacked on the stop/ATR bounds it *lowers*
   2h expectancy (+0.0387 vs +0.0562) while cutting trade count 60%, and on the live
   record it would have blocked **4 of 6 winners**. The bounds already capture it.

Also rejected earlier and worth restating: tightening the ATR timeframe (Round 15) —
a tighter stop raises cost_in_R proportionally.

### Shipped

Two gates in `trade_plan.qualify()`, scalp only (`gate_stop_pct_min` 1.0,
`gate_stop_pct_max` 2.25, `gate_bias_atr_max` 2.25); other profiles carry no such
keys and are untouched.

- **`stop reachability`** — the stop, and therefore TP1, must sit in 1.0-2.25%.
- **`regime volatility`** — the **bias-TF** ATR must be <= 2.25%. Distinct from the
  existing "volatility fit", which reads the 15m ATR that sets the stop: ZEC passed
  that at 1.24% while its 1H ATR was 2.58%.

`stop_pct_min` / `stop_pct_max` were left as warnings. They were never gates — the
scalp ceiling of 1.5% would reject most of the profitable band, which is how a
2.876% stop passed a profile that documents a 1.5% ceiling.

**Live corroboration, 14 closed trades that reached a level:** blocks **4 of 8**
stop-outs — both ZECs, AAVE, XRP, together **-0.674 USDT** of realised loss — and
**0 of 6** target hits.

**Honest limit: NEAR (stop 1.802 / 1H ATR 1.93) and TAO (2.048 / 1.77) are NOT
blocked, and should not be.** They sit in the middle of the profitable band. With
TP-in-1h around 30%, losses are the cost of the distribution; a filter that removed
those two would be fitted to them. What is preventable here has been prevented.


## Round 18 (2026-08-23) — does the geometry resolve inside a 5m-1h hold?

**Question asked:** the strategy is meant for 5-minute to 1-hour trades. Are the
indicators and gates timed for that?

**Answer: the indicators and gates are internally coherent and working. The
GEOMETRY they produce is not a 5m-1h trade, and the live record proves it.**

### Measurement 1 — replay, 19,855 gated entries, 33 symbols, real 5m candles

Every 5m bar taken as a hypothetical entry, with the stop the planner would
actually set (1.5 x ATR(14) on 15m, ATR read only from bars already closed), then
walked forward to see which level is touched first.

| horizon | TP1 first | stop first | still open | med MFE | med MAE | mean net |
|---|---|---|---|---|---|---|
| 5m  | 0.7%  | 0.5%  | **98.9%** | +0.155% | -0.136% | -0.176% |
| 15m | 5.1%  | 3.0%  | 91.8% | +0.301% | -0.247% | -0.132% |
| 30m | 14.2% | 8.9%  | 77.0% | +0.457% | -0.354% | -0.071% |
| 1h  | 28.9% | 18.3% | **52.8%** | +0.705% | -0.480% | **+0.053%** |
| 2h  | 47.6% | 26.9% | 25.6% | +1.070% | -0.640% | +0.302% |

- **At five minutes 98.9% of positions have touched nothing.** The bottom of the
  intended range is geometrically impossible at this stop distance.
- **At one hour 52.8% still have not resolved.** The median 1h excursion is
  +0.705% against a TP1 sitting at 0.943%.
- Net of the 0.2% round trip the average trade does not turn positive until 1h,
  and only clears it meaningfully at 2h. This agrees with the 21,315-signal
  replay in Round 17 (+0.348% at 8h).

**The cost gate is the binding volatility constraint, not `atr_pct_min`.**
Median stop 0.943%, median cost 0.212R against a 0.25R ceiling; the gate needs
stop >= 0.80%, i.e. ATR15m >= 0.533%, well above the profile's 0.3% floor. It
rejects **33.0%** of otherwise valid entries. That is the gate working.

**Why the stop cannot simply be tightened to fit the window** (re-confirmed, do
not retry): `cost_in_R = 2 x fee / stop_pct`. Halving the stop doubles the cost
in R. Round 15 measured 1.0xATR5m at 0.744R of cost, needing 75-87% win rates.

### Measurement 2 — the live record, 58 closed trades

| exit reason | n | mean R | median R | sum R |
|---|---|---|---|---|
| signal_exit (removed) | 21 | +0.025 | +0.022 | +0.523 |
| **exchange_exit** | **13** | **-0.742** | **-1.147** | **-9.644** |
| profit_close | 11 | +0.049 | +0.062 | +0.535 |
| adverse_exit (off) | 7 | -0.318 | -0.277 | -2.225 |
| time_stop (removed) | 4 | -0.073 | -0.082 | -0.293 |
| **tp1** | **1** | **+1.187** | | +1.187 |
| ALL | 58 | -0.171 | -0.034 | -9.918 |

Winners **+0.216R** mean, losers **-0.464R**, win rate 43.1%, breakeven needed
**68.2%**.

**Exactly ONE trade in 58 has ever reached TP1** — and that single trade
(+1.187R) out-earned all eleven `profit_close`s combined (+0.535R).

**This is the mismatch, stated precisely.** The plan sets a 1R target the replay
says needs 1-2 hours to reach. The engine banks at the 1-hour mark for a
fraction of R, while the stop keeps its full 1R. Winners are cut at a fifth of
their planned size; losers are paid in full. The replay predicts 28.9% TP-first
vs 18.3% stop-first at 1h — a favourable ratio — and the live record turns that
into 1 TP against 13 stop-outs, because the engine takes the winner off the
table before the geometry can complete.

**Stop fills are fine.** Overshoot on 13 `exchange_exit`s: median **+0.082%**,
worst +2.972% (a genuine gap, e.g. ZEC 2026-08-23 filling 842.2 against a stop at
848.42 for -0.165 where -0.134 was planned). The stop mechanism is not the
problem; what the stop is measured against is.

### Indicator lookbacks, for the record

Nothing in the stack measures a five-minute move:

| input | timeframe | real lookback |
|---|---|---|
| EMA200 (bias) | 1H | ~8.3 days |
| ATR(14) | 15m | 3.5 h |
| Ichimoku 9/26/52 | 5m | 45m / 2.2h / 4.3h |
| EMA50 (decision) | 5m | 4.2 h |
| RSI(14) | 5m | 70 min |
| session VWAP | 5m | since 00:00 UTC |

The fastest meaningful signal is Tenkan-sen at 45 minutes. For a trade intended
to last five minutes there is no input that can see it.

### Changed this round

Only one thing, and it is a correctness fix rather than a re-timing: **the plan's
`management` block was describing rules the live engine does not follow** — bank
50% at TP1, trail the stop, exit after 4 decision candles. Tabdeal supports
neither `reduceOnly` nor a partial close, so TP1 is a full close and there is no
time stop at all. `trade_plan._management()` now emits the real rules for
Tabdeal and keeps the generic ones for other venues. Same class of drift as
Round 12, but in program output rather than documentation.

**Not changed, because it is the operator's call:** `profit_close_after_h`,
`hold_take_score`, and whether to let a winner run to TP1 instead of banking it
at the hour.


## Round 17 (2026-08-23) — the big replay corrects Round 16, and kills shorts

**What was run.** `/tmp/replay2.py`: the real `skill.score_direction` over ~25 days
of 15m history for all 33 Tabdeal coins, **21,315 signals**, measuring forward
return at six horizons. Round 16's conclusions came from `/tmp/votes.py` — 8 coins,
1,331 evaluations — and two of them do not survive the larger sample.

**Results, net of the 0.200% round trip (win rate):**

| | 30m | 60m | 2h | 4h | 8h | 24h |
|---|---|---|---|---|---|---|
| ALL (21,315) | −0.189% | −0.177% | −0.150% | −0.079% | +0.022% | +0.433% |
| **long** (11,574) | −0.172% | −0.137% | −0.068% | **+0.086%** | **+0.348%** | **+1.392%** |
| **short** (9,741) | −0.209% | −0.225% | −0.247% | −0.275% | −0.366% | **−0.706%** |
| score ≥6 (14,549) | −0.182% | −0.169% | −0.134% | −0.047% | +0.088% | +0.598% |
| score ≥7 (7,029) | −0.179% | −0.161% | −0.116% | −0.005% | +0.212% | +0.850% |
| score ≥8 (1,461) | −0.160% | −0.134% | −0.069% | +0.122% | +0.438% | +1.292% |

**Correction 1 — the ≥8-vote collapse does not replicate.** Round 16 reported
signals with ≥8 of 9 votes winning 41% over 4h against 52% at 7 votes, from n=134,
and used it to argue that near-unanimity means an extended move. At n=1,461, score
≥8 is the **best** bucket at every horizon. The earlier figure was noise and should
not have been cited as evidence.

Round 16's redundancy fix itself still stands — the 90.7% and 87.7% pairwise
agreements are direct measurements of the checks, independent of any outcome — but
its outcome-based justification was wrong. Grouping duplicated checks is right
because a vote count of correlated votes is not evidence, not because unanimity
predicts badly.

**Correction 2 — 4 hours is too short.** Round 16 set `time_stop_hours` to 4.0 on
the small sample's +0.280%. The large sample puts 4h at −0.079% overall and +0.086%
long-only: roughly breakeven. The edge clears the fee at **8h** and grows to 24h.

**New finding — shorts are negative at every horizon and get worse with time**
(−0.209% at 30m to −0.706% at 24h, n=9,741) while longs improve monotonically.
This is a clean asymmetry on a large sample. Caveat worth keeping: the window is
~25 days of one regime and the market rose through it, so this is evidence that
shorts do not work *here and now*, not a timeless result. Implemented as a
reversible `allow_shorts` setting rather than deleting the short path.

**Changes.** `time_stop_hours` 4.0 → 8.0; `allow_shorts: false`.

**Method note.** Round 16's sample was 6% the size of this one and drawn from 8
coins. Both were "measured, not assumed", and the smaller one still misled on two
of three conclusions. Sample size is part of the evidence, not a footnote.
