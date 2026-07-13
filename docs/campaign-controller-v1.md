# Bounded campaign controller v1

`CampaignController` is the execution composition layer for the content-bound campaign
contracts. It does not infer campaign success from an investigation run. Each finite epoch is
reserved before publication, leased to one stable controller owner, executed through an
idempotent `EpochRunner`, checkpointed, evaluated by the pure campaign policy, and advanced by
one durable recommendation.

The production `InvestigationEpochRunner` composes `InvestigationController` and `RunRequest`.
It validates the canonical Factory snapshot's run, project, terminal status, and persisted scope;
derives usage and the Factory-ledger checkpoint source from that snapshot; and permits its fact
projector to supply only domain progress, next actions, and evidence references.

## Recovery contract

The controller stages the exact execution and the policy-input digest before applying a
recommendation. Stable W-0052 operation identities make checkpoint, next-epoch, and terminal
effects idempotent. On restart, a staged recommendation wins over changed caller facts and its
effect is reconciled before new work runs. An orphaned lease without a staged execution remains
waiting and is never silently rerun or called successful. A verified staged execution may use
the explicit `epoch.lease-reconciled` persistence operation to resume checkpoint publication.

Fault-injection boundaries are `launched`, `execution-staged`, `checkpointed`,
`recommendation-staged`, `recommendation-effect`, `health-recorded`, and
`recommendation-applied`.

## Canonical health observations

Each recommendation stages its exact bounded policy-input envelope, recommendation, and health
rollup before any effect. Once the semantic effect begins, the controller commits the health
event and bounded projection before marking the recommendation applied. Recovery reuses those
staged bytes; it never asks API or browser callers to recreate policy facts. Operator stop may
supersede a recommendation that has no durable effect, but first reconciles health when an exact
transition or published next epoch proves that the effect began.

Health binds campaign, epoch, checkpoint, policy-input digest, and recommendation identity. It
stores exact canonical outcome totals (including at most 256 artifact IDs), current policy-input
retry and no-progress counts, epoch and cumulative novel-progress counts, phase, checkpoint
`cumulative_usage.wall_seconds`, and an optional explicit next-checkpoint wall-second
expectation. The expectation is never inferred from wall clock, must fit cumulative wall
authority, and is accepted only when the recommendation actually schedules another epoch;
terminal, waiting, suspended, and blocked observations keep it null. Attempts and
known-progress digests share the 256-item policy-input bound.

The append-only campaign stream retains the complete rebuild authority. Its SQLite read model
keeps only the latest 32 rollups, and callers may request at most that tail. Public projections
use current/comparison values, counts, and bounded problem codes; canonical policy envelopes are
not exposed. An upgraded pre-W-0070 campaign may begin its health stream at any verified
checkpoint sequence; observations after that point must remain contiguous.

## Gates and isolation

- Epoch and cumulative reservations are checked before W-0052 publication.
- Work count and parallel width must fit the exact epoch budget.
- Campaign, epoch, lease, owner, project, and scope identities are checked at every handoff.
- Waiting, suspended, and terminal campaigns cannot invoke runners.
- Pausing and resuming are scheduler-authorized state changes; stopping is operator-authorized.
- Scope, ceiling, capability, and risk authority are immutable during execution; changed
  contracts require the exact W-0053 change request and durable operator decision, represented
  as a new content-bound campaign authority rather than an in-place mutation.
- Handoffs expose only the latest 32 evidence and run references plus total counts and a
  truncation flag; raw transcripts are never copied into the summary.
