# Outcome projection v1

Factory exposes `outcomeProjection` in every canonical run snapshot. The block is a
versioned, deterministic read model over already committed ledger rows, replayed project
memory, pending questions, and proof-bundle publication records. It is not a second state
machine, lifecycle table, index, or cache.

## Contract

`schemaVersion: 1` and `vocabularyVersion: factory-outcomes/v1` version the public shape and
the meanings below independently. Entities are sorted by `(entityType, entityId)` and
diagnostics are sorted by entity, facet, and raw value. Rebuilding from the same canonical
state therefore produces byte-for-byte equivalent JSON.

Each entity has six orthogonal facets:

| Facet | Question answered | Representative normalized states |
| --- | --- | --- |
| execution | What phase is execution in? | `queued`, `active`, `waiting`, `terminal`, `unknown` |
| completion | Is this entity's own work complete? | `incomplete`, `completed`, `unknown` |
| disposition | What outcome was assigned? | `successful`, `failed`, `blocked`, `cancelled`, `deferred`, `needs-review`, `mixed`, `unknown` |
| validation | What has proof policy concluded? | `unvalidated`, `pending`, `demonstrated`, `reproduced`, `contradicted`, `invalid`, `inconclusive`, `verified`, `stale`, `unknown` |
| acceptance | What has an operator decided? | `undecided`, `accepted`, `rejected`, `waived`, `unknown` |
| publication | What durable publication exists? | `unpublished`, `published`, `verified`, `archived`, `stale`, `unknown` |

Every facet preserves `rawState`, supplies a normalized `state`, says whether the raw value is
`known`, identifies whether the canonical raw state is `terminal`, and names its `owner`.
A facet that does not apply is explicit rather than absent. An unfamiliar raw value is
preserved as `state: unknown`, `known: false`, `terminal: false`; the projection becomes
`degraded` and emits an `unknown-state` diagnostic. Projection never rewrites canonical
history to repair an unknown state.

## Authority and non-promotion rules

| Authority | Owns |
| --- | --- |
| `muster` | Durable work-item execution and completion |
| `factory-scheduler` | Run and worker execution/completion |
| `validator-policy` | Hypothesis, finding, and reproduction conclusions |
| `rekit-tool-result` | Only the execution/completion of its own Rekit tool work item |
| `operator` | Answers and explicit acceptance, rejection, or waiver |
| `factory-dossier-publisher` | Transactional proof-bundle publication presence |
| `offline-proof-verifier` | Current byte, scope, manifest, and trust-anchor validity |

Authority is facet-local. In particular:

- a completed worker or work item never makes its run complete or successful;
- a rendered report never makes a finding demonstrated, reproduced, or accepted;
- a successful reproduction attempt never directly changes its parent finding facet;
- a proof-bundle publication changes only publication; it does not imply verification;
- proof verification changes the bundle's validation facet, not finding acceptance;
- only an operator decision changes acceptance.

Finding publication may list canonical proof-bundle IDs, but that relation does not promote
the finding's completion, disposition, validation, or acceptance facets.

## Current projection coverage

The initial projection covers runs, workers, work items, hypotheses, findings, reproduction
validations, proof bundles, and pending or finding-scoped operator decisions. Campaign and
archive entities will join this vocabulary when their canonical event sources land. Reports
remain ordinary work results in this slice; report rendering does not acquire independent
outcome authority.

The generic/SSE snapshot deliberately uses the cheap `dossier_list` publication projection.
That establishes that a proof bundle was transactionally published, but it does **not** read
and re-verify dossier bytes. Such bundles have `validation.state: unknown` and
`validation.known: false`. The dedicated
dossier route supplies explicit `verified` or `stale-or-invalid` facts and callers may pass
those facts through the same pure projector. The high-frequency snapshot never guesses
validity and never pays the byte-verification cost.

## Consistency boundary

The projector observes only data that is already visible in the canonical stores. Its
`consistency.cursor` combines the run-scoped maximum SQLite event `rowid` with the replayed
project-memory sequence. The row ID is monotonic and remains unambiguous when event timestamps
tie; opaque event UUIDs are identities, not cursors. `mode: full-fold` and
`replaceOnReconnect: true` make the v1 convergence contract explicit; an incremental
projector is deferred until it can prove parity with this fold. Dossier artifacts and their
`dossier.published` event are committed in one SQLite transaction, so a
published outcome cannot precede its artifact references. Project-memory replay supplies the
full fold; there is no incremental derived cache. Polling clients and SSE reconnects converge
by fetching the canonical snapshot and replacing the versioned projection for that run.

This first slice does not migrate Mission Control, exports, notifications, or reports. Those
consumers continue using their backward-compatible fields while parity is established.
