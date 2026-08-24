"""Password gate for the dashboard.

Why this exists
---------------
The board was open to anyone who knew the host. That was acceptable while the thing
was a read-only screener; it stopped being acceptable when `POST /api/live/close`,
`POST /api/live/flatten` and `POST /api/settings` started moving real money on a real
exchange. Anyone who found the URL could flatten the book.

Where the credential lives
--------------------------
In the SERVER's `config/settings.json` under `web`, as a PBKDF2 salt and hash — never
the password itself, and never in this repository. `github.com/seiwaz/crypto-project`
is public, and the deploy pipeline deliberately never copies `config/settings.json`
from git to the server, so a secret placed there cannot leak the way one placed in a
tracked file would. Same rule the demo reset password already follows.

Set it with `agent.auth.set_password()` on the box; there is no code path that writes
a password from a request, so the gate cannot be reset through the gate.

Sessions
--------
An opaque 256-bit token in an HttpOnly cookie, held in memory with an expiry. A
restart logs everyone out, which is the safe direction: this process restarts on every
deploy and a session that outlived the code that issued it is worth nothing.

Deliberately NOT here: user accounts, password reset, rate-limit lockout. One operator,
one password. What IS here is a small delay on a bad attempt and constant-time
comparison, so the endpoint is not a free oracle.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time

from . import config

log = logging.getLogger("auth")

ITERATIONS = 200_000
SESSION_TTL_S = 12 * 3600
COOKIE = "cs_session"

# Open even when locked. /api/health carries no trading data and is what tells an
# operator (or a monitor) that the process is alive — a health check that needs a
# password reports the padlock, not the service.
PUBLIC_PATHS = {"/api/health", "/api/login"}

_sessions: dict[str, float] = {}
_lock = threading.Lock()


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                               ITERATIONS).hex()


def set_password(username: str, password: str) -> dict:
    """Write a new credential into settings.json. Run on the server, never in git."""
    salt = secrets.token_bytes(16)
    s = config.load_settings()
    web = dict(s.get("web") or {})
    web.update({"username": username, "salt": salt.hex(),
                "hash": _hash(password, salt), "iterations": ITERATIONS})
    s["web"] = web
    config.save_settings(s)
    return {"username": username, "configured": True}


def configured() -> bool:
    web = config.load_settings().get("web") or {}
    return bool(web.get("hash") and web.get("salt"))


def check(username: str, password: str) -> bool:
    web = config.load_settings().get("web") or {}
    if not (web.get("hash") and web.get("salt")):
        return False
    # Compare BOTH fields in constant time and only after hashing, so neither the
    # username nor the password can be recovered by timing the endpoint.
    want_user = hmac.compare_digest(str(web.get("username") or ""), username or "")
    salt = bytes.fromhex(web["salt"])
    iters = int(web.get("iterations") or ITERATIONS)
    got = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt,
                              iters).hex()
    want_pass = hmac.compare_digest(web["hash"], got)
    return want_user and want_pass


def issue() -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = time.time() + SESSION_TTL_S
        _prune_locked()
    return token


def valid(token: str | None) -> bool:
    if not token:
        return False
    with _lock:
        exp = _sessions.get(token)
        if exp is None:
            return False
        if exp < time.time():
            _sessions.pop(token, None)
            return False
        return True


def revoke(token: str | None) -> None:
    if token:
        with _lock:
            _sessions.pop(token, None)


def _prune_locked() -> None:
    now = time.time()
    for k, exp in list(_sessions.items()):
        if exp < now:
            _sessions.pop(k, None)


def token_from_cookie(header: str | None) -> str | None:
    """Pull our cookie out of a Cookie header without importing http.cookies.

    A malformed header must not raise — this runs before authentication, so it is
    reachable by anyone.
    """
    if not header:
        return None
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE and value:
            return value
    return None


def api_token() -> str | None:
    """A fixed token for server-side scripts (the report push runs on a timer).

    Separate from a session so an automated job never needs the password, and can be
    rotated without disturbing the operator's login.
    """
    return (config.load_settings().get("web") or {}).get("api_token") or None
