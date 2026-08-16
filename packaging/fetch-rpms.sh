#!/usr/bin/env bash
# Download the RPM prerequisites so the bundle can install with no network.
#
# This must run ON a CentOS Stream 10 host (or container) of the same architecture
# as the target, because it resolves against that release's repositories. It cannot
# run on the build laptop.
#
#   # on any CentOS Stream 10 box, or:
#   podman run --rm -v "$PWD/packaging:/out:z" quay.io/centos/centos:stream10 \
#       bash /out/fetch-rpms.sh /out/rpms
#
#   # then, back on the build machine:
#   ./packaging/make-bundle.sh          # picks up packaging/rpms/ automatically
#
# What is downloaded and why:
#
#   python3       the runtime; brings python3-libs, which contains the _sqlite3
#                 extension and pulls in sqlite-libs
#   python3-pip   only because `python3 -m venv` runs ensurepip; no Python package
#                 is ever installed from an index
#   procps-ng     provides `ps`, which run.sh uses to confirm a PID is really ours
#                 rather than a recycled number
#
# There is no database package. The app stores everything in one SQLite file
# through Python's stdlib module — no server, no port, no daemon to supervise.

set -euo pipefail

OUT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/rpms}"
PACKAGES=(python3 python3-pip procps-ng)

command -v dnf >/dev/null || {
  echo "✗ dnf not found — run this on a CentOS/RHEL host, not the build machine" >&2
  exit 1
}

mkdir -p "$OUT"
echo "Downloading $(IFS=' '; echo "${PACKAGES[*]}") and dependencies into $OUT"

# --resolve --alldeps pulls the full closure, so the target needs no repository at
# all. Already-installed packages are still fetched, because the target may not
# have them even though this build host does.
dnf download --resolve --alldeps --destdir "$OUT" "${PACKAGES[@]}"

( cd "$OUT" && { sha256sum *.rpm > SHA256SUMS.rpms; } )

cat > "$OUT/MANIFEST" <<EOF
built:   $(date -u +%Y-%m-%dT%H:%M:%SZ)
os:      $(. /etc/os-release && echo "$PRETTY_NAME")
arch:    $(uname -m)
request: ${PACKAGES[*]}
count:   $(ls -1 "$OUT"/*.rpm 2>/dev/null | wc -l | tr -d ' ')
EOF

echo
echo "✓ $(ls -1 "$OUT"/*.rpm | wc -l | tr -d ' ') RPMs, $(du -sh "$OUT" | cut -f1) total"
echo "  arch: $(uname -m) — the target host must match"
