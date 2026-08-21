"""A second read-only guard, mirroring the one inside the skill's client.

The skill already refuses to call any write path. This repeats the check at our own
API layer so the property does not depend on a single file in someone else's
directory staying as it is. Two independent copies that must both agree is cheap;
discovering that an analysis tool could place orders is not.

`self_test()` runs at server startup. If the guard ever stops rejecting a known-bad
path, the server refuses to start rather than serving a dashboard whose central
promise has quietly lapsed.
"""

from __future__ import annotations

# Kept deliberately identical in spirit to the skill's PUBLIC_PATHS /
# PRIVATE_ALLOWLIST / FORBIDDEN. Widen the allowlist only for genuine read
# endpoints, and never soften FORBIDDEN.
PUBLIC_PREFIXES = (
    "/v3/orderbook/", "/v2/depth/", "/v2/trades/", "/market/stats",
    "/market/udf/history",
)

PRIVATE_ALLOWLIST = frozenset({
    "/users/profile",
    "/users/limitations",
    "/users/wallets/list",
    "/v2/wallets",
    "/users/wallets/balance",
    "/margin/fee-rates",
    "/margin/delegation-limit",
    "/margin/v2/delegation-limit",
    "/positions/list",
    "/positions/active-count",
    "/market/orders/list",
    "/market/trades/list",
})

FORBIDDEN = (
    "orders/add", "orders/batch", "cancel", "withdraw", "/close", "edit-collateral",
    "convert", "update-status", "apikeys", "logout", "login", "transfer",
)


# --------------------------------------------------------------------------------
# Toobit
# --------------------------------------------------------------------------------
#
# Toobit's REST API is Binance-shaped, which means the write endpoints sit right
# next to the read ones under the same prefixes — POST /api/v1/futures/order is one
# character away from paths we do use. The allowlist is therefore exact-match only,
# with no prefix wildcards for anything under /api/v1/futures/.
#
# `/api/v1/futures/order/history` is genuinely a read, and is still refused: we do
# not need it, and "order" appearing nowhere in an allowed path is a property worth
# keeping.

TOOBIT_PUBLIC_PREFIXES = (
    "/quote/v1/klines", "/quote/v1/depth", "/quote/v1/ticker",
    "/quote/v1/index", "/quote/v1/trades",
)

TOOBIT_ALLOWLIST = frozenset({
    "/api/v1/ping",
    "/api/v1/time",
    "/api/v1/exchangeInfo",
    "/api/v1/futures/fundingRate",
    "/api/v1/futures/historyFundingRate",
    # Account reads. Currently geo-blocked for this network, but harmless to allow.
    "/api/v1/account",
    "/api/v1/futures/balance",
    "/api/v1/futures/positions",
})

TOOBIT_FORBIDDEN = (
    "order", "withdraw", "transfer", "cancel", "close", "leverage", "margintype",
    "apikey", "deposit", "subaccount", "sub-account", "batch", "modify", "adjust",
    "login", "logout", "convert", "redeem", "borrow", "repay",
)


# --------------------------------------------------------------------------------
# Tabdeal
# --------------------------------------------------------------------------------
#
# Tabdeal is the riskiest of the three to guard, for two reasons.
#
# First, its API is Binance-shaped like Toobit's, so the write paths sit right next
# to the reads: POST /fapi/v1/order, POST /fapi/v1/positionSlTp and POST
# /fapi/v1/leverage all live under /fapi/v1/ alongside the depth and account reads.
# The allowlist is therefore exact-match only, as it is for Toobit.
#
# Second — and unlike Toobit or Nobitex — the credentials this project holds for
# Tabdeal are *live trade-permission keys on a funded account* (`canTrade: true`).
# On the other two venues a guard failure would leak a read we did not intend; here
# it could place a real order with real money. Nothing in the screener or the demo
# has any reason to write, so the guard refuses every non-GET verb outright.
#
# Market data spans two hosts: `api1.tabdeal.org` serves /fapi/* (depth, account,
# exchangeInfo) and `api-web.tabdeal.org` serves /plots/history (the OHLCV the web
# charts use). Paths are guarded the same way regardless of host, since a path that
# is safe on one is not automatically safe on the other.

TABDEAL_PUBLIC_PREFIXES = (
    "/plots/history", "/r/plots/history",
)

