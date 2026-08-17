#!/usr/bin/env bash
# Lifecycle for the local crypto screener.
#
#   ./run.sh setup      venv, .env, symbol discovery, credential and Ollama checks
#   ./run.sh start      start the dashboard (scanner runs inside it)
#   ./run.sh stop       graceful shutdown
#   ./run.sh restart
#   ./run.sh status     running?, PID, last scan, Ollama state, coin count
#   ./run.sh logs [-f]
#   ./run.sh scan-once  one scan in the foreground, for debugging
#   ./run.sh journal    demo account, open positions, closed trades, report
#   ./run.sh agents     what each agent is doing right now
#   ./run.sh watch      timed observation run that writes a journal when it ends
#   ./run.sh demo on|off       enable/disable the paper trader
#   ./run.sh scanner on|off    enable/disable scheduled scanning
#
# Every process this project runs is owned by this script: the server (which hosts
# the scanner and demo threads) and the watcher. `stop` stops all of them. Nothing
# should ever be launched by hand — an unsupervised process still writing to the
# database is how a journal ends up describing an account nobody is managing.
#
# The scan scheduler runs as a thread inside the server process rather than as a
# second daemon: one process to supervise, one PID to reap, and no way for the two
# to disagree about the database. `scan-once` still runs standalone for debugging.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv"
PY="$VENV/bin/python"
VAR="$ROOT/var"
LOGS="$VAR/logs"
PIDFILE="$VAR/server.pid"
LOGFILE="$LOGS/server.log"
WATCHPID="$VAR/watch.pid"
WATCHLOG="$LOGS/watch.log"

BIND_HOST="${BIND_HOST:-127.0.0.1}"
BIND_PORT="${BIND_PORT:-8787}"

c_reset=$'\033[0m'; c_dim=$'\033[2m'; c_red=$'\033[31m'
c_green=$'\033[32m'; c_yellow=$'\033[33m'; c_bold=$'\033[1m'

say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$c_green" "$c_reset" "$*"; }
warn() { printf '%s!%s %s\n' "$c_yellow" "$c_reset" "$*"; }
die()  { printf '%s✗%s %s\n' "$c_red" "$c_reset" "$*" >&2; exit 1; }

find_python() {
  for candidate in python3.12 python3.13 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        command -v "$candidate"; return 0
      fi
    fi
  done
  return 1
}

ensure_venv() {
  if [[ ! -x "$PY" ]]; then
    local base; base="$(find_python)" || die "need Python 3.10+ on PATH"
    say "Creating virtualenv with $base"
    "$base" -m venv "$VENV"
  fi
}

# Returns 0 and echoes the PID when the server is genuinely running.
# A PID file left behind by a killed process is cleaned up rather than trusted —
# refusing to start because of a stale file is a bad way to greet someone.
pid_from() {
  # pid_from <pidfile> <command-substring>
  local file="$1" match="$2"
  [[ -f "$file" ]] || return 1
  local pid; pid="$(cat "$file" 2>/dev/null || true)"
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$file"; return 1
  fi
  # Confirm it is actually ours and not a recycled PID.
  if ! ps -p "$pid" -o command= 2>/dev/null | grep -q "$match"; then
    rm -f "$file"; return 1
  fi
  printf '%s' "$pid"; return 0
}

running_pid() { pid_from "$PIDFILE" 'agent.server'; }
watch_pid()   { pid_from "$WATCHPID" 'agent.watch'; }

# stop_pid <pidfile> <label> — TERM, wait, then KILL.
stop_pid() {
  local file="$1" label="$2" pid
  pid="$(cat "$file" 2>/dev/null || true)"
  [[ -n "$pid" ]] || { rm -f "$file"; return 1; }
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 40); do
    kill -0 "$pid" 2>/dev/null || { rm -f "$file"; ok "$label stopped"; return 0; }
    sleep 0.25
  done
  warn "$label did not exit gracefully — sending SIGKILL"
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$file"
  ok "$label stopped"
}

# Flip a boolean under settings.demo / settings root, without a restart: both loops
# re-read settings every cycle, so a toggle takes effect within one interval.
set_flag() {
  local path="$1" value="$2"
  "$PY" - "$path" "$value" <<'PY'
import json, sys, pathlib
path, value = sys.argv[1], sys.argv[2] == "on"
f = pathlib.Path("config/settings.json")
s = json.loads(f.read_text(encoding="utf-8"))
node, _, leaf = path.rpartition(".")
target = s.setdefault(node, {}) if node else s
target[leaf] = value
f.write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"{path} = {value}")
PY
}

