"""The persistent Project — a durable working area for one target (E1.1/E1.2).

A project is keyed by its target and lives under
``$REKIT_HOME/projects/<id>/``. Its ``id`` is a readable basename plus a short
hash of the target's absolute path, so the same target always maps to the same
project and is revisitable rather than ephemeral.

    $REKIT_HOME/projects/<id>/
      project.json     # meta: id, target, createdAt, lastOpenedAt
      ledger.jsonl     # the append-only typed event log (state = fold over this)

The :class:`Project` is the write API over the ledger: each ``record_*`` method
appends exactly one typed event to ``ledger.jsonl`` and folds it into the live
:class:`~.ledger.Ledger`. Because state is a fold, reopening a project replays
the log and reconstructs identical state — nothing is held only in memory.

Lifecycle: :func:`open_project` (open/create, keyed by target),
:func:`list_projects` (enumerate past projects), :func:`resume` (re-enter one by
id).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from . import artifacts as _artifacts, events as _events, home as _home
from .artifacts import Artifact
from .ledger import LEDGER_FILENAME, Ledger, load as _load_ledger

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def project_id(target: str) -> str:
    """A stable id for a target: readable basename + a short hash of its absolute
    path, so the same target always maps to the same project."""
    absolute = os.path.abspath(os.path.expanduser(str(target)))
    digest = hashlib.sha256(absolute.encode("utf-8")).hexdigest()[:12]
    base = _SAFE.sub("_", os.path.basename(os.path.normpath(absolute))) or "target"
    return f"{base}-{digest}"


class Project:
    """A persistent working area for one target: meta + an event-sourced ledger.

    Constructed via :func:`open_project` / :func:`resume`; not usually built
    directly. Holds a live :class:`Ledger` (the fold) that every ``record_*``
    method mutates by appending to the log.
    """

    def __init__(self, directory: str | Path, target: str, ledger: Ledger | None = None):
        self.dir = Path(directory)
        self.target = os.path.abspath(os.path.expanduser(str(target)))
        self.ledger = ledger if ledger is not None else Ledger()

    # -- identity / paths ---------------------------------------------------

    @property
    def id(self) -> str:
        return self.dir.name

    @property
    def meta_path(self) -> Path:
        return self.dir / "project.json"

    @property
    def ledger_path(self) -> Path:
        return self.dir / LEDGER_FILENAME

    def root_artifact(self) -> Artifact:
        """The target itself as an Artifact (a tree for a folder, else its kind)."""
        return _artifacts.from_path(self.target)

    # -- meta ---------------------------------------------------------------

    def read_meta(self) -> dict[str, Any]:
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _ensure(self, now: str | None = None) -> None:
        """Create the project dir and stamp meta; preserve ``createdAt`` across
        reopens and advance ``lastOpenedAt``."""
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = now or _events.utc_now()
        meta = self.read_meta()
        meta.setdefault("id", self.id)
        meta.setdefault("target", self.target)
        meta.setdefault("createdAt", stamp)
        meta["lastOpenedAt"] = stamp
        self.meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # -- event append + fold (the write API) --------------------------------

    def _append(self, etype: str, payload: dict[str, Any]) -> _events.Event:
        """Append one typed event to ``ledger.jsonl`` and fold it into the live
        ledger. The single choke point through which every mutation flows."""
        event = _events.Event(seq=self.ledger.seq + 1, type=etype,
                              ts=_events.utc_now(), payload=payload)
        with open(self.ledger_path, "a", encoding="utf-8") as fh:
            fh.write(event.to_json_line())
        self.ledger.apply(event)
        return event

    def add_artifact(self, artifact: Artifact, *, is_tree: bool = False) -> bool:
        """Record an artifact. Content-addressed: an artifact already in the
        ledger is a no-op (no event appended). Returns True if newly added."""
        if self.ledger.has_artifact(artifact.content_hash):
            return False
        self._append(_events.ARTIFACT_ADDED, {
            "artifact": _artifacts.to_dict(artifact),
            "isTree": bool(is_tree),
        })
        return True

    def record_derivation(self, transform: str, input_artifact: Artifact,
                          outputs: list[Artifact], *, capability: str | None = None,
                          add_outputs: bool = True) -> bool:
        """Record that ``transform`` turned ``input_artifact`` into ``outputs``.

        Content-addressed cache: if a derivation with the same ``(transform,
        input hash)`` was already recorded, this is a **no-op** — the crux of
        "run a second goal, re-derive nothing". Returns True if newly recorded.

        With ``add_outputs`` (default) each output artifact is also added to the
        ledger, so the revealed content becomes first-class in the same step.
        """
        input_hash = input_artifact.content_hash
        if self.ledger.has_derivation(transform, input_hash):
            return False
        self._append(_events.DERIVATION_RECORDED, {
            "transform": transform,
            "inputHash": input_hash,
            "capability": capability,
            "outputs": [_artifacts.to_dict(o) for o in outputs],
        })
        if add_outputs:
            for out in outputs:
                is_tree = out.kind == _artifacts.KIND_TREE
                self.add_artifact(out, is_tree=is_tree)
        return True

    def record_lead(self, capability: str, kind: str, *, requires: list[str] | None = None,
                    env_hints: list[str] | None = None, example_path: str | None = None) -> None:
        """Record that ``capability`` was wanted for ``kind`` but no provider was
        available (a "install X" lead). Repeated leads bump a count."""
        self._append(_events.LEAD_RECORDED, {
            "capability": capability,
            "kind": kind,
            "requires": list(requires or []),
            "envHints": list(env_hints or []),
            "examplePath": example_path,
        })

    def record_finding(self, artifact: Artifact, finding: dict) -> None:
        """Attach an analysis finding to an artifact (a generic dict — the
        workflow-specific report shape is E6; the ledger holds the substrate)."""
        self._append(_events.FINDING_RECORDED, {
            "artifactHash": artifact.content_hash,
            "finding": dict(finding),
        })

    def mark_analyzed(self, artifact: Artifact) -> None:
        """Mark an artifact/tree as analyzed."""
        self._append(_events.ARTIFACT_ANALYZED, {"artifactHash": artifact.content_hash})

    def reload(self) -> Ledger:
        """Rebuild the live ledger by replaying ``ledger.jsonl`` from scratch."""
        self.ledger = _load_ledger(self.ledger_path)
        return self.ledger


# -- lifecycle -------------------------------------------------------------


def open_project(target: str, *, now: str | None = None) -> Project:
    """Open (creating if needed) the persistent Project for ``target``.

    Keyed by target under ``$REKIT_HOME/projects/<id>/``. If the project already
    exists its ledger is replayed so the returned Project resumes prior state.
    """
    directory = _home.projects_root() / project_id(target)
    ledger = _load_ledger(directory / LEDGER_FILENAME) if directory.exists() else Ledger()
    project = Project(directory, target, ledger=ledger)
    project._ensure(now=now)
    return project


def list_projects() -> list[dict[str, Any]]:
    """Enumerate past projects under ``$REKIT_HOME/projects`` (meta only), newest
    opened first. Each entry is the project's ``project.json`` plus its ``id``."""
    root = _home.projects_root()
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        meta_path = child / "project.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        meta.setdefault("id", child.name)
        out.append(meta)
    out.sort(key=lambda m: m.get("lastOpenedAt") or "", reverse=True)
    return out


def resume(project_id_: str, *, now: str | None = None) -> Project | None:
    """Re-enter a past project by id, replaying its ledger. Returns None if no
    such project exists under ``$REKIT_HOME/projects``."""
    directory = _home.projects_root() / project_id_
    meta_path = directory / "project.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = {}
    target = meta.get("target") or project_id_
    ledger = _load_ledger(directory / LEDGER_FILENAME)
    project = Project(directory, target, ledger=ledger)
    project._ensure(now=now)
    return project
