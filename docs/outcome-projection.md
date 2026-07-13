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

### Semantic content identity

Every projection carries `semanticSha256`, a lowercase SHA-256 over the complete public
semantic projection. It is an outcome-projection content identity, not a source revision.
Consumers can recompute it with the pure public helpers
`canonical_outcome_semantic_bytes`, `outcome_semantic_sha256`, and
`verify_outcome_semantic_sha256` in `rekit_factory.outcomes`; no ledger, memory, dossier, or
process-private state is required.

The canonical byte domain is `factory-outcomes/semantic-sha256/v1`. Its UTF-8 JSON envelope is:

```json
{"domain":"factory-outcomes/semantic-sha256/v1","projection":{}}
```

The `projection` member contains every present top-level public field except exactly
`semanticSha256` itself, its nonsemantic transport mirror `semanticCanonicalBase64`, and
`sourceWatermarks`. Objects are recursively copied and serialized with keys sorted, no
insignificant whitespace, UTF-8 characters unescaped, and only exact JSON objects, arrays,
strings, booleans, nulls, and finite numbers accepted. Python-only containers, non-string object
keys, and non-finite numbers fail closed. Arrays retain their order; the projector canonicalizes
every set-like entity and diagnostic collection before hashing.

This include-by-default rule binds `schemaVersion`, `vocabularyVersion`, facet and authority
definitions, every entity field and parent, every raw and normalized facet value, every
diagnostic, `degraded`, and the consistency contract. Vocabulary meaning changes therefore
require a `vocabularyVersion` change. Future public semantic fields automatically join the
domain unless a later identity-domain version explicitly classifies observation metadata as
non-semantic.

`sourceWatermarks` remains visible as observation diagnostics but is deliberately excluded.
Moving only `factoryEventRowid`, `memorySequence`, or another source observation does not alter
the semantic bytes or digest. Conversely, watermark equality says nothing about semantic
equality; clients compare `semanticSha256` only when they want this exact projection meaning.
The helper never removes fields from its caller, and the projector snapshots JSON values before
attaching the identity so later mutation of input containers cannot invalidate the returned
projection. Mutating a returned semantic field is detectable because the verifier then fails.

`semanticCanonicalBase64` is standard Base64, including canonical padding, of the exact bytes
hashed for `semanticSha256`. Shared finalization computes the canonical bytes once, Base64
encodes that byte string, and hashes that same byte string. A browser can therefore verify the
SHA over decoded bytes without reparsing JSON numbers or reproducing Python float formatting.
`decode_outcome_semantic_canonical_base64` strictly rejects malformed, noncanonical, or
byte-mismatched transport text.

The transport mirror is deliberately nonsemantic and does not revise
`factory-outcomes/semantic-sha256/v1`: moving the field within an object or changing its text
cannot change the recomputed semantic identity. Such a change does make transport verification
fail, so consumers can distinguish an intact semantic claim from an untrustworthy byte carrier.

### Incremental parity reference

`rekit_factory.outcome_incremental` provides a pure in-memory parity reference. It is a
genuine source accumulator: accepted changes update a detached canonical source snapshot,
identify affected entity relationships, and refold only those intrinsic entities with the
same facet/entity primitives used by the full projector. Global ordering, source diagnostics,
dangling-parent diagnostics, degradation, consistency semantics, `semanticCanonicalBase64`, and
`semanticSha256` are then materialized by the shared finalizer. The incremental path never calls
`project_outcomes`.

The strict change domain is `factory-outcome-source-change/v1`:

```json
{
  "schemaVersion": 1,
  "sourceVersion": "factory-outcome-source-change/v1",
  "changeId": "worker-a-r2",
  "sourceKind": "worker",
  "sourceId": "worker-a",
  "sourceRevision": 2,
  "operation": "upsert",
  "value": {"id": "worker-a", "status": "done"}
}
```

`sourceKind` is exactly one of `run`, `worker`, `work-item`, `project-memory`, `dossier`, or
`pending-decision`; `operation` is `upsert` or `remove`, and removals carry a null `value`.
Run and project-memory are singleton streams with source IDs `run` and `project-memory`.
Every other source ID must exactly match `value.id`. Finding-scoped attempts, decisions, and
dossiers carry a valid `findingId`; a missing parent remains valid and becomes a public
`dangling-parent` diagnostic rather than being invented or discarded.

