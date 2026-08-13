"""Venue registry.

Two venues are supported and both stay working: Toobit (default) and Nobitex. They
differ in almost everything about data access, and in nothing about the maths — both
end up calling the skill's `trade_plan.py` with a snapshot in the same shape.

An adapter provides:

    NAME, LABEL, PLAN_EXCHANGE
    discover(coins)            -> watchlist dict
    scannable(watchlist)       -> the entries worth spending calls on
    analyze(entry, profile, *, capital, risk_pct, count, hold_hours)
                               -> (snapshot, plan, candles_by_role, side_info)
    needs_credentials          -> bool
"""

from __future__ import annotations

from . import config

NOBITEX = "nobitex"
TOOBIT = "toobit"
SUPPORTED = (TOOBIT, NOBITEX)


class _NobitexAdapter:
    """Nobitex, via the skill's own bundled client."""

    NAME = NOBITEX
    LABEL = "Nobitex — معاملات تعهدی"
    PLAN_EXCHANGE = NOBITEX
    needs_credentials = True

    @staticmethod
    def discover(coins):
        from . import discover as nobitex_discover
        return nobitex_discover.run(verbose=False)

    @staticmethod
    def scannable(watchlist):
        from . import discover as nobitex_discover
        return nobitex_discover.scannable(watchlist)

    @staticmethod
    def analyze(entry, profile, *, capital, risk_pct, count=300, hold_hours=0.0,
                account_level=None):
        from . import skill
        return skill.analyze(entry["symbol"], profile, capital=capital,
                             risk_pct=risk_pct, count=count, exchange=NOBITEX,
                             hold_hours=hold_hours, account_level=account_level)


class _ToobitAdapter:
    """Toobit USDT perpetuals, public API only."""

    NAME = TOOBIT
    LABEL = "Toobit — USDT perpetuals"
    PLAN_EXCHANGE = "generic-perp"
    # Everything the screener needs is public; the private endpoints are geo-blocked
    # for this network and nothing depends on them.
    needs_credentials = False

    @staticmethod
    def discover(coins):
        from . import toobit
        return toobit.discover(coins)

    @staticmethod
    def scannable(watchlist):
        from . import toobit
        return toobit.scannable(watchlist)

    @staticmethod
    def analyze(entry, profile, *, capital, risk_pct, count=300, hold_hours=0.0,
                account_level=None):
        from . import toobit
        return toobit.analyze(entry, profile, capital=capital, risk_pct=risk_pct,
                              count=count, hold_hours=hold_hours)


_ADAPTERS = {NOBITEX: _NobitexAdapter, TOOBIT: _ToobitAdapter}


def current_name() -> str:
    name = str(config.load_settings().get("exchange") or TOOBIT).lower()
    return name if name in _ADAPTERS else TOOBIT


def adapter(name: str | None = None):
    return _ADAPTERS[(name or current_name()).lower()]


def label(name: str | None = None) -> str:
    return adapter(name).LABEL


def run_discovery(name: str | None = None, verbose: bool = True) -> dict:
    """Discover for one venue and persist its watchlist."""
    from . import config

    name = (name or current_name()).lower()
    venue = adapter(name)
    config.load_dotenv()
    config.ensure_dirs()
    watchlist = venue.discover(config.load_coins())
    config.save_watchlist(name, watchlist)
    if verbose:
        report(watchlist, venue)
    return watchlist


def report(watchlist: dict, venue) -> None:
    groups: dict[str, list[dict]] = {}
    for c in watchlist.get("coins", []):
        groups.setdefault(c["status"], []).append(c)

    print(f"\nSymbol discovery — {venue.LABEL}")
    print(f"  requested : {watchlist.get('requested')} coins from config/coins.txt")
    print(f"  source    : {watchlist.get('source')}")
    if watchlist.get("margin_detection"):
        print(f"  markets   : {watchlist['margin_detection']}")
    for status in sorted(groups):
        rows = groups[status]
        print(f"\n  {status}  ({len(rows)})")
        for c in rows:
            line = f"    {c['coin']:<8} -> {c.get('symbol') or '—':<22}"
            if c.get("reason"):
                line += f"  {c['reason']}"
            print(line)
    print()
