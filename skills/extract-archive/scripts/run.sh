#!/bin/sh
# Unpack a zip-family archive into a tree. Usage: run.sh <input> <out_dir>
# Pure stdlib — runs the bundled extractor with the system python3, no host tool.
set -eu
[ $# -eq 2 ] || { echo "usage: run.sh <input> <out_dir>" >&2; exit 2; }
INPUT="$1"; OUT="$2"
SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$OUT"
exec python3 "$SCRIPTS_DIR/extract.py" "$INPUT" "$OUT"
