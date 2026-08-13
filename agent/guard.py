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


class ReadOnlyViolation(RuntimeError):
    pass


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


def self_test() -> list[str]:
    """Return a list of failures; empty means both guards behave."""
    failures = []
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
          f"toobit: {len(_TOOBIT_MUST_REJECT)} rejected / {len(_TOOBIT_MUST_ALLOW)} allowed")