`changeId` binds exact canonical envelope bytes. Reusing it for different content fails closed,
while an exact retry is a no-op. `sourceRevision` is a positive, monotonic revision within one
source stream. A newly observed older revision is receipted but cannot rewind the canonical
head; conflicting reuse of the same stream revision fails closed. Batches are applied
transactionally in deterministic stream/revision/change order, so batch and arrival ordering
cannot change the converged source state. Removal followed by a higher-revision re-add is an
ordinary lifecycle of the source record, not a new database or second state machine.

`IncrementalOutcomeFold.source_snapshot()` emits the complete canonical
`factory-outcome-source-state/v1` JSON boundary: run, sorted workers and work items, complete
project-memory projection, sorted dossiers and pending decisions, and diagnostic source
watermarks. It also records the deterministic current revision head for each source stream so
a restarted accumulator cannot be rewound by a late stale change. The snapshot additionally
persists every accepted strict v1 change envelope as a deterministically sorted receipt. On
admission, those receipts rebuild both exact-change and stream-revision conflict maps; every
head must have its exact receipt, receipts cannot exceed a head, present records require heads,
and each current head must reproduce the materialized value or tombstone. Missing or tampered
heads, receipts, and source values therefore fail closed instead of weakening retry semantics
after process recreation. `from_source_snapshot()` rebuilds the in-memory accumulator and its
intrinsic entity materialization without calling the full projector. These receipts preserve
in-memory idempotency and conflict detection; they do not claim an external delivery protocol.

The shared public `consistency` object is deliberately derivation-path neutral:

```json
{
  "mode": "canonical-source-state",
  "sourceRead": "external-to-projection",
  "crossStoreRevision": "not-claimed",
  "watermarksAreProjectionIdentity": false,
  "incrementalParity": "in-memory-reference"
}
```

It describes the common projection guarantee and does not pretend the incremental reference
performed a full fold, SQLite transaction, or project-memory replay. The controller still
obtains its SQLite inputs under its explicit read transaction, as described below; that is a
producer boundary outside the pure outcome projection.

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

Crash tests abort that real transaction both during the middle of the dossier artifact loop and
at the final publication event. Each failure leaves zero proof-dossier artifacts and zero
publication events visible. The already sealed content-addressed directory remains unprojected;
an exact retry reuses it, preserves every byte, and publishes five bound artifacts plus one event
exactly once. This proof adds no production fault-injection control.

Project memory is a separately fsynced JSONL source. It is replayed outside the SQLite read
transaction, and v1 does not claim an atomic revision across those two stores. The
`sourceWatermarks` object reports the independently observed run-scoped maximum Factory event
rowid and project-memory sequence. They are diagnostic source positions only: other ledger
tables can change without either value changing, so watermark equality **must not** be used as
full projection identity, an ETag, or a change-detection cursor.

`semanticSha256` identifies only the outcome meaning present in one completed canonical-source
projection.
It does **not** claim an atomic revision across SQLite and project memory, identify every ledger
table or run-snapshot field, bind unprojected proof bytes, replace artifact digests, or certify
that two observations read the sources at the same instant. Because it excludes observation
metadata and carries no HTTP cache semantics, it is not advertised as an ETag or incremental
cursor. A cross-store revision and production incremental fold remain deferred. Clients obtain
current state by fetching and replacing the complete versioned projection.

The SSE stream is only an invalidation transport for that replacement. Mission Control seeds a
run subscription with the latest event ID already included in its fetched snapshot, records a
new cursor only after the corresponding replacement snapshot succeeds, and preserves that
run-scoped cursor when it recreates an `EventSource`. A known cursor receives only later events.
An ID absent from the selected run—including an ID copied from another run—receives one `reset`
event anchored at the selected run's latest event, after which normal continuation resumes.
This bounds stale-client recovery without treating an event ID as semantic identity or allowing
a foreign cursor to skip current-run events. The old-stream identity and request-generation
guards remain authoritative when delayed responses race a replacement stream.

The in-memory incremental reference is not a production cache, durable accumulator, SSE source,
or consumer migration. Mission Control, exports, notifications, and reports continue using
their existing paths until a separate production design adopts the proven parity boundary.
