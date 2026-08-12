"""Paths, settings, and credential loading.

Credentials are read here and handed to subprocesses through the environment only.
Nothing in this module is ever serialised into an API response — see server.py's
`public_settings()` for what the browser is allowed to see.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
WEB_DIR = ROOT / "web"
VAR_DIR = ROOT / "var"
LOG_DIR = VAR_DIR / "logs"
DB_PATH = VAR_DIR / "screener.sqlite3"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
WATCHLIST_PATH = CONFIG_DIR / "watchlist.json"

# The coins the user asked for, in their original order. This list is the *request*;
# config/watchlist.json is the *answer* — what each coin actually resolved to on
# Nobitex. Never treat these as tradeable symbols directly.
REQUESTED_COINS = [
    "BTC", "ETH", "BNB", "XRP", "SOL", "TRX", "HYPE", "DOGE", "ZEC", "ADA",
    "LINK", "XLM", "BCH", "GRAM", "LTC", "HBAR", "SUI", "AVAX", "SHIB", "TAO",
    "CRO", "UNI", "NEAR", "WLFI", "ONDO", "ASTER", "MNT", "AAVE", "DOT", "ICP",
    "WLD", "PEPE", "PUMP", "MORPHO", "ETC", "ENA", "POL", "ATOM", "ALGO", "KAS",
    "RENDER", "FIL", "JUP", "ARB", "APT", "INJ", "CRV", "PENGU", "VET", "TIA",
]

DEFAULT_SKILL_DIR = Path.home() / ".claude" / "skills" / "crypto-leverage-trade-plan"

DEFAULTS = {
    "profile": "intraday",
    # Sized to sit inside Nobitex's pool depth: at 10,000 USDT even BTC fails the
    # liquidity-depth gate, which would report your position size as a market defect.
    "capital": 1000.0,
    # Which currency `capital` is denominated in. Markets quoted in the other
    # currency are converted at the live USDTIRT rate, which is recorded per scan.
    "capital_currency": "USDT",
    "risk_pct": 1.0,
    "scan_interval_minutes": 15,
    "language": "en",
    "exchange": "nobitex",
    # One 8h renewal period. This feeds the renewal-fee calculation and moves
    # verdicts materially — at 24h the extra charges fail the cost gate on their own.
    "hold_hours": 8.0,
    "account_level": None,
    "candle_count": 300,
    "chart_candles": 180,
    "keep_scans": 40,
    "skill_dir": str(DEFAULT_SKILL_DIR),
    "llm": {
        "enabled": True,
        "model": None,
        "host": "http://127.0.0.1:11434",
        "timeout_seconds": 120,
        # Commentary is generated on demand rather than for all 50 coins per scan:
        # this machine has no GPU, so a per-scan pass would take longer than the scan.
        "mode": "on_demand",
        "decision": None,
    },
}

_lock = threading.Lock()


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            return _deep_merge(DEFAULTS, json.load(fh))
    except FileNotFoundError:
        return dict(DEFAULTS)
    except json.JSONDecodeError:
        return dict(DEFAULTS)


def save_settings(patch: dict) -> dict:
    """Merge `patch` into settings.json and return the result."""
    with _lock:
        current = load_settings()
        merged = _deep_merge(current, patch)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2, ensure_ascii=False)
        tmp.replace(SETTINGS_PATH)
        return merged


def load_watchlist() -> dict:
    try:
        with open(WATCHLIST_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def skill_dir() -> Path:
    return Path(os.environ.get("CRYPTO_SKILL_DIR") or load_settings()["skill_dir"])


def load_dotenv(path: Path | None = None) -> None:
    """Read .env into os.environ without overwriting anything already set."""
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def credential_status() -> dict:
    """Whether credentials are present — never their values."""
    return {
        "api_key_set": bool(os.environ.get("NOBITEX_API_KEY")),
        "api_secret_set": bool(os.environ.get("NOBITEX_API_SECRET")),
        "token_set": bool(os.environ.get("NOBITEX_TOKEN")),
    }


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, VAR_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
