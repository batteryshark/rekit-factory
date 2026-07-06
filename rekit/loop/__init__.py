"""rekit.loop — the ralph loop (E2).

The driver: it reads ledger context, asks the harness adapter for the next
actions, applies them against the ledger, and repeats until the goal terminates.
The loop — not any individual skill — chooses the model tier per step (cheap for
the high-volume floor/triage, beefy for heavy judgment) and decides when to fan
work out to scoped subagents. Default tempo is parallel, not waterfall.

Filled by epic E2. No logic here yet (E0 scaffold).
"""
