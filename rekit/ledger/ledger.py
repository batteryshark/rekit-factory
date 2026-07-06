"""The persistent, event-sourced project ledger (E1.1 + E1.3).

This promotes parallax's in-memory ``orchestrator.Ledger`` into a durable,
first-class ledger. What the orchestrator knows about a target — its artifacts by
content hash, their kinds, the trees to analyze, the derivations that produced
them, the tool-need leads, and the findings — is here recorded as an append-only
stream of typed events (``ledger.jsonl``). The live in-memory state is a
deterministic **fold** over that stream, so:

- every mutation appends exactly one typed event, then folds it in;
- reload = replay the log from scratch → byte-identical state (lossless);
- derivations are **content-addressed**: recording a derivation whose
  ``(transform, input hash)`` was already seen is a free no-op — a second goal
  over the same target re-derives nothing.

The ledger is harness-neutral: any brain reads/writes it through this API, and
the raw ``ledger.jsonl`` is the audit/observability substrate (E7.2).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import artifacts as _artifacts, events as _events
from .artifacts import Artifact

LEDGER_FILENAME = "ledger.jsonl"


@dataclass
class LedgerEntry:
    """The ledger's record for one artifact: the artifact itself, whether it is a
    tree to analyze, whether analysis has run, and the findings attached to it."""
    artifact: Artifact
    is_tree: bool = False
    analyzed: bool = False
    findings: list[dict] = field(default_factory=list)


@dataclass
class Derivation:
    """A recorded transform/skill application: ``input -> outputs`` under a
    capability. Keyed for the content-addressed cache by ``(transform, input
    hash)`` — the same key twice is a no-op."""
    transform: str
    input_hash: str
    capability: str | None
    outputs: list[Artifact]

    @property
    def key(self) -> tuple[str, str]:
        return (self.transform, self.input_hash)


class Ledger:
    """In-memory projection of the event stream — a fold, never mutated except by
    :meth:`apply`. Callers use the recording methods on :class:`~.project.Project`
    (which append + fold); this object is the read model.
    """

    def __init__(self) -> None:
        self.entries: dict[str, LedgerEntry] = {}      # content hash -> entry
        self.kinds: Counter = Counter()
        self.trees: list[str] = []                     # content hashes of trees, in add order
        self.derivations: dict[tuple[str, str], Derivation] = {}
        self.leads: dict[tuple[str, str], dict] = {}   # (capability, kind) -> lead
        self.seq: int = 0                              # highest applied event seq

    # -- fold ---------------------------------------------------------------

    def apply(self, event: _events.Event) -> None:
        """Fold a single event into the state. Pure w.r.t. the event; unknown
        types are ignored so a newer log never breaks an older reader."""
        p = event.payload
        etype = event.type
        if etype == _events.ARTIFACT_ADDED:
            self._apply_artifact_added(p)
        elif etype == _events.DERIVATION_RECORDED:
            self._apply_derivation_recorded(p)
        elif etype == _events.LEAD_RECORDED:
            self._apply_lead_recorded(p)
        elif etype == _events.FINDING_RECORDED:
            self._apply_finding_recorded(p)
        elif etype == _events.ARTIFACT_ANALYZED:
            self._apply_artifact_analyzed(p)
        # else: unknown/forward-compatible event type -> no-op.
        if event.seq > self.seq:
            self.seq = event.seq

    def _apply_artifact_added(self, p: dict[str, Any]) -> None:
        art = _artifacts.from_dict(p["artifact"])
        if art.content_hash in self.entries:
            return
        is_tree = bool(p.get("isTree"))
        self.entries[art.content_hash] = LedgerEntry(art, is_tree=is_tree)
        self.kinds[art.kind] += 1
        if is_tree:
            self.trees.append(art.content_hash)

    def _apply_derivation_recorded(self, p: dict[str, Any]) -> None:
        deriv = Derivation(
            transform=p["transform"],
            input_hash=p["inputHash"],
            capability=p.get("capability"),
            outputs=[_artifacts.from_dict(o) for o in p.get("outputs", [])],
        )
        self.derivations.setdefault(deriv.key, deriv)

    def _apply_lead_recorded(self, p: dict[str, Any]) -> None:
        key = (p["capability"], p["kind"])
        lead = self.leads.get(key)
        if lead is None:
            self.leads[key] = {
                "capability": p["capability"],
                "kind": p["kind"],
                "requires": list(p.get("requires") or []),
                "envHints": list(p.get("envHints") or []),
                "examplePath": p.get("examplePath"),
                "count": 1,
            }
        else:
            lead["count"] += 1

    def _apply_finding_recorded(self, p: dict[str, Any]) -> None:
        entry = self.entries.get(p["artifactHash"])
        if entry is not None:
            entry.findings.append(dict(p["finding"]))

    def _apply_artifact_analyzed(self, p: dict[str, Any]) -> None:
        entry = self.entries.get(p["artifactHash"])
        if entry is not None:
            entry.analyzed = True

    # -- read model ---------------------------------------------------------

    def has_artifact(self, content_hash: str) -> bool:
        return content_hash in self.entries

    def has_derivation(self, transform: str, input_hash: str) -> bool:
        return (transform, input_hash) in self.derivations

    def get_derivation(self, transform: str, input_hash: str) -> Derivation | None:
        return self.derivations.get((transform, input_hash))

    def findings(self) -> list[dict]:
        """All findings, each annotated with the artifact it belongs to."""
        out: list[dict] = []
        for h, entry in self.entries.items():
            for finding in entry.findings:
                out.append({**finding, "artifact": entry.artifact.id,
                            "artifactPath": entry.artifact.path})
        return out

    def snapshot(self) -> dict[str, Any]:
        """A JSON-serialisable summary of the whole ledger — the read model as
        one dict. Two ledgers are state-identical iff their snapshots are equal,
        which is exactly the lossless-reload / replay assertion."""
        return {
            "artifacts": {
                h: {
                    "artifact": _artifacts.to_dict(e.artifact),
                    "isTree": e.is_tree,
                    "analyzed": e.analyzed,
                    "findings": list(e.findings),
                }
                for h, e in self.entries.items()
            },
            "kinds": dict(self.kinds),
            "trees": list(self.trees),
            "derivations": {
                f"{t}\x00{ih}": {
                    "transform": d.transform,
                    "inputHash": d.input_hash,
                    "capability": d.capability,
                    "outputs": [_artifacts.to_dict(o) for o in d.outputs],
                }
                for (t, ih), d in self.derivations.items()
            },
            "leads": {f"{c}\x00{k}": dict(v) for (c, k), v in self.leads.items()},
            "seq": self.seq,
        }


def replay(source: Iterable[_events.Event]) -> Ledger:
    """Build a fresh :class:`Ledger` by folding a stream of events — the
    reconstruction primitive behind lossless reload."""
    ledger = Ledger()
    for event in source:
        ledger.apply(event)
    return ledger


def read_events(path: str | Path) -> list[_events.Event]:
    """Parse ``ledger.jsonl`` into :class:`~.events.Event` objects (append order).

    Blank lines and unparseable lines are skipped rather than aborting the whole
    replay — a half-written trailing line from a crash must not lose the history
    before it.
    """
    path = Path(path)
    out: list[_events.Event] = []
    if not path.exists():
        return out
    import json as _json
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(_events.Event.from_dict(_json.loads(line)))
            except (ValueError, KeyError):
                continue
    return out


def load(path: str | Path) -> Ledger:
    """Read ``ledger.jsonl`` and replay it into a live :class:`Ledger`."""
    return replay(read_events(path))
