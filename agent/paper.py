"""Local paper broker that simulates USDT-M perpetuals on Toobit or Tabdeal.

Toobit's own demo trading is web-only — their documentation says "users can only
access the Demo Trading via Webpage", the production API lists no `TBV_` symbols, and
no demo API host resolves in DNS. So the demo cannot be delegated to them; it has to
run here. Everything below is simulation against the venue's *real* public prices.

Nothing in this module can reach an exchange with intent. It reads market data through
the venue clients, every path of which is behind a read-only allowlist in `guard.py`,
and writes only to the local database. That matters more on Tabdeal than anywhere
else: the credentials held for that venue carry live trade permission on a funded
account, so "the simulator cannot place an order" has to be a structural property,
not an intention.

`_venue()` picks the client from `settings.exchange`, and the two differ in ways that
change the numbers, not just the plumbing:

| | Toobit | Tabdeal اهرم حرفه‌ای |
|---|---|---|
| Quantity | contracts (`contractMultiplier`, `1000SHIB`) | coins, always 1:1 |
| Maintenance margin | 9-tier ladder, 0.25%–2.5% by notional | flat 0.5%, every symbol |
| Margin mode | isolated | **cross** — see `liquidation_price` |
| Fees | 0.02% maker / 0.06% taker | 0.1% **both**, no maker discount |
| Funding | live per contract, 8h | not published for this product |
| Mark | index `edp`, falling back to last | order-book mid |

The contract mechanics are read from the venue rather than assumed:

* `contractMultiplier` and any numeric symbol prefix decide how many coins a contract
  is — FIL is 0.1 coins per contract, so a position quoted in coins is wrong by 10×.
* `riskLimits` is a 9-tier ladder. Each tier sets `maxLeverage` and `maintMargin`, and
  which tier applies depends on the position's notional. Using tier 0 for every size
  understates the maintenance requirement on a large position and puts liquidation
  further away than it really is.
* Fees are Toobit's published VIP-0 futures rates.
* Funding is read live, including its period and the venue's own cap and floor.

This module deliberately contains no *trading* maths — no ATR, no stop placement, no
position sizing. Those belong to the skill's planner. What lives here is exchange
mechanics: what a venue does to a position once it exists.
"""

from __future__ import annotations

import math
import time

from . import toobit

# Toobit VIP-0 futures fees, from their published fee schedule: maker 0.0200%,
# taker 0.0600%. The demo opens and closes at market, so taker applies both ways
# unless a fill is explicitly recorded as a maker fill.
MAKER_FEE_PCT = 0.02
TAKER_FEE_PCT = 0.06


def _venue():
    """The market-data module for the venue the demo is currently simulating.

    The paper broker models *exchange mechanics*, and those differ enough between
    venues that reading Toobit's book while claiming to trade Tabdeal would produce
    a record of a position that could not exist. Resolved per call rather than at
    import time so switching `settings.exchange` takes effect without a restart.
    """
    from . import exchange                                    # noqa: PLC0415
    if exchange.current_name() == exchange.TABDEAL:
        from . import tabdeal                                 # noqa: PLC0415
        return tabdeal
    return toobit


def fees_for_venue() -> tuple[float, float]:
    """(maker_pct, taker_pct) for the active venue.

    Tabdeal charges 0.1% on **both** sides — there is no maker discount, so the
    demo's maker-entry optimisation saves nothing there. Getting this wrong is not
    cosmetic: at ~$270 notional against a $3 R it is the difference between 0.06R
    and 0.16R of cost per trade, which is most of this strategy's measured edge.
    """
    v = _venue()
    if getattr(v, "NAME", "") == "tabdeal":
        return v.MAKER_FEE_PCT, v.TAKER_FEE_PCT
    return MAKER_FEE_PCT, TAKER_FEE_PCT

# Funding period comes from the API per contract ("8H" for every perp checked), but a
# venue could quote a contract without one, and a missing period must not silently
# become "no funding cost".
DEFAULT_FUNDING_HOURS = 8.0


class PaperError(RuntimeError):
    pass


# --------------------------------------------------------------------------------
# Contract specification
# --------------------------------------------------------------------------------


def _filters(contract: dict) -> dict[str, dict]:
    return {f.get("filterType"): f for f in (contract.get("filters") or [])}


def _fnum(d: dict, key: str) -> float | None:
    try:
        return float(d[key])
    except (KeyError, TypeError, ValueError):
        return None


