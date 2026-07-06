"""rekit.harness — harness adapters (E2).

The thin seam that makes rekit harness-agnostic. One interface, one real adapter
to start (pi first; claude / codex / opencode later)::

    invoke(brain, prompt, tools, ledger_context, tier) -> actions

The brain is the decider; rekit only mediates what it sees. The adapter also
exposes a "spawn scoped subagent" primitive so the loop can fan work out — and
fanned-out subagents inherit the same scoped skill set, never the whole rack.
This is the cut line for the legacy pydantic-ai decider: it degrades to at most
one adapter and is then deleted.

Filled by epic E2. No logic here yet (E0 scaffold).
"""
