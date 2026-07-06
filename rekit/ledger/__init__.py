"""rekit.ledger — the persistent project ledger (E1).

The spine of the runtime. Per project, a durable file protocol under
``$REKIT_HOME/projects/<id>/`` records every artifact (hash, kind, provenance,
derivations), pending work/leads, and findings. It is harness-neutral so any
brain reads and writes it, survives runs, and makes a project revisitable rather
than ephemeral. Harness-agnosticism, revisit, the UI, and observability all fall
out of this one thing.

Content-addressed, so re-entering a project re-derives nothing; the ledger is
also a typed event stream (``ledger.jsonl``) whose replay reconstructs current
state losslessly.

Public surface
--------------
- Project lifecycle: :func:`open_project`, :func:`list_projects`, :func:`resume`.
- The read/write ledger: :class:`Project` (write API) over :class:`Ledger` (fold).
- Artifact model: :class:`Artifact`, :func:`from_path`, :func:`classify`,
  :func:`hash_file`, :func:`hash_tree`.
- Home resolution: :func:`rekit_home`, :func:`projects_root`.
- Event stream primitives: :class:`Event`, :func:`replay`, :func:`load`.
"""

from __future__ import annotations

from .artifacts import (
    Artifact,
    classify,
    from_dict as artifact_from_dict,
    from_path,
    hash_file,
    hash_tree,
    to_dict as artifact_to_dict,
)
from .events import Event, EVENT_TYPES, utc_now
from .home import projects_root, rekit_home
from .ledger import Derivation, Ledger, LedgerEntry, load, read_events, replay
from .project import (
    Project,
    list_projects,
    open_project,
    project_id,
    resume,
)

__all__ = [
    # lifecycle
    "open_project",
    "list_projects",
    "resume",
    "project_id",
    # ledger
    "Project",
    "Ledger",
    "LedgerEntry",
    "Derivation",
    # artifacts
    "Artifact",
    "from_path",
    "classify",
    "hash_file",
    "hash_tree",
    "artifact_to_dict",
    "artifact_from_dict",
    # home
    "rekit_home",
    "projects_root",
    # events
    "Event",
    "EVENT_TYPES",
    "utc_now",
    "replay",
    "load",
    "read_events",
]
