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


class ReadOnlyViolation(RuntimeError):
    pass


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


def self_test() -> list[str]:
    """Return a list of failures; empty means the guard behaves."""
    failures = []
    for path in _MUST_REJECT:
        if is_read_only(path):
            failures.append(f"should have rejected {path}")
    for path in _MUST_ALLOW:
        if not is_read_only(path):
            failures.append(f"should have allowed {path}")
    return failures


if __name__ == "__main__":
    problems = self_test()
    print("\n".join(problems) if problems
          else f"guard ok: {len(_MUST_REJECT)} rejected, {len(_MUST_ALLOW)} allowed")
