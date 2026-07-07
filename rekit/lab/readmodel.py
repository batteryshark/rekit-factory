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
import os
from pathlib import Path
from typing import Any

from ..ledger import runlog as _runlog
from ..ledger.home import projects_root
from ..ledger.ledger import LEDGER_FILENAME, load as _load_ledger, read_events as _read_events
from ..human import inbox as _inbox

#: Derived statuses (not written by any single log — see module docstring).
BLOCKED = "blocked"
SUSPENDED = "suspended"

#: Ledger kinds a goalpack's rendered report is recorded under (see
#: ``goalpacks._persist_report`` — kept as literals so this stays a light leaf).
REPORT_MARKDOWN_KIND = "report/markdown"
REPORT_JSON_KIND = "report/json"

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
    # A pending decision only means blocked/suspended while the run is *in flight*;
    # once it has ended (e.g. reaped after its process died) show the terminal
    # status even if a stale question lingers.
    if pending and not run_state.ended_at:
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


def _ledger_msg(t: str, p: dict[str, Any]) -> str:
    """Render one ledger event to a compact activity line."""
    if t == "artifact_added":
        a = p.get("artifact", {})
        who = a.get("path") or (a.get("contentHash") or "")[:12]
        return f"+ {a.get('kind', 'artifact')} {who}"
    if t == "derivation_recorded":
        return f"{p.get('transform', '?')} → {len(p.get('outputs', []))} output(s)"
    if t == "lead_recorded":
        return f"want {p.get('capability', '?')} for {p.get('kind', '?')}"
    if t == "finding_recorded":
        fin = p.get("finding", {})
        return fin.get("note") or fin.get("text") or fin.get("summary") or "finding"
    if t == "artifact_analyzed":
        return f"analyzed {(p.get('artifactHash') or '')[:12]}"
    return t


def _run_msg(t: str, p: dict[str, Any]) -> str:
    """Render one run event to a compact activity line."""
    if t == "run_started":
        return f"goal: {(p.get('goal') or '')[:80]} · {p.get('harness', '?')} · {p.get('tier', '?')}"
    if t == "round_started":
        return f"round {p.get('index', '?')} · tier {p.get('tier', '?')}"
    if t == "round_ended":
        return (f"round {p.get('index', '?')} +{p.get('findings', 0)}f "
                f"+{p.get('leads', 0)}l +{p.get('derivations', 0)}d")
    if t == "status_changed":
        r = p.get("reason")
        return f"→ {p.get('status', '?')}" + (f" · {r}" if r else "")
    if t == "step":
        return p.get("text", "")
    if t == "run_ended":
        return f"done={p.get('done')} · {p.get('reason', '')}"
    return t


def _event_row(source: str, ev: Any) -> dict[str, Any]:
    msg = _run_msg(ev.type, ev.payload) if source == "run" else _ledger_msg(ev.type, ev.payload)
    return {"source": source, "type": ev.type, "ts": ev.ts, "seq": ev.seq, "msg": msg}


def event_stream(project_dir: str | Path, *, limit: int = 200) -> list[dict[str, Any]]:
    """Merge ``ledger.jsonl`` + ``run.jsonl`` into one chronological activity feed
    (the observability pane, E7.2). Oldest→newest, capped to the last ``limit``."""
    d = Path(project_dir)
    rows = [_event_row("ledger", ev) for ev in _read_events(d / LEDGER_FILENAME)]
    rows += [_event_row("run", ev) for ev in _runlog.read_run_events(d / _runlog.RUN_LOG_FILENAME)]
    # By timestamp; run events before ledger within the same second (deterministic).
    rows.sort(key=lambda r: (r["ts"], 0 if r["source"] == "run" else 1, r["seq"]))
    return rows[-limit:]


def project_detail(project_dir: str | Path) -> dict[str, Any]:
    """The full folded view for one project (E7.1 ledger browser + E7.2 activity):
    everything :func:`project_view` returns, plus the findings, leads, artifacts,
    derivations, and the merged event stream."""
    d = Path(project_dir)
    view = project_view(d)
    ledger = _load_ledger(d / LEDGER_FILENAME)
    view["findings"] = ledger.findings()
    view["leads"] = [dict(v) for v in ledger.leads.values()]
    view["artifacts"] = [
        {"id": e.artifact.id, "kind": e.artifact.kind, "path": e.artifact.path,
         "isTree": e.is_tree, "analyzed": e.analyzed, "findings": len(e.findings)}
        for e in ledger.entries.values()
    ]
    view["derivations"] = [
        {"transform": dv.transform, "capability": dv.capability, "inputHash": ih,
         "outputs": [{"id": o.id, "kind": o.kind, "path": o.path} for o in dv.outputs]}
        for (_t, ih), dv in ledger.derivations.items()
    ]
    view["hasReport"] = any(
        e.artifact.kind in (REPORT_MARKDOWN_KIND, REPORT_JSON_KIND)
        for e in ledger.entries.values())
    view["events"] = event_stream(d)
    return view


def _read_text(path: str | None) -> str | None:
    """Read a file's text, best-effort — a missing/unreadable report is just None."""
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def project_report(project_dir: str | Path) -> dict[str, Any]:
    """The goalpack's rendered report for one project, read from disk (E7.1).

    Finds the ``report/markdown`` and ``report/json`` artifacts in the ledger and
    reads their content. Returns ``{"hasReport", "markdown", "json", "meta"}`` —
    ``markdown``/``json`` are None when the goalpack rendered no report (an
    *act*-goal) or nothing has run yet. The last-recorded report wins, so a re-run
    shows the freshest one.
    """
    d = Path(project_dir)
    ledger = _load_ledger(d / LEDGER_FILENAME)
    markdown = json_text = None
    meta: dict[str, Any] = {}
    for e in ledger.entries.values():
        a = e.artifact
        if a.kind == REPORT_MARKDOWN_KIND:
            text = _read_text(a.path)
            if text is not None:
                markdown = text
                meta = dict(a.meta or {})
        elif a.kind == REPORT_JSON_KIND:
            text = _read_text(a.path)
            if text is not None:
                json_text = text
                if not meta:
                    meta = dict(a.meta or {})
    return {"hasReport": bool(markdown or json_text),
            "markdown": markdown, "json": json_text, "meta": meta}


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


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` names a live process (signal 0 probes without killing)."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, just not ours
    except (OSError, ValueError):
        return False
    return True


def reap_stale(root: str | Path | None = None) -> list[str]:
    """Mark 'running' runs whose owning process is gone as ended — the zombie sweep.

    A run interrupted by a killed server (not a graceful Stop) leaves ``run.jsonl``
    frozen mid-round, so it shows as a 'running' card forever. This appends a
    terminal ``run_ended`` for any run whose recorded pid is no longer alive, and
    expires its pending decisions so the Inbox clears too. Called on server start
    (and periodically). A genuinely live run — even a long pi call or one blocked
    on a decision — has a live owning pid and is left untouched. Returns the reaped
    project ids.
    """
    base = Path(root) if root is not None else projects_root()
    if not base.is_dir():
        return []
    reaped: list[str] = []
    for child in base.iterdir():
        if not child.is_dir() or not (child / "project.json").exists():
            continue
        state = _runlog.load_run_state(child / _runlog.RUN_LOG_FILENAME)
        if state.status == _runlog.RUNNING and not _pid_alive(state.pid):
            _runlog.RunLog(child).run_ended(
                done=False, status=_runlog.IDLE,
                reason="interrupted — owning process ended")
            for q in _inbox.pending_questions(child):
                _inbox.answer(child, q["id"], "expired")
            reaped.append(child.name)
    return reaped
