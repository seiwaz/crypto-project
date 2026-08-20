# Indicators and Methods — full reference

Every tool used by the skill, with its formula, the reasoning behind its parameters,
how to read it, and the mistake people usually make with it. Use this when the user
asks *why*, when you need an exact formula, or when teaching the method.

## Contents

1. [EMA](#1-ema--exponential-moving-average) — trend filter
2. [VWAP](#2-vwap--volume-weighted-average-price) — intraday value anchor
3. [RSI](#3-rsi14--relative-strength-index) — momentum and pullback quality
4. [ATR](#4-atr14--average-true-range) — **volatility; the backbone of all sizing**
5. [Bollinger Bands](#5-bollinger-bands) — volatility compression
6. [RVOL](#6-rvol--relative-volume) — participation
7. [Market structure](#7-market-structure--hhhl-and-lhll) — the non-indicator core
8. [Fibonacci](#8-fibonacci) — entry zones and extension targets
9. [Candlestick triggers](#9-candlestick-triggers--precise-definitions) — exact rules
10. [Support and resistance](#10-support-and-resistance)
11. [Funding rate](#11-funding-rate) — derivatives crowd positioning
12. [Open interest](#12-open-interest-oi) — quality of a move
13. [Liquidation maps](#13-liquidation-maps)
14. [BTC correlation and dominance](#14-btc-correlation-and-dominance)
15. [Trend quality: Choppiness Index / Efficiency Ratio](#15-trend-quality-not-just-direction-choppiness-index--efficiency-ratio) — advisory, not yet automated
16. [Ichimoku Cloud](#16-ichimoku-cloud-ichimoku-kinko-hyo) — live in the score
17. [Persian glossary](#persian-glossary)

---

## 1. EMA — Exponential Moving Average

Averages price while weighting recent candles more heavily, so it turns faster than a
simple moving average.

```
k = 2 ÷ (N + 1)
EMA(today) = Close(today) × k + EMA(yesterday) × (1 − k)
Seed: first EMA = simple average of the first N closes
```

For EMA(20), `k = 2/21 ≈ 0.0952` — each new candle carries about 9.5% of the weight.

**Period choices:**

| Period | Meaning | Role |
|---|---|---|
| 20 | Short-term mean | Pullback zone in a healthy trend; trailing reference |
| 50 | Medium-term mean | Deeper dynamic support; direction filter on the decision TF |
| 200 | Global long-term reference | The bull/bear dividing line — long only above, short only below |

These numbers aren't mathematically special. They matter because more traders watch
them than any others, which makes them partly self-fulfilling. That's a legitimate
reason to use them and a bad reason to believe they're precise.

**Reading it:** slope gives trend direction (flat = range, don't trade a trend
system); price position relative to EMA(200) gives bias; `20 > 50 > 200` is "perfect
order", the strongest trend configuration; crossovers mark regime change.

**Common mistake:** using an EMA crossover as an entry trigger. Crossovers are
structurally late — by construction they confirm what already happened. Use them as a
direction filter and take entries from structure and price action.

---

## 2. VWAP — Volume Weighted Average Price

The average price at which money actually changed hands. If 1000 units traded at 3000
and 10 units at 3100, VWAP stays near 3000 — because that's where the real volume was.

```
Typical price (TP) = (High + Low + Close) ÷ 3
VWAP = Σ(TP × Volume) ÷ Σ(Volume)     ← cumulative from session start
```

**VWAP resets every session** — in crypto, typically 00:00 UTC. This is why it only
means anything on low timeframes (1m–30m) and why the intraday and swing profiles
drop it. On a daily chart, plain VWAP is meaningless; use Anchored VWAP from a
specific event instead.

**Reading it:** price above VWAP means today's buyers are in profit (bullish bias);
below means sellers control. A return *to* VWAP inside a trend is the highest-quality
scalp entry, because price has come back to the session's fair value. Institutions
use VWAP as an execution benchmark, which is part of why it attracts price.

---

## 3. RSI(14) — Relative Strength Index

Converts the ratio of average gains to average losses over 14 candles into a 0–100
value. It measures *speed*, not direction.

```
change = Close(t) − Close(t−1)
gain = max(change, 0)      loss = max(−change, 0)

First 14 candles:
  AvgGain = simple mean of 14 gains
  AvgLoss = simple mean of 14 losses

Subsequent (Wilder smoothing):
  AvgGain = (prev AvgGain × 13 + current gain) ÷ 14
  AvgLoss = (prev AvgLoss × 13 + current loss) ÷ 14

RS  = AvgGain ÷ AvgLoss
RSI = 100 − (100 ÷ (1 + RS))
```

### Why the 45–65 band instead of 30/70

This is the most important thing to understand about RSI, and the thing most retail
material gets wrong. "Buy below 30, sell above 70" is actively harmful in a trending
market:

- In a strong uptrend RSI never reaches 30; its pullback lows sit around 40–50
- RSI can hold above 70 for weeks while price doubles
- Waiting for RSI 30 in an uptrend means waiting for the trend to break — you'd be
  buying precisely when the thesis stopped being true

So the skill uses RSI as a **pullback-quality filter**, not an exhaustion signal:

| RSI in an uptrend | Interpretation |
|---|---|
| 70+ | Extended — too late to enter, wait for a pullback |
| 55–70 | Healthy trend in motion |
| **45–65** | **Ideal pullback entry zone** |
| 40–45 | Early weakness — be selective |
| < 40 | Trend likely breaking — don't take the long |

### Divergence

The one case where RSI generates an independent signal:

```
Bearish divergence:  price higher high  +  RSI lower high   → weakening advance
Bullish divergence:  price lower low    +  RSI higher low   → weakening decline
```

Rules that keep divergence from becoming noise: only on clearly defined swing
extremes, never on every micro-wiggle; treat it as a *warning*, not a timing signal;
require a real trigger (minor trendline break on a lower TF) before entering; and
weight higher-timeframe divergence far more — most 5-minute divergences are noise.

---

## 4. ATR(14) — Average True Range

**The most important indicator in this system**, because stop distance, position size,
and leverage are all derived from it.

Measures the average size of price movement per candle, ignoring direction. In other
words: how much noise is normal in *this* market right now.

```
True Range (TR) = max of:
    (1) High − Low                    ← the candle's own range
    (2) |High − previous Close|       ← gap up
    (3) |Low  − previous Close|       ← gap down

First ATR = simple mean of 14 TR values
ATR(t)    = (previous ATR × 13 + TR(t)) ÷ 14      ← Wilder smoothing
```

The three cases exist because on a gap, `High − Low` understates the real movement.

### Why ATR-based stops instead of fixed percentages

This is the difference between a system and a guess:

> A 1% stop may be very wide on Bitcoin and get hit in five minutes on a volatile
> altcoin. `1.5 × ATR` **auto-calibrates to the instrument's current volatility** —
> tighter in quiet markets, wider in turbulent ones, without you changing anything.

### Choosing the multiplier

- Below 1.0 → the stop sits inside ordinary noise and gets hit at random
- 1.5 → just outside a typical candle's range; right for 5–15m
- 2.0 → higher timeframes have longer wicks relative to bodies; right for 1–4H
- 2.5 → daily candles, swing holds
- Above 3.0 → the stop is so far out that position size becomes trivially small and
  the trade stops being worth the fees

### Secondary use

```
volatility % = (ATR ÷ price) × 100
```

If this exceeds ~5% on the 4H, the market is too turbulent for the standard profile —
halve the size or stand aside.

**Common mistake:** comparing raw ATR across instruments. Bitcoin's ATR might be 800
and an altcoin's 0.02. Only the percentage form is comparable.

---

## 5. Bollinger Bands

```
Middle band = SMA(20)
σ = standard deviation of the last 20 closes
Upper = SMA(20) + 2σ
Lower = SMA(20) − 2σ
Bandwidth = (Upper − Lower) ÷ Middle
```

Two legitimate uses in this system:

1. **Squeeze** — when bandwidth hits its lowest value in ~50 candles, volatility is
   coiling and expansion is likely. It gives you *timing*, never direction.
2. **Target ceiling** — don't place a TP beyond the opposite band; price stays inside
   the bands roughly 95% of the time.

**Common mistake:** "price touched the upper band, so sell." In a strong trend price
*walks* the upper band, which signals strength rather than exhaustion.

---

## 6. RVOL — Relative Volume

```
RVOL = current candle volume ÷ average volume of the last 20 candles
```

| RVOL | Meaning | Action |
|---|---|---|
| < 0.7 | Dead market | Don't trade |
| 1.0–1.5 | Normal | Sufficient to confirm a pullback candle |
| **≥ 1.5** | Real money entering | **Required for a breakout trade** |
| > 3.0 | Spike — news or liquidation cascade | Careful; often mean-reverts |

**Why volume is mandatory for breakouts:** a break on thin volume means a few small
orders touched the level. That's a fakeout, and it usually reverses straight into your
stop. High volume means real liquidity consumed the level.

**Price/volume combinations:**

| Price | Volume | Reading |
|---|---|---|
| Up | Up | Healthy trend ✅ |
| Up | Down | Weakness — reversal risk ⚠️ |
| Down | Up | Genuine selling ✅ (valid for shorts) |
| Down | Down | Just absent buyers, not active selling |

---

## 7. Market structure — HH/HL and LH/LL

The most fundamental method here. No indicator substitutes for it.

**Algorithmic swing definition (fractal):**

```
Swing high: a candle whose High exceeds the High of the 2 candles on each side
Swing low:  a candle whose Low  is below the Low  of the 2 candles on each side
```

A swing isn't confirmed until 2 candles later. That lag is inherent — accept it rather
than trying to front-run it.

```
Uptrend:   HH (higher highs) + HL (higher lows)
Downtrend: LH (lower highs)  + LL (lower lows)
Range:     highs and lows roughly level
```

**Break of structure:** in an uptrend, when price closes below the most recent higher
low, the uptrend is no longer valid. This is exactly why stops go *behind the last
swing* — that's where the thesis is falsified, not merely where the loss becomes
uncomfortable.

> Core principle: a stop loss marks where your scenario is proven wrong, not where
> your patience runs out.

---

## 8. Fibonacci

**Retracement** — for locating pullback entries. Draw from swing low to swing high in
an uptrend.

| Level | Interpretation |
|---|---|
| 0.236 | Very shallow — very strong trend |
| **0.382** | **Entry zone in a strong trend** |
| 0.5 | Not a Fibonacci ratio mathematically, but market psychology respects it |
| **0.618** | **The "golden ratio" — most common reversal zone** |
| 0.786 | Last defense; a break here usually ends the trend |

**Extension** — for TP2.

| Level | Use |
|---|---|
| 1.272 | Conservative target |
| **1.618** | **Standard TP2** |
| 2.618 | Only in explosive moves |

Fibonacci's real power is **confluence**: a 0.618 level that coincides with EMA(50)
and a prior horizontal support is far more reliable than any of the three alone. The
scoring system in the skill formalizes this idea.

**Common mistake:** drawing fibs on every small wiggle. Only use the clear dominant
impulse leg.

---

## 9. Candlestick triggers — precise definitions

"Reversal candle" needs numbers, or it becomes whatever you want it to be.

**Bullish pin bar:**

```
range = High − Low
body  = |Close − Open|

body           ≤ 33% of range
lower wick     ≥ 66% of range
upper wick     ≤ 15% of range
preferably Close > Open
```

Meaning: sellers pushed price down and buyers took the entire move back.

**Bullish engulfing:**

```
previous candle bearish (Close < Open)
current candle bullish  (Close > Open)
current Close > previous Open
current Open  ≤ previous Close
current volume > previous volume     ← most traders skip this and shouldn't
```

**Golden rule:** both patterns only count *inside a level that already mattered*
(support, EMA, fib zone). A pin bar in open space is noise.

---

## 10. Support and resistance

How to draw them properly:

1. Go to a higher timeframe than you trade (1H and 4H for scalping)
2. Find areas price has reacted to at least twice
3. **Draw zones, not lines** — from wicks to bodies
4. More touches and higher reaction volume = more reliable

**Role reversal:** broken resistance becomes support and vice versa. Entry pattern B
(retest entry) is built entirely on this.

---

## 11. Funding rate

Not a chart indicator — derivatives market data. Nobitex doesn't display it, but since
its prices track the global market, read it from a global exchange or an aggregator.

**Mechanism:** in perpetual futures a payment passes between longs and shorts every
~8 hours to keep the perp price tethered to spot.

```
Positive funding → longs pay shorts  → longs are the crowd
Negative funding → shorts pay longs  → shorts are the crowd
```

**Reading it (contrarian):**

| Funding | Crowd | Trading implication |
|---|---|---|
| Strongly positive (> 0.05%) | Longs crowded | ⚠️ Long-liquidation cascade risk — poor time to go long |
| Mildly positive | Normal | Neutral |
| Zero to negative | Shorts crowded | ✅ Favorable for longs (squeeze fuel) |

The logic: when everyone is on one side, there's nobody left to push that side
further — but everyone's stop is sitting there.

---

## 12. Open Interest (OI)

Total open contracts.

| Price | OI | Meaning | Quality |
|---|---|---|---|
| ⬆️ | ⬆️ | New money entering longs | **Strong trend** ✅ |
| ⬆️ | ⬇️ | Shorts covering | Weak advance ⚠️ |
| ⬇️ | ⬆️ | New money entering shorts | **Strong decline** ✅ |
| ⬇️ | ⬇️ | Longs closing | Weak decline, possible bottom ⚠️ |

---

## 13. Liquidation maps

Clusters of stops and liquidation prices. **Price is attracted to liquidity**, because
that's where forced orders sit. If a large cluster of long liquidations sits 2% below,
a move down to it before any genuine rally is likely. Don't place your own stop or
target exactly on a visible cluster.

---

## 14. BTC correlation and dominance

```
BTC dominance = BTC market cap ÷ total crypto market cap
```

| BTC | Dominance | Effect on alts |
|---|---|---|
| Up | Up | Alts lag |
| Up | Down | **Best case — alt season** ✅ |
| Down | Up | **Worst case — alts fall hard** ❌ |
| Down | Down | Rare and unstable |

Most alts correlate above 0.8 with Bitcoin, which means **altcoin analysis that
ignores BTC is close to worthless**.

**Correlation is not one number — it depends on the regime, and it's asymmetric.**
Measured tail dependence between BTC and alts runs roughly 0.85–0.88 in a crash
(lower tail) against only 0.23–0.25 in a rally (upper tail): alts move independently
on the way up and together on the way down. A correlation read during calm or trending
conditions will understate what happens exactly when it matters — a portfolio that
looks diversified across 5 "only 0.5-correlated" alts can still take one shared
drawdown when BTC breaks. Don't treat a single measured correlation as a stable
property of a coin; treat it as regime-conditional, and expect it to jump toward the
tail figure precisely when a fast BTC move starts, not before.

**"BTC alignment" means the coin's own trend first, BTC only as a fallback.** Found
2026-08-20 in the demo's live Toobit scoring: a "BTC / dominance alignment" check was
being resolved from BTC's own trend for every coin, regardless of that coin's actual
behavior — i.e. a short only counted as favoured when BTC itself was falling, even if
the coin was already in its own clear downtrend independent of BTC. This is backwards
for the same reason §14's crash-correlation note matters: professional screening looks
for coins *diverging* from BTC (relative strength/weakness) as the stronger signal, not
coins merely moving in lockstep with it. The fix, verified live: check the instrument's
**own** trend first (price vs its own EMA200 plus recent structure); fall back to BTC's
trend only when the coin has no clear trend of its own. Two consecutive full-watchlist
scans went from zero qualifying setups to seven, immediately, once this was corrected —
a single blanket-BTC check can silently suppress a large share of real opportunities.
Full writeup: this repo's `docs/RESEARCH_LOG.md`, Round 3.

---

## 15. Trend quality, not just direction (Choppiness Index / Efficiency Ratio)

Added in the 2026-08-19 research round. Not yet wired into the automated score —
this is glossary/advisory material for now; validate against a live sample before it
changes a gate. `price vs EMA200 + recent move%` (what this skill and the demo trader
both use) answers *which way* price has drifted, not whether that drift is a real
trend or noise inside a range — a small move over enough bars clears the threshold
either way.

**Choppiness Index** — sums true range over N bars against the period's total high-low
span, log-scaled to 0–100. High = congestion/range, low = trending. This is the
opposite polarity from ADX and reads range structure directly rather than inferring it
from directional-movement smoothing.

**Kaufman's Efficiency Ratio** — `net change ÷ sum of |close-to-close| moves` over a
window, bounded 0–1. Close to 1 means price took a straight path (clean trend); close
to 0 means it round-tripped (chop). Reacts faster than ADX because it's a single-window
calculation, not a multi-stage smoothed average.

**ADX** — measures trend *strength*, not direction (pair with +DI/-DI for that). Below
~20 is the conventional "dead zone": moving averages whipsaw and trendlines break
without follow-through. Backtests report that a strict ADX>20 filter cuts false trend
signals by roughly 30–40%, at the cost of fewer signals overall — and ADX itself lags
more than the other two because of its extra smoothing stages, so it confirms a trend
later than it started.

**Why this matters here specifically:** the regime check's move-threshold (0.5% over
12 bars) is loose enough that a genuinely choppy market can still print "up" or "down."
A Choppiness Index or Efficiency Ratio check alongside it would catch that case without
replacing it — direction and quality are different questions and both should pass.

---

## 16. Ichimoku Cloud (Ichimoku Kinko Hyo)

Added 2026-08-20, live in the automated score. Five lines built from rolling
high/low midpoints, no closes involved directly:

```
Tenkan-sen (conversion)  = (9-period high + 9-period low) / 2
Kijun-sen (base)         = (26-period high + 26-period low) / 2
Senkou Span A (leading)  = (Tenkan + Kijun) / 2,  plotted 26 bars forward
Senkou Span B (leading)  = (52-period high + 52-period low) / 2, plotted 26 bars forward
Chikou Span (lagging)    = current close, plotted 26 bars back
```

**The cloud (Kumo)** is the band between Span A and Span B. Price above it is
bullish, below it is bearish, inside it is Ichimoku's own definition of "no trade" —
the system here follows that convention exactly: the check only fires when price is
clearly outside the cloud, and is skipped (not forced to guess) when price is inside
it, the same pattern every other check here uses when its own data is inconclusive.
Cloud color matters too: Span A above Span B ("green") reinforces a bullish read;
Span B above Span A ("red") reinforces bearish — both are recorded alongside the
check's observed value.

**The one thing implementations get wrong:** Span A/B are *plotted 26 bars ahead* of
the data used to compute them, which means the cloud boundary sitting under *today's*
candle was actually calculated from the market as it stood 26 bars ago, not today's
high/low. Computing "today's cloud" from today's rolling window instead of the
26-bars-back one silently produces a cloud that lags by half a cycle and will
misread the current regime. Needs roughly 78 bars of history (26 back, plus 52 more
to compute Span B at that point) before it resolves at all — on a fast timeframe like
scalp's 15m decision TF that's under 20 hours of data, comfortably inside the 300
candles this system already fetches.

**Where it sits relative to the EMA-trend checks already here:** Tenkan/Kijun are
themselves rolling-extreme averages, so they're directionally correlated with the
EMA50/EMA200 checks rather than fully independent evidence (see §15's confluence-
quality note — genuinely different methods matter more than more of the same kind).
The cloud position specifically is the most distinct signal Ichimoku offers here,
which is why that's what got wired in rather than a Tenkan/Kijun cross.

---

## Tool → question mapping

| Tool | Question it answers |
|---|---|
| EMA 200 (higher TF) | Am I allowed to go long, or short? |
| EMA 20/50 + VWAP | Where do I enter? |
| Market structure | Is my scenario still valid? |
| RSI | Is momentum with me, or is the move tired? |
| RVOL | Is real money behind this? |
| **ATR** | **Where is the stop and how big is the position?** |
| Fibonacci | Where is the target? |
| Funding / OI | Where is the crowd and who is trapped? |
| BTC / dominance | Is the whole market with me? |
| Sizing and R:R math | Is this trade worth the risk at all? |

Priority when learning: risk math > market structure > ATR > everything else.

None of these predict the future. They shift probability slightly in your favor.
Profit comes from disciplined repetition of a small edge across many trades, not from
the accuracy of any single analysis.

---

## Persian glossary

| English | فارسی |
|---|---|
| Long / Short position | موقعیت خرید / فروش (لانگ / شورت) |
| Entry price | نقطه ورود |
| Stop loss | حد ضرر |
| Take profit | حد سود |
| Leverage | اهرم / ضریب / لوریج |
| Margin / collateral | وجه تضمین / وثیقه |
| Notional value | ارزش اسمی |
| Liquidation | لیکوئید شدن |
| Position size | حجم پوزیشن |
| Risk-reward ratio | نسبت ریوارد به ریسک |
| Win rate | وین‌ریت / نرخ برد |
| Expectancy | امید ریاضی |
| Fee | کارمزد |
| Funding rate | نرخ تأمین مالی / فاندینگ |
| Open interest | اوپن اینترست |
| Market structure | ساختار بازار |
| Support / resistance | حمایت / مقاومت |
| Divergence | واگرایی |
| Breakout / retest | شکست / ریتست |
| Timeframe | تایم‌فریم |
| Trading journal | ژورنال معاملاتی |