def contract_spec(symbol: str) -> dict:
    """Everything the simulator needs about one contract, straight from the venue.

    Raises rather than defaulting: a made-up tick size or lot step would produce
    positions that could never exist on the real venue, which is precisely the kind of
    plausible-but-wrong number this project is built to avoid.
    """
    venue = _venue()
    if getattr(venue, "NAME", "") == "tabdeal":
        return _tabdeal_spec(symbol, venue)

    row = next((c for c in toobit.contracts() if c.get("symbol") == symbol), None)
    if row is None:
        raise PaperError(f"{symbol} is not a live Toobit contract")

    filt = _filters(row)
    tiers = []
    for t in row.get("riskLimits") or []:
        value = _fnum(t, "value")
        maint = _fnum(t, "maintMargin")
        lev = _fnum(t, "maxLeverage")
        if value is None or maint is None or lev is None:
            continue
        tiers.append({"max_notional": value, "maint_rate": maint, "max_leverage": lev})
    if not tiers:
        raise PaperError(f"{symbol} has no usable risk tiers")
    tiers.sort(key=lambda t: t["max_notional"])

    return {
        "symbol": symbol,
        "underlying": row.get("underlying"),
        "index_token": row.get("indexToken"),
        "units_per_contract": toobit.coin_units_per_contract(row),
        "tick_size": _fnum(filt.get("PRICE_FILTER") or {}, "tickSize"),
        "step_size": _fnum(filt.get("LOT_SIZE") or {}, "stepSize"),
        "min_qty": _fnum(filt.get("LOT_SIZE") or {}, "minQty"),
        "max_qty": _fnum(filt.get("LOT_SIZE") or {}, "maxQty"),
        "min_notional": _fnum(filt.get("MIN_NOTIONAL") or {}, "minNotional") or 0.0,
        "tiers": tiers,
    }


def _tabdeal_spec(symbol: str, venue) -> dict:
    """Contract spec for Tabdeal, whose mechanics are simpler than Toobit's.

    Three real differences, none of them cosmetic:

    * **Quantity is in coins.** No `contractMultiplier`, no `1000SHIB` scaling, so
      `units_per_contract` is 1.0 and a "contract" and a coin are the same thing.
    * **One flat maintenance rate, no ladder.** Tabdeal charges 0.5% of position
      value on every symbol regardless of size, so the tier list here is a single
      entry covering all notionals rather than a 9-rung ladder. `tier_for` still
      works unchanged against it.
    * **Tick and step come from decimal precision**, the only sizing information
      this venue publishes — there is no PRICE_FILTER/LOT_SIZE to read. `min_qty`
      is set to the step for the same reason, and `min_notional` to 0.0 because the
      venue states no minimum; that is "not published", not "verified as none", so
      an order this simulator accepts could still be rejected in reality.

    The leverage ceiling recorded here is the product's 100x. Note it is *cross*
    margin on this venue, which `liquidation_price()` does not model — see the
    warning there.
    """
    row = venue.contract_for(symbol)
    if row is None:
        raise PaperError(f"{symbol} is not a live Tabdeal futures symbol")

    tick = venue._precision_step(row.get("pricePrecision"))
    step = venue._precision_step(row.get("quantityPrecision"))
    return {
        "symbol": symbol,
        "underlying": row.get("baseAsset"),
        "index_token": None,
        "units_per_contract": 1.0,
        "tick_size": tick,
        "step_size": step,
        "min_qty": step,
        "max_qty": None,
        "min_notional": 0.0,
        "margin_mode": "cross",
        "tiers": [{"max_notional": float("inf"),
                   "maint_rate": venue.MAINT_MARGIN_PCT / 100.0,
                   "max_leverage": venue.MAX_LEVERAGE}],
    }


def tier_for(spec: dict, notional: float) -> dict:
    """The risk tier a position of this notional actually sits in.

    Tiers are ordered by the notional ceiling they cover. Anything above the last
    tier's ceiling still faces that tier's terms — the venue does not offer a gentler
    one — so the largest tier is the floor of the ladder, not an error.
    """
    for tier in spec["tiers"]:
        if notional <= tier["max_notional"]:
            return tier
    return spec["tiers"][-1]


def round_to_step(qty: float, step: float | None) -> float:
    """Contracts, rounded *down* to a tradable lot.

    Down, never nearest: rounding up would size the position above the risk the
    planner authorised.
    """
    if not step or step <= 0:
        return qty
    return math.floor(qty / step + 1e-9) * step


