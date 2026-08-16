#!/usr/bin/env bash
# Server-side operations. This project runs on 94.74.166.123, not locally.
#
#   ./packaging/srv.sh sync              push code, restart, verify
#   ./packaging/srv.sh run <args...>     run.sh on the server (agents|journal|demo off|…)
#   ./packaging/srv.sh test              run the test suite on the server
#   ./packaging/srv.sh logs [-f]         journalctl for the service
#   ./packaging/srv.sh ssh [command]     shell, or one command
#   ./packaging/srv.sh status            service, agents, and the public URL
#
# `sync` pushes only the application source — agent/, web/, config/*.json, run.sh,
# tests/ — and never `var/`. The database, journals and logs on the server are the
# live record of a running demo account; overwriting them from a laptop would
# destroy the only copy.
#
# Auth uses a dedicated passphrase-free deploy key so everything runs unattended.
# Override any of these with the matching environment variable.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HOST="${SCREENER_HOST:-94.74.166.123}"
PORT="${SCREENER_SSH_PORT:-2266}"
USER="${SCREENER_USER:-root}"
KEY="${SCREENER_KEY:-$HOME/.ssh/crypto-screener-deploy}"
PREFIX="${SCREENER_PREFIX:-/opt/crypto-screener}"
SVC_USER="${SCREENER_SVC_USER:-screener}"
WEB_PORT="${SCREENER_WEB_PORT:-8787}"

SSH_OPTS=(-p "$PORT" -i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no
          -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=25
          -o ServerAliveInterval=15 -o BatchMode=yes)

c_reset=$'\033[0m'; c_green=$'\033[32m'; c_yellow=$'\033[33m'
c_red=$'\033[31m'; c_bold=$'\033[1m'
ok()   { printf '%s✓%s %s\n' "$c_green" "$c_reset" "$*"; }
warn() { printf '%s!%s %s\n' "$c_yellow" "$c_reset" "$*"; }
die()  { printf '%s✗%s %s\n' "$c_red" "$c_reset" "$*" >&2; exit 1; }
step() { printf '\n%s%s%s\n' "$c_bold" "$*" "$c_reset"; }

remote() { ssh "${SSH_OPTS[@]}" "$USER@$HOST" "$@"; }
as_svc() { remote "cd '$PREFIX' && sudo -u '$SVC_USER' $*"; }

cmd_sync() {
  [[ -f "$KEY" ]] || die "no deploy key at $KEY"
  step "Sync → $HOST:$PREFIX"

  # --delete inside these directories only, so a file removed locally also goes on
  # the server, while var/ and .venv and .env are never considered.
  rsync -az --delete \
    -e "ssh ${SSH_OPTS[*]}" \
    --exclude '__pycache__' --exclude '*.pyc' \
    agent/ "$USER@$HOST:$PREFIX/agent/"
  rsync -az --delete -e "ssh ${SSH_OPTS[*]}" web/ "$USER@$HOST:$PREFIX/web/"
  rsync -az -e "ssh ${SSH_OPTS[*]}" run.sh "$USER@$HOST:$PREFIX/run.sh"
  [[ -d tests ]] && rsync -az --delete -e "ssh ${SSH_OPTS[*]}" tests/ "$USER@$HOST:$PREFIX/tests/"
  # coins.txt is user-editable on the server; settings.json holds live tuning. Push
  # coins only, and only when it differs, so a server-side edit is not clobbered.
  rsync -az -e "ssh ${SSH_OPTS[*]}" config/coins.txt "$USER@$HOST:$PREFIX/config/coins.txt"

  # The skill too. It was patched locally once and the server kept the old copy,
  # which is exactly the drift that produces two machines computing different plans
  # from the same code.
  SKILL_SRC="${CRYPTO_SKILL_DIR:-$HOME/.claude/skills/crypto-leverage-trade-plan}"
  if [[ -d "$SKILL_SRC/scripts" ]]; then
    rsync -az --delete -e "ssh ${SSH_OPTS[*]}" --exclude '__pycache__' \
      "$SKILL_SRC/" "$USER@$HOST:$PREFIX/skill/"
  else
    warn "no skill at $SKILL_SRC — server copy left as it is"
  fi

  remote "chown -R $SVC_USER:$SVC_USER '$PREFIX/agent' '$PREFIX/web' '$PREFIX/run.sh' '$PREFIX/config' '$PREFIX/skill' 2>/dev/null; chmod +x '$PREFIX/run.sh'"
  ok "code and skill pushed"

  step "Restart"
  remote "systemctl restart crypto-screener" || die "restart failed"
  sleep 4
  cmd_verify
}

cmd_verify() {
  remote "systemctl is-active --quiet crypto-screener" \
    && ok "service active" || { remote "journalctl -u crypto-screener -n 30 --no-pager"; die "service not active"; }

  local body
  body="$(curl -sS -m 20 "http://$HOST:$WEB_PORT/api/health" 2>/dev/null || true)"
  if [[ "$body" == *'"ok": true'* ]]; then
    ok "health: $body"
  else
    warn "health endpoint did not answer as expected: ${body:-<empty>}"
  fi
}

cmd_test() {
  step "Tests on the server"
  # Its own database, so the running demo's account is never touched by a test.
  as_svc "SCREENER_DB=/tmp/screener-test.sqlite3 CRYPTO_SKILL_DIR='$PREFIX/skill' \
          '$PREFIX/.venv/bin/python' tests/test_demo_lifecycle.py" \
    && ok "tests passed" || die "tests failed"
}

case "${1:-status}" in
  sync)   cmd_sync ;;
  verify) cmd_verify ;;
  test)   cmd_test ;;
  run)    shift; as_svc "./run.sh $*" ;;
  logs)   shift || true
          if [[ "${1:-}" == "-f" ]]; then remote "journalctl -u crypto-screener -f"
          else remote "journalctl -u crypto-screener -n ${1:-60} --no-pager"; fi ;;
  ssh)    shift; if [[ $# -eq 0 ]]; then remote; else remote "$@"; fi ;;
  status)
    step "Server $HOST"
    remote "systemctl is-active crypto-screener" | sed 's/^/  service: /'
    as_svc "./run.sh agents" 2>&1 | tail -6
    printf '  url    : http://%s:%s  ' "$HOST" "$WEB_PORT"
    curl -sS -m 15 -o /dev/null -w '(HTTP %{http_code})\n' "http://$HOST:$WEB_PORT/api/health" 2>/dev/null || echo "(unreachable)"
    ;;
  *) die "usage: $0 {sync|verify|test|run <args>|logs [-f|N]|ssh [cmd]|status}" ;;
esac
