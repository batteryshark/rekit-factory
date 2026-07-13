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
| publication | What durable publication exists? | `unpublished`, `published`, `unknown` |

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
| `rekit-tool-result` | Reserved for a future distinct, authoritative Rekit result entity |
| `operator` | Answers and explicit acceptance, rejection, or waiver |
| `factory-dossier-publisher` | Transactional proof-bundle publication presence |
| `offline-proof-verifier` | Current byte, scope, manifest, and trust-anchor validity |

Authority is facet-local. In particular:

- a completed worker or work item never makes its run complete or successful;
- Muster owns every execution, completion, and disposition facet derived from a durable
  work-item status, including Rekit-backed work. Operation names do not prove result authority;
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
Every listed dossier therefore has `publication.state: published`, owned by the dossier
publisher, regardless of its current verification result. Publication never becomes
`verified` or `stale`. The cheap projection does **not** read and re-verify dossier bytes, so
its bundles have `validation.state: unknown` and `validation.known: false`. The pure projector
can accept explicit `verified` or `stale-or-invalid` facts and place them only in the
verifier-owned validation facet. No production `outcomeProjection` route supplies those facts
in this slice: the dedicated dossier route verifies bundles but does not invoke the outcome
projector. Wiring that route into a verified outcome projection is deferred. The
high-frequency snapshot never guesses validity and never pays the byte-verification cost.

## Consistency boundary

Every SQLite-backed field in a run snapshot is read under one explicit SQLite read
transaction. That includes the run, work, workers, questions, model/tool/session rows,
artifacts, dossier publication rows, knowledge references, coverage, events, and the event
rowid watermark. Dossier artifacts and their `dossier.published` event are themselves written
in one transaction, so a response cannot expose the dossier publication without the
same-response artifact rows (or vice versa).

Project memory is a separately fsynced JSONL source. It is replayed outside the SQLite read
transaction, and v1 does not claim an atomic revision across those two stores. The
`sourceWatermarks` object reports the independently observed run-scoped maximum Factory event
rowid and project-memory sequence. They are diagnostic source positions only: other ledger
tables can change without either value changing, so watermark equality **must not** be used as
full projection identity, an ETag, or a change-detection cursor. A complete ledger revision or
content identity and incremental fold remain deferred. Clients obtain current state by
fetching and replacing the complete versioned projection.

This first slice does not migrate Mission Control, exports, notifications, or reports. Those
consumers continue using their backward-compatible fields while parity is established.
