#!/bin/sh
# Drive dex2jar the way the transform does. Usage: decompile.sh <input.dex|.apk> <out_dir>
set -eu
[ $# -eq 2 ] || { echo "usage: decompile.sh <input> <out_dir>" >&2; exit 2; }
INPUT="$1"; OUT="$2"
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REKIT_BIN="${REKIT_HOME:-$HOME/.rekit}/bin"

# Resolve the launcher: DEX2JAR_HOME → shared $REKIT_HOME/bin → skill bin/ → PATH.
if [ -n "${DEX2JAR_HOME:-}" ] && [ -x "$DEX2JAR_HOME/d2j-dex2jar.sh" ]; then
    D2J="$DEX2JAR_HOME/d2j-dex2jar.sh"
elif [ -n "${DEX2JAR_HOME:-}" ] && [ -x "$DEX2JAR_HOME/bin/d2j-dex2jar.sh" ]; then
    D2J="$DEX2JAR_HOME/bin/d2j-dex2jar.sh"
elif [ -x "$REKIT_BIN/d2j-dex2jar.sh" ]; then
    D2J="$REKIT_BIN/d2j-dex2jar.sh"
elif [ -x "$SKILL_DIR/bin/d2j-dex2jar.sh" ]; then
    D2J="$SKILL_DIR/bin/d2j-dex2jar.sh"
elif command -v d2j-dex2jar.sh >/dev/null 2>&1; then
    D2J="d2j-dex2jar.sh"
else
    echo "dex2jar not found — run scripts/fetch.sh or set DEX2JAR_HOME" >&2; exit 1
fi

mkdir -p "$OUT"
exec "$D2J" -o "$OUT/classes.jar" "$INPUT"
