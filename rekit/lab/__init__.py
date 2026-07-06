"""rekit.lab — Mission Control, the operator surface (E7).

The lab UI is a **read-model over ``$REKIT_HOME``** plus a thin supervisor. This
package holds the read-model: pure functions that fold each project's three logs
— ``ledger.jsonl`` (what was discovered), ``run.jsonl`` (what the run is doing),
``inbox.jsonl`` (pending decisions) — into the JSON views the fleet grid and the
project workspace render. ``rekit serve`` (the HTTP/websocket transport) and a
future TUI are just consumers of these functions.

Public surface (E7.0):
- :func:`fleet` — every project as a summary view, needs-you first.
- :func:`project_view` — one project's full folded view.
- :func:`health` — status counts for the fleet health ring.
"""

from .readmodel import (
    BLOCKED,
    SUSPENDED,
    event_stream,
    fleet,
    health,
    project_detail,
    project_view,
)
from .server import DEFAULT_HOST, DEFAULT_PORT, handle, make_server, serve

__all__ = [
    "fleet", "project_view", "project_detail", "event_stream", "health",
    "BLOCKED", "SUSPENDED",
    "serve", "make_server", "handle", "DEFAULT_HOST", "DEFAULT_PORT",
]
