#!/usr/bin/env python3
"""Unpack a zip-family archive into a directory tree — pure stdlib.

A pure-stdlib zip-family extractor for use as a rekit builtin skill — extract +
zip-slip guard, self-contained. Invoked by ``run.sh`` as::

    extract.py <input> <out_dir>

Accepts any zip container (zip / jar / apk / aar / ipa). Safe against zip-slip: no
member may escape ``<out_dir>`` (these archives are untrusted RE targets), so a
member resolving to ``../`` or an absolute path is rejected before extraction.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path


def _extract(src: str, out_dir: str) -> None:
    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)
    dest_root = dest.resolve()
    with zipfile.ZipFile(src) as zf:
        for member in zf.namelist():
            target = (dest / member).resolve()
            # Reject anything that would land outside the output dir (../, absolute
            # paths, symlink-style escapes): a hostile archive can't write elsewhere.
            if target != dest_root and dest_root not in target.parents:
                raise ValueError(f"unsafe archive member escapes output dir: {member!r}")
        zf.extractall(dest)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: extract.py <input> <out_dir>", file=sys.stderr)
        return 2
    _extract(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
