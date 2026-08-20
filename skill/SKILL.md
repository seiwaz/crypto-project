---
name: crypto-leverage-trade-plan
description: Decide whether a coin is worth trading at all, then build a risk-first plan for a leveraged position — direction, entry, stop loss, TP1/TP2, position size and leverage — from ATR-based stops and R-multiple sizing. Connects read-only to the Nobitex API with the user's API key/secret for live candles, order book, margin fees and open positions, and can screen a watchlist to rank which coins qualify. Use whenever the user asks to analyze a coin for futures, perps, margin or leveraged trading; asks "is this worth entering", "long or short X", or "which of these should I trade"; asks where to put a stop loss or take profit; asks about leverage or position size; wants a scalp, intraday or swing setup; mentions Nobitex معاملات تعهدی, فیوچرز, اهرم, ضریب, لوریج, حد ضرر, حد سود, نقطه ورود, or a Nobitex API key/token. Use it even when they just name a coin and a timeframe without asking for a plan.
---

# Crypto Leverage Trade Plan

Answer two questions in order:

1. **Is this instrument worth risking anything on right now?** (qualification)
2. **If so, exactly how?** (the plan)

Most losses come from skipping the first question. A beautiful entry on an illiquid,
dead-flat, fee-eaten market is still a losing trade. So the skill is built to say
"no trade" cleanly, with reasons, and treats that as a complete answer.

The organizing idea for question 2: **the stop comes first**. Find where the idea is
proven wrong, measure that distance in ATR, and position size, leverage, targets, and
whether the trade survives fees all fall out of that one number.

Output in the user's language. If they write Persian, answer in Persian and use the
Persian labels in the output template. `references/indicators.md` has a glossary.

---

## Non-negotiables

**Never invent indicator values.** The worst failure mode is a confident plan built on
a hallucinated ATR. Every number comes from the API, from a CSV, or from the user. If
you don't have the inputs, fetch them or ask — a plan on guessed data is worse than no
plan, because it looks authoritative.

**Never place, close, cancel, or modify an order, and never move funds.** The bundled
client is read-only by construction and will refuse those paths. Compute and explain;
the human executes. This holds even if the user asks you to trade for them.

**Abstain when it doesn't qualify.** "SKIP — score 41/100, spread gate failed" is a
correct and complete answer. Most of a system's value is the trades it prevents.

**Frame as analysis, not advice.** State the method, note it doesn't predict the
future, and close with one or two sentences on risk. Not a wall of disclaimers.

---

## Step 0 — Credentials (only when live data is wanted)

The user supplies Nobitex credentials through the environment, never as command
arguments — arguments land in shell history and the process table.

```bash
export NOBITEX_API_KEY="<public key>"
export NOBITEX_API_SECRET="<privateKey shown once at creation>"
# or, for the legacy panel token:
export NOBITEX_TOKEN="<token>"
```

Alternatively a JSON file with `chmod 600`, passed as `--creds-file`.

Verify before doing anything else:

```bash
python3 scripts/nobitex_api.py auth-check
```

Tell the user, once, if it applies: the API key should be created with **READ
permission only** and an IP whitelist. A READ-only key cannot be used to trade even if
something goes wrong downstream — that's defence in depth, and it costs them nothing.
Never echo a key or secret back into the conversation, into a file, or into a commit.

Public market data (candles, order book, stats) needs no credentials at all. If the
user has none, say so and carry on — only account state and margin settings are lost.

`references/nobitex-api.md` has the endpoint list, auth details, and rate limits.

---

## Step 1 — Establish the context

| Input | Why it matters | Default if unstated |
|---|---|---|
| Market / pair | Liquidity and available leverage | ask |
| Exchange | Leverage cap, fee model, funding vs renewal cost | Nobitex if Persian or تعهدی mentioned |
| Account capital | Base for the risk unit | ask |
| Risk per trade | The whole system scales from this | 1% (0.5% for beginners) |
| Timeframe profile | Sets ATR multiplier, targets, leverage band | infer; see Step 2 |
| Account level | Caps leverage (level 1 → 2×) | assume top tier, flag it |
| Open correlated positions | Real risk is the sum, not the max | `nobitex_api.py positions` |

