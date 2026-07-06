#!/bin/sh
# Install ilspycmd where rekit's host-gating looks: the shared $REKIT_HOME/bin (so
# one install serves every skill), falling back to the skill's own bin/ if that
# isn't writable. Idempotent: re-running re-installs.
# Usage: scripts/fetch.sh [version]   (default: latest)
set -eu

VERSION="${1:-}"
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Prefer the shared $REKIT_HOME/bin (host-gating always searches it); fall back to
# the skill's own bin/ when the shared dir can't be created.
REKIT_HOME_DIR="${REKIT_HOME:-$HOME/.rekit}"
if mkdir -p "$REKIT_HOME_DIR/bin" 2>/dev/null; then
    BIN_DIR="$REKIT_HOME_DIR/bin"
else
    BIN_DIR="$SKILL_DIR/bin"
fi

# ilspycmd is a dotnet global tool — no dotnet, no install (and no run). Fail loud
# so the caller knows the .NET SDK is the missing piece, not a network hiccup.
command -v dotnet >/dev/null 2>&1 || {
    echo "ilspy: 'dotnet' not on PATH — ilspycmd is a .NET tool and needs the .NET SDK" >&2
    echo "ilspy: install it from https://dotnet.microsoft.com/download and re-run" >&2
    exit 1
}

mkdir -p "$BIN_DIR"

echo "ilspy: installing ilspycmd${VERSION:+ v$VERSION} into $BIN_DIR"
# --tool-path drops a self-contained 'ilspycmd' launcher directly in bin/ so
# resolution finds a single file (no global ~/.dotnet/tools indirection).
if [ -n "$VERSION" ]; then
    dotnet tool install ilspycmd --tool-path "$BIN_DIR" --version "$VERSION"
else
    dotnet tool install ilspycmd --tool-path "$BIN_DIR"
fi

echo "ilspy: installed -> $BIN_DIR/ilspycmd"
"$BIN_DIR/ilspycmd" --version 2>/dev/null || true
