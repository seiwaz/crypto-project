# Tabdeal اهرم حرفه‌ای — live-trading readiness dossier

**Status as of 2026-08-22: NOT READY.** Everything needed to *build* real execution is
now known and collected here. What is missing is not knowledge — it is a working
execution layer, two unsolved mechanic gaps, a funded wallet, and evidence the
strategy is profitable on this venue at these fees.

Nothing in this document has been executed. The codebase remains structurally
read-only (`agent/guard.py` refuses every non-GET verb on Tabdeal and the server
refuses to boot if that guard stops working). Going live is a deliberate act that
must dismantle that guard on purpose.

Read alongside `CLAUDE.md` → "Tabdeal — LIVE as the demo's sole venue" for how the
paper side already works.

---

## 1. The execution API, verified

Source: the official Postman collection
(`github.com/Tabdeal-Exchange/tabdeal-api-postman`, `master`), cross-checked against
live authenticated GET probes. Base `https://api1.tabdeal.org`.

**Auth (identical to the read path already implemented):** `X-MBX-APIKEY` header plus
`timestamp` + HMAC-SHA256 `signature` over the urlencoded parameters. `recvWindow`
optional. Write endpoints take **form-encoded** bodies (`multipart/form-data` in the
collection; the official SDK posts `application/x-www-form-urlencoded` and signs the
same urlencoded string). Reads live under `/r/`, writes do not.

### Writes — none of these are reachable today

| Purpose | Method + path | Parameters |
|---|---|---|
| Place order | `POST /fapi/v1/order` | `symbol`, `side` BUY/SELL, `type` **LIMIT or MARKET only**, `quantity`, `price` (LIMIT only), `timeInForce` GTC/IOC/FOK (default GTC), `reduceOnly` *(present but documented unsupported)*, `newClientOrderId`, `timestamp`, `signature`, `recvWindow` |
| Cancel order | `DELETE /fapi/v1/order` | `symbol`, `orderId`, `timestamp`, `signature` |
| **Close position** | `DELETE /fapi/v1/position` | `symbol`. Market-closes **the entire position**. Returns `{"msg":"success"}` |
| **Set SL/TP** | `POST /fapi/v1/positionSlTp` | **`positionId` (required)**, `symbol` (optional), `slPrice`, `tpPrice` (at least one), `workingType` MARK_PRICE\|CONTRACT_PRICE |
| Set leverage | `POST /fapi/v1/leverage` | `symbol`, `leverage` (1–100) |
| Fund the wallet | `POST /fapi/v1/transfer` | `type` **2 = spot→futures, 1 = futures→spot**, `amount`, `asset` |

### Reads — already implemented and guarded

`GET /r/fapi/v3/positionRisk` returns Binance-shaped fields: `positionAmt`,
`entryPrice`, `markPrice`, `unRealizedProfit`, `liquidationPrice`, `leverage`.
Also available: `/r/fapi/v3/account`, `/r/fapi/v3/balance`, `/r/fapi/v1/openOrders`,
`/r/fapi/v1/allOrders`, `/r/fapi/v1/userTrades`, `/r/fapi/v1/income`,
`/r/fapi/v1/position` (history), and **`/r/fapi/v1/forceOrders`** — liquidation
records, which is the endpoint that tells you the account got wiped.

### Venue mechanics

- **CROSS margin.** The whole wallet backs every position. Tabdeal states plainly
  that one loser can liquidate the entire account, closing profitable positions
  with it.
- **Maintenance margin: flat 0.5%** of position value, every symbol, no tier ladder.
- **Leverage 1–100x**, per symbol.
- **Fees: 0.1% maker AND taker**, no maker discount. Flagged on the fee page as a
  temporary promotional rate — treat as a floor that can rise.
- **No funding rate** is published for this product.
- Quantity is in **coins**; no contract multiplier. Symbols are underscore form
  (`BTC_USDT`); `BTCUSDT` is rejected.

---

## 2. Two mechanic gaps with no clean native solution

These are not missing code. They are missing *primitives*, and the strategy as
designed depends on them.

### 2a. The TP1 partial cannot be done safely

The strategy closes **50% at TP1** and locks the runner's stop at the TP1 price
(`_reduce_at_tp1`, Round 10 — the "risk-free" mechanic). On Tabdeal:

- `positionSlTp` sets **one** SL and **one** TP for the whole position. It cannot
  express "take half here, trail the rest".
- `DELETE /fapi/v1/position` closes **all** of it, not half.
- `reduceOnly` is documented as **not currently supported**, so the only way to shed
  half is an opposing MARKET order for half the quantity — and without `reduceOnly`
  there is no guarantee it reduces rather than flips. If the position size has
  changed underneath (partial fill, partial liquidation), an oversized opposing
  order **opens a reverse position** instead of trimming.

**Options, none free:** (a) drop the TP1 partial on this venue and run single-target
trades; (b) implement the partial via opposing orders with a re-read of
`positionRisk` immediately before sizing, accepting a race window; (c) ask Tabdeal
whether `reduceOnly` is scheduled. **Decision required before building.**

