---
name: extract-archive
capability: unpack
accepts: [archive/zip, archive/jar, archive/apk, archive/aar, archive/ipa]
emits: [tree]
tier: read-only
run: scripts/run.sh
keywords: [zip, jar, apk, aar, ipa, unzip, unpack, extract, archive]
description: >-
  Unpack a zip-family archive (zip / jar / apk / aar / ipa — all zip containers)
  into a tree of its members. Pure Python (stdlib `zipfile`), no host tool. Safe
  against zip-slip: no member may escape the output directory (these archives are
  untrusted RE targets).
---

# extract-archive — zip family → tree

A reference unpack that exercises the whole artifact-graph contract with pure
stdlib and no host tool. Accepts the zip container family — `.zip`, `.jar`,
`.apk`, `.aar`, `.ipa` — and emits a `tree` of the extracted members.

## How rekit drives it

`scripts/run.sh <input> <out_dir>` runs the bundled stdlib extractor
(`scripts/extract.py`) with the system `python3` — no host tool, no venv:

```sh
python3 scripts/extract.py <input> <out_dir>
```

The runner executes it under a macOS Seatbelt sandbox: no network, writes confined
to `<out_dir>`. The archive is treated as untrusted — every member is checked
before extraction and any that would resolve outside `<out_dir>` (`../`, absolute
paths, symlink-style escapes) is rejected, so a hostile archive can't write
elsewhere (zip-slip).

## Output

A tree of the extracted members under `<out_dir>`, classified `tree`, re-entering
the ledger so the recovered contents drive the next loop round.

Because it is pure stdlib and touches no network, this skill is tier `read-only`:
the runner auto-runs it without asking the human.
