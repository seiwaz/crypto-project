"""Local HTTP server: JSON API plus the static dashboard.

Binds to 127.0.0.1 and nothing else.

Built on `http.server` rather than FastAPI or Flask, deliberately. The whole
application then has zero third-party dependencies, so `./run.sh setup` needs no
wheels, works with no internet, and will still start in a year when today's pinned
versions have rotted. The routing this needs is a dozen read endpoints and a handful
of local POSTs — not worth a framework.

Nothing here proxies the exchange. The browser can never ask this server to call an
arbitrary Nobitex path: the only outbound calls are made by the scanner through the
skill's own client, and `guard.self_test()` runs at startup to confirm the read-only
allowlist still rejects every write path it should.

Credentials are never serialised into a response. `public_settings()` is the only
thing that reaches the browser, and .env values are not part of it.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import posixpath
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote, parse_qs

from . import config, demo, discover, exchange, guard, llm, report, scanner, store

log = logging.getLogger("server")

VERDICT_ORDER = {"TAKE": 0, "WATCH": 1, "INCOMPLETE": 2, "SKIP": 3, "ERROR": 4}

_stop_event = threading.Event()


# --------------------------------------------------------------------------------
# Payload assembly
# --------------------------------------------------------------------------------


def public_settings(settings: dict) -> dict:
    """The subset of settings the browser may see. Credentials live in .env and are
    not part of settings.json, but this stays an explicit allowlist so a future
    addition cannot leak by default."""
    return {
        "profile": settings["profile"],
        "capital": settings["capital"],
        "capital_currency": settings["capital_currency"],
        "risk_pct": settings["risk_pct"],
        "scan_interval_minutes": settings["scan_interval_minutes"],
        "language": settings["language"],
        "hold_hours": settings["hold_hours"],
        "exchange": settings["exchange"],
        "exchange_label": exchange.label(settings["exchange"]),
        "exchanges": list(exchange.SUPPORTED),
        "chart_candles": settings["chart_candles"],
    }


def _manual_checks(snapshot: dict, saved: dict, fetched_at: str | None) -> list[dict]:
    """The checks the skill could not settle, with our saved resolution state.

    A tick is only meaningful against the data it was made on. If the analysis has
    been refreshed since, the tick is reported stale and the UI treats the verdict as
    unconfirmed again — otherwise yesterday's confirmation quietly props up today's
    TAKE.
    """
    out = []
    for check in ((snapshot or {}).get("direction_score") or {}).get("checks", []):
        if check.get("long") is not None:
            continue
        key = check["check"]
        rec = saved.get(key) or {}
        resolved = bool(rec.get("resolved"))
        resolved_at = rec.get("resolved_at")
        stale = bool(resolved and resolved_at and fetched_at and resolved_at < fetched_at)
        out.append({
            "key": key,
            "observed": check.get("observed"),
            "resolved_by": check.get("resolved_by"),
            "resolved": resolved and not stale,
            "stale": stale,
            "note": rec.get("note"),
            "resolved_at": resolved_at,
        })
    return out


def build_card(row: dict, meta: dict, manual_saved: dict,
               commentary_rows: dict) -> dict:
    plan = json.loads(row["plan_json"]) if row.get("plan_json") else None
    snapshot = json.loads(row["snapshot_json"]) if row.get("snapshot_json") else None
    qual = (plan or {}).get("qualification") or {}
    checks = _manual_checks(snapshot, manual_saved, row.get("fetched_at"))
    unresolved = [c["key"] for c in checks if not c["resolved"]]

    return {
        "coin": row["coin"],
        "symbol": row.get("symbol"),
        "quote": row.get("quote"),
        "lot_size": meta.get("lot_size", 1),
        "lot_label": meta.get("lot_label"),
        "availability": meta.get("status"),
        "fetched_at": row.get("fetched_at"),
        "error": row.get("error"),
        "verdict": row.get("verdict"),
        "score": row.get("score"),
        "score_coverage": row.get("score_coverage"),
        "action": qual.get("action"),
        "side": row.get("side"),
        "side_tied": bool(row.get("side_tied")),
        "capital_used": row.get("capital_used"),
        "levels": (plan or {}).get("levels"),
        "sizing": (plan or {}).get("sizing"),
        "economics": (plan or {}).get("economics"),
        "management": (plan or {}).get("management"),
        "timeframes": (plan or {}).get("timeframes"),
        "gates": qual.get("gates", []),
        "gates_failed": qual.get("gates_failed", []),
        "score_breakdown": qual.get("score_breakdown", []),
        "missing_factors": qual.get("missing_factors", []),
        "blockers": (plan or {}).get("blockers", []),
        "warnings": (plan or {}).get("warnings", []),
        "direction_note": ((snapshot or {}).get("direction_score") or {}).get("note"),
        "direction_checks": ((snapshot or {}).get("direction_score") or {}).get("checks", []),
        "manual_checks": checks,
        "unresolved_manual": unresolved,
        # A TAKE with unconfirmed manual checks is not a confirmed TAKE.
        "provisional": bool(row.get("verdict") == "TAKE" and unresolved),
        "venue": (plan or {}).get("venue"),
        "funding": (snapshot or {}).get("funding"),
        "commentary": commentary_rows.get(row["coin"], {}),
        "caveat": qual.get("caveat"),
        "disclaimer": (plan or {}).get("disclaimer"),
    }


def sort_key(card: dict):
    verdict = card.get("verdict") or "ERROR"
    return (VERDICT_ORDER.get(verdict, 5), -(card.get("score") or 0), card["coin"])


def state_payload(lang: str | None = None) -> dict:
    """Assemble the board.

    `lang` is the language the *browser* is currently showing, which is not always
    settings.json's. The client keeps its choice in localStorage, so a fresh browser
    renders English while settings.json still says fa — and commentary is stored per
    language. Picking the row from settings.json served Persian text into an English
    page. The client's language wins; settings.json is only the default for a browser
    that has not chosen yet.
    """
    settings = config.load_settings()
    watchlist = config.load_watchlist()
    by_coin = {c["coin"]: c for c in watchlist.get("coins", [])}
    manual_all = store.all_manual_checks()

    commentary_rows: dict[str, dict] = {}
    lang = lang if lang in ("en", "fa") else settings.get("language", "en")
    active = settings.get("exchange")
    rows = store.latest_results(active)
    scan_by_coin = {r["coin"]: r["scan_id"] for r in rows}
    for coin in by_coin:
        rec = store.commentary_for(coin, lang, active, scan_by_coin.get(coin))
        if rec:
            commentary_rows[coin] = {
                "status": rec["status"], "text": rec["text"],
                "model": rec["model"], "reason": rec["reason"],
                "reason_code": rec["reason_code"],
                "reason_params": json.loads(rec["reason_params"] or "{}"),
                "created_at": rec["created_at"], "lang": lang,
            }

    cards = [build_card(row, by_coin.get(row["coin"], {}),
                        manual_all.get(row["coin"], {}), commentary_rows)
             for row in rows]
    cards.sort(key=sort_key)

    scanned = {c["coin"] for c in cards}
    # "Unavailable" means the market cannot be scanned at all — not merely that we
    # have not reached it yet. Conflating the two would report a coin awaiting its
    # first scan as unlisted.
    venue = exchange.adapter()
    scannable_coins = {c["coin"] for c in venue.scannable(watchlist)}
    unavailable, pending = [], []
    for c in watchlist.get("coins", []):
        entry = {"coin": c["coin"], "symbol": c.get("symbol"), "status": c["status"],
                 "reason": c.get("reason"), "market_closed": c.get("market_closed")}
        if c["coin"] not in scannable_coins:
            unavailable.append(entry)
        elif c["coin"] not in scanned:
            pending.append(entry)

    scan = store.latest_scan() or {}
    counts: dict[str, int] = {}
    for c in cards:
        counts[c.get("verdict") or "ERROR"] = counts.get(c.get("verdict") or "ERROR", 0) + 1

    return {
        "settings": public_settings(settings),
        "scan": {
            "id": scan.get("id"),
            "status": scan.get("status"),
            "started_at": scan.get("started_at"),
            "finished_at": scan.get("finished_at"),
            "total": scan.get("total"),
            "completed": scan.get("completed"),
            "failed": scan.get("failed"),
            "current_coin": scan.get("current_coin"),
            "note": scan.get("note"),
            "running": scanner.is_running() or scan.get("status") == "running",
            "usdt_irt": scan.get("usdt_irt"),
        },
        "watchlist": {
            "generated_at": watchlist.get("generated_at"),
            "requested": watchlist.get("requested"),
            "margin_detection": watchlist.get("margin_detection"),
            "exchange": watchlist.get("exchange"),
            "exchange_label": watchlist.get("exchange_label"),
            "scannable": len(venue.scannable(watchlist)),
        },
        "counts": counts,
        "coins": cards,
        "unavailable": unavailable,
        "pending": pending,
        "llm": (config.load_settings().get("llm") or {}).get("decision") or {},
        "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "CryptoScreener"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter, and into our logger
        log.debug("%s - %s", self.address_string(), fmt % args)

    def handle_one_request(self):
        """Swallow the client hanging up mid-request.

        A browser closing a keep-alive socket raises inside the base class's own
        request-line read, before do_GET is ever entered, so the try/except in the
        verbs cannot see it. Left alone it prints a full traceback per navigation and
        buries real errors in the log.
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    # -- helpers ----------------------------------------------------------------

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=float).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- routing ----------------------------------------------------------------

    # A client that navigates away mid-response resets the socket during the write.
    # That is not a server error and there is nobody left to send a 500 to.
    DISCONNECTS = (BrokenPipeError, ConnectionResetError, TimeoutError)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/"):
                return self._api_get(path)
            return self._static(path)
        except self.DISCONNECTS:
            self.close_connection = True
        except Exception:
            log.exception("GET %s failed", path)
            self._safe_error(path)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            return self._api_post(path)
        except self.DISCONNECTS:
            self.close_connection = True
        except Exception:
            log.exception("POST %s failed", path)
            self._safe_error(path)

    def _safe_error(self, path: str):
        """Report a 500 without raising a second exception on a dead socket."""
        try:
            self._error(500, "internal error")
        except self.DISCONNECTS:
            self.close_connection = True

    def _api_get(self, path: str):
        if path == "/api/state":
            wanted = parse_qs(urlparse(self.path).query).get("lang", [None])[0]
            return self._json(state_payload(wanted))
        if path == "/api/health":
            return self._json({"ok": True, "read_only": True,
                               "guard_failures": guard.self_test()})
        if path == "/api/watchlist":
            return self._json(config.load_watchlist())
        if path == "/api/llm":
            return self._json((config.load_settings().get("llm") or {}))
        if path == "/api/live":
            from . import live                                 # noqa: PLC0415
            return self._json(live.state())

        if path == "/api/live/history":
            # Sampled price/PnL series per position, for the dashboard chart.
            from . import live                                 # noqa: PLC0415
            q = parse_qs(urlparse(self.path).query)
            try:
                keep = int((q.get("closed") or ["5"])[0])
            except (TypeError, ValueError):
                keep = 5
            return self._json(live.history(include_closed=keep))

        if path == "/api/demo":
            return self._json(demo.state())
        if path == "/api/demo/report":
            return self._json(report.build())

        parts = [p for p in path.split("/") if p]
        # /api/coin/<COIN>[/series|/history]
        if len(parts) >= 3 and parts[1] == "coin":
            coin = unquote(parts[2]).upper()
            if len(parts) == 3:
                return self._coin_detail(coin)
            if parts[3] == "series":
                series = store.series_for(coin, "decision")
                if not series:
                    return self._error(404, f"no stored chart data for {coin}")
                return self._json(series)
            if parts[3] == "history":
                return self._json({"coin": coin, "history": store.history_for(coin)})
        return self._error(404, "unknown endpoint")

    def _coin_detail(self, coin: str):
        active = config.load_settings().get("exchange")
        row = store.result_for(coin, active) or store.result_for(coin)
        if not row:
            return self._error(404, f"no result stored for {coin}")
        watchlist = config.load_watchlist()
        meta = next((c for c in watchlist.get("coins", []) if c["coin"] == coin), {})
        wanted = parse_qs(urlparse(self.path).query).get("lang", [None])[0]
        lang = wanted if wanted in ("en", "fa") else \
            config.load_settings().get("language", "en")
        rec = store.commentary_for(coin, lang, active, row["scan_id"])
        card = build_card(row, meta, store.manual_checks_for(coin),
                          {coin: dict(rec) if rec else {}})
        card["snapshot"] = json.loads(row["snapshot_json"]) if row["snapshot_json"] else None
        card["plan"] = json.loads(row["plan_json"]) if row["plan_json"] else None
        card["history"] = store.history_for(coin)
        return self._json(card)

    def _api_post(self, path: str):
        body = self._body()

        if path == "/api/settings":
            allowed = {"profile", "capital", "capital_currency", "risk_pct",
                       "scan_interval_minutes", "language", "hold_hours", "exchange"}
            patch = {k: v for k, v in body.items() if k in allowed}
            if "profile" in patch and patch["profile"] not in ("scalp", "intraday", "swing"):
                return self._error(400, "profile must be scalp, intraday or swing")
            if "exchange" in patch and patch["exchange"] not in exchange.SUPPORTED:
                return self._error(400,
                                   f"exchange must be one of {', '.join(exchange.SUPPORTED)}")
            for numeric in ("capital", "risk_pct", "hold_hours", "scan_interval_minutes"):
                if numeric in patch:
                    try:
                        patch[numeric] = float(patch[numeric])
                    except (TypeError, ValueError):
                        return self._error(400, f"{numeric} must be a number")
                    if patch[numeric] <= 0:
                        return self._error(400, f"{numeric} must be positive")
            try:
                settings = config.save_settings(patch)
            except OSError as exc:
                # This failed silently for days: the systemd unit's ProtectSystem=
                # strict only granted ReadWritePaths on var/, so writing config/
                # settings.json raised "Read-only file system", the generic handler
                # turned it into a bare 500, and the UI just re-read the old value
                # and snapped the control back. A control that looks like it works
                # and does nothing is worse than one that reports why.
                log.error("could not persist settings: %s", exc)
                return self._error(500, f"could not write settings.json: {exc}. "
                                        f"The service may lack write access to "
                                        f"config/ (systemd ReadWritePaths).")
            return self._json({"settings": public_settings(settings)})

        if path == "/api/scan":
            coins = body.get("coins")
            if coins is not None and not isinstance(coins, list):
                return self._error(400, "coins must be a list")
            started = scanner.start_background([str(c).upper() for c in coins] if coins else None)
            return self._json({"started": started,
                               "reason": None if started else "a scan is already running"})

        if path == "/api/scan/cancel":
            scanner.request_cancel()
            return self._json({"cancelling": True})

        # The demo mutates only local rows. There is no exchange write behind any of
        # these — `demo` reads prices through the read-only allowlist and persists to
        # SQLite, and no order endpoint is reachable from this process.
        if path == "/api/demo/cycle":
            cycle = demo.cycle()
            filled = demo.try_fill_slots()
            return self._json({"cycle": cycle, "fill": filled,
                               "state": demo.state()})

        if path == "/api/live/flatten":
            # The kill switch. Closes every open position on the venue, whatever the
            # local records think, and works even if the engine loop is wedged.
            from . import live, tabdeal_broker                 # noqa: PLC0415
            del live
            try:
                return self._json(tabdeal_broker.TabdealBroker(
                    dry_run=False).flatten_all())
            except Exception as exc:                           # noqa: BLE001
                return self._error(500, f"flatten failed: {exc}")

        if path == "/api/demo/reset":
            # Wiping the account (balance, positions, trade history) is the single
            # most destructive thing this public, unauthenticated dashboard can do -
            # it throws away the sample the whole project exists to collect. The rest
            # of the API stays open by design (see CLAUDE.md), but this one action
            # gets a password gate. Configured server-side only, in settings.json's
            # "demo" block - never in git, never returned by public_settings(), so it
            # can't leak through the API or the public repo.
            cfg = demo.settings()
            required = cfg.get("reset_password")
            if required and str(body.get("password") or "") != str(required):
                return self._error(403, "wrong password")
            store.paper_init(exchange=cfg["exchange"], capital=cfg["capital"],
                             slots=cfg["slots"], heat_cap_pct=cfg["heat_cap_pct"],
                             reset=True)
            store.set_kv("demo.last_fill", {})
            return self._json(demo.state())

        if path == "/api/manual-check":
            coin = str(body.get("coin", "")).upper()
            key = body.get("key")
            if not coin or not key:
                return self._error(400, "coin and key are required")
            rec = store.set_manual_check(coin, key, bool(body.get("resolved")),
                                         body.get("note"))
            return self._json(rec)

        if path == "/api/commentary":
            return self._commentary(body)

        if path == "/api/llm/reassess":
            return self._json(llm.ensure_decision(force=True))

        return self._error(404, "unknown endpoint")

    def _commentary(self, body: dict):
        coin = str(body.get("coin", "")).upper()
        row = store.result_for(coin)
        if not row or not row.get("plan_json"):
            return self._error(404, f"no analysis stored for {coin}")
        settings = config.load_settings()
        lang = body.get("lang") or settings.get("language", "en")
        plan = json.loads(row["plan_json"])
        snapshot = json.loads(row["snapshot_json"]) if row["snapshot_json"] else {}
        checks = _manual_checks(snapshot, store.manual_checks_for(coin),
                                row.get("fetched_at"))
        unresolved = [c["key"] for c in checks if not c["resolved"]]
        out = llm.commentary(coin, plan, unresolved, lang)
        store.save_commentary(coin, lang, scan_id=row["scan_id"], text=out["text"],
                              model=out["model"], status=out["status"],
                              reason=out["reason"], reason_code=out.get("reason_code"),
                              reason_params=out.get("reason_params"),
                              exchange=row.get("exchange"))
        out["lang"] = lang
        return self._json(out)

    # -- static -----------------------------------------------------------------

    def _static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        # posixpath.normpath collapses ../ before we ever touch the filesystem.
        clean = posixpath.normpath(unquote(path)).lstrip("/")
        target = (config.WEB_DIR / clean).resolve()
        try:
            target.relative_to(config.WEB_DIR.resolve())
        except ValueError:
            return self._error(403, "forbidden")
        if not target.is_file():
            return self._error(404, "not found")

        ctype, _ = mimetypes.guess_type(str(target))
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        # Vendored assets are immutable; app files must never be served from cache.
        #
        # "no-cache" means revalidate, but this server sends no ETag or Last-Modified
        # for a validator to revalidate against, so a browser is free to reuse what it
        # has — Safari does. The symptom is a change that is live on disk, correct in
        # the API, and invisible on screen. "no-store" removes the ambiguity, and on a
        # loopback dashboard serving a few tens of kilobytes it costs nothing.
        if "/vendor/" in path or "/fonts/" in path:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)


