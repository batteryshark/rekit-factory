"""The lab read-model — Mission Control as a pure fold over ``$REKIT_HOME`` (E7.0).

The UI reaches into no rekit internals; it reads files. This module walks
``$REKIT_HOME/projects/*`` and folds each project's three append-only logs into
one JSON-serialisable view:

- ``ledger.jsonl``  → what was discovered (artifacts, findings, leads).
- ``run.jsonl``     → what the run is doing (status, round, tier, cost).
- ``inbox.jsonl``   → decisions waiting on a human.

The one thing no single log records — and this module derives — is that a run is
**blocked** (or **suspended**, for a missing-tool decision) exactly when it has a
pending inbox question. That join is deliberately kept out of the run log so the
loop never needs to know a human channel exists; the read-model owns it.

Pure functions over the filesystem, so a watcher can call them on every change and
diff the result. ``rekit serve`` is a thin HTTP/websocket wrapper over these; a
TUI would be a second consumer. Pure stdlib.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..ledger import runlog as _runlog
from ..ledger.home import projects_root
from ..ledger.ledger import LEDGER_FILENAME, load as _load_ledger
from ..human import inbox as _inbox

#: Derived statuses (not written by any single log — see module docstring).
BLOCKED = "blocked"
SUSPENDED = "suspended"

#: Fleet sort order: what needs a human first, finished work last.
_RANK = {BLOCKED: 0, SUSPENDED: 1, _runlog.RUNNING: 2,
         _runlog.IDLE: 3, _runlog.FAILED: 4, _runlog.DONE: 5}


def _read_meta(project_dir: Path) -> dict[str, Any]:
    try:
        return json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _ledger_summary(project_dir: Path) -> dict[str, Any]:
    """A compact fold of ``ledger.jsonl`` — the counts the fleet card shows."""
    ledger = _load_ledger(project_dir / LEDGER_FILENAME)
    return {
        "artifacts": len(ledger.entries),
        "kinds": dict(ledger.kinds),
        "findings": len(ledger.findings()),
        "leads": len(ledger.leads),
        "derivations": len(ledger.derivations),
        "trees": len(ledger.trees),
    }


def _derive_status(run_state: _runlog.RunState, pending: list[dict[str, Any]]) -> str:
    """The read-model join: a pending decision means the run is waiting on a human.

    A pending *tool* decision suspends the run; any other pending decision blocks
    it. With nothing pending, the run's own status stands.
    """
    if pending:
        if any(q.get("kind") == _inbox.KIND_TOOL for q in pending):
            return SUSPENDED
        return BLOCKED
    return run_state.status


def project_view(project_dir: str | Path) -> dict[str, Any]:
    """Fold one project's dir into a full view: meta + run + ledger + pending."""
    d = Path(project_dir)
    meta = _read_meta(d)
    run_state = _runlog.load_run_state(d / _runlog.RUN_LOG_FILENAME)
    pending = _inbox.pending_questions(d)
    return {
        "id": meta.get("id", d.name),
        "target": meta.get("target"),
        "createdAt": meta.get("createdAt"),
        "lastOpenedAt": meta.get("lastOpenedAt"),
        "status": _derive_status(run_state, pending),
        "needsYou": bool(pending),
        "run": run_state.to_dict(),
        "ledger": _ledger_summary(d),
        "pending": pending,
    }


def fleet(root: str | Path | None = None) -> list[dict[str, Any]]:
    """Every project under ``$REKIT_HOME/projects`` as a view, needs-you first.

    ``root`` overrides the projects dir (for tests); otherwise it honours
    ``$REKIT_HOME`` via :func:`~rekit.ledger.home.projects_root`. A dir counts as a
    project iff it has a ``project.json``.
    """
    base = Path(root) if root is not None else projects_root()
    if not base.is_dir():
        return []
    views = [
        project_view(child)
        for child in base.iterdir()
        if child.is_dir() and (child / "project.json").exists()
    ]
    # Stable two-key sort: newest-opened first, then hoist by status rank.
    views.sort(key=lambda v: v.get("lastOpenedAt") or "", reverse=True)
    views.sort(key=lambda v: _RANK.get(v["status"], 9))
    return views


def health(views: list[dict[str, Any]]) -> dict[str, int]:
    """Status counts for the fleet health ring — ``{status: n, ..., total: N}``."""
    counts: dict[str, int] = {}
    for v in views:
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    counts["total"] = len(views)
    return counts