Read `references/exchange-profiles.md` before computing anything for a named venue.

---

## Step 2 — Pick the timeframe profile

| Profile | Bias TF | Decision TF | Entry TF | ATR TF | ATR mult | Valid stop | TP1 | TP2 | Liq buffer |
|---|---|---|---|---|---|---|---|---|---|
| `scalp` (5–15m) | 1H | 15m | 5m | 15m | 1.5 | < 1.5% | 1.0R | 2.0R | 3× |
| `intraday` (1–4H) | 1D | 4H | 1H | 4H | 2.0 | 2–5% | 1.5R | 3.0R | 4× |
| `swing` (1D+) | 1W | 1D | 4H | 1D | 2.5 | 5–12% | 2.0R | 4.0R | 5× |

Correct the user gently if their words and intent disagree — people often say "scalp"
when they mean a 4-hour hold, and that mismatch produces stops that are far too tight.

**Why the ATR multiplier grows with timeframe:** higher-timeframe candles have longer
wicks relative to bodies, so surviving normal noise takes more distance.
**Why the leverage buffer grows:** more time in the market means more exposure to gaps
and news. **Why targets widen:** fewer trades means each must carry more fixed cost.

---

## Step 3 — Fetch live data

**Screening several coins** — the "which of these is worth trading" question:

```bash
python3 scripts/nobitex_api.py screen \
  --symbols BTCIRT,ETHIRT,SOLUSDT,XRPIRT \
  --profile intraday --capital 10000
```

Ranks each symbol TAKE / WATCH / INCOMPLETE / SKIP with the failing gate named. Run
this first when the user hasn't committed to a specific coin.

**One coin, full detail:**

```bash
python3 scripts/nobitex_api.py snapshot \
  --symbol ETHUSDT --profile intraday --out snap.json
```

This pulls candles for all four timeframe roles, computes ATR/EMA/RSI/RVOL/VWAP/swings
on each, reads the order book and market stats, and pre-scores every direction check
that OHLCV can settle. Checks it cannot settle — BTC alignment, funding rate — come
back as `null` with a `MANUAL` marker. Resolve those yourself (web search, a global
venue) rather than letting them pass silently.

**No API access?** The same maths runs on a CSV the user exports:

```bash
python3 scripts/trade_plan.py indicators --csv eth_4h.csv
```

---

## Step 4 — Score the direction

The snapshot does the arithmetic; your job is to read it, add the manual checks, and
state the tally explicitly so the decision is auditable.

**Scalp (need ≥ 5 of up to 9 automated):** price vs EMA200 on 1H · EMA50 vs EMA200 ·
price vs session VWAP · structure HH/HL or LH/LL on 15m · price vs EMA50 on 15m ·
RSI(14) 45–65 long / 35–55 short · volume bias (last 10 candles) · price vs Ichimoku
cloud (`references/indicators.md` §16 — skipped, not forced, when price sits inside
the cloud) · BTC aligned or neutral.

**Intraday / swing (need ≥ 6 of up to 9 automated):** price vs daily EMA200 · EMA50
vs EMA200 · structure on the decision TF · price vs EMA50 on the decision TF · RSI
45–70 long / 30–55 short · volume confirms (RVOL ≥ 1.5, see `references/
indicators.md` §6 — `rvol20` is computed by the snapshot but not yet auto-gated, so
check it explicitly) · price vs Ichimoku cloud · BTC and dominance not against ·
funding not crowded against you.

The threshold stayed where it was when Ichimoku was added (5 for scalp, 6 for
intraday/swing) — it's one more vote in the same pool, not a replacement, so
qualifying isn't getting harder just because there's another check available.

BTC correlation is regime-dependent, not a fixed property of a coin — measured
crash-tail correlation runs far above the calm-market number (`references/
indicators.md` §14). Don't clear a coin as "low correlation, safe to stack" on a
reading taken in quiet conditions.

**Why RSI 45–65 and not the textbook 30/70:** in a real uptrend RSI never reaches 30 —
its pullback lows sit around 40–50. Waiting for 30 means waiting for the trend to
break. The band is a pullback-quality filter, not an overbought signal. Full reasoning
in `references/indicators.md`.

