"""rekit — the orchestration runtime for the Parallax reverse-engineering lab.

rekit is the kernel: the ralph loop, the persistent project ledger, discovery
and artifact tracking, skill loading/scoping, the human channel, and the
dashboard. It is *harness-agnostic* — pi / claude / codex / opencode are
pluggable brains behind one thin adapter seam — and it depends on **nothing**,
not even parallax. Heavy dependencies live inside the skills that need them.

Plan of record: parallax/docs/rekit-epic.md.

This is E0 scaffolding: the subpackages below are docstring-only stubs that name
their intent and the epic that fills them. No orchestration logic lives here yet.
"""

__version__ = "0.1.0"
