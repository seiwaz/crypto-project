"""Adapter around the crypto-leverage-trade-plan skill.

This module is the *only* place that talks to the analysis engine, and it does so by
running the skill's own scripts as subprocesses. It deliberately contains no trading
maths: no ATR, no position sizing, no scoring. If you are about to add a formula here,
add a flag to the skill's CLI instead — one implementation, tested, in one place.

The single exception is `ema_series()`, which calls the skill's own `trade_plan.ema`
once per bar to turn its scalar EMA into a series for the chart overlay. That reuses
their function rather than restating the formula, so the line on the chart and the
number in the verdict can never disagree.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import time
import threading
from pathlib import Path

from . import config

# The skill's client serialises its own requests with a ~1.1s gap, but that state
# lives inside one process. We spawn a process per coin, so the gap has to be
# re-enforced out here or the boundary between two coins can burst.
_MIN_GAP_SECONDS = 1.2
_gap_lock = threading.Lock()
_last_call_finished = 0.0


class SkillError(RuntimeError):
    pass


def scripts_dir() -> Path:
    return config.skill_dir() / "scripts"


def check_installed() -> tuple[bool, str]:
    d = scripts_dir()
    missing = [n for n in ("nobitex_api.py", "trade_plan.py") if not (d / n).exists()]
    if missing:
        return False, f"missing {', '.join(missing)} under {d}"
    return True, str(d)


def _python() -> str:
    return sys.executable or "python3"


def _env() -> dict:
    """Subprocess environment. Credentials travel here, never in argv — argv is
    visible in shell history and to any process listing."""
    env = dict(os.environ)
    env.setdefault("NOBITEX_USER_AGENT", "TraderBot/LocalScreener")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _respect_gap() -> None:
    with _gap_lock:
        global _last_call_finished
        wait = _MIN_GAP_SECONDS - (time.monotonic() - _last_call_finished)
        if wait > 0:
            time.sleep(wait)


def _mark_call_finished() -> None:
    global _last_call_finished
    _last_call_finished = time.monotonic()


# Only the API client talks to the network; trade_plan.py is pure computation, so
# rate-limiting it just adds sleep to every scan. On a 47-coin Toobit pass that was
# roughly two minutes of doing nothing.
_NETWORK_SCRIPTS = {"nobitex_api.py"}


def _run(script: str, args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    ok, detail = check_installed()
    if not ok:
        raise SkillError(f"crypto-leverage-trade-plan skill not found: {detail}")
    cmd = [_python(), str(scripts_dir() / script), *args]
    throttled = script in _NETWORK_SCRIPTS
    if throttled:
        _respect_gap()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=_env(),
            cwd=str(scripts_dir()),
        )
    except subprocess.TimeoutExpired as exc:
        if throttled:
            _mark_call_finished()
        raise SkillError(f"{script} timed out after {timeout}s") from exc
    if throttled:
        _mark_call_finished()
    return proc


def auth_check() -> dict:
    proc = _run("nobitex_api.py", ["auth-check"], timeout=90)
    if proc.returncode != 0:
        raise SkillError(_tail(proc.stderr) or f"auth-check exited {proc.returncode}")
    return json.loads(proc.stdout)


def market_stats_all() -> dict:
    """Every live market — `GET /market/stats` with no params. Used by discovery."""
    return _via_client("market_stats_all")


def margin_fee_rates() -> dict:
    """`GET /margin/fee-rates` — doubles as the list of margin-enabled currencies.

    Used in place of /margin/v2/delegation-limit, which rejects every parameter
    spelling we tried and so cannot enumerate. See discover.py for the detail.
    """
    return _via_client("margin_fee_rates")


def _via_client(what: str) -> dict:
    """Call the skill's client in a child process.

    The skill's CLI has no bulk-stats subcommand, so discovery imports *their*
    NobitexClient rather than issuing its own request. The read-only allowlist and
    the rate limiter therefore still apply — this is not a way around them.
    """
    code = _CLIENT_SNIPPETS[what]
    _respect_gap()
    proc = subprocess.run(
        [_python(), "-c", code], capture_output=True, text=True, timeout=180,
        env=_env(), cwd=str(scripts_dir()),
    )
    _mark_call_finished()
    if proc.returncode != 0:
        raise SkillError(_tail(proc.stderr) or f"client call {what} failed")
    return json.loads(proc.stdout)


_CLIENT_PREAMBLE = (
    "import json,sys;"
    "sys.path.insert(0,'.');"
    "import nobitex_api as n;"
    "c=n.NobitexClient(n.load_credentials());"
)

_CLIENT_SNIPPETS = {
    "market_stats_all": _CLIENT_PREAMBLE + "print(json.dumps(c.market_stats()))",
    "margin_fee_rates": _CLIENT_PREAMBLE + "print(json.dumps(c.margin_fee_rates()))",
}


def analyze(symbol: str, profile: str, *, capital: float, risk_pct: float,
            count: int = 300, exchange: str = "nobitex", hold_hours: float = 0.0,
            account_level: int | None = None,
            want_candles: bool = True) -> tuple[dict, dict, dict, dict]:
    """Snapshot then plan, in that order, sharing one temp directory.

    Returns (snapshot, plan, candles_by_role, side_info). The planner reads the
    snapshot from disk, so both calls have to see the same file.
    """
    with tempfile.TemporaryDirectory(prefix="scan-") as tmp:
        out = Path(tmp) / "snap.json"
        args = ["snapshot", "--symbol", symbol, "--profile", profile,
                "--count", str(count), "--out", str(out)]
        if want_candles:
            args += ["--save-csv", str(Path(tmp) / "c")]
        proc = _run("nobitex_api.py", args, timeout=240)
        if proc.returncode != 0 or not out.exists():
            raise SkillError(_tail(proc.stderr) or _tail(proc.stdout)
                             or f"snapshot exited {proc.returncode}")
        snap = json.loads(out.read_text(encoding="utf-8"))
        candles = _collect_csvs(Path(tmp), snap) if want_candles else {}

        side, side_info = side_from_direction(snap)
        built = plan(str(out), side, capital, profile=profile, risk_pct=risk_pct,
                     exchange=exchange, hold_hours=hold_hours,
                     account_level=account_level)
    return snap, built, candles, side_info


def plan(snapshot_path: str, side: str, capital: float, *, profile: str,
         risk_pct: float, exchange: str = "nobitex", hold_hours: float = 0.0,
         account_level: int | None = None,
         leverage_cap: float | None = None,
         leverage: float | None = None,
         max_margin_pct: float | None = None,
         tp1_r: float | None = None, tp2_r: float | None = None,
         atr_mult: float | None = None) -> dict:
    args = ["plan", "--snapshot", snapshot_path, "--side", side,
            "--capital", f"{capital:.10g}", "--profile", profile,
            "--risk-pct", str(risk_pct), "--exchange", exchange,
            "--hold-hours", str(hold_hours), "--json"]
    if max_margin_pct:
        args += ["--max-margin-pct", f"{max_margin_pct:.6g}"]
    # The profile's TP1 is 1.5R on intraday, which this venue never reached in 30
    # trades. Overridable so the target can be set from what the instrument actually
    # travels rather than from a constant.
    if tp1_r:
        args += ["--tp1-r", f"{tp1_r:.6g}"]
    if tp2_r:
        args += ["--tp2-r", f"{tp2_r:.6g}"]
    # The stop multiplier decides R, and therefore whether the target is reachable in
    # the holding period at all. The profile default assumes the profile's own hold.
    if atr_mult:
        args += ["--atr-mult", f"{atr_mult:.6g}"]
    if account_level:
        args += ["--account-level", str(account_level)]
    if leverage_cap:
        args += ["--leverage-cap", f"{leverage_cap:.6g}"]
    if leverage:
        args += ["--leverage", f"{leverage:.6g}"]
    proc = _run("trade_plan.py", args, timeout=120)
    if proc.returncode != 0:
        raise SkillError(_tail(proc.stderr) or f"plan exited {proc.returncode}")
    return json.loads(proc.stdout)


def side_from_direction(snap: dict) -> tuple[str, dict]:
    """Pick the side the snapshot's own direction_score favours.

    Ties resolve to the side with more automated agreement; a genuine tie returns
    'long' but is reported as `tied` so the UI can say the direction is unconvincing
    rather than implying a real signal.
    """
    ds = snap.get("direction_score") or {}
    longs = ds.get("long_score")
    shorts = ds.get("short_score")
    if longs is None or shorts is None:
        return "long", {"side": "long", "tied": True, "reason": "no direction score"}
    if shorts > longs:
        return "short", {"side": "short", "tied": False,
                         "long_score": longs, "short_score": shorts}
    return "long", {"side": "long", "tied": longs == shorts,
                    "long_score": longs, "short_score": shorts}


# --------------------------------------------------------------------------------
# Chart data
# --------------------------------------------------------------------------------

_EMA_PERIODS = (20, 50, 200)


def _load_trade_plan_module():
    d = str(scripts_dir())
    if d not in sys.path:
        sys.path.insert(0, d)
    import trade_plan  # noqa: PLC0415
    return trade_plan


def _load_api_module():
    """The skill's nobitex_api module.

    Despite the name it also holds two venue-agnostic functions — `compute_indicators`
    and `score_direction` — that take plain OHLCV rows. The Toobit adapter imports
    them rather than growing a second copy of the indicator set, so both venues score
    a chart identically.
    """
    d = str(scripts_dir())
    if d not in sys.path:
        sys.path.insert(0, d)
    import nobitex_api  # noqa: PLC0415
    return nobitex_api


def compute_indicators(rows: list[dict]) -> dict:
    """ATR/EMA/RSI/RVOL/VWAP/structure for one timeframe — the skill's own."""
    return _load_api_module().compute_indicators(rows)


