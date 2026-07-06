#!/bin/sh
# Fetch dex2jar and install the launcher where rekit's host-gating looks: the
# shared $REKIT_HOME/bin (so one install serves every skill), falling back to the
# skill's own bin/ if that isn't writable. Idempotent: re-running re-installs.
# Usage: scripts/fetch.sh [url] [version]   (defaults pinned below)
set -eu

VERSION="${2:-v2.4}"
URL="${1:-https://github.com/pxb1988/dex2jar/releases/download/${VERSION}/dex-tools-${VERSION}.zip}"
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="$SKILL_DIR/dist"

# Prefer the shared $REKIT_HOME/bin (host-gating always searches it); fall back to
# the skill's own bin/ when the shared dir can't be created.
REKIT_HOME_DIR="${REKIT_HOME:-$HOME/.rekit}"
if mkdir -p "$REKIT_HOME_DIR/bin" 2>/dev/null; then
    BIN_DIR="$REKIT_HOME_DIR/bin"
else
    BIN_DIR="$SKILL_DIR/bin"
fi

echo "dex2jar: fetching ${VERSION}"
command -v java >/dev/null 2>&1 || { echo "dex2jar: WARNING — no 'java' on PATH; dex2jar needs a JVM to run" >&2; }

mkdir -p "$DIST_DIR"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Download (curl or wget).
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$tmp/dex2jar.zip"
elif command -v wget >/dev/null 2>&1; then
    wget -qO "$tmp/dex2jar.zip" "$URL"
else
    echo "dex2jar: need curl or wget to download" >&2; exit 1
fi

# dex2jar ships as a dex-tools-*/ dir of d2j-*.sh launchers + lib/ — unzip into
# dist/, mark the launchers executable, then expose a single-file wrapper at
# bin/d2j-dex2jar.sh so resolution finds one launcher.
rm -rf "$DIST_DIR"/* ; mkdir -p "$DIST_DIR"
unzip -q -o "$tmp/dex2jar.zip" -d "$DIST_DIR"
chmod +x "$DIST_DIR"/dex-tools-*/d2j-*.sh 2>/dev/null || true
REAL="$(ls "$DIST_DIR"/dex-tools-*/d2j-dex2jar.sh 2>/dev/null | head -n1)"
[ -n "$REAL" ] && [ -f "$REAL" ] || { echo "dex2jar: unexpected archive layout" >&2; exit 1; }

cat > "$BIN_DIR/d2j-dex2jar.sh" <<EOF
#!/bin/sh
exec "$REAL" "\$@"
EOF
chmod +x "$BIN_DIR/d2j-dex2jar.sh"

echo "dex2jar: installed -> $BIN_DIR/d2j-dex2jar.sh (${VERSION})"
"$BIN_DIR/d2j-dex2jar.sh" --help >/dev/null 2>&1 || true
