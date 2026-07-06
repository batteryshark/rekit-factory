#!/bin/sh
# Fetch Ghidra into the skill's dist/ + expose analyzeHeadless where rekit's
# host-gating looks: the shared $REKIT_HOME/bin (so one install serves every
# skill), falling back to the skill's own bin/ if that isn't writable.
# Idempotent: re-running re-installs.
# Usage: scripts/fetch.sh [download-url]   (default: pinned 11.3.2 asset below)
set -eu

# Pinned release. GitHub's asset name carries a build-date suffix that changes per
# build, so we can't derive it from the version alone — hardcode the known asset
# URL and let the caller override with $1 if this one ever 404s.
VERSION="11.3.2"
DEFAULT_URL="https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_11.3.2_build/ghidra_11.3.2_PUBLIC_20250415.zip"
URL="${1:-$DEFAULT_URL}"

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

echo "ghidra: fetching v${VERSION}"
echo "ghidra: from $URL"
echo "ghidra: (if this 404s, grab the current asset URL from"
echo "        https://github.com/NationalSecurityAgency/ghidra/releases and pass it as \$1)"
command -v java >/dev/null 2>&1 || { echo "ghidra: WARNING — no 'java' on PATH; Ghidra needs a JVM to run" >&2; }

mkdir -p "$DIST_DIR"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Download (curl or wget).
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$tmp/ghidra.zip"
elif command -v wget >/dev/null 2>&1; then
    wget -qO "$tmp/ghidra.zip" "$URL"
else
    echo "ghidra: need curl or wget to download" >&2; exit 1
fi

# Ghidra ships as a single ghidra_<ver>_PUBLIC/ dir at the archive root — unzip
# into dist/, then expose the launcher at bin/analyzeHeadless (a thin wrapper so
# resolution finds a single file; the real one lives under support/, not bin/).
rm -rf "$DIST_DIR"/* ; mkdir -p "$DIST_DIR"
unzip -q -o "$tmp/ghidra.zip" -d "$DIST_DIR"
HEADLESS="$(find "$DIST_DIR" -type f -name analyzeHeadless -path '*/support/*' | head -n1)"
[ -n "$HEADLESS" ] || { echo "ghidra: unexpected archive layout (no support/analyzeHeadless)" >&2; exit 1; }

cat > "$BIN_DIR/analyzeHeadless" <<EOF
#!/bin/sh
exec "$HEADLESS" "\$@"
EOF
chmod +x "$BIN_DIR/analyzeHeadless"

echo "ghidra: installed -> $BIN_DIR/analyzeHeadless (v${VERSION})"