def score_direction(profile: str, tfs: dict) -> dict:
    """The skill's direction scoring, including its MANUAL markers."""
    return _load_api_module().score_direction(profile, tfs)


def ema_series(closes: list[float], period: int) -> list[float | None]:
    """EMA at every bar, computed with the skill's own `ema()`.

    `ema()` is causal — the value at bar i depends only on closes[:i+1] — so calling
    it on each prefix reproduces the series exactly, without restating the formula.
    Bars before the seed window are None, which is honest: there is no EMA yet.
    """
    tp = _load_trade_plan_module()
    return [None] * (period - 1) + [
        tp.ema(closes[: i + 1], period) for i in range(period - 1, len(closes))
    ]


def _collect_csvs(tmp: Path, snap: dict) -> dict:
    """Read the per-timeframe CSVs the snapshot wrote and attach EMA overlays."""
    out = {}
    for role in ("decision", "entry", "bias", "atr"):
        tf_meta = (snap.get("timeframes") or {}).get(role) or {}
        res = tf_meta.get("resolution")
        if not res:
            continue
        path = tmp / f"c_{role}_{res}.csv"
        if not path.exists():
            continue
        rows = _read_csv(path)
        if not rows:
            continue
        out[role] = {
            "timeframe": tf_meta.get("timeframe"),
            "resolution": res,
            "candles": rows,
        }
    dec = out.get("decision")
    if dec:
        closes = [r["close"] for r in dec["candles"]]
        dec["ema"] = {f"ema{p}": ema_series(closes, p) for p in _EMA_PERIODS}
    return out


def _read_csv(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                rows.append({
                    "timestamp": r["timestamp"],
                    "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                    "volume": float(r.get("volume") or 0.0),
                })
            except (KeyError, TypeError, ValueError):
                continue
    return rows


def _tail(text: str | None, limit: int = 400) -> str:
    if not text:
        return ""
    text = text.strip()
    return text[-limit:]
