#!/bin/sh
# Drive Ghidra headless the way the transform does. Usage: decompile.sh <binary> <out_dir>
set -eu
[ $# -eq 2 ] || { echo "usage: decompile.sh <binary> <out_dir>" >&2; exit 2; }
INPUT="$1"; OUT="$2"
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS_DIR="$SKILL_DIR/scripts"
REKIT_BIN="${REKIT_HOME:-$HOME/.rekit}/bin"

# Resolve the launcher: GHIDRA_HOME (support/) → shared $REKIT_HOME/bin → skill bin/ → PATH.
if [ -n "${GHIDRA_HOME:-}" ] && [ -x "$GHIDRA_HOME/support/analyzeHeadless" ]; then
    HEADLESS="$GHIDRA_HOME/support/analyzeHeadless"
elif [ -x "$REKIT_BIN/analyzeHeadless" ]; then
    HEADLESS="$REKIT_BIN/analyzeHeadless"
elif [ -x "$SKILL_DIR/bin/analyzeHeadless" ]; then
    HEADLESS="$SKILL_DIR/bin/analyzeHeadless"
elif command -v analyzeHeadless >/dev/null 2>&1; then
    HEADLESS="analyzeHeadless"
else
    echo "ghidra not found — run scripts/fetch.sh or set GHIDRA_HOME" >&2; exit 1
fi

mkdir -p "$OUT/project"
exec "$HEADLESS" "$OUT/project" rekit -import "$INPUT" \
    -scriptPath "$SCRIPTS_DIR" -postScript DecompileToC.java "$OUT/decompiled.c" \
    -deleteProject
