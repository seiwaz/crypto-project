#!/usr/bin/env bash
# Install the crypto screener on CentOS Stream 10 (also fine on RHEL/Rocky/Alma 9-10).
#
# Run as root from inside an extracted bundle:
#
#   tar xzf crypto-screener-<version>.tar.gz
#   sudo crypto-screener-<version>/packaging/install-centos.sh
#
# What it does, and what it deliberately does not:
#
#   * Installs python3 and python3-pip from dnf. Nothing else — the app is stdlib
#     only, so there is no pip install step and the host needs no package index
#     access after these two.
#   * Creates a locked system account. The service never needs a login shell, and
#     the dashboard is read-only by design; running it as root would be the single
#     largest unnecessary risk in the deployment.
#   * Binds 127.0.0.1 only, and does NOT open a firewall port. The server refuses
#     any other bind address, so exposing it needs a deliberate reverse proxy in
#     front — not a firewall rule here.
#   * Installs a systemd unit that drives ./run.sh rather than the Python module,
#     so run.sh stays the single owner of every process, exactly as it is on a
#     developer machine.
#
# Options:
#   --prefix DIR   install location          (default /opt/crypto-screener)
#   --user NAME    service account           (default screener)
#   --port N       loopback port             (default 8787)
#   --no-start     install but do not start
#   --uninstall    remove service, files and account

set -euo pipefail

PREFIX=/opt/crypto-screener
SVC_USER=screener
PORT=8787
START=1
UNINSTALL=0
UNIT=/etc/systemd/system/crypto-screener.service

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)    PREFIX="$2"; shift 2 ;;
    --user)      SVC_USER="$2"; shift 2 ;;
    --port)      PORT="$2"; shift 2 ;;
    --no-start)  START=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help)   sed -n '2,32p' "$0"; exit 0 ;;
    *)           echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

c_reset=$'\033[0m'; c_green=$'\033[32m'; c_yellow=$'\033[33m'
c_red=$'\033[31m'; c_bold=$'\033[1m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$c_green" "$c_reset" "$*"; }
warn() { printf '%s!%s %s\n' "$c_yellow" "$c_reset" "$*"; }
die()  { printf '%s✗%s %s\n' "$c_red" "$c_reset" "$*" >&2; exit 1; }
step() { printf '\n%s%s%s\n' "$c_bold" "$*" "$c_reset"; }

[[ $EUID -eq 0 ]] || die "run as root: sudo $0"

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --------------------------------------------------------------------------------
# Uninstall
# --------------------------------------------------------------------------------

if [[ $UNINSTALL -eq 1 ]]; then
  step "Removing crypto-screener"
  if systemctl list-unit-files 2>/dev/null | grep -q '^crypto-screener\.service'; then
    systemctl disable --now crypto-screener.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    ok "service removed"
  fi
  # The database and journal live under $PREFIX/var and go with it. Say so rather
  # than deleting a trading record silently.
  if [[ -d "$PREFIX" ]]; then
    warn "deleting $PREFIX (this includes var/ — the demo database and journals)"
    rm -rf "$PREFIX"
    ok "files removed"
  fi
  if id "$SVC_USER" &>/dev/null; then
    userdel "$SVC_USER" 2>/dev/null || true
    ok "account $SVC_USER removed"
  fi
  exit 0
fi

# --------------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------------

step "Preflight"

[[ -f "$SRC/run.sh" && -d "$SRC/agent" ]] \
  || die "run this from inside an extracted bundle (no run.sh/agent found at $SRC)"
[[ -d "$SRC/skill/scripts" ]] \
  || die "bundle has no skill/ directory — the app cannot run without it.
  Rebuild with packaging/make-bundle.sh"

if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  say "  os           : ${PRETTY_NAME:-unknown}"
  case "${ID:-}${ID_LIKE:-}" in
    *rhel*|*centos*|*fedora*) ;;
    *) warn "not a RHEL-family system — dnf steps may fail" ;;
  esac
else
  warn "cannot read /etc/os-release; continuing"
fi

if [[ -f "$SRC/SHA256SUMS" ]]; then
  if ( cd "$SRC" && sha256sum --quiet -c SHA256SUMS 2>/dev/null ); then
    ok "bundle checksums verified"
  else
    warn "checksum verification failed or incomplete — continuing, but the transfer may be damaged"
  fi
fi

step "Packages"
# The full prerequisite set, and nothing beyond it:
#
#   python3      the runtime; python3-libs carries the _sqlite3 extension
#   python3-pip  only because `python3 -m venv` runs ensurepip, which RHEL ships
#                separately; no Python package is installed from an index
#   procps-ng    provides `ps`, used by run.sh to confirm a PID is really ours
#
# There is no database package to install. The app keeps its state in one SQLite
# file through Python's stdlib module: no server, no port, no daemon.
NEEDED=(python3 python3-pip procps-ng)

