"""rekit.harness — harness adapters (E2).

The thin seam that makes rekit harness-agnostic. One interface, one real adapter
to start (pi first; claude / codex / opencode later)::

    invoke(brain, prompt, tools, ledger_context, tier) -> actions

The brain is the decider; rekit only mediates what it sees. The adapter also
exposes a "spawn scoped subagent" primitive so the loop can fan work out — and
fanned-out subagents inherit the same scoped skill set, never the whole rack.
This is the cut line for the legacy pydantic-ai decider: it degrades to at most
one adapter and is then deleted.

Public surface (E2 first slice)
-------------------------------
- The seam: :class:`HarnessAdapter`, :class:`HarnessResult`, :class:`ToolCall`,
  :class:`HarnessError`.
- Tier routing: :func:`resolve_tier`, :class:`TierRoute`, :func:`default_tiers`
  (cheap → MiniMax M3, beefy → z.ai GLM 5.2; overridable).
- Adapters: :class:`PiAdapter` (real pi brain), :class:`MockAdapter`
  (deterministic, for hermetic loop tests).
"""

from .base import HarnessAdapter, HarnessError, HarnessResult, ToolCall
from .mock import MockAdapter, MockInvocation, MockTurn
from .pi import PiAdapter
from .tiers import (
    BEEFY,
    CHEAP,
    TierRoute,
    canonical_tier,
    default_tiers,
    resolve_tier,
)

__all__ = [
    # seam
    "HarnessAdapter",
    "HarnessResult",
    "ToolCall",
    "HarnessError",
    # tiers
    "resolve_tier",
    "canonical_tier",
    "default_tiers",
    "TierRoute",
    "CHEAP",
    "BEEFY",
    # adapters
    "PiAdapter",
    "MockAdapter",
    "MockTurn",
    "MockInvocation",
]