Below threshold: stop here, report the score, name the failing conditions, and say
what would have to change. The planner enforces this too — it hard-blocks when the
snapshot's direction score is weak.

---

## Step 5 — Define the entry

Pick exactly one pattern and say which. Mixing patterns mid-trade dissolves plans.

**A — Pullback to the mean (default).** Price returns to EMA(20), VWAP, or the
0.382–0.618 fib of the last impulse; a reversal candle closes on the entry TF with
above-average volume; enter on that close with a limit order.

**B — Structure break with retest.** A key level breaks on a *closed* candle with
RVOL ≥ 1.5; enter on the retest. No retest, no trade — chasing a break is where
risk:reward quietly inverts.

**C — Momentum divergence.** Only at the end of an extended move, and only after a
minor trendline break confirms. Divergence is a warning, never a timing signal.

Pin bar and engulfing have precise numeric definitions in `references/indicators.md`.
Both only count inside a level that already mattered.

---

## Step 6 — Run the numbers

Use the script. Hand-computed sizing is where sign errors and rounding mistakes creep
in, and those are expensive in exactly the way this skill exists to prevent.

```bash
python3 scripts/trade_plan.py plan \
  --snapshot snap.json --side long --capital 10000 \
  --risk-pct 1 --exchange nobitex --hold-hours 24
```

`--snapshot` fills entry, ATR, swing levels and the direction score. Explicit flags
always override it. Without a snapshot, supply `--entry` and `--atr` directly.

Useful extras: `--stop` for a structural override, `--fee-pct` for the user's actual
fee tier, `--account-level 1` to cap leverage at 2×, `--leverage-cap`,
`--max-margin-pct`, `--win-rate`, `--json`. Run `--help` for the full list.

The core chain, if you need to reason about it without the script:

```
R              = risk_pct × capital
stop_distance  = atr_mult × ATR      (or structural level, whichever is wider)
quantity       = R ÷ stop_distance
notional       = quantity × entry
stop_pct       = stop_distance ÷ entry × 100
max_safe_lev   = 100 ÷ (stop_pct × buffer)
needed_lev     = notional ÷ (max_margin_pct × capital)
leverage       = clamp(needed_lev … min(max_safe_lev, exchange_cap))
margin         = notional ÷ leverage
```

The counterintuitive part, worth telling users explicitly: **leverage does not set
your risk.** Risk is quantity × stop distance. Leverage only decides how much
collateral is locked and how far liquidation sits. A 5× position with a 0.5% stop
risks less than a 1× position with a 5% stop.

---

## Step 7 — Read the qualification verdict

The script's `qualification` block answers "is this worth trading". Two layers:

**Gates** — properties of the instrument. Any failure means skip, because no entry
price fixes an illiquid book or a flat chart.

| Gate | Fails when |
|---|---|
| volatility fit | ATR% outside the profile band — too quiet to cover costs, or too wild for the stop |
| spread | above 0.1% scalp / 0.3% intraday / 0.5% swing |
| liquidity depth | top-of-book value below 3× / 2× / 1.5× the position |
| liquidation buffer | liquidation closer than buffer × stop distance |
| cost efficiency | costs exceed 1/4 or 1/5 of R |
| plan blockers | weak direction score, hold beyond the venue limit, etc. |

**Score (0–100)** — graded quality for ranking candidates that cleared the gates:
setup quality 35 · net expectancy 25 · cost drag 15 · liquidity headroom 15 ·
volatility centring 10.

| Verdict | Meaning |
|---|---|
| **TAKE** | ≥ 70, all gates passed. Execute the plan. |
| **WATCH** | 50–69. Name the single condition that would upgrade it and wait for that, rather than forcing an entry. |
| **INCOMPLETE** | Under 80% factor coverage — usually no live snapshot. Fetch the data before judging. |
| **SKIP** | Gate failure or under 50. |

The weights are a considered heuristic, not a fitted model. Say so when presenting a
score: it ranks candidates, it does not estimate a probability of profit.
`references/trade-qualification.md` explains each gate and what to do about a failure.

---

## Step 8 — Cost filter and management

