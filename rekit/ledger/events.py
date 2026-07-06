"""Typed ledger events — the append-only log the ledger is a fold over (E1.3).

Every mutation of a project's ledger is a typed :class:`Event` appended to
``ledger.jsonl`` under the project dir. The current ledger state is a
deterministic fold over that log, so reload = replay and nothing is lost. The
same stream feeds the observability log pane (E7.2) and an audit trail.

An event is ``(seq, type, ts, payload)``:

- ``seq``   monotonically increasing per project (append order).
- ``type``  one of :data:`EVENT_TYPES`.
- ``ts``    ISO-8601 UTC timestamp (informational; not part of the fold's identity).
- ``payload`` a JSON-serialisable dict specific to the type.

Event types (the vocabulary of a ledger's history):

- ``artifact_added``    — an artifact entered the ledger (payload: ``artifact``, ``isTree``).
- ``derivation_recorded`` — a transform/skill produced outputs from an input
  (payload: ``transform``, ``inputHash``, ``capability``, ``outputs``).
- ``lead_recorded``     — a capability wanted but no provider was available
  (payload: ``capability``, ``kind``, ``requires``, ``envHints``, ``examplePath``).
- ``finding_recorded``  — an analysis finding attributed to an artifact
  (payload: ``artifactHash``, ``finding``).
- ``artifact_analyzed`` — an artifact/tree was marked analyzed (payload: ``artifactHash``).

New types can be added without touching persisted logs: an unknown type replays
as a no-op (forward-compatible), so an older reader never crashes on a newer log.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

ARTIFACT_ADDED = "artifact_added"
DERIVATION_RECORDED = "derivation_recorded"
LEAD_RECORDED = "lead_recorded"
FINDING_RECORDED = "finding_recorded"
ARTIFACT_ANALYZED = "artifact_analyzed"

EVENT_TYPES = frozenset({
    ARTIFACT_ADDED,
    DERIVATION_RECORDED,
    LEAD_RECORDED,
    FINDING_RECORDED,
    ARTIFACT_ANALYZED,
})


def utc_now() -> str:
    """ISO-8601 UTC timestamp to the second."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Event:
    seq: int
    type: str
    ts: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "type": self.type, "ts": self.ts, "payload": self.payload}

    def to_json_line(self) -> str:
        """One compact JSON object + newline — the ``ledger.jsonl`` record shape.

        ``sort_keys`` makes the on-disk line stable for the same content, which
        keeps diffs and any future content-hashing of the log deterministic.
        """
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            seq=int(data["seq"]),
            type=str(data["type"]),
            ts=str(data.get("ts") or ""),
            payload=dict(data.get("payload") or {}),
        )
