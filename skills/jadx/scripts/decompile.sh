#!/bin/sh
# Drive jadx the way the transform does. Usage: decompile.sh <input.apk|.dex> <out_dir>
set -eu
[ $# -eq 2 ] || { echo "usage: decompile.sh <input> <out_dir>" >&2; exit 2; }
INPUT="$1"; OUT="$2"
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REKIT_BIN="${REKIT_HOME:-$HOME/.rekit}/bin"

# Resolve the launcher: JADX_HOME → shared $REKIT_HOME/bin → skill bin/ → PATH.
if [ -n "${JADX_HOME:-}" ] && [ -x "$JADX_HOME/bin/jadx" ]; then
    JADX="$JADX_HOME/bin/jadx"
elif [ -x "$REKIT_BIN/jadx" ]; then
    JADX="$REKIT_BIN/jadx"
elif [ -x "$SKILL_DIR/bin/jadx" ]; then
    JADX="$SKILL_DIR/bin/jadx"
elif command -v jadx >/dev/null 2>&1; then
    JADX="jadx"
else
    echo "jadx not found — run scripts/fetch.sh or set JADX_HOME" >&2; exit 1
fi

mkdir -p "$OUT"
exec "$JADX" --no-res --no-debug-info -q -d "$OUT" "$INPUT"