cmd_setup() {
  say "${c_bold}Setup${c_reset}"
  mkdir -p "$VAR" "$LOGS" "$ROOT/config"
  ensure_venv
  ok "virtualenv: $("$PY" --version)"
  say "  no third-party packages required — the app is stdlib only"

  if [[ ! -f "$ROOT/.env" ]]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
    chmod 600 "$ROOT/.env"
    warn "created .env from .env.example — add your Nobitex API key (READ permission only)"
  else
    chmod 600 "$ROOT/.env"
    ok ".env present (mode 600)"
  fi

  say ""
  say "${c_bold}Credentials${c_reset}"
  if ! "$PY" - <<'PY'
import json, sys
from agent import config, exchange, skill
config.load_dotenv()
venue = exchange.adapter()
print(f"  venue      : {venue.LABEL}")
if not venue.needs_credentials:
    print("  this venue needs no credentials — the screener uses public endpoints only")
    sys.exit(0)
st = config.credential_status()
if not (st["api_key_set"] and st["api_secret_set"]) and not st["token_set"]:
    print("  no credentials in .env — public market data will still work,")
    print("  but margin fee rates and account state will not.")
    sys.exit(1)
try:
    out = skill.auth_check()
except Exception as exc:
    print(f"  auth-check failed: {exc}")
    sys.exit(1)
print(f"  public API : {out.get('public_api')}")
print(f"  private API: {out.get('private_api')}")
print(f"  signing    : {out.get('ed25519_backend')} (self-test {out.get('ed25519_self_test')})")
sys.exit(0 if out.get("private_api") == "ok" else 1)
PY
  then
    warn "credential check did not pass — fix .env and re-run ./run.sh setup"
  else
    ok "credentials verified"
  fi

  say ""
  say "${c_bold}Symbol discovery${c_reset}"
  if [[ -f "$ROOT/config/coins.txt" ]]; then
    n="$("$PY" -c 'from agent import config; print(len(config.load_coins()))')"
    say "  reading $n coins from config/coins.txt — edit that file to change the list"
  fi
  "$PY" -c 'from agent import exchange; exchange.run_discovery()' \
    || warn "discovery failed — check network access to the exchange"

  say ""
  say "${c_bold}Local model${c_reset}"
  "$PY" - <<'PY'
from agent import llm
d = llm.ensure_decision(force=True)
for line in d["reasoning"]:
    print(f"  {line}")
if not d["commentary_enabled"]:
    rec = d["recommendation"]
    print(f"  suggested: {rec['command']}  (~{rec['estimated_ram_gb']} GB)")
    print("  commentary stays disabled until a model is installed; nothing else is affected.")
PY

  say ""
  ok "setup complete — run ./run.sh start"
}

cmd_start() {
  mkdir -p "$VAR" "$LOGS"
  if pid="$(running_pid)"; then
    ok "already running (pid $pid) — http://$BIND_HOST:$BIND_PORT"
    return 0
  fi
  [[ -x "$PY" ]] || die "no virtualenv — run ./run.sh setup first"
  [[ -f "$ROOT/config/watchlist.json" ]] || warn "config/watchlist.json missing — run ./run.sh setup"

  BIND_HOST="$BIND_HOST" BIND_PORT="$BIND_PORT" \
    nohup "$PY" -m agent.server >>"$LOGFILE" 2>&1 &
  local pid=$!
  printf '%s' "$pid" > "$PIDFILE"

  for _ in $(seq 1 40); do
    sleep 0.25
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PIDFILE"
      say "${c_dim}$(tail -n 20 "$LOGFILE")${c_reset}"
      die "server exited during startup — see $LOGFILE"
    fi
    if curl -fsS -m 2 "http://$BIND_HOST:$BIND_PORT/api/health" >/dev/null 2>&1; then
      ok "started (pid $pid)"
      say "  Dashboard: ${c_bold}http://$BIND_HOST:$BIND_PORT${c_reset}"
      say "  Logs:      ./run.sh logs -f"
      return 0
    fi
  done
  warn "started (pid $pid) but health check did not answer yet — see $LOGFILE"
}

cmd_stop() {
  # Stop every process this script owns. A watcher still running after `stop` is the
  # exact problem this command exists to prevent: something hitting the API and
  # writing to the database with nothing supervising it.
  local stopped=0
  if watch_pid >/dev/null; then stop_pid "$WATCHPID" "watch"; stopped=1; fi
  if ! running_pid >/dev/null; then
    [[ $stopped -eq 1 ]] || say "not running"
    return 0
  fi
  stop_pid "$PIDFILE" "server"
}

cmd_watch() {
  local action="${1:-status}" minutes="${2:-60}"
  case "$action" in
    start)
      [[ -x "$PY" ]] || die "no virtualenv — run ./run.sh setup first"
      watch_pid >/dev/null && die "watch already running (pid $(watch_pid))"
      running_pid >/dev/null || die "server is not running — ./run.sh start first"
      mkdir -p "$LOGS"
      nohup "$PY" -m agent.watch "$minutes" >>"$WATCHLOG" 2>&1 &
      printf '%s' "$!" > "$WATCHPID"
      sleep 1
      watch_pid >/dev/null || { rm -f "$WATCHPID"; die "watch failed — see $WATCHLOG"; }
      ok "watch started (pid $(cat "$WATCHPID")) for ${minutes}m — log: $WATCHLOG"
      ;;
    stop)
      watch_pid >/dev/null || { say "watch not running"; return 0; }
      stop_pid "$WATCHPID" "watch"
      ;;
    status)
      if pid="$(watch_pid)"; then ok "watch running (pid $pid)"; else say "watch not running"; fi
      ;;
    *) die "usage: ./run.sh watch {start [MINUTES]|stop|status}" ;;
  esac
}