TABDEAL_ALLOWLIST = frozenset({
    "/r/fapi/v1/ping",
    "/r/fapi/v1/time",
    "/r/fapi/v1/exchangeInfo",
    "/r/fapi/v1/depth",
    "/r/fapi/v1/aggDepth",
    # Account reads. Harmless, and useful for reconciling the demo against a real
    # balance later — but never anything that mutates.
    "/r/fapi/v3/account",
    "/r/fapi/v3/balance",
    "/r/fapi/v3/positionRisk",
    "/r/fapi/v1/userTrades",
    "/r/fapi/v1/income",
})

TABDEAL_FORBIDDEN = (
    "order", "sltp", "positionclose", "close", "leverage", "transfer", "withdraw",
    "cancel", "margin/loan", "margin/repay", "borrow", "repay", "apikey", "deposit",
    "listenkey", "userdatastream", "login", "logout", "convert", "batch",
)


class ReadOnlyViolation(RuntimeError):
    pass


def assert_tabdeal_read_only(path: str, method: str = "GET") -> None:
    """Same contract as the Toobit guard, for Tabdeal paths on either host.

    `/fapi/v1/leverage` is a GET-able read, but it is refused anyway: the identical
    path with POST *sets* leverage on a live funded account, and keeping the whole
    path out of reach is worth more than the one number it would tell us.
    """
    if method.upper() != "GET":
        raise ReadOnlyViolation(
            f"Refusing {method.upper()} {path}: this application issues GET only, "
            f"and Tabdeal credentials carry live trade permission.")
    low = path.lower()
    for bad in TABDEAL_FORBIDDEN:
        if bad in low:
            raise ReadOnlyViolation(
                f"Refusing '{path}': matches the forbidden substring '{bad}'. This "
                f"application is read-only and never places or modifies anything.")
    base = path.split("?", 1)[0]
    if any(base.startswith(p) for p in TABDEAL_PUBLIC_PREFIXES):
        return
    if base in TABDEAL_ALLOWLIST:
        return
    raise ReadOnlyViolation(f"Path '{base}' is not on the Tabdeal read-only allowlist.")


def tabdeal_is_read_only(path: str, method: str = "GET") -> bool:
    try:
        assert_tabdeal_read_only(path, method)
        return True
    except ReadOnlyViolation:
        return False


# --------------------------------------------------------------------------------
# Tabdeal — the live-trading write allowlist
# --------------------------------------------------------------------------------
#
# This is the ONLY door in the application through which an order can reach an
# exchange, and it is bolted shut by default. `tabdeal_broker.py` is the only caller.
# The read guard above is unchanged and still refuses every one of these paths, so
# the market-data client cannot acquire write capability by accident.
#
# Three independent things must all be true before a write is permitted: the caller
# must be the broker (it is the only importer), live trading must be enabled in
# settings, and the exact path+verb must appear below. Enumerated exactly — no
# prefixes, no wildcards — because on this Binance-shaped API a wildcard under
# /fapi/v1/ would admit every write the venue offers.
#
# Deliberately absent and never to be added: anything resembling withdrawal. The
# venue exposes no futures withdrawal endpoint, and this list must not become the
# place where one appears.

TABDEAL_WRITE_ALLOWLIST = frozenset({
    ("POST", "/fapi/v1/order"),           # place
    ("DELETE", "/fapi/v1/order"),         # cancel one order
    ("DELETE", "/fapi/v1/position"),      # market-close an entire position
    ("POST", "/fapi/v1/positionSlTp"),    # exchange-side stop / target
    ("POST", "/fapi/v1/leverage"),        # set leverage before entry
    ("POST", "/fapi/v1/transfer"),        # spot <-> futures wallet
})


class LiveTradingDisabled(RuntimeError):
    pass


def assert_tabdeal_write_allowed(path: str, method: str, *,
                                 live_enabled: bool) -> None:
    """Gate a real, money-moving Tabdeal request.

    `live_enabled` is passed in rather than read here so the decision is made by the
    caller's own settings load and is visible at the call site — a guard that reads
    global state can be flipped from somewhere the reader is not looking.
    """
    if not live_enabled:
        raise LiveTradingDisabled(
            f"Refusing {method} {path}: live trading is disabled. Set "
            f"demo.live_trading=true in settings.json to arm it.")
    base = path.split("?", 1)[0]
    if (method.upper(), base) not in TABDEAL_WRITE_ALLOWLIST:
        raise ReadOnlyViolation(
            f"Refusing {method} {base}: not on the Tabdeal write allowlist.")


