#!/usr/bin/env python3
"""Survey an arbitrary source tree into a structural overview — pure stdlib.

A pure-stdlib, read-only surveyor for use as a rekit builtin skill. Invoked by
``run.sh`` as::

    summarize.py <input_tree> <out_dir>

It walks ``<input_tree>`` (never writing to it), skipping noise directories and
bounding the work, then folds the shape of the project into two outputs under
``<out_dir>``:

* ``tree-summary.json`` — the structured survey (counts, layout, tests, docs,
  releases) a brain can parse.
* ``tree-summary.md`` — a human-readable report: the counts and top-level layout,
  then an ORDERED OUTLINE cataloguing the doc files, the release/changelog files,
  and the test files in one bounded listing.

Handles a non-existent / non-directory input by printing usage to stderr and
returning a nonzero code (mirrors extract.py's error handling). A good first round
for any Python / Go / Rust / Swift (or mixed) project.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

#: Directories never worth descending into — build output, VCS, caches, vendored deps.
NOISE_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    "target", ".idea", ".mypy_cache", ".pytest_cache", ".tox", ".gradle",
})

#: Cap on files scanned so a huge/pathological tree can't run away (noted if hit).
MAX_FILES = 50000

#: How many entries to keep in the .md listings before collapsing to "+N more".
LIST_CAP = 40
#: How many extensions to report.
EXT_CAP = 20

#: Directory basenames that mark a test grouping across languages.
TEST_DIR_NAMES = frozenset({"tests", "test", "spec", "__tests__"})


def _is_test_file(name: str) -> bool:
    """Whether a file name matches a common cross-language test pattern."""
    lower = name.lower()
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith("_test.py") or name.endswith("_test.go") or name.endswith("_test.rs"):
        return True
    if name.endswith("Test.java") or name.endswith("Tests.swift"):
        return True
    # *.spec.* / *.test.* (js/ts and friends): "core.spec.ts", "app.test.js".
    if ".spec." in lower or ".test." in lower:
        return True
    return False


def _is_doc_file(name: str) -> bool:
    """Whether a file name belongs in the doc map (README/markdown/license/etc.)."""
    upper = name.upper()
    if name.lower().endswith(".md"):
        return True
    for prefix in ("README", "CHANGELOG", "LICENSE", "CONTRIBUTING"):
        if upper.startswith(prefix):
            return True
    return False


def _is_release_file(name: str) -> bool:
    """Whether a file name is a release/changelog hint."""
    return name.upper().startswith("CHANGELOG")


def _rel(root: Path, path: Path) -> str:
    """POSIX-style path of ``path`` relative to ``root`` (stable across platforms)."""
    return Path(os.path.relpath(path, root)).as_posix()


def _survey(root: Path) -> dict:
    """Walk ``root`` read-only and fold its shape into a JSON-serialisable dict."""
    total_files = 0
    total_dirs = 0
    truncated = False
    ext_counts: dict[str, int] = {}
    # Recursive file count per immediate top-level child dir.
    toplevel_dir_files: dict[str, int] = {}
    toplevel_files: list[str] = []
    toplevel_dirs: set[str] = set()
    tests: list[str] = []
    test_dirs: list[str] = []
    docs: list[str] = []
    releases: list[str] = []
    has_releases_dir = False

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune noise dirs in place so os.walk never descends into them.
        dirnames[:] = sorted(d for d in dirnames if d not in NOISE_DIRS)
        here = Path(dirpath)
        rel_here = _rel(root, here)

        # Count directories (excluding the root itself) and note top-level children.
        for d in dirnames:
            total_dirs += 1
            child_rel = _rel(root, here / d)
            depth = 0 if rel_here == "." else rel_here.count("/") + 1
            if depth == 0:  # here is root -> d is a top-level child dir
                toplevel_dirs.add(d)
                toplevel_dir_files.setdefault(d, 0)
                if d in TEST_DIR_NAMES:
                    test_dirs.append(child_rel)
                if d == "releases":
                    has_releases_dir = True
            if d in TEST_DIR_NAMES and depth != 0:
                test_dirs.append(child_rel)

        # Which top-level bucket does this dir's files belong to (for the layout)?
        if rel_here == ".":
            top_bucket = None
        else:
            top_bucket = rel_here.split("/", 1)[0]

        for fn in filenames:
            if total_files >= MAX_FILES:
                truncated = True
                break
            total_files += 1
            rel_file = _rel(root, here / fn)

            ext = Path(fn).suffix.lower() or "(no ext)"
            ext_counts[ext] = ext_counts.get(ext, 0) + 1

            if top_bucket is None:
                toplevel_files.append(fn)
            else:
                toplevel_dir_files[top_bucket] = toplevel_dir_files.get(top_bucket, 0) + 1

            if _is_test_file(fn):
                tests.append(rel_file)
            if _is_doc_file(fn):
                docs.append(rel_file)
            if _is_release_file(fn):
                releases.append(rel_file)
        if truncated:
            break

    top_exts = sorted(ext_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:EXT_CAP]
    layout = [
        {"dir": d, "files": toplevel_dir_files.get(d, 0)}
        for d in sorted(toplevel_dirs)
    ]

    if has_releases_dir and "releases/" not in releases:
        releases = releases + ["releases/"]

    return {
        "root": root.name,
        "total_files": total_files,
        "total_dirs": total_dirs,
        "truncated": truncated,
        "by_extension": [{"ext": e, "count": c} for e, c in top_exts],
        "toplevel": {
            "dirs": layout,
            "files": sorted(toplevel_files),
        },
        "tests": {
            "count": len(tests) + len(test_dirs),
            "files": sorted(tests),
            "dirs": sorted(set(test_dirs)),
        },
        "docs": sorted(docs),
        "releases": {
            "has_releases_dir": has_releases_dir,
            "files": sorted(releases),
        },
    }


def _bounded(items: list[str], cap: int = LIST_CAP) -> tuple[list[str], int]:
    """Return ``items`` capped to ``cap`` plus the count of hidden extras."""
    if len(items) <= cap:
        return items, 0
    return items[:cap], len(items) - cap


def _render_md(data: dict) -> str:
    """Render the human-readable report with the ORDERED OUTLINE section."""
    lines: list[str] = []
    lines.append(f"# tree-summary: {data['root']}")
    lines.append("")
    lines.append("## Counts")
    lines.append("")
    lines.append(f"- files: {data['total_files']}")
    lines.append(f"- dirs: {data['total_dirs']}")
    lines.append(f"- tests: {data['tests']['count']}")
    lines.append(f"- docs: {len(data['docs'])}")
    if data["truncated"]:
        lines.append(f"- note: scan truncated at {MAX_FILES} files")
    lines.append("")

    lines.append("## Files by extension")
    lines.append("")
    if data["by_extension"]:
        for row in data["by_extension"]:
            lines.append(f"- `{row['ext']}`: {row['count']}")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Top-level layout")
    lines.append("")
    for row in data["toplevel"]["dirs"]:
        lines.append(f"- `{row['dir']}/` ({row['files']} files)")
    top_files, extra = _bounded(data["toplevel"]["files"])
    for fn in top_files:
        lines.append(f"- `{fn}`")
    if extra:
        lines.append(f"- +{extra} more")
    if not data["toplevel"]["dirs"] and not data["toplevel"]["files"]:
        lines.append("- (empty)")
    lines.append("")

    # ---- the ordered outline: docs, then releases, then tests -------------------
    lines.append("## Ordered outline")
    lines.append("")

    lines.append("### Docs")
    docs, extra = _bounded(data["docs"])
    if docs:
        for d in docs:
            lines.append(f"- {d}")
        if extra:
            lines.append(f"- +{extra} more")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("### Releases")
    rel_files, extra = _bounded(data["releases"]["files"])
    if rel_files:
        for r in rel_files:
            lines.append(f"- {r}")
        if extra:
            lines.append(f"- +{extra} more")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("### Tests")
    test_dirs, dextra = _bounded(data["tests"]["dirs"])
    test_files, fextra = _bounded(data["tests"]["files"])
    if test_dirs or test_files:
        for td in test_dirs:
            lines.append(f"- {td}/")
        if dextra:
            lines.append(f"- +{dextra} more dirs")
        for tf in test_files:
            lines.append(f"- {tf}")
        if fextra:
            lines.append(f"- +{fextra} more")
    else:
        lines.append("- (none)")
    lines.append("")

    return "\n".join(lines)


def _summarize(src: str, out_dir: str) -> None:
    root = Path(src)
    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)

    data = _survey(root)

    (dest / "tree-summary.json").write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (dest / "tree-summary.md").write_text(_render_md(data), encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: summarize.py <input_tree> <out_dir>", file=sys.stderr)
        return 2
    src = argv[1]
    if not os.path.isdir(src):
        print(f"usage: summarize.py <input_tree> <out_dir>: not a directory: {src!r}",
              file=sys.stderr)
        return 2
    _summarize(src, argv[2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
