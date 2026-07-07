#!/bin/sh
# Survey a source tree into a structural overview. Usage: run.sh <input> <out_dir>
# Pure stdlib — runs the bundled surveyor with the system python3, no host tool.
set -eu
[ $# -eq 2 ] || { echo "usage: run.sh <input> <out_dir>" >&2; exit 2; }
INPUT="$1"; OUT="$2"
SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$OUT"
exec python3 "$SCRIPTS_DIR/summarize.py" "$INPUT" "$OUT"
