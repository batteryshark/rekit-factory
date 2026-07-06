"""rekit.loop — the ralph loop (E2).

The driver: it reads ledger context, asks the harness adapter for the next
actions, applies them against the ledger, and repeats until the goal terminates.
The loop — not any individual skill — chooses the model tier per step (cheap for
the high-volume floor/triage, beefy for heavy judgment) and decides when to fan
work out to scoped subagents. Default tempo is parallel, not waterfall.

Public surface (E2 first slice)
-------------------------------
- :func:`run` — drive a goal to termination via a harness adapter over the ledger.
- :class:`LoopSummary` / :class:`RoundResult` — the coverage-aware outcome.
- :func:`build_context` — render the ledger snapshot into brain context.
- :func:`fan_out` — orchestrator-level parallelism: run N brain invocations
  concurrently and converge their results losslessly on one ledger.
- :class:`FanoutSummary` / :class:`ItemResult` — the fan-out outcome.
"""

from .fanout import (
    FanoutSummary,
    ItemResult,
    default_fold,
    fan_out,
)
from .loop import (
    LoopSummary,
    RoundResult,
    build_context,
    run,
)

__all__ = [
    "run",
    "LoopSummary",
    "RoundResult",
    "build_context",
    "fan_out",
    "FanoutSummary",
    "ItemResult",
    "default_fold",
]