_TABDEAL_WRITE_MUST_REJECT = (
    ("POST", "/fapi/v1/order", False),          # correct path, but not armed
    ("POST", "/fapi/v1/withdraw", True),        # never, armed or not
    ("POST", "/api/v1/capital/withdraw", True),
    ("GET", "/fapi/v1/order", True),            # wrong verb for this door
    ("POST", "/fapi/v1/unknown", True),
    ("POST", "/fapi/v2/order", True),           # version not enumerated
)

_TABDEAL_WRITE_MUST_ALLOW = (
    ("POST", "/fapi/v1/order"),
    ("DELETE", "/fapi/v1/position"),
    ("POST", "/fapi/v1/positionSlTp"),
)


def assert_toobit_read_only(path: str, method: str = "GET") -> None:
    """Same contract as `assert_read_only`, for Toobit paths.

    Method is checked too: on a Binance-shaped API the verb is what separates
    reading a position from opening one, so anything other than GET is refused
    outright rather than reasoned about.
    """
    if method.upper() != "GET":
        raise ReadOnlyViolation(
            f"Refusing {method.upper()} {path}: this application issues GET only.")
    low = path.lower()
    for bad in TOOBIT_FORBIDDEN:
        if bad in low:
            raise ReadOnlyViolation(
                f"Refusing '{path}': matches the forbidden substring '{bad}'. This "
                f"application is read-only and never places or modifies anything.")
    base = path.split("?", 1)[0]
    if any(base.startswith(p) for p in TOOBIT_PUBLIC_PREFIXES):
        return
    if base in TOOBIT_ALLOWLIST:
        return
    raise ReadOnlyViolation(f"Path '{base}' is not on the Toobit read-only allowlist.")


def toobit_is_read_only(path: str, method: str = "GET") -> bool:
    try:
        assert_toobit_read_only(path, method)
        return True
    except ReadOnlyViolation:
        return False


def assert_read_only(path: str) -> None:
    low = path.lower()
    for bad in FORBIDDEN:
        if bad in low:
            raise ReadOnlyViolation(
                f"Refusing '{path}': this application is read-only and never places, "
                f"modifies, closes or cancels anything.")
    base = path.split("?", 1)[0]
    if any(base.startswith(p) for p in PUBLIC_PREFIXES):
        return
    if base in PRIVATE_ALLOWLIST:
        return
    if base.startswith("/positions/") and base.endswith("/status"):
        return
    if base.startswith("/margin/predict/"):
        return
    raise ReadOnlyViolation(f"Path '{base}' is not on the read-only allowlist.")


def is_read_only(path: str) -> bool:
    try:
        assert_read_only(path)
        return True
    except ReadOnlyViolation:
        return False


_MUST_REJECT = (
    "/market/orders/add",
    "/market/orders/batch-add",
    "/market/orders/cancel",
    "/market/orders/update-status",
    "/users/wallets/withdraw",
    "/users/wallets/transfer",
    "/positions/12/close",
    "/positions/12/edit-collateral",
    "/apikeys/create",
    "/apikeys/delete/abc",
    "/auth/login",
    "/auth/logout",
    "/market/orders/add?symbol=BTCUSDT",
    "/MARKET/ORDERS/ADD",
    "/some/unknown/endpoint",
)

_MUST_ALLOW = (
    "/market/stats",
    "/market/udf/history?symbol=BTCUSDT&resolution=240",
    "/v3/orderbook/BTCUSDT",
    "/margin/fee-rates",
    "/margin/v2/delegation-limit",
    "/positions/list",
    "/positions/99/status",
    "/users/profile",
)


_TOOBIT_MUST_REJECT = (
    ("/api/v1/futures/order", "POST"),
    ("/api/v1/futures/order", "GET"),
    ("/api/v1/futures/batchOrders", "POST"),
    ("/api/v1/futures/order/cancel", "GET"),
    ("/api/v1/futures/leverage", "POST"),
    ("/api/v1/futures/marginType", "POST"),
    ("/api/v1/futures/position/close", "POST"),
    ("/api/v1/account/withdraw", "GET"),
    ("/api/v1/account/transfer", "POST"),
    ("/api/v1/futures/order/history", "GET"),
    ("/api/v1/apikey/create", "GET"),
    ("/quote/v1/klines", "POST"),          # right path, wrong verb
    ("/api/v1/exchangeInfo", "DELETE"),
    ("/api/v1/unknown/endpoint", "GET"),
)

