#!/bin/sh
# Drive ilspycmd the way the transform does. Usage: decompile.sh <assembly.dll|.exe> <out_dir>
set -eu
[ $# -eq 2 ] || { echo "usage: decompile.sh <assembly.dll|.exe> <out_dir>" >&2; exit 2; }
INPUT="$1"; OUT="$2"
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REKIT_BIN="${REKIT_HOME:-$HOME/.rekit}/bin"

# Resolve the launcher: ILSPY_HOME → shared $REKIT_HOME/bin → skill bin/ → PATH.
if [ -n "${ILSPY_HOME:-}" ] && [ -x "$ILSPY_HOME/bin/ilspycmd" ]; then
    ILSPY="$ILSPY_HOME/bin/ilspycmd"
elif [ -n "${ILSPY_HOME:-}" ] && [ -x "$ILSPY_HOME/ilspycmd" ]; then
    ILSPY="$ILSPY_HOME/ilspycmd"
elif [ -x "$REKIT_BIN/ilspycmd" ]; then
    ILSPY="$REKIT_BIN/ilspycmd"
elif [ -x "$SKILL_DIR/bin/ilspycmd" ]; then
    ILSPY="$SKILL_DIR/bin/ilspycmd"
elif command -v ilspycmd >/dev/null 2>&1; then
    ILSPY="ilspycmd"
else
    echo "ilspycmd not found — run scripts/fetch.sh (needs dotnet) or set ILSPY_HOME" >&2; exit 1
fi

mkdir -p "$OUT"
exec "$ILSPY" "$INPUT" -o "$OUT" -p
