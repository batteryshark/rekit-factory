#!/usr/bin/env python3
"""Unpack an Electron asar archive into a directory tree — pure stdlib.

A pure-stdlib asar parser for use as a rekit builtin skill — parse + extract,
self-contained. Invoked by ``run.sh`` as::

    extract.py <input.asar> <out_dir>

asar layout (Chromium Pickle framing)::

    [uint32 = 4][uint32 = header_size][uint32 = payload_size][uint32 = json_len]
    [json header (json_len bytes, padded to 4)]
    [concatenated file data ...]

The JSON header is a tree of ``{"files": {name: entry}}`` where a file entry has a
string ``offset`` (relative to the data base = ``8 + header_size``) and ``size``;
directories nest under ``files``. Safe against path traversal — entry names are
single components and anything with a separator or ``..`` is rejected (asar bundles
are untrusted RE targets).
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path


def _parse(path: str) -> tuple[dict, int, bytes]:
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 16:
        raise ValueError("not an asar archive (too short)")
    magic, header_size, _payload_size, json_len = struct.unpack("<IIII", data[0:16])
    if magic != 4:
        raise ValueError("not an asar archive (bad header framing)")
    if 16 + json_len > len(data):
        raise ValueError("asar header runs past end of file")
    header = json.loads(data[16:16 + json_len].decode("utf-8"))
    data_base = 8 + header_size
    return header, data_base, data


def _extract(node: dict, base: int, data: bytes, dest: Path) -> None:
    for name, entry in (node.get("files") or {}).items():
        if name in (".", "..") or "/" in name or "\\" in name or name.startswith(("/", "\\")):
            raise ValueError(f"unsafe asar entry name: {name!r}")
        target = dest / name
        if not isinstance(entry, dict):
            continue
        if "files" in entry:
            target.mkdir(parents=True, exist_ok=True)
            _extract(entry, base, data, target)
        elif entry.get("unpacked"):
            continue  # stored outside the archive (app.asar.unpacked) — not present here
        elif "link" in entry:
            continue  # skip symlinks for safety
        elif "offset" in entry and "size" in entry:
            start = base + int(entry["offset"])
            end = start + int(entry["size"])
            if start < 0 or end > len(data):
                raise ValueError(f"asar entry {name!r} points outside the archive")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data[start:end])


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: extract.py <input.asar> <out_dir>", file=sys.stderr)
        return 2
    src, out_dir = argv[1], argv[2]
    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)
    header, base, data = _parse(src)
    _extract(header, base, data, dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
