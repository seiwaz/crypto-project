"""Resolve the requested coin list to real Nobitex symbols.

Several of the requested coins are not listed on Nobitex, and several more are listed
for spot but not for margin. Rather than hardcoding symbols and silently dropping the
misses, this enumerates what actually exists and records a reason for everything that
did not resolve, so a missing coin is visible in the UI rather than mysterious.

Two wrinkles the naive `<COIN>USDT` mapping gets wrong:

* Nobitex quotes low-unit-value coins in scaled lots — SHIB trades as `1K_SHIB` and
  PEPE as `1M_PEPE`. Mapping them to `SHIBUSDT` reports them as unlisted when they are
  perfectly tradeable. Prices on those markets are *per lot*, which the UI must label.
* `GET /margin/v2/delegation-limit` rejects every parameter spelling we tried
  (`InvalidSymbol`, symbol ""), so it cannot enumerate the margin universe.
  `GET /margin/fee-rates` returns exactly that list in one call and is on the skill's
  read-only allowlist, so it is used instead.

Writes config/watchlist.json.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import config, skill

AVAILABLE = "available"            # listed and margin-enabled
SPOT_ONLY = "spot-only"            # listed, but not offered for margin
MARGIN_UNKNOWN = "margin-unknown"  # listed; margin list could not be read
NOT_LISTED = "not-listed"          # absent from /market/stats entirely

_LOT_RE = re.compile(r"^(\d+)([kmb]?)_(.+)$")
_MULT = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def _parse_stats(payload: dict) -> dict:
    """`/market/stats` → {(src, dst): stats}. Keys arrive as 'btc-rls', '1k_shib-usdt'."""
    out = {}
    for key, value in (payload.get("stats") or {}).items():
        src, sep, dst = key.partition("-")
        if sep:
            out[(src.lower(), dst.lower())] = value
    return out


def _lot_size(src: str) -> tuple[int, str | None]:
    """'1k_shib' → (1000, '1K'). 'btc' → (1, None)."""
    m = _LOT_RE.match(src)
    if not m:
        return 1, None
    count, unit, _ = m.groups()
    return int(count) * _MULT.get(unit, 1), f"{count}{unit.upper()}"


def _margin_currencies(fee_payload, unavailable_reason: str | None = None
                       ) -> tuple[set[str] | None, str]:
    """Margin-enabled currencies from /margin/fee-rates.

    None means "could not determine", which becomes MARGIN_UNKNOWN — never a silent
    "available".
    """
    if fee_payload is None:
        return None, (unavailable_reason
                      or "/margin/fee-rates was not fetched — it needs API credentials")
    if not isinstance(fee_payload, dict):
        return None, f"unexpected response type {type(fee_payload).__name__}"
    rates = fee_payload.get("feeRates")
    if not isinstance(rates, list) or not rates:
        return None, str(fee_payload.get("message") or "no feeRates array in response")
    coins = {str(r["currency"]).lower() for r in rates
             if isinstance(r, dict) and r.get("currency")}
    if not coins:
        return None, "feeRates array contained no currencies"
    return coins, f"/margin/fee-rates listed {len(coins)} margin currencies"


def _fee_rate_for(fee_payload, currency: str) -> float | None:
    for r in (fee_payload or {}).get("feeRates") or []:
        if isinstance(r, dict) and str(r.get("currency", "")).lower() == currency:
            try:
                return float(r["positionFeeRate"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def resolve(stats_payload: dict, fee_payload=None,
            fee_error: str | None = None) -> dict:
    stats = _parse_stats(stats_payload)
    by_src: dict[str, dict] = {}
    for (src, dst), value in stats.items():
        by_src.setdefault(src, {})[dst] = value

    # coin -> the market source key that represents it, preferring the unscaled market
    src_for_coin: dict[str, str] = {}
    for src in by_src:
        _, lot_label = _lot_size(src)
        base = src.split("_", 1)[1] if lot_label else src
        if base not in src_for_coin or not lot_label:
            src_for_coin[base] = src

    margin_coins, margin_note = _margin_currencies(fee_payload, fee_error)

    entries = []
    requested = config.load_coins()
    for coin in requested:
        low = coin.lower()
        entry = {"coin": coin, "symbol": None, "quote": None, "status": NOT_LISTED,
                 "reason": None, "market_closed": None, "lot_size": 1,
                 "lot_label": None, "market_key": None, "position_fee_rate": None}

        src = src_for_coin.get(low)
        if not src:
            entry["reason"] = "not listed on Nobitex (absent from /market/stats)"
            entries.append(entry)
            continue

        quotes = by_src[src]
        # Prefer USDT: tighter spreads, and no local-rate exposure on top of the trade.
        quote_key = "usdt" if "usdt" in quotes else ("rls" if "rls" in quotes else None)
        if not quote_key:
            entry["reason"] = f"listed as '{src}' but with no USDT or IRT market"
            entries.append(entry)
            continue

        lot_size, lot_label = _lot_size(src)
        quote = "USDT" if quote_key == "usdt" else "IRT"
        entry.update(
            symbol=f"{src.upper()}{quote}",
            quote=quote,
            market_key=f"{src}-{quote_key}",
            lot_size=lot_size,
            lot_label=lot_label,
        )

        picked = quotes[quote_key]
        if isinstance(picked, dict):
            entry["market_closed"] = bool(picked.get("isClosed"))

        if margin_coins is None:
            # The full explanation is reported once under `margin_detection`; repeating
            # it on all 43 rows buries the per-coin notes that are actually specific.
            entry["status"] = MARGIN_UNKNOWN
            entry["reason"] = "margin availability unverified"
        elif src in margin_coins:
            entry["status"] = AVAILABLE
            entry["position_fee_rate"] = _fee_rate_for(fee_payload, src)
        else:
            entry["status"] = SPOT_ONLY
            entry["reason"] = "listed for spot, but not offered for margin trading"

        if entry["market_closed"]:
            entry["reason"] = ((entry["reason"] + "; ") if entry["reason"] else "") \
                + "market currently closed"
        if lot_label:
            note = f"quoted per {lot_size:,} {coin} (Nobitex lists it as {src.upper()})"
            entry["reason"] = (entry["reason"] + "; " + note) if entry["reason"] else note
        entries.append(entry)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exchange": "nobitex",
        "exchange_label": "Nobitex — معاملات تعهدی",
        "source": "GET /market/stats + GET /margin/fee-rates",
        "margin_detection": margin_note,
        "delegation_limit_note": (
            "GET /margin/v2/delegation-limit rejects every parameter spelling tried "
            "(InvalidSymbol, symbol \"\"), so it cannot enumerate margin availability. "
            "/margin/fee-rates returns the same universe in one allowlisted call."),
        "requested": len(requested),
        "coins": entries,
    }


def scannable(watchlist: dict) -> list[dict]:
    """Coins worth spending API calls on: listed, open, and margin-capable."""
    return [c for c in watchlist.get("coins", [])
            if c["status"] in (AVAILABLE, MARGIN_UNKNOWN) and not c.get("market_closed")]


def usdt_irt_rate(stats_payload: dict) -> float | None:
    """Live USDT/IRT, for converting capital into a market's quote currency.

    Returns None when the pair is missing — callers must then skip the conversion
    rather than assume a rate.
    """
    node = _parse_stats(stats_payload).get(("usdt", "rls"))
    if not isinstance(node, dict):
        return None
    for field in ("latest", "bestSell", "bestBuy", "dayClose"):
        try:
            value = float(node.get(field))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def run(verbose: bool = True) -> dict:
    config.load_dotenv()
    config.ensure_dirs()

    stats = skill.market_stats_all()
    fees, fee_error = None, None
    creds = config.credential_status()
    if not (creds["api_key_set"] and creds["api_secret_set"]) and not creds["token_set"]:
        fee_error = ("no API credentials in .env, so /margin/fee-rates could not be "
                     "read — margin availability is unverified for every listed coin")
    else:
        try:
            fees = skill.margin_fee_rates()
        except skill.SkillError as exc:
            fee_error = f"/margin/fee-rates failed: {exc}"
    if fee_error and verbose:
        print(f"  {fee_error}")

    watchlist = resolve(stats, fees, fee_error)
    watchlist["usdt_irt"] = usdt_irt_rate(stats)

    config.save_watchlist("nobitex", watchlist)

    if verbose:
        report(watchlist)
    return watchlist


def report(watchlist: dict) -> None:
    by_status: dict[str, list[dict]] = {}
    for c in watchlist["coins"]:
        by_status.setdefault(c["status"], []).append(c)

    print(f"\nSymbol discovery — {watchlist['requested']} coins requested")
    print(f"  margin source : {watchlist['margin_detection']}")
    rate = watchlist.get("usdt_irt")
    print(f"  USDT/IRT      : {rate:,.0f} rls" if rate else "  USDT/IRT      : unavailable")

    for status, heading in ((AVAILABLE, "scannable (margin available)"),
                            (MARGIN_UNKNOWN, "scannable (margin unverified)"),
                            (SPOT_ONLY, "spot only — excluded from scans"),
                            (NOT_LISTED, "not listed on Nobitex")):
        group = by_status.get(status, [])
        if not group:
            continue
        print(f"\n  {heading}  ({len(group)})")
        for c in group:
            line = f"    {c['coin']:<7} -> {c['symbol'] or '—':<13}"
            if c.get("reason"):
                line += f"  {c['reason']}"
            print(line)
    print()


if __name__ == "__main__":
    run()
