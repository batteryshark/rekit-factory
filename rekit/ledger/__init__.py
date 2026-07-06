"""rekit.ledger — the persistent project ledger (E1).

The spine of the runtime. Per project, a durable file protocol under
``$REKIT_HOME/projects/<id>/`` records every artifact (hash, kind, provenance,
derivations), pending work/leads, and findings. It is harness-neutral so any
brain reads and writes it, survives runs, and makes a project revisitable rather
than ephemeral. Harness-agnosticism, revisit, the UI, and observability all fall
out of this one thing.

Content-addressed, so re-entering a project re-derives nothing; the ledger also
serves as a typed event stream (E1.3) whose replay reconstructs current state.

Filled by epic E1. No logic here yet (E0 scaffold).
"""
