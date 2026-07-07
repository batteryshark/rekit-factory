"""The run/liveness log — the read-model spine for Mission Control (E7.0).

Sits next to ``ledger.jsonl`` under a project dir as ``run.jsonl`` and obeys the
same contract as the ledger: **state is a fold over an append-only log**. Each
lifecycle transition of a *run* — started, per-round, status change, ended — is
one typed :class:`~.events.Event`; the live :class:`RunState` is a deterministic
fold, so a watcher (``rekit serve``, a TUI, an audit trail) reconstructs a run's
status by replay and never needs a privileged hook into the loop.

The ledger records *what was discovered*; this records *what the run is doing* —
the two things the ledger cannot express and the fleet card needs: **liveness**
(running / idle / done / failed) and the per-round **tier / cost heartbeat**.

The vocabulary:

- ``run_started``    — payload: ``goal``, ``harness``, ``tier``, ``maxRounds``, ``tools``.
- ``round_started``  — payload: ``index``, ``tier``, ``tools``.
- ``round_ended``    — payload: per-round deltas ``findings`` / ``leads`` /
  ``derivations`` / ``skillRuns``, the ``model`` / ``provider`` that ran, and a
  ``cost`` object (``usd`` / tokens) — a stub until the harness returns usage.
- ``status_changed`` — payload: ``status`` (see the status constants) + ``reason``.
- ``step``           — payload: ``text`` (a freeform current-activity caption).
- ``run_ended``      — payload: ``done`` + ``reason``.

``blocked`` / ``suspended`` are legal statuses here, but the read-model normally
*derives* them by joining a pending :mod:`rekit.human.inbox` question onto this
log — this module never needs to know about the human channel.

Reuses :class:`~.events.Event` (same ``seq/type/ts/payload`` shape and
serialisation as the ledger) so both logs parse and replay identically. Pure
stdlib; a leaf the loop imports (never the reverse).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import events as _events
from .ledger import read_events as _read_events

RUN_LOG_FILENAME = "run.jsonl"

# Event types (the run's vocabulary).
RUN_STARTED = "run_started"
ROUND_STARTED = "round_started"
ROUND_ENDED = "round_ended"
STATUS_CHANGED = "status_changed"
STEP = "step"
RUN_ENDED = "run_ended"

RUN_EVENT_TYPES = frozenset({
    RUN_STARTED, ROUND_STARTED, ROUND_ENDED, STATUS_CHANGED, STEP, RUN_ENDED,
})

# Status constants (the status pill values).
RUNNING = "running"
BLOCKED = "blocked"
SUSPENDED = "suspended"
IDLE = "idle"
DONE = "done"
FAILED = "failed"


@dataclass
class RunState:
    """The fold over a ``run.jsonl`` — everything the fleet card needs about a run.

    A run with no log yet folds to the neutral default (``idle``, zero counters),
    so a watcher can render a not-yet-started project without special-casing.
    """

    goal: str = ""
    harness: str = ""
    tier: str = ""
    model: str | None = None
    provider: str | None = None
    pid: int = 0
    status: str = IDLE
    reason: str = ""
    round: int = 0
    max_rounds: int = 0
    step: str = ""
    findings: int = 0
    leads: int = 0
    derivations: int = 0
    skill_runs: int = 0
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    started_at: str = ""
    ended_at: str = ""
    done: bool = False
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serialisable summary — the read model as one dict (the UI shape)."""
        return {
            "goal": self.goal,
            "harness": self.harness,
            "tier": self.tier,
            "model": self.model,
            "provider": self.provider,
            "status": self.status,
            "reason": self.reason,
            "pid": self.pid,
            "round": self.round,
            "maxRounds": self.max_rounds,
            "step": self.step,
            "counters": {
                "findings": self.findings,
                "leads": self.leads,
                "derivations": self.derivations,
                "skillRuns": self.skill_runs,
            },
            "cost": {
                "usd": round(self.cost_usd, 4),
                "tokensIn": self.tokens_in,
                "tokensOut": self.tokens_out,
            },
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "done": self.done,
            "seq": self.seq,
        }


