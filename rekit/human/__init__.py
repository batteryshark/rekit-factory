"""rekit.human — the human channel (E4).

Two halves of one surface. The passive half is the log/event pane (what ran, what
the agent said, script output; collapsible). The active half is the
``ask_human`` / ``present_choices`` tool: the UI renders a question plus option
buttons and blocks until answered.

The active half is not only for hard gates (*"allow network to fetch this?"*,
*"spend budget decompiling?"*). The orchestrator has agency to consult the user
proactively — direction forks (*"go deeper here or move on?"*) and genuine
ambiguity (*"licence check or telemetry — which matters to you?"*). Trust-tier
gates route through here too.

Filled by epic E4. No logic here yet (E0 scaffold).
"""
