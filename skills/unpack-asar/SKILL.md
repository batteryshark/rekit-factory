---
name: unpack-asar
capability: unpack
accepts: [archive/asar]
emits: [tree]
tier: read-only
run: scripts/run.sh
keywords: [asar, electron, unpack, extract, archive, pickle]
description: >-
  Unpack an Electron asar archive into a tree of its bundled files. Pure Python
  (stdlib only), no host tool: parses the Chromium Pickle header and slices each
  file's bytes out of the payload. Safe against path traversal — entry names must
  be single path components, and anything with a separator or `..` is rejected
  (asar bundles are untrusted RE targets).
---

# unpack-asar — Electron asar → tree

An Electron `.asar` archive is an app bundle framed with a Chromium Pickle header:

```
[uint32 = 4][uint32 = header_size][uint32 = payload_size][uint32 = json_len]
[json header (json_len bytes, padded to 4)]
[concatenated file data ...]
```

The JSON header is a tree of `{"files": {name: entry}}`; each file entry carries a
string `offset` (relative to the data base = `8 + header_size`) and `size`, and
directories nest under `files`. This skill parses that header and writes every
file back out under `<out_dir>`, reconstructing the directory tree.

## How rekit drives it

`scripts/run.sh <input.asar> <out_dir>` runs the bundled stdlib extractor
(`scripts/extract.py`) with the system `python3` — no host tool, no venv:

```sh
python3 scripts/extract.py <input.asar> <out_dir>
```

The runner executes it under a macOS Seatbelt sandbox: no network, writes confined
to `<out_dir>`. The input asar is treated as untrusted — a hostile bundle can't
exfiltrate or escape, and unsafe entry names (`..`, path separators, absolute
paths) are rejected rather than followed.

## Output

A tree of the bundled files under `<out_dir>`, classified `tree`, re-entering the
ledger so the recovered app source drives the next loop round.

Because it is pure stdlib and touches no network, this skill is tier `read-only`:
the runner auto-runs it without asking the human.
