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

Public surface (E4.3)
---------------------
- :class:`HumanChannel` — the ABC every impl satisfies (``ask`` /
  ``present_choices`` / ``confirm``).
- :class:`CLIHumanChannel` — the default interactive stdin/stdout impl.
- :class:`ScriptedHumanChannel` — pre-programmed answers for hermetic tests.
- :func:`gate_skill` — auto-allow per policy or route a gated-tier skill through
  the channel; the seam the ralph loop and sandbox network-gate call.
"""

from .channel import (
    CLIHumanChannel,
    HumanChannel,
    ScriptedHumanChannel,
    gate_skill,
)
from .inbox import (
    INBOX_FILENAME,
    LedgerHumanChannel,
    all_questions,
    answer,
    pending_questions,
    post_question,
)

__all__ = [
    "HumanChannel",
    "CLIHumanChannel",
    "ScriptedHumanChannel",
    "gate_skill",
    # file-backed inbox channel (E7.3)
    "LedgerHumanChannel",
    "post_question",
    "answer",
    "pending_questions",
    "all_questions",
    "INBOX_FILENAME",
]