def round_to_tick(price: float, tick: float | None) -> float:
    if not tick or tick <= 0:
        return price
    return round(price / tick) * tick


# --------------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------------


def mark_price(spec: dict) -> tuple[float | None, str]:
    """Mark price, and where it came from.

    Liquidation must be judged on mark, not last traded price — mark is derived from
    an index across venues, so a single thin book cannot wick a position out of a
    thesis that is still intact. Toobit publishes both an index and an `edp`
    (estimated delivery price) per index token; `edp` is the closer analogue of mark.

    Returns (None, reason) rather than falling back to a guess when nothing is
    available, so the caller can show "no data" instead of a plausible number.
    """
    venue = _venue()
    if getattr(venue, "NAME", "") == "tabdeal":
        # No index or `edp` equivalent is published here, so the order-book mid is
        # the closest honest analogue of a mark — and on a book this thin it is the
        # right one, because it is what the position could actually be closed at.
        price = venue.mark_price(spec["symbol"])
        return (price, "book_mid") if price else (None, "unavailable")

    token = spec.get("index_token")
    if token:
        try:
            entry = toobit.index_prices().get(token) or {}
        except Exception:                                    # noqa: BLE001
            entry = {}
        for field in ("edp", "index"):
            if isinstance(entry.get(field), float):
                return entry[field], field
    try:
        tick = toobit.ticker(spec["symbol"])
        price = float(tick.get("last") or tick.get("p"))
        return price, "last"
    except Exception:                                        # noqa: BLE001
        return None, "unavailable"


def funding(symbol: str) -> dict | None:
    """Live funding for the contract, with its period and the venue's cap."""
    raw = _venue().funding_rate(symbol)
    if not raw:
        # Tabdeal publishes no funding rate for اهرم حرفه‌ای, so this is the normal
        # path there rather than an error. `_cycle_one` already treats a missing
        # rate as "no funding accrued this cycle".
        return None
    hours = DEFAULT_FUNDING_HOURS
    period = str(raw.get("period") or "")
    if period.upper().endswith("H"):
        try:
            hours = float(period[:-1])
        except ValueError:
            hours = DEFAULT_FUNDING_HOURS
    return {
        "rate": raw.get("rate"),
        "period": period or f"{hours:g}H",
        "period_hours": hours,
        "next_funding_time": raw.get("next_funding_time"),
        "cap": raw.get("rate_cap"),
        "floor": raw.get("rate_floor"),
    }


# --------------------------------------------------------------------------------
# Position mechanics
# --------------------------------------------------------------------------------


def coins(contracts_qty: float, spec: dict) -> float:
    return contracts_qty * spec["units_per_contract"]


def notional(contracts_qty: float, price: float, spec: dict) -> float:
    return coins(contracts_qty, spec) * price


def fee(notional_value: float, *, maker: bool = False) -> float:
    maker_pct, taker_pct = fees_for_venue()
    pct = maker_pct if maker else taker_pct
    return abs(notional_value) * pct / 100.0


def unrealised_pnl(side: str, entry: float, mark: float, contracts_qty: float,
                   spec: dict) -> float:
    """Linear USDT-M: PnL is in quote currency and moves 1:1 with price × coins."""
    q = coins(contracts_qty, spec)
    return (mark - entry) * q if side == "long" else (entry - mark) * q


def liquidation_price(side: str, entry: float, leverage: float,
                      maint_rate: float) -> float | None:
    """Isolated-margin liquidation, solved rather than approximated.

    A long is liquidated when the loss eats the margin down to the maintenance
    requirement, and that requirement is itself a function of the liquidation price:

        (entry − liq)·q = entry·q/lev − liq·q·maint          [long]

    which rearranges to a closed form with no iteration:

        liq = entry·(1 − 1/lev) ÷ (1 − maint)                [long]
        liq = entry·(1 + 1/lev) ÷ (1 + maint)                [short]

    This is an estimate in the same sense the planner's is: it ignores fees already
    paid and any funding accrued, both of which move the real trigger slightly
    against the position.

    **This formula is isolated-margin only, and Tabdeal is cross.** On a cross-margin
    venue there is no per-position liquidation price: the whole wallet backs every
    position, so what actually triggers a liquidation is total equity falling below
    the *summed* maintenance requirement of all open positions at once. Tabdeal is
    explicit that one loser can close the entire account, profitable positions
    included. The number returned here for a Tabdeal position is therefore the price
    at which that position *alone* would exhaust *its own* notional margin share —
    useful as a per-position risk marker, and what the planner's liquidation-buffer
    gate checks, but **it is not the price Tabdeal will actually liquidate at**. The
    real trigger is portfolio-wide and arrives earlier when other positions are
    losing. Modelling that properly is an open item; until then, treat cross-margin
    liquidation distance as optimistic.
    """
    if leverage <= 0:
        return None
    if side == "long":
        denom = 1.0 - maint_rate
        return entry * (1.0 - 1.0 / leverage) / denom if denom > 0 else None
    denom = 1.0 + maint_rate
    return entry * (1.0 + 1.0 / leverage) / denom