def serve(host: str | None = None, port: int | None = None,
          with_scheduler: bool = True) -> None:
    import os

    config.load_dotenv()
    config.ensure_dirs()
    store.init()
    store.mark_stale_scans()

    failures = guard.self_test()
    if failures:
        raise SystemExit("read-only guard self-test FAILED:\n  "
                         + "\n  ".join(failures))
    log.info("read-only guard: ok")

    llm.ensure_decision()

    host = host or os.environ.get("BIND_HOST", "127.0.0.1")
    port = int(port or os.environ.get("BIND_PORT", 8787))

    # Loopback stays the default and the refusal stays in place. Binding anywhere
    # else needs ALLOW_PUBLIC_BIND=1 set deliberately, because this dashboard has no
    # authentication of any kind: anyone who can reach the port can reset the demo
    # account, change capital and risk, and start scans. It cannot place an exchange
    # order and it serves no credentials, but it is otherwise wide open.
    #
    # Requiring an explicit opt-in means a public bind is always something someone
    # chose, never something a stray BIND_HOST in an environment file caused.
    if host not in ("127.0.0.1", "localhost", "::1"):
        if os.environ.get("ALLOW_PUBLIC_BIND") != "1":
            raise SystemExit(
                f"refusing to bind {host}: this dashboard is loopback-only.\n"
                "  It has no authentication. To expose it anyway, set "
                "ALLOW_PUBLIC_BIND=1 —\n"
                "  and put it behind a reverse proxy with auth, or restrict the port "
                "by source IP.")
        log.warning("PUBLIC BIND: %s:%s — no authentication; anyone who can reach "
                    "this port can reset the demo account and change settings",
                    host, port)

    if with_scheduler:
        threading.Thread(target=scanner.scheduler_loop, args=(_stop_event,),
                         name="scheduler", daemon=True).start()
        # The demo manages its own positions on a timer. Without this it would only
        # mark to market when someone opened the tab, and an exit would be recorded
        # at whatever price the page happened to load at rather than where the level
        # was actually hit.
        threading.Thread(target=demo.scheduler_loop, args=(_stop_event,),
                         name="demo", daemon=True).start()
        # The live engine. Started unconditionally but inert: its loop checks
        # demo.live_trading every cycle and does nothing while that is false, so
        # arming and disarming take effect without a restart. Every write it can
        # make still has to pass the guard's write allowlist.
        from . import live                                     # noqa: PLC0415
        threading.Thread(target=live.scheduler_loop, args=(_stop_event,),
                         name="live", daemon=True).start()
        if live.settings()["enabled"]:
            log.warning("LIVE TRADING IS ARMED — this process can place real orders "
                        "on Tabdeal with real funds")

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    log.info("dashboard on http://%s:%s", host, port)
    print(f"  Dashboard: http://{host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        httpd.server_close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    serve()
