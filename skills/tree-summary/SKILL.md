---
name: tree-summary
capability: survey
accepts: [tree]
emits: [file]
tier: read-only
run: scripts/run.sh
keywords: [tree, summary, overview, survey, outline, structure, layout, tests, docs, source, python, go, rust, swift]
description: >-
  A one-shot structural overview of an arbitrary source tree — file/kind counts,
  top-level layout, test count, and a doc map + ordered outline of docs / releases
  / tests. Pure stdlib, read-only, no host tool. A good first round for any source
  project.
---

# tree-summary — source tree → structural overview

A read-only first pass over an arbitrary source tree: it surveys the layout so the
loop knows what it is looking at before it commits to anything expensive. Accepts a
`tree` (any directory) and emits a `file` pair — a machine-readable JSON and a
human-readable Markdown report — that catalog the shape of the project.

## How rekit drives it

`scripts/run.sh <input> <out_dir>` runs the bundled stdlib surveyor
(`scripts/summarize.py`) with the system `python3` — no host tool, no venv:

```sh
python3 scripts/summarize.py <input> <out_dir>
```

The runner executes it under a macOS Seatbelt sandbox: no network, writes confined
to `<out_dir>`. The input tree is walked read-only — nothing under it is created,
modified, or deleted. Noise directories (`.git`, `node_modules`, `__pycache__`,
`.venv`, `dist`, `build`, `target`, …) are skipped, and the walk is bounded so a
huge or pathological tree can't run away (it notes when it truncates).

## Output

Two files under `<out_dir>`, both classified `file`:

- `tree-summary.json` — the structured survey a brain can parse: total files/dirs,
  a count of files by extension, the top-level layout (each child dir with its
  recursive file count, plus top-level files), a test count + list, a doc map, and
  a best-effort releases hint.
- `tree-summary.md` — a human-readable report: the counts and layout, then an
  ORDERED OUTLINE cataloguing the doc files, the release/changelog files, and the
  test files in one bounded, readable listing (the "visual tree viewer" half).

Because it is pure stdlib and touches no network, this skill is tier `read-only`:
the runner auto-runs it without asking the human. It is a natural first round for
any Python / Go / Rust / Swift (or mixed) project.