cmd_agents() {
  say "${c_bold}Agents${c_reset}"
  if pid="$(running_pid)"; then
    ok "server      running (pid $pid)"
  else
    warn "server      stopped — scanner and demo both run inside it"
  fi
  [[ -x "$PY" ]] && "$PY" -c '
from agent import config, demo
s = config.load_settings()
d = s.get("demo") or {}
mark = lambda b: "on " if b else "off"
print("  scanner     %s every %s min" % (mark(s.get("scanner_enabled", True)),
                                         s.get("scan_interval_minutes", 15)))
print("  demo        %s every %ss, %s slots" % (mark(d.get("enabled")),
                                                d.get("cycle_seconds", 90),
                                                d.get("slots", 5)))
print("  strategy    %s - time stop %gh" % (s.get("profile"), demo.time_stop_hours()))
'
  if pid="$(watch_pid)"; then ok "watch       running (pid $pid)"; else say "  watch       stopped"; fi
}

cmd_status() {
  if pid="$(running_pid)"; then
    ok "server running (pid $pid) — http://$BIND_HOST:$BIND_PORT"
  else
    warn "server not running"
  fi
  [[ -x "$PY" ]] || { warn "no virtualenv — run ./run.sh setup"; return 0; }
  "$PY" - <<'PY'
from agent import config, exchange, llm, store
config.load_dotenv()
store.init()
venue = exchange.adapter()
print(f"  venue      : {venue.LABEL}")
wl = config.load_watchlist()
scan = store.latest_scan()

if wl:
    groups = {}
    for c in wl.get("coins", []):
        groups[c["status"]] = groups.get(c["status"], 0) + 1
    parts = ", ".join(f"{v} {k}" for k, v in sorted(groups.items()))
    print(f"  coins      : {wl.get('requested')} requested — {parts}")
    print(f"  scannable  : {len(venue.scannable(wl))}")
else:
    print("  coins      : no watchlist — run ./run.sh setup")

if scan:
    print(f"  last scan  : #{scan['id']} {scan['status']} "
          f"{scan['completed']}/{scan['total']} "
          f"(failed {scan['failed']}) started {scan['started_at']}")
    if scan["status"] == "running" and scan["current_coin"]:
        print(f"               currently on {scan['current_coin']}")
else:
    print("  last scan  : never")

st = llm.status()
if not st["installed"]:
    print("  ollama     : not installed — commentary disabled")
elif not st["running"]:
    print(f"  ollama     : installed but not answering ({st['error']})")
else:
    decision = (config.load_settings().get("llm") or {}).get("decision") or {}
    model = decision.get("model") or "no suitable model"
    persian = "yes" if decision.get("persian_ok") else "no (English only)"
    print(f"  ollama     : running, {len(st['models'])} model(s); using {model}")
    print(f"               Persian commentary: {persian}")

creds = config.credential_status()
have = creds["api_key_set"] and creds["api_secret_set"]
print(f"  credentials: {'present' if have or creds['token_set'] else 'MISSING'}")
PY
}

cmd_logs() {
  [[ -f "$LOGFILE" ]] || die "no log file yet at $LOGFILE"
  if [[ "${1:-}" == "-f" ]]; then tail -f "$LOGFILE"; else tail -n 200 "$LOGFILE"; fi
}

cmd_scan_once() {
  [[ -x "$PY" ]] || die "no virtualenv — run ./run.sh setup first"
  shift || true
  PYTHONUNBUFFERED=1 "$PY" - "$@" <<'PY'
import logging, sys
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
from agent import scanner
coins = [a.upper() for a in sys.argv[1:]] or None
scanner.scan_once(coins, verbose=True)
PY
}

cmd_journal() {
  [[ -x "$PY" ]] || die "no virtualenv — run ./run.sh setup first"
  "$PY" -c 'from agent import journal; print(journal.text())'
}

case "${1:-}" in
  setup)     cmd_setup ;;
  start)     cmd_start ;;
  stop)      cmd_stop ;;
  restart)   cmd_stop; cmd_start ;;
  status)    cmd_status ;;
  logs)      cmd_logs "${2:-}" ;;
  scan-once) cmd_scan_once "$@" ;;
  journal)   cmd_journal ;;
  agents)    cmd_agents ;;
  watch)     cmd_watch "${2:-status}" "${3:-60}" ;;
  demo)
    case "${2:-}" in
      clear-breaker)
        "$PY" -c 'from agent import demo; import json; print(json.dumps(demo.clear_breaker(), indent=1, default=str))' ;;
      on|off) set_flag "demo.enabled" "$2" ;;
      *) die "usage: ./run.sh demo {on|off|clear-breaker}" ;;
    esac ;;
  scanner)   set_flag "scanner_enabled" "${2:-}" ;;
  *)
    say "usage: ./run.sh {setup|start|stop|restart|status|agents|logs [-f]|"
    say "                 scan-once [COIN...]|journal|watch {start [MIN]|stop|status}|"
    say "                 demo {on|off}|scanner {on|off}}"
    exit 1
    ;;
esac
