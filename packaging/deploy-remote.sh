#!/usr/bin/env bash
# One-shot remote deployment: build, upload, install, verify.
#
#   ./packaging/deploy-remote.sh root@94.74.166.123 --ssh-port 2266 --public
#
# Everything the manual sequence does, with the checks that catch the failures this
# deployment actually hit: a stale bundle without --public, and a network that
# accepts TCP but never delivers a byte.
#
# Options:
#   --ssh-port N     ssh port                        (default 22)
#   --port N         dashboard port                  (default 8787)
#   --public         expose the dashboard on 0.0.0.0 (NO AUTHENTICATION — see below)
#   --password PASS  use sshpass; prefer keys, and never put a password in history
#   --no-build       use the newest existing bundle instead of rebuilding
#
# --public means anyone who can reach the port can reset the demo account, change
# capital and risk, and start scans. It cannot place an exchange order and serves no
# credentials. Prefer `ssh -L <port>:127.0.0.1:<port>` unless you need it open.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TARGET=""
SSH_PORT=22
PORT=8787
PUBLIC=0
PASSWORD=""
BUILD=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ssh-port) SSH_PORT="$2"; shift 2 ;;
    --port)     PORT="$2"; shift 2 ;;
    --public)   PUBLIC=1; shift ;;
    --password) PASSWORD="$2"; shift 2 ;;
    --no-build) BUILD=0; shift ;;
    -h|--help)  sed -n '2,22p' "$0"; exit 0 ;;
    *)          TARGET="$1"; shift ;;
  esac
done

c_reset=$'\033[0m'; c_green=$'\033[32m'; c_yellow=$'\033[33m'
c_red=$'\033[31m'; c_bold=$'\033[1m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$c_green" "$c_reset" "$*"; }
warn() { printf '%s!%s %s\n' "$c_yellow" "$c_reset" "$*"; }
die()  { printf '%s✗%s %s\n' "$c_red" "$c_reset" "$*" >&2; exit 1; }
step() { printf '\n%s%s%s\n' "$c_bold" "$*" "$c_reset"; }

[[ -n "$TARGET" ]] || die "usage: $0 user@host [--ssh-port N] [--port N] [--public]"
HOST="${TARGET#*@}"

SSH_OPTS=(-p "$SSH_PORT" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20
          -o ServerAliveInterval=10)
if [[ -n "$PASSWORD" ]]; then
  command -v sshpass >/dev/null || die "--password needs sshpass installed"
  export SSHPASS="$PASSWORD"
  SSH=(sshpass -e ssh "${SSH_OPTS[@]}")
  SCP=(sshpass -e scp -P "$SSH_PORT" -o StrictHostKeyChecking=accept-new)
else
  SSH=(ssh "${SSH_OPTS[@]}")
  SCP=(scp -P "$SSH_PORT" -o StrictHostKeyChecking=accept-new)
fi

# --------------------------------------------------------------------------------
# Reachability, diagnosed rather than merely attempted
# --------------------------------------------------------------------------------

step "Reachability"
# A local VPN or transparent proxy can accept the TCP handshake and then fail to
# relay anything, which looks exactly like a hung server. Comparing ICMP latency
# against TCP connect time tells the two apart in one shot: a handshake cannot
# legitimately complete faster than the round trip.
rtt=$(ping -c 3 -t 8 "$HOST" 2>/dev/null | awk -F'/' '/round-trip|rtt/ {print $5}')
tcp=$(curl -sS -m 10 -o /dev/null -w '%{time_connect}' "telnet://$HOST:$SSH_PORT" 2>/dev/null || echo "")
if [[ -n "$rtt" && -n "$tcp" ]]; then
  say "  icmp round-trip : ${rtt} ms"
  say "  tcp connect     : $(awk "BEGIN{printf \"%.1f\", $tcp*1000}") ms"
  if awk "BEGIN{exit !($tcp*1000 < $rtt/4)}"; then
    warn "TCP connects far faster than the network round trip."
    warn "Something local is terminating the connection — a VPN or transparent proxy."
    warn "Disconnect it, or run this from a network without it."
  fi
