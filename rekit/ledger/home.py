"""REKIT_HOME resolution — where the runtime keeps its persistent state.

``$REKIT_HOME`` (default ``~/.rekit``) is the root under which skills, shared
binaries, and per-project ledgers live::

    $REKIT_HOME/
      skills/<skill>/      # SKILL.md + scripts/ (own uv venv)
      bin/                 # shared heavy binaries (ghidra, jadx, …)
      projects/<id>/       # the persistent ledger for one target

This helper lives *inside* ``rekit.ledger`` on purpose: E1 owns the projects
layout, and keeping the resolver local avoids a shared top-level module that a
parallel subpackage (``rekit.skills``) would also want to edit. Other
subpackages that later need ``skills/`` or ``bin/`` can call
:func:`rekit_home` the same way.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Environment variable that overrides the default home.
ENV_VAR = "REKIT_HOME"

#: Default home when the env var is unset.
DEFAULT_HOME = "~/.rekit"


def rekit_home() -> Path:
    """The resolved ``$REKIT_HOME`` — the env override if set, else ``~/.rekit``.

    Read fresh every call (never cached) so a test can set ``REKIT_HOME`` to a
    temp dir per case and every project operation honours it.
    """
    raw = os.environ.get(ENV_VAR)
    base = raw if raw and raw.strip() else DEFAULT_HOME
    return Path(base).expanduser()


def projects_root() -> Path:
    """``$REKIT_HOME/projects`` — the parent of every per-target project dir."""
    return rekit_home() / "projects"