def margin_ratio(maint_requirement: float, margin: float, upnl: float) -> float | None:
    """Maintenance requirement over remaining equity, as a percentage.

    Toobit shows this as the number that reaches 100% at liquidation, which makes it
    the one field that tells a reader how close the position is without their having
    to compare two prices.
    """
    equity = margin + upnl
    if equity <= 0:
        return 100.0
    return min(100.0, maint_requirement / equity * 100.0)


def position_state(pos: dict, spec: dict, mark: float) -> dict:
    """Mark-to-market one open position the way the venue would."""
    qty = float(pos["contracts"])
    entry = float(pos["entry_price"])
    lev = float(pos["leverage"])
    side = pos["side"]

    notional_now = notional(qty, mark, spec)
    tier = tier_for(spec, notional_now)
    maint_req = notional_now * tier["maint_rate"]
    margin = float(pos["margin"])
    upnl = unrealised_pnl(side, entry, mark, qty, spec)

    risk_r = float(pos.get("risk_amount") or 0.0)
    return {
        "mark": mark,
        "notional": notional_now,
        "coins": coins(qty, spec),
        "unrealised_pnl": upnl,
        "unrealised_r": (upnl / risk_r) if risk_r else None,
        "roi_pct": (upnl / margin * 100.0) if margin else None,
        "maint_rate": tier["maint_rate"],
        "maint_requirement": maint_req,
        "tier_max_leverage": tier["max_leverage"],
        "margin_ratio_pct": margin_ratio(maint_req, margin, upnl),
        "liquidation_price": liquidation_price(side, entry, lev, tier["maint_rate"]),
    }


def funding_payment(side: str, notional_value: float, rate: float) -> float:
    """Signed funding for one period. Positive rate: longs pay shorts.

    Returned as a cash-flow, so a negative number is money leaving the account.
    """
    charge = notional_value * rate
    return -charge if side == "long" else charge


def funding_periods_elapsed(opened_at: float, now: float, period_hours: float) -> int:
    if period_hours <= 0:
        return 0
    return int((now - opened_at) // (period_hours * 3600))


# --------------------------------------------------------------------------------
# Exit detection
# --------------------------------------------------------------------------------


def exit_reason(side: str, high: float, low: float, *, stop: float | None,
                tp1: float | None, tp2: float | None,
                liq: float | None) -> tuple[str, float] | None:
    """Which level a candle hit, resolved pessimistically.

    When one candle's range covers both the stop and a target, OHLC cannot say which
    came first. Assuming the target is how backtests flatter themselves; this assumes
    the stop, so a simulated record can only understate performance.

    Liquidation is checked before the stop, because a position that liquidates is gone
    regardless of where its stop sat.

    Each level is a one-sided comparison, not a `low <= level <= high` range
    containment — found 2026-08-20, live, as the actual cause of stops that stopped
    working: a long's stop only needs `low <= stop` (price dipped to or below it at
    some point in the checked range); requiring `stop <= high` too was a real bug —
    once price moved cleanly past the stop and the most recent candle's own high no
    longer reached back up to the old stop level, the old range check silently
    stopped firing forever, even with the position sitting far past its stop. `liq`
    right below was already written as a one-sided check; `stop`/`tp1`/`tp2` were the
    ones that had drifted into the wrong pattern.
    """
    if side == "long":
        if liq is not None and low <= liq:
            return "liquidated", liq
        if stop is not None and low <= stop:
            return "stopped", stop
        if tp2 is not None and high >= tp2:
            return "tp2", tp2
        if tp1 is not None and high >= tp1:
            return "tp1", tp1
        return None

    if liq is not None and high >= liq:
        return "liquidated", liq
    if stop is not None and high >= stop:
        return "stopped", stop
    if tp2 is not None and low <= tp2:
        return "tp2", tp2
    if tp1 is not None and low <= tp1:
        return "tp1", tp1
    return None


def now_ts() -> float:
    return time.time()