fi

if ! "${SSH[@]}" "$TARGET" 'echo ok' >/dev/null 2>&1; then
  die "cannot open an ssh session to $TARGET:$SSH_PORT
  If TCP connects but ssh hangs with no banner, the path is intercepted — see above."
fi
ok "ssh works"

# --------------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------------

if [[ $BUILD -eq 1 ]]; then
  step "Build"
  ./packaging/make-bundle.sh >/dev/null || die "bundle build failed"
fi

BUNDLE="$(ls -t dist/*.tar.gz 2>/dev/null | head -1)"
[[ -n "$BUNDLE" ]] || die "no bundle in dist/ — drop --no-build"
NAME="$(basename "$BUNDLE" .tar.gz)"

# The failure that cost a round trip once: a bundle built from git archive HEAD
# before the public-bind change was committed installs fine and then refuses to
# serve, with nothing obviously wrong. Check the tarball, not the working tree.
if [[ $PUBLIC -eq 1 ]]; then
  tar -xzOf "$BUNDLE" --include="*/packaging/install-centos.sh" | grep -q -- "--public" \
    || die "$NAME predates --public. Commit your changes and rebuild."
  tar -xzOf "$BUNDLE" --include="*/agent/server.py" | grep -q "ALLOW_PUBLIC_BIND" \
    || die "$NAME has a server.py that cannot bind publicly. Rebuild."
  ok "bundle supports a public bind"
fi
ok "$NAME ($(du -h "$BUNDLE" | cut -f1))"

# --------------------------------------------------------------------------------
# Upload and install
# --------------------------------------------------------------------------------

step "Upload"
"${SCP[@]}" "$BUNDLE" "$TARGET:/tmp/$NAME.tar.gz" || die "upload failed"
ok "uploaded to /tmp/$NAME.tar.gz"

step "Install"
INSTALL_FLAGS="--port $PORT"
[[ $PUBLIC -eq 1 ]] && INSTALL_FLAGS="$INSTALL_FLAGS --public"
"${SSH[@]}" "$TARGET" "set -e
  cd /tmp
  rm -rf '$NAME'
  tar xzf '$NAME.tar.gz'
  ./'$NAME'/packaging/install-centos.sh $INSTALL_FLAGS" || die "install failed"

# --------------------------------------------------------------------------------
# Verify from here, not just from the server
# --------------------------------------------------------------------------------

step "Verify"
"${SSH[@]}" "$TARGET" "systemctl is-active crypto-screener" >/dev/null 2>&1 \
  && ok "service active" || warn "service is not active — journalctl -u crypto-screener -n 50"

"${SSH[@]}" "$TARGET" "curl -fsS -m 10 http://127.0.0.1:$PORT/api/health" \
  && ok "health ok on the server" || warn "health endpoint did not answer on the server"

if [[ $PUBLIC -eq 1 ]]; then
  # The point of --public is reachability from outside, so check from outside.
  code=$(curl -sS -m 20 -o /dev/null -w '%{http_code}' "http://$HOST:$PORT/api/health" 2>/dev/null || echo 000)
  if [[ "$code" == "200" ]]; then
    ok "reachable from here: http://$HOST:$PORT"
  else
    warn "not reachable from here (HTTP $code) — firewall upstream of the host, or this network is intercepting"
  fi
fi

cat <<EOF

${c_bold}Done${c_reset}

  dashboard  http://$( [[ $PUBLIC -eq 1 ]] && echo "$HOST" || echo "127.0.0.1" ):$PORT
  logs       ssh -p $SSH_PORT $TARGET journalctl -u crypto-screener -f
  agents     ssh -p $SSH_PORT $TARGET sudo -u screener /opt/crypto-screener/run.sh agents
  journal    ssh -p $SSH_PORT $TARGET sudo -u screener /opt/crypto-screener/run.sh journal
EOF
[[ $PUBLIC -eq 1 ]] && warn "the dashboard has no authentication — restrict the port to your address"
exit 0
