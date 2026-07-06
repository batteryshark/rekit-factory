#!/bin/sh
# Fetch jadx and install the launcher where rekit's host-gating looks: the shared
# $REKIT_HOME/bin (so one install serves every skill), falling back to the skill's
# own bin/ if that isn't writable. Idempotent: re-running re-installs.
# Usage: scripts/fetch.sh [version]   (default: pinned below)
set -eu

VERSION="${1:-1.5.1}"
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="$SKILL_DIR/dist"
URL="https://github.com/skylot/jadx/releases/download/v${VERSION}/jadx-${VERSION}.zip"

# Prefer the shared $REKIT_HOME/bin (host-gating always searches it); fall back to
# the skill's own bin/ when the shared dir can't be created.
REKIT_HOME_DIR="${REKIT_HOME:-$HOME/.rekit}"
if mkdir -p "$REKIT_HOME_DIR/bin" 2>/dev/null; then
    BIN_DIR="$REKIT_HOME_DIR/bin"
else
    BIN_DIR="$SKILL_DIR/bin"
fi

echo "jadx: fetching v${VERSION}"
command -v java >/dev/null 2>&1 || { echo "jadx: WARNING — no 'java' on PATH; jadx needs a JVM to run" >&2; }

mkdir -p "$DIST_DIR"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Download (curl or wget).
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$tmp/jadx.zip"
elif command -v wget >/dev/null 2>&1; then
    wget -qO "$tmp/jadx.zip" "$URL"
else
    echo "jadx: need curl or wget to download" >&2; exit 1
fi

# jadx ships as bin/ + lib/ at the archive root — unzip into dist/, then expose
# the launcher at bin/jadx (a thin wrapper so resolution finds a single file).
rm -rf "$DIST_DIR"/* ; mkdir -p "$DIST_DIR"
unzip -q -o "$tmp/jadx.zip" -d "$DIST_DIR"
[ -f "$DIST_DIR/bin/jadx" ] || { echo "jadx: unexpected archive layout" >&2; exit 1; }

cat > "$BIN_DIR/jadx" <<EOF
#!/bin/sh
exec "$DIST_DIR/bin/jadx" "\$@"
EOF
chmod +x "$BIN_DIR/jadx"

echo "jadx: installed -> $BIN_DIR/jadx (v${VERSION})"
"$BIN_DIR/jadx" --version 2>/dev/null || true
