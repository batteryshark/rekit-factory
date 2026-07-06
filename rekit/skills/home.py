"""REKIT_HOME resolution — where skills, shared bins, and projects live.

``$REKIT_HOME`` is the root of the lab's on-disk state::

    $REKIT_HOME/                 # ~/.rekit, configurable via $REKIT_HOME
      skills/<skill>/            # SKILL.md + scripts/ (own uv venv) — self-contained
      bin/                       # shared heavy binaries: ghidra, jadx (versioned)
      projects/<id>/             # ledger + artifact cache + findings (persistent)

This helper lives inside ``rekit.skills`` on purpose: E3 only needs the ``skills/``
and ``bin/`` subtrees, and a parallel agent owns ``rekit.ledger``. Keeping the
resolver local avoids fighting over a shared top-level ``rekit/paths.py``. If a
shared path module ever lands, this stays a thin re-export.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Environment variable that overrides the default home location.
ENV_VAR = "REKIT_HOME"

#: Default home when ``$REKIT_HOME`` is unset.
DEFAULT_HOME = "~/.rekit"


def rekit_home(environ: dict | None = None) -> Path:
    """The REKIT_HOME root: ``$REKIT_HOME`` if set (and non-empty), else ``~/.rekit``.

    ``~`` and any ``$VARS`` are expanded; the path is returned absolute but is
    *not* created — discovery tolerates a missing tree and simply finds nothing.
    """
    environ = environ if environ is not None else os.environ
    raw = str(environ.get(ENV_VAR) or "").strip() or DEFAULT_HOME
    return Path(os.path.expandvars(raw)).expanduser().resolve()


def skills_dir(environ: dict | None = None) -> Path:
    """``$REKIT_HOME/skills`` — the folder discovery scans for ``<skill>/SKILL.md``."""
    return rekit_home(environ) / "skills"


def bin_dir(environ: dict | None = None) -> Path:
    """``$REKIT_HOME/bin`` — shared heavy binaries (ghidra, jadx) that skills gate on.

    Handed to a skill's host requirements as a search path so a shared install
    resolves without every skill vendoring its own copy.
    """
    return rekit_home(environ) / "bin"
