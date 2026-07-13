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
`recommendation-staged`, `recommendation-effect`, and `recommendation-applied`.

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

