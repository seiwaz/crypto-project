"""Local paper broker that simulates Toobit USDT-M perpetuals.

Toobit's own demo trading is web-only — their documentation says "users can only
access the Demo Trading via Webpage", the production API lists no `TBV_` symbols, and
no demo API host resolves in DNS. So the demo cannot be delegated to them; it has to
run here. Everything below is simulation against Toobit's *real* public prices.

Nothing in this module can reach an exchange with intent. It reads market data through
`toobit._get`, which is behind the read-only allowlist in `guard.py`, and writes only
to the local database.

The contract mechanics are read from Toobit rather than assumed:

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
    """Everything the simulator needs about one contract, straight from Toobit.

    Raises rather than defaulting: a made-up tick size or lot step would produce
    positions that could never exist on the real venue, which is precisely the kind of
    plausible-but-wrong number this project is built to avoid.
    """
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
    token = spec.get("index_token")
    if token:
        try:
            data = toobit._get("/quote/v1/index", {"symbol": token})
        except Exception:                                    # noqa: BLE001
            data = None
        if isinstance(data, dict):
            for field, label in (("edp", "edp"), ("index", "index")):
                block = data.get(field)
                if isinstance(block, dict):
                    try:
                        return float(block[token]), label
                    except (KeyError, TypeError, ValueError):
                        continue
    try:
        tick = toobit.ticker(spec["symbol"])
        price = float(tick.get("last") or tick.get("p"))
        return price, "last"
    except Exception:                                        # noqa: BLE001
        return None, "unavailable"


def funding(symbol: str) -> dict | None:
    """Live funding for the contract, with its period and the venue's cap."""
    raw = toobit.funding_rate(symbol)
    if not raw:
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
    pct = MAKER_FEE_PCT if maker else TAKER_FEE_PCT
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
    """
    def touched(level: float | None) -> bool:
        return level is not None and low <= level <= high

    if side == "long":
        if liq is not None and low <= liq:
            return "liquidated", liq
        if touched(stop):
            return "stopped", stop
        if touched(tp2):
            return "tp2", tp2
        if touched(tp1):
            return "tp1", tp1
        return None

    if liq is not None and high >= liq:
        return "liquidated", liq
    if touched(stop):
        return "stopped", stop
    if touched(tp2):
        return "tp2", tp2
    if touched(tp1):
        return "tp1", tp1
    return None


def now_ts() -> float:
    return time.time()