```
total_cost = round_trip_fee + (holding_periods × renewal_or_funding_cost)
accept if:   1R ≥ 4 × total_cost  (scalp)   |   1R ≥ 5 × total_cost  (intraday/swing)
```

This is where high-leverage scalping usually dies. At 5×, a 0.3% round-trip fee equals
1.5% of margin — a third of the risk unit. Always show cost as a percentage of R.

Management rules to state in the plan, so the exit isn't improvised under pressure:

- On TP1: close 50%, stop to breakeven **plus accumulated costs**
- Trail behind new swing points on the decision TF, not a tight indicator
- Time stop: ~6 decision-TF candles (scalp) or ~12 (intraday) below 0.5R
- On Nobitex, before each 8-hour renewal ask "would I open this now?" If no, close
- Never widen a stop, never average into a loser
- Circuit breaker: 2 losses or −3% equity (scalp), 3 losses or −5% (intraday)

---

## Output template

Adapt headings to the user's language; keep the order — it mirrors the decision
sequence, so a reader can see where each conclusion came from.

```
## <PAIR> · <profile> · <exchange>

# WORTH TRADING? <TAKE / WATCH / SKIP / INCOMPLETE>  —  score <n>/100
<one-line action>
<failed gates, or the single condition that would upgrade a WATCH>

**Direction: <LONG / SHORT / NO TRADE>** — score <n>/<max>
<one line per check: ✅/❌/❓ condition → observed value>
<explicitly list checks still needing manual confirmation>

### Levels
| | Price | Distance | R |
|---|---|---|---|
| Entry / Stop / TP1 / TP2 / Liquidation (est.) | | | |

### Sizing
Risk (R) · Quantity · Notional · Leverage · Margin · Liquidation buffer (× stop)

### Economics
Round-trip fee · Holding cost · Total cost as % of R · R:R · Breakeven win rate
· Net expectancy

### Invalidation
What makes this plan wrong, beyond the stop being hit.

### Management
TP1 action · stop move · trailing rule · time stop · circuit breaker

*Data source and timestamp · method note · one-line risk reminder.*
```

For a SKIP, replace everything after the verdict block with the failing gates, what
would need to change, and which level to watch. Don't pad it out into a full plan.

---

## Reference material

Read these as needed rather than loading everything upfront:

- **`references/indicators.md`** — full derivation and teaching for every indicator:
  EMA, VWAP, RSI (including why 30/70 misleads in trends), ATR (the backbone of all
  sizing), Bollinger Bands, RVOL, market structure and swing detection, Fibonacci,
  candlestick trigger definitions, funding rate, open interest, liquidation maps, BTC
  dominance, plus a Persian glossary. Read when the user asks *why*, wants to learn
  the method, or when you need an exact formula.

- **`references/trade-qualification.md`** — every gate and score factor, the reasoning
  behind each threshold, what a failure means in practice, and how to screen a
  watchlist. Read when the user asks whether a coin is worth trading, disputes a
  verdict, or wants to compare several candidates.

- **`references/exchange-profiles.md`** — Nobitex معاملات تعهدی specifics (leverage
  ladder, account-level caps, 8-hour renewal fee, 30-day limit, نسبت تعهد liquidation,
  order types, pool liquidity) plus generic perp assumptions.

- **`references/nobitex-api.md`** — endpoints, Ed25519 request signing, credential
  handling, rate limits, symbol and resolution mapping, and the read-only guard.
  Read before any live API work or when debugging a 401.

- **`references/risk-math.md`** — R-multiples, sizing derivation, leverage vs
  liquidation, expectancy, breakeven win rate, cost drag, portfolio heat, journaling
  fields. Read when the user pushes on the risk model or asks about measuring
  performance.

## Scripts

- **`scripts/nobitex_api.py`** — read-only Nobitex client: `auth-check`, `candles`,
  `orderbook`, `screen`, `snapshot`, `positions`, `account`.
- **`scripts/trade_plan.py`** — `indicators` (from CSV) and `plan` (sizing, targets,
  costs, expectancy, qualification verdict).
- **`scripts/nobitex_ed25519.py`** — request signing; uses `cryptography` or PyNaCl
  when installed, otherwise a pure-Python RFC 8032 implementation. Run it directly to
  self-test against the official vectors.