def _apply(state: RunState, event: _events.Event) -> None:
    """Fold one event into ``state`` (mutating). Unknown types are ignored so a
    newer log never breaks an older reader — the ledger's forward-compat rule."""
    p = event.payload
    t = event.type
    if t == RUN_STARTED:
        state.goal = p.get("goal", state.goal)
        state.harness = p.get("harness", state.harness)
        state.tier = p.get("tier", state.tier)
        state.max_rounds = int(p.get("maxRounds") or state.max_rounds or 0)
        state.pid = int(p.get("pid") or 0)
        state.status = RUNNING
        state.started_at = state.started_at or event.ts
        if p.get("step"):
            state.step = p["step"]
    elif t == ROUND_STARTED:
        state.round = int(p.get("index", state.round))
        if p.get("tier"):
            state.tier = p["tier"]
        state.status = RUNNING
    elif t == ROUND_ENDED:
        state.findings += int(p.get("findings") or 0)
        state.leads += int(p.get("leads") or 0)
        state.derivations += int(p.get("derivations") or 0)
        state.skill_runs += int(p.get("skillRuns") or 0)
        if p.get("model"):
            state.model = p["model"]
        if p.get("provider"):
            state.provider = p["provider"]
        cost = p.get("cost") or {}
        state.cost_usd += float(cost.get("usd") or 0)
        state.tokens_in += int(cost.get("tokensIn") or 0)
        state.tokens_out += int(cost.get("tokensOut") or 0)
    elif t == STATUS_CHANGED:
        if p.get("status"):
            state.status = p["status"]
        state.reason = p.get("reason", state.reason)
    elif t == STEP:
        state.step = p.get("text", state.step)
    elif t == RUN_ENDED:
        state.done = bool(p.get("done"))
        state.reason = p.get("reason", state.reason)
        state.status = p.get("status") or (DONE if state.done else IDLE)
        state.ended_at = event.ts
    # else: unknown/forward-compatible type -> no-op.
    if event.seq > state.seq:
        state.seq = event.seq


def fold_run(events: Iterable[_events.Event]) -> RunState:
    """Build a :class:`RunState` by folding a stream of run events."""
    state = RunState()
    for event in events:
        _apply(state, event)
    return state


def read_run_events(path: str | Path) -> list[_events.Event]:
    """Parse ``run.jsonl`` into events (append order); missing file -> ``[]``."""
    return _read_events(path)


def load_run_state(path: str | Path) -> RunState:
    """Read ``run.jsonl`` and fold it into a live :class:`RunState`."""
    return fold_run(read_run_events(path))


def _max_seq(path: Path) -> int:
    """The highest seq already on disk (0 if none) — so a new append continues the
    sequence even across process restarts and concurrent readers."""
    hi = 0
    for ev in _read_events(path):
        if ev.seq > hi:
            hi = ev.seq
    return hi


class RunLog:
    """The write API over ``run.jsonl`` — one append per lifecycle transition.

    The loop constructs a :class:`RunLog` from the project dir and calls the
    lifecycle methods as it drives a run; each appends exactly one typed event.
    Seq is recomputed from disk per append, so interleaved writers (a supervisor
    stamping a status while the loop runs a round) never collide on ordering.
    """

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.path = self.dir / RUN_LOG_FILENAME

    def _append(self, etype: str, payload: dict[str, Any]) -> _events.Event:
        self.dir.mkdir(parents=True, exist_ok=True)
        seq = _max_seq(self.path) + 1
        event = _events.Event(seq=seq, type=etype, ts=_events.utc_now(), payload=payload)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(event.to_json_line())
        return event

    def run_started(self, *, goal: str, harness: str, tier: str,
                    max_rounds: int = 0, tools: Sequence[str] | None = None,
                    step: str = "") -> _events.Event:
        return self._append(RUN_STARTED, {
            "goal": goal, "harness": harness, "tier": tier,
            "maxRounds": max_rounds, "tools": list(tools or []), "step": step,
            "pid": os.getpid(),  # the owning process, so a reaper can spot a zombie
        })

    def round_started(self, index: int, tier: str,
                      tools: Sequence[str] | None = None) -> _events.Event:
        return self._append(ROUND_STARTED, {
            "index": index, "tier": tier, "tools": list(tools or []),
        })

    def round_ended(self, index: int, *, findings: int = 0, leads: int = 0,
                    derivations: int = 0, skill_runs: int = 0,
                    model: str | None = None, provider: str | None = None,
                    cost: dict[str, Any] | None = None) -> _events.Event:
        return self._append(ROUND_ENDED, {
            "index": index, "findings": findings, "leads": leads,
            "derivations": derivations, "skillRuns": skill_runs,
            "model": model, "provider": provider, "cost": cost or {},
        })

    def status(self, status: str, reason: str = "") -> _events.Event:
        return self._append(STATUS_CHANGED, {"status": status, "reason": reason})

    def step(self, text: str) -> _events.Event:
        return self._append(STEP, {"text": text})

    def run_ended(self, *, done: bool, reason: str = "",
                  status: str | None = None) -> _events.Event:
        return self._append(RUN_ENDED, {
            "done": bool(done), "reason": reason, "status": status,
        })

    def state(self) -> RunState:
        """Fold the current log into a :class:`RunState`."""
        return load_run_state(self.path)
