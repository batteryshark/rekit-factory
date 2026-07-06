"""rekit — a harness-agnostic orchestration runtime for goal-driven analysis.

rekit is the kernel: a persistent project ledger, a ralph loop over pluggable
brains (pi / claude / codex / opencode behind one thin adapter seam), skill
loading + scoping, a human channel, and goalpacks. Point it at a target, pick a
goal, and agents work it against the ledger. It orchestrates but does not
understand code — parsers, decompilers, and analysis live inside *skills*, never
here — and it depends on **nothing**; heavy dependencies live inside the skills
that need them.
"""

__version__ = "0.1.0"
