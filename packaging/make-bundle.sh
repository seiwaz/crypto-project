#!/usr/bin/env bash
# Build a self-contained install bundle for a Linux host.
#
# The bundle carries everything the app needs to run offline apart from network
# access to the exchange: the repository at HEAD, the crypto-leverage-trade-plan
# skill it calls, and the installer. There are no third-party Python packages to
# vendor — the app is stdlib only — so nothing here needs a package index.
#
#   ./packaging/make-bundle.sh            # bundle from HEAD
#   ./packaging/make-bundle.sh --dirty    # include uncommitted working-tree changes
#
# Output: dist/crypto-screener-<version>.tar.gz and a .sha256 beside it.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DIST="$ROOT/dist"
SKILL_SRC="${CRYPTO_SKILL_DIR:-$HOME/.claude/skills/crypto-leverage-trade-plan}"
DIRTY=0
[[ "${1:-}" == "--dirty" ]] && DIRTY=1

c_reset=$'\033[0m'; c_green=$'\033[32m'; c_red=$'\033[31m'; c_bold=$'\033[1m'
ok()  { printf '%s✓%s %s\n' "$c_green" "$c_reset" "$*"; }
die() { printf '%s✗%s %s\n' "$c_red" "$c_reset" "$*" >&2; exit 1; }

command -v git >/dev/null || die "git is required to build a bundle"
[[ -d "$SKILL_SRC/scripts" ]] || die "skill not found at $SKILL_SRC
  The app calls its scripts directly and cannot run without them.
  Set CRYPTO_SKILL_DIR to the correct path and try again."

for required in nobitex_api.py trade_plan.py; do
  [[ -f "$SKILL_SRC/scripts/$required" ]] || die "skill is missing scripts/$required"
done

VERSION="$(date -u +%Y%m%d).$(git rev-parse --short HEAD)"
[[ $DIRTY -eq 1 ]] && VERSION="${VERSION}.dirty"
NAME="crypto-screener-${VERSION}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

printf '%sBuilding %s%s\n' "$c_bold" "$NAME" "$c_reset"

mkdir -p "$STAGE/$NAME"

# The application. `git archive` gives exactly the tracked files, which is the
# cleanest possible definition of "the app" — no var/, no .venv, no scratch files,
# and crucially no .env, because it is gitignored.
if [[ $DIRTY -eq 1 ]]; then
  git ls-files -z | tar --null -T - -cf - | tar -xf - -C "$STAGE/$NAME"
else
  git archive HEAD | tar -x -C "$STAGE/$NAME"
fi

# Belt and braces: a credential file must never reach a bundle, whatever the
# working tree looks like.
find "$STAGE/$NAME" -name '.env' -delete
[[ -f "$STAGE/$NAME/.env" ]] && die "refusing to ship a .env"

# The skill. Copied rather than referenced: the target host has no ~/.claude.
mkdir -p "$STAGE/$NAME/skill"
cp -R "$SKILL_SRC/." "$STAGE/$NAME/skill/"
rm -rf "$STAGE/$NAME/skill/scripts/__pycache__"

cat > "$STAGE/$NAME/BUNDLE" <<EOF
name:        crypto-screener
version:     $VERSION
commit:      $(git rev-parse HEAD)
built:       $(date -u +%Y-%m-%dT%H:%M:%SZ)
built_on:    $(uname -s) $(uname -r)
skill_from:  $SKILL_SRC
dirty:       $DIRTY
EOF

# Checksums for every shipped file, so a truncated transfer is caught at install
# time rather than as a confusing runtime error.
( cd "$STAGE/$NAME" && find . -type f ! -name SHA256SUMS -print0 \
    | sort -z | xargs -0 shasum -a 256 > SHA256SUMS ) 2>/dev/null \
  || ( cd "$STAGE/$NAME" && find . -type f ! -name SHA256SUMS -print0 \
        | sort -z | xargs -0 sha256sum > SHA256SUMS )

mkdir -p "$DIST"
tar -czf "$DIST/$NAME.tar.gz" -C "$STAGE" "$NAME"
( cd "$DIST" && { shasum -a 256 "$NAME.tar.gz" || sha256sum "$NAME.tar.gz"; } > "$NAME.tar.gz.sha256" )

files=$(tar -tzf "$DIST/$NAME.tar.gz" | wc -l | tr -d ' ')
size=$(du -h "$DIST/$NAME.tar.gz" | cut -f1 | tr -d ' ')

ok "$DIST/$NAME.tar.gz  ($size, $files files)"
ok "$DIST/$NAME.tar.gz.sha256"
cat <<EOF

Install on the target host:

  scp $DIST/$NAME.tar.gz user@host:~
  ssh user@host
  tar xzf $NAME.tar.gz
  sudo $NAME/packaging/install-centos.sh
EOF
