"""Real order execution on Tabdeal اهرم حرفه‌ای. **Disarmed by default.**

This is the only module in the project that can move real money. Everything else —
`tabdeal.py`, `paper.py`, `demo.py` — is either read-only or a simulation.

## It does nothing until three separate things are true

1. `demo.live_trading` is `true` in the server's `settings.json`. Default false, and
   deliberately not settable through the public dashboard API.
2. The exact path+verb appears in `guard.TABDEAL_WRITE_ALLOWLIST`.
3. The call is not in `dry_run`, which is the constructor default. A dry-run broker
   builds and signs nothing, logs exactly what it would have sent, and returns a
   simulated response — so the whole execution path can be exercised end to end
   against the real strategy without an order ever leaving the machine.

Disarmed, every write raises `guard.LiveTradingDisabled`. That is intentional: a
silent no-op would let a caller believe it had traded.

## Mirrors the demo exactly

The demo's lifecycle is: place a maker limit slightly better than mark → cancel it if
unfilled after `maker_timeout_minutes` → on fill, set the stop → at TP1 take half and
move the stop to the TP1 price → exit on TP2, stop, signal or time. Every one of
those steps has a method here with the same semantics, so `demo.py` can drive either
the paper broker or this one without changing its logic.

## Three venue constraints that shape the code

**No `reduceOnly`.** Tabdeal documents it as unsupported, and there is no partial
close endpoint — `DELETE /fapi/v1/position` closes the whole thing. So the TP1 half
exit has to be an *opposing order*, which can overshoot into a reverse position if
the size is stale. `reduce_position()` therefore re-reads the live position
immediately before sizing, refuses anything that would meet or exceed it, and
verifies afterwards that the position shrank and did not flip — closing immediately
if it did. This is the single most dangerous operation in the file.

**One SL and one TP per position.** `positionSlTp` cannot express "half here, rest
there", so the exchange-side stop is the safety net and the TP1 partial is managed
by our loop. Set the stop on fill and never rely on the loop for downside.

**Cross margin.** Every position shares one collateral pool, so a single loser can
liquidate the account. Position-level sizing does not bound account risk here the way
it does under isolated margin.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, guard, tabdeal

log = logging.getLogger(__name__)

# A hard ceiling on any single order, independent of what the planner asks for. This
# is a blast radius limit, not a strategy parameter: if a sizing bug ever produces a
# nonsense quantity, this is what stops it reaching the exchange.
DEFAULT_MAX_ORDER_NOTIONAL = 200.0

RECV_WINDOW_MS = 5000
_TIMEOUT = 20


class BrokerError(RuntimeError):
    pass


class TabdealBroker:
    """Signed, gated access to Tabdeal's futures write endpoints."""

    def __init__(self, *, dry_run: bool = True, max_order_notional: float | None = None):
        self.dry_run = dry_run
        self.max_order_notional = (max_order_notional
                                   if max_order_notional is not None
                                   else DEFAULT_MAX_ORDER_NOTIONAL)
        self._key = os.environ.get("TABDEAL_API_KEY") or ""
        self._secret = os.environ.get("TABDEAL_API_SECRET") or ""

    # ---------------------------------------------------------------- arming

    @staticmethod
    def live_enabled() -> bool:
        """Read fresh every call, so disarming takes effect immediately."""
        demo = config.load_settings().get("demo") or {}
        return bool(demo.get("live_trading"))

    def _preflight(self, method: str, path: str) -> None:
        guard.assert_tabdeal_write_allowed(path, method,
                                           live_enabled=self.live_enabled())
        if not self.dry_run and not (self._key and self._secret):
            raise BrokerError("TABDEAL_API_KEY / TABDEAL_API_SECRET are not set")

    # ---------------------------------------------------------------- transport

    def _send(self, method: str, path: str, params: dict) -> dict:
        self._preflight(method, path)
        payload = {k: v for k, v in params.items() if v is not None}
        payload["timestamp"] = int(time.time() * 1000)
        payload["recvWindow"] = RECV_WINDOW_MS
        query = urllib.parse.urlencode(payload)
        signature = hmac.new(self._secret.encode(), query.encode(),
                             hashlib.sha256).hexdigest()

        if self.dry_run:
            log.warning("DRY RUN %s %s %s", method, path, payload)
            return {"dry_run": True, "method": method, "path": path,
                    "params": payload}

        body = f"{query}&signature={signature}".encode()
        url = tabdeal.base_url() + path
        if method in ("DELETE", "GET"):
            url = f"{url}?{query}&signature={signature}"
            body = None
        req = urllib.request.Request(
            url, data=body, method=method,
            headers={"X-MBX-APIKEY": self._key,
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"})
        log.warning("LIVE %s %s %s", method, path,
                    {k: v for k, v in payload.items() if k != "signature"})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            # Never retry a write. A timeout or 5xx may mean the order *did* land,
            # and a blind retry is how one intended position becomes two. The caller
            # must reconcile against positionRisk instead.
            raise BrokerError(f"HTTP {exc.code} on {method} {path}: {detail}") from None
        except Exception as exc:                                   # noqa: BLE001
            raise BrokerError(
                f"{method} {path} failed, OUTCOME UNKNOWN — reconcile against "
                f"positionRisk before retrying: {exc}") from None

    # ---------------------------------------------------------------- reads

    def positions(self) -> list[dict]:
        """Live positions, straight from the venue. The source of truth."""
        raw = tabdeal._get("/r/fapi/v3/positionRisk", self._signed_query(), timeout=15)
        rows = raw if isinstance(raw, list) else []
        return [r for r in rows if abs(float(r.get("positionAmt") or 0)) > 0]

    def position_for(self, symbol: str) -> dict | None:
        return next((p for p in self.positions() if p.get("symbol") == symbol), None)

    def balance(self) -> list[dict]:
        return tabdeal._get("/r/fapi/v3/balance", self._signed_query(), timeout=15)

    def _signed_query(self) -> dict:
        ts = int(time.time() * 1000)
        q = {"timestamp": ts, "recvWindow": RECV_WINDOW_MS}
        sig = hmac.new(self._secret.encode(), urllib.parse.urlencode(q).encode(),
                       hashlib.sha256).hexdigest()
        return {**q, "signature": sig}

    # ---------------------------------------------------------------- writes

    def set_leverage(self, symbol: str, leverage: float) -> dict:
        return self._send("POST", "/fapi/v1/leverage",
                          {"symbol": symbol, "leverage": int(round(leverage))})

    def place_order(self, symbol: str, side: str, quantity: float, *,
                    order_type: str = "LIMIT", price: float | None = None,
                    time_in_force: str = "GTC",
                    client_order_id: str | None = None,
                    ref_price: float | None = None) -> dict:
        """Place a LIMIT or MARKET order. Those are the only two types Tabdeal takes.

        `ref_price` is used only to enforce the notional ceiling when placing a
        MARKET order, which has no price of its own.
        """
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise BrokerError(f"side must be BUY or SELL, got {side!r}")
        order_type = order_type.upper()
        if order_type not in ("LIMIT", "MARKET"):
            raise BrokerError(f"Tabdeal supports LIMIT and MARKET only, got "
                              f"{order_type!r}")
        if order_type == "LIMIT" and not price:
            raise BrokerError("a LIMIT order needs a price")
        if quantity <= 0:
            raise BrokerError(f"quantity must be positive, got {quantity}")

        mark = price or ref_price
        if mark and quantity * float(mark) > self.max_order_notional:
            raise BrokerError(
                f"order notional {quantity * float(mark):.2f} exceeds the "
                f"{self.max_order_notional:.2f} ceiling — refusing to send")

        return self._send("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": order_type,
            "quantity": _num(quantity),
            "price": _num(price) if order_type == "LIMIT" else None,
            "timeInForce": time_in_force if order_type == "LIMIT" else None,
            "newClientOrderId": client_order_id,
        })

    def cancel_order(self, symbol: str, order_id) -> dict:
        return self._send("DELETE", "/fapi/v1/order",
                          {"symbol": symbol, "orderId": order_id})

    def set_position_sl_tp(self, position_id, *, sl_price: float | None = None,
                           tp_price: float | None = None,
                           working_type: str = "MARK_PRICE",
                           symbol: str | None = None) -> dict:
        """The exchange-side safety net. Set this the moment a position exists.

        Triggering on MARK_PRICE rather than last traded price is deliberate: mark is
        derived across the book, so a single thin wick cannot take the position out
        of a thesis that is still intact.
        """
        if sl_price is None and tp_price is None:
            raise BrokerError("at least one of sl_price / tp_price is required")
        return self._send("POST", "/fapi/v1/positionSlTp", {
            "positionId": position_id, "symbol": symbol,
            "slPrice": _num(sl_price), "tpPrice": _num(tp_price),
            "workingType": working_type,
        })

    def close_position(self, symbol: str) -> dict:
        """Market-close the ENTIRE position for a symbol. There is no partial form."""
        return self._send("DELETE", "/fapi/v1/position", {"symbol": symbol})

    def transfer(self, amount: float, *, asset: str = "USDT",
                 to_futures: bool = True) -> dict:
        """Move funds between the spot and futures wallets. 2 = in, 1 = out."""
        return self._send("POST", "/fapi/v1/transfer", {
            "type": 2 if to_futures else 1, "amount": _num(amount), "asset": asset})

    # ---------------------------------------------------------------- composed

    def reduce_position(self, symbol: str, fraction: float = 0.5) -> dict:
        """Shed part of a position with an opposing order. The TP1 partial.

        The dangerous one. Without `reduceOnly` an opposing order does not know it is
        meant to reduce, so an oversized one opens a reverse position instead. Three
        defences, in order:

        1. Size from a *fresh* `positionRisk` read, never from our own records — the
           venue is the only thing that knows the current size.
        2. Refuse outright if the computed quantity is not strictly less than the
           live position.
        3. Verify afterwards that the position shrank and kept its sign. If it
           flipped, close it immediately rather than leaving a reversed position
           nobody intended.
        """
        if not 0 < fraction < 1:
            raise BrokerError(f"fraction must be strictly between 0 and 1, got "
                              f"{fraction}")
        live = self.position_for(symbol)
        if not live:
            raise BrokerError(f"no live position on {symbol} to reduce")

        amt = float(live["positionAmt"])
        is_long = amt > 0
        spec = _spec(symbol)
        qty = _round_down(abs(amt) * fraction, spec["step_size"])
        if qty <= 0:
            raise BrokerError(f"{symbol}: reduce size rounds to zero at step "
                              f"{spec['step_size']}")
        if qty >= abs(amt):
            raise BrokerError(f"{symbol}: reduce size {qty} is not smaller than the "
                              f"position {abs(amt)} — refusing, this would flip it")

        result = self.place_order(symbol, "SELL" if is_long else "BUY", qty,
                                  order_type="MARKET",
                                  ref_price=float(live.get("markPrice") or 0) or None)
        if self.dry_run:
            return {"reduced": qty, "of": abs(amt), "order": result}

        after = self.position_for(symbol)
        new_amt = float(after["positionAmt"]) if after else 0.0
        if new_amt * amt < 0:
            log.error("%s FLIPPED after a reduce (%s -> %s) — closing immediately",
                      symbol, amt, new_amt)
            self.close_position(symbol)
            raise BrokerError(f"{symbol}: reduce flipped the position and it was "
                              f"closed; investigate before trading this symbol again")
        if abs(new_amt) >= abs(amt):
            raise BrokerError(f"{symbol}: position did not shrink after a reduce "
                              f"({amt} -> {new_amt}) — reconcile manually")
        return {"reduced": abs(amt) - abs(new_amt), "remaining": abs(new_amt),
                "order": result}

    def flatten_all(self) -> dict:
        """Kill switch: close every open position, reporting per symbol.

        Deliberately independent of the scheduler and of any local record — it asks
        the venue what is open and closes that. It keeps going after a failure so one
        stuck symbol cannot strand the rest.
        """
        out = {"closed": [], "failed": []}
        for pos in self.positions():
            sym = pos.get("symbol")
            try:
                self.close_position(sym)
                out["closed"].append(sym)
            except Exception as exc:                               # noqa: BLE001
                log.error("flatten_all: %s failed: %s", sym, exc)
                out["failed"].append({"symbol": sym, "error": str(exc)})
        return out


# ------------------------------------------------------------------ helpers


def _num(value) -> str | None:
    """Format for the wire without scientific notation.

    `1e-05` is what Python gives for a cheap coin's tick, and an exchange that parses
    it as a decimal string will reject or misread it.
    """
    if value is None:
        return None
    return f"{float(value):.10f}".rstrip("0").rstrip(".") or "0"


def _round_down(qty: float, step: float | None) -> float:
    if not step or step <= 0:
        return qty
    import math
    return math.floor(qty / step + 1e-9) * step


def _spec(symbol: str) -> dict:
    from . import paper                                            # noqa: PLC0415
    return paper.contract_spec(symbol)