if compgen -G "$SRC/packaging/rpms/*.rpm" >/dev/null; then
  # Offline: install from the RPMs carried in the bundle, with every repository
  # disabled so a host with no network — or no subscription — still installs.
  count=$(ls -1 "$SRC/packaging/rpms"/*.rpm | wc -l | tr -d ' ')
  say "  installing $count bundled RPMs (offline, repositories disabled)"
  if [[ -f "$SRC/packaging/rpms/SHA256SUMS.rpms" ]]; then
    ( cd "$SRC/packaging/rpms" && sha256sum --quiet -c SHA256SUMS.rpms ) \
      && ok "RPM checksums verified" || warn "RPM checksum check failed — continuing"
  fi
  dnf install -y --disablerepo='*' "$SRC/packaging/rpms"/*.rpm \
    || rpm -Uvh --replacepkgs "$SRC/packaging/rpms"/*.rpm \
    || die "offline package install failed"
  ok "prerequisites installed from the bundle"
else
  say "  no bundled RPMs — installing from configured repositories"
  say "  (build an offline bundle with packaging/fetch-rpms.sh on a CentOS host)"
  dnf install -y "${NEEDED[@]}" >/dev/null 2>&1 || dnf install -y "${NEEDED[@]}" \
    || die "package install failed and no bundled RPMs to fall back on"
  ok "prerequisites installed from repositories"
fi

for cmd in ps runuser; do
  command -v "$cmd" >/dev/null || die "missing required command: $cmd"
done

PYBIN="$(command -v python3.12 || command -v python3)"
"$PYBIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
  || die "need Python 3.10 or newer; found $("$PYBIN" --version 2>&1)"
ok "$("$PYBIN" --version)"
# The whole database layer, verified in one line. If this imports, the app has
# everything it needs to store scans, plans and the demo journal.
"$PYBIN" -c 'import sqlite3; print("  sqlite       : %s (embedded, no server)" % sqlite3.sqlite_version)' \
  || die "python3 is missing sqlite3 support — install python3-libs"

# --------------------------------------------------------------------------------
# Account and files
# --------------------------------------------------------------------------------

step "Account"
if id "$SVC_USER" &>/dev/null; then
  ok "user $SVC_USER exists"
else
  useradd --system --home-dir "$PREFIX" --shell /sbin/nologin \
          --comment "crypto screener" "$SVC_USER"
  ok "created locked system user $SVC_USER"
fi

step "Files"
KEEP_VAR=""
if [[ -d "$PREFIX/var" ]]; then
  KEEP_VAR="$(mktemp -d)"
  cp -a "$PREFIX/var/." "$KEEP_VAR/"
  warn "existing install found — preserving var/ (database, journals, logs)"
fi

mkdir -p "$PREFIX"
# Replace code, keep state. rsync is not guaranteed present on a minimal image.
for entry in agent web config run.sh README.md .env.example packaging skill BUNDLE; do
  [[ -e "$SRC/$entry" ]] || continue
  rm -rf "${PREFIX:?}/$entry"
  cp -a "$SRC/$entry" "$PREFIX/$entry"
done

mkdir -p "$PREFIX/var/logs"
if [[ -n "$KEEP_VAR" ]]; then
  cp -a "$KEEP_VAR/." "$PREFIX/var/"
  rm -rf "$KEEP_VAR"
  ok "var/ restored"
fi

if [[ ! -f "$PREFIX/.env" ]]; then
  cp "$PREFIX/.env.example" "$PREFIX/.env"
  ok "created .env from .env.example"
else
  ok ".env preserved"
fi

chown -R "$SVC_USER:$SVC_USER" "$PREFIX"
chmod 750 "$PREFIX"
chmod 600 "$PREFIX/.env"
chmod +x "$PREFIX/run.sh"
ok "installed to $PREFIX, owned by $SVC_USER"

# The skill ships inside the install rather than under a home directory that does
# not exist on a server. config.py reads CRYPTO_SKILL_DIR ahead of settings.json.
SKILL_DIR="$PREFIX/skill"
[[ -f "$SKILL_DIR/scripts/trade_plan.py" ]] || die "skill/scripts/trade_plan.py missing"
ok "skill at $SKILL_DIR"

# --------------------------------------------------------------------------------
# Setup and verification
# --------------------------------------------------------------------------------

step "Setup"
runuser -u "$SVC_USER" -- env \
  CRYPTO_SKILL_DIR="$SKILL_DIR" BIND_HOST=127.0.0.1 BIND_PORT="$PORT" \
  bash -lc "cd '$PREFIX' && ./run.sh setup" || die "./run.sh setup failed"

step "Verification"
runuser -u "$SVC_USER" -- env CRYPTO_SKILL_DIR="$SKILL_DIR" \
  "$PREFIX/.venv/bin/python" - <<'PY' || die "verification failed"
import sys
from agent import config, guard, skill

ok, detail = skill.check_installed()
print(f"  skill        : {'ok' if ok else 'MISSING'} — {detail}")
if not ok:
    sys.exit(1)

failures = guard.self_test()
print(f"  read-only    : {'ok' if not failures else failures}")
if failures:
    sys.exit(1)

print(f"  database     : {config.DB_PATH}")
print(f"  coins listed : {len(config.load_coins())}")
PY
ok "verified"

# --------------------------------------------------------------------------------
# systemd
# --------------------------------------------------------------------------------

step "Service"
cat > "$UNIT" <<EOF
[Unit]
Description=Crypto screener (read-only local dashboard)
Documentation=file://$PREFIX/README.md
After=network-online.target
Wants=network-online.target

[Service]
# Type=forking with run.sh, not a bare python invocation: run.sh owns every process
# this project starts, on a server exactly as on a laptop. systemd supervises it
# through the same PID file run.sh already maintains.
Type=forking
User=$SVC_USER
Group=$SVC_USER
WorkingDirectory=$PREFIX
PIDFile=$PREFIX/var/server.pid
Environment=CRYPTO_SKILL_DIR=$SKILL_DIR
Environment=BIND_HOST=127.0.0.1
Environment=BIND_PORT=$PORT
Environment=PYTHONUNBUFFERED=1
ExecStart=$PREFIX/run.sh start
ExecStop=$PREFIX/run.sh stop
Restart=on-failure
RestartSec=10

# The dashboard reads market data and writes one SQLite database. Nothing else is
# needed, so nothing else is permitted.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$PREFIX/var
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictNamespaces=true
LockPersonality=true
MemoryDenyWriteExecute=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable crypto-screener.service >/dev/null 2>&1
ok "unit installed at $UNIT"

if [[ $START -eq 1 ]]; then
  systemctl restart crypto-screener.service
  sleep 3
  if systemctl is-active --quiet crypto-screener.service; then
    ok "service running"
    # Checked with python rather than curl, so curl is not a prerequisite on a
    # minimal image — python3 is guaranteed present by this point.
    if "$PYBIN" - "$PORT" <<'PY'
import json, sys, urllib.request
url = f"http://127.0.0.1:{sys.argv[1]}/api/health"
try:
    with urllib.request.urlopen(url, timeout=10) as r:
        body = json.load(r)
except Exception as exc:
    print(f"  health check failed: {exc}")
    sys.exit(1)
print(f"  health       : ok={body.get('ok')} read_only={body.get('read_only')} "
      f"guard_failures={body.get('guard_failures')}")
sys.exit(0 if body.get("ok") and not body.get("guard_failures") else 1)
PY
    then
      ok "dashboard answering on 127.0.0.1:$PORT"
    else
      warn "service is up but /api/health did not answer — journalctl -u crypto-screener -n 50"
    fi
  else
    systemctl status crypto-screener.service --no-pager -l | head -20
    die "service failed to start"
  fi
else
  ok "installed but not started (--no-start)"
fi

cat <<EOF

${c_bold}Installed${c_reset}

  dashboard   http://127.0.0.1:$PORT   (loopback only — no firewall port is opened)
  files       $PREFIX
  account     $SVC_USER (no login shell)
  database    $PREFIX/var/screener.sqlite3

  systemctl {start,stop,restart,status} crypto-screener
  journalctl -u crypto-screener -f

  sudo -u $SVC_USER $PREFIX/run.sh agents      what each agent is doing
  sudo -u $SVC_USER $PREFIX/run.sh journal     demo account and report
  sudo -u $SVC_USER $PREFIX/run.sh demo off    pause the paper trader

${c_bold}First run${c_reset}

  Discover which of your coins Toobit lists, then let the scanner take over:

    sudo -u $SVC_USER $PREFIX/run.sh setup

  Toobit needs no credentials — the screener uses public endpoints only. Nobitex
  does; if you switch venue, put a READ-permission key in $PREFIX/.env.

  Edit $PREFIX/config/coins.txt to change the watchlist, one ticker per line, then
  re-run setup.

  To reach the dashboard from your workstation, forward the port rather than
  binding it publicly — the server refuses any non-loopback bind:

    ssh -N -L $PORT:127.0.0.1:$PORT $(id -un)@\$(hostname -f 2>/dev/null || hostname)
EOF