### 2b. Cross-margin liquidation is not modelled

`paper.liquidation_price()` solves the **isolated** formula — the price at which one
position exhausts its own margin share. Under cross margin the real trigger is
portfolio-wide: total equity below the *summed* maintenance requirement of all open
positions. With 10–20 concurrent positions sharing one pool, **the reported
liquidation distance is optimistic**, and the 6% portfolio-heat cap was designed for
isolated margin where each position's loss is bounded by its own margin.

This is documented in the code but not fixed. It must be modelled before real money,
because it is the mechanism that turns a bad day into a zero.

---

## 3. What must be built

Nothing below exists yet.

1. **`agent/tabdeal_broker.py`** — the signed write client. Order placement, cancel,
   close, SL/TP, leverage, transfer. Must be a *separate module* from `tabdeal.py`
   so the read path cannot accidentally acquire write capability.
2. **Open the guard, narrowly.** `guard.py` currently refuses all non-GET on Tabdeal
   and `self_test()` blocks server startup if that lapses. Real trading needs an
   explicit, enumerated write allowlist — not a removed guard. Keep the tripwire;
   change what it permits.
3. **Order-state reconciliation.** The paper broker assumes fills are instant and
   exact. Real orders partially fill, get rejected, sit unfilled, and fill at a
   different price than requested. Every position must be reconciled against
   `positionRisk` each cycle, with the exchange as the source of truth — never the
   local DB.
4. **A kill switch.** One command that cancels all open orders and flattens every
   position, callable without the scheduler loop being healthy.
5. **Exchange-side stops as the safety net.** Set `positionSlTp` immediately on fill,
   so a stop exists even if the monitoring loop dies. This is the single most
   important control, and it is available — use it.
6. **Cross-margin risk model** (see 2b).

---

## 4. Pre-flight checklist

| | Item | State |
|---|---|---|
| ☐ | Futures wallet funded | **Empty.** `balance: []`, no position history. Spot holds SHIB + IRT, **no USDT** — needs USDT bought/deposited, then `transfer type=2` |
| ☐ | API key scoped correctly | **Currently full-permission**, `canTrade: true` and futures reports `canWithdraw: true`. Rotate to the narrowest set that works, IP-whitelisted to 94.74.166.123 |
| ☐ | Key rotated after chat exposure | The key was pasted into a chat transcript on 2026-08-21. Rotate before funding |
| ☐ | Execution layer built + tested | Not started |
| ☐ | TP1-partial decision made | Open (§2a) |
| ☐ | Cross-margin model | Open (§2b) |
| ☐ | Kill switch | Not built |
| ☐ | Rate limits known | **Undocumented.** 50/50 read requests succeeded in a burst test, but write limits are unknown |
| ☐ | Minimum order size known | **Undocumented.** `exchangeInfo` gives only price/quantity precision — no `minNotional`. An order the simulator accepts may be rejected live |
| ☐ | Phase 4 gate met | **Not met** — see §5 |

---

## 5. The gate that actually matters

`CLAUDE.md` sets Phase 4 at **≥100 demo trades with stable positive expectancy across
≥3 consecutive evaluation periods**, plus explicit approval.

The Toobit record (919 trades, +0.105R) **does not transfer**: different venue, 2.5×
the fees, 33 coins instead of 74. Repriced at Tabdeal's fees it falls to **+0.045R
with a 50% win rate**, and two of five blocks go negative.

The Tabdeal sample is currently **6 closed trades** — noise.

And the decisive number is not trade count but edge. Measured across ~10 days of
Tabdeal 15m candles, every bar as a hypothetical entry, ±1R barriers: **win and loss
are equal to within 0.3pp at every stop multiplier.** At this horizon the market is a
symmetric random walk, so the barrier geometry contributes nothing and:

```
E_R = (d − 0.2%) ÷ stop_pct
```

where `d` is the direction filter's edge in % per 30-minute hold. **The strategy is
profitable if and only if `d` exceeds 0.2%.** No parameter tuning changes that sign —
`atr_mult` only scales magnitude.

**Recommended bar before real money:** ≥100 closed Tabdeal trades showing positive
expectancy *net of the 0.1%/0.1% fees* across three consecutive blocks. At the
current rate that is days, not hours.

---

## 6. Suggested first-money protocol

When the gate is met and the build is done — smallest viable, reversible steps:

1. Fund with an amount whose total loss is acceptable. Cross margin means the whole
   wallet is at risk from one position.
2. Run **one** symbol, **one** slot, minimum size, exchange-side SL set on every
   fill.
3. Reconcile every fill by hand against `userTrades` for the first day.
4. Only widen slots after the paper and live records agree on fills, fees and exits.

Never skip step 3. The paper broker has been correct so far — verified to the cent —
but it has never met a partial fill, a reject, or a real order book.