_TOOBIT_MUST_ALLOW = (
    "/api/v1/ping",
    "/api/v1/time",
    "/api/v1/exchangeInfo",
    "/quote/v1/klines?symbol=BTC-SWAP-USDT&interval=4h&limit=300",
    "/quote/v1/depth?symbol=BTC-SWAP-USDT&limit=20",
    "/quote/v1/ticker/24hr?symbol=BTC-SWAP-USDT",
    "/api/v1/futures/fundingRate?symbol=BTC-SWAP-USDT",
    "/api/v1/futures/positions",
)


_TABDEAL_MUST_REJECT = (
    ("/fapi/v1/order", "POST"),
    ("/fapi/v1/order", "GET"),
    ("/fapi/v1/positionSlTp", "POST"),        # sets a real stop on a real position
    ("/fapi/v1/positionSlTp", "GET"),
    ("/fapi/v1/positionClose", "POST"),
    ("/fapi/v1/leverage", "POST"),
    ("/fapi/v1/leverage", "GET"),             # same path mutates under POST
    ("/fapi/v1/transfer", "POST"),
    ("/r/fapi/v1/openOrders", "GET"),         # "order" substring, and we never need it
    ("/r/fapi/v1/allOrders", "GET"),
    ("/api/v1/margin/loan", "GET"),
    ("/api/v1/userDataStream", "POST"),
    ("/plots/history", "POST"),               # right path, wrong verb
    ("/r/fapi/v1/depth", "DELETE"),
    ("/r/fapi/v1/unknown", "GET"),
)

_TABDEAL_MUST_ALLOW = (
    "/r/fapi/v1/ping",
    "/r/fapi/v1/exchangeInfo",
    "/r/fapi/v1/depth?symbol=BTC_USDT&limit=20",
    "/r/fapi/v3/positionRisk",
    "/plots/history?symbol=BTC_USDT&resolution=15&from=1787000000&to=1787340000",
    "/r/plots/history?symbol=ETH_USDT&resolution=60&from=1787000000&to=1787340000",
)


def self_test() -> list[str]:
    """Return a list of failures; empty means all three guards behave."""
    failures = []
    for path, method in _TABDEAL_MUST_REJECT:
        if tabdeal_is_read_only(path, method):
            failures.append(f"tabdeal: should have rejected {method} {path}")
    for path in _TABDEAL_MUST_ALLOW:
        if not tabdeal_is_read_only(path):
            failures.append(f"tabdeal: should have allowed {path}")
    # The write door: shut when disarmed, and selective when armed.
    for method, path, armed in _TABDEAL_WRITE_MUST_REJECT:
        try:
            assert_tabdeal_write_allowed(path, method, live_enabled=armed)
            failures.append(f"tabdeal-write: should have rejected {method} {path} "
                            f"(live_enabled={armed})")
        except (ReadOnlyViolation, LiveTradingDisabled):
            pass
    for method, path in _TABDEAL_WRITE_MUST_ALLOW:
        try:
            assert_tabdeal_write_allowed(path, method, live_enabled=True)
        except (ReadOnlyViolation, LiveTradingDisabled):
            failures.append(f"tabdeal-write: should have allowed armed {method} {path}")
    # Every write path must still be refused by the read guard.
    for method, path in TABDEAL_WRITE_ALLOWLIST:
        if tabdeal_is_read_only(path, method):
            failures.append(f"tabdeal: read guard must still refuse {method} {path}")
    for path in _MUST_REJECT:
        if is_read_only(path):
            failures.append(f"nobitex: should have rejected {path}")
    for path in _MUST_ALLOW:
        if not is_read_only(path):
            failures.append(f"nobitex: should have allowed {path}")
    for path, method in _TOOBIT_MUST_REJECT:
        if toobit_is_read_only(path, method):
            failures.append(f"toobit: should have rejected {method} {path}")
    for path in _TOOBIT_MUST_ALLOW:
        if not toobit_is_read_only(path):
            failures.append(f"toobit: should have allowed {path}")
    return failures


if __name__ == "__main__":
    problems = self_test()
    if problems:
        print("\n".join(problems))
        raise SystemExit(1)
    print(f"guard ok — nobitex: {len(_MUST_REJECT)} rejected / {len(_MUST_ALLOW)} allowed; "
          f"toobit: {len(_TOOBIT_MUST_REJECT)} rejected / {len(_TOOBIT_MUST_ALLOW)} allowed; "
          f"tabdeal: {len(_TABDEAL_MUST_REJECT)} rejected / {len(_TABDEAL_MUST_ALLOW)} allowed; "
          f"tabdeal-write: {len(_TABDEAL_WRITE_MUST_REJECT)} rejected / "
          f"{len(_TABDEAL_WRITE_MUST_ALLOW)} allowed when armed")
