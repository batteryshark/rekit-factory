# Bounded campaign and epoch contracts v1

`factory-campaign-contract/v1` is the pure execution contract for a finite research
campaign. It complements—but does not replace—the smaller
`factory-campaign-lifecycle/v1` outcome source. Durable, exactly-once storage is implemented
by `CampaignPersistence` and documented in `campaign-persistence-v1.md`; progress decisions,
controller execution, and UI controls remain separate downstream concerns.

## Identity and bounds

A campaign identity is the SHA-256 of canonical JSON binding its project and normalized
goal, exact authorization scope revision/digest, per-epoch and cumulative budgets,
completion criteria, operator policy, and sorted component versions. Every epoch has its
own content identity binding the campaign, ordinal, finite work set, budget, and prior
checkpoint. The first epoch cannot claim a prior checkpoint; every later epoch must.

Every budget uses explicit units for work items, concurrency, retries, input/output tokens,
cost units, wall seconds, tool calls, network calls, and artifact bytes. Limits distinguish
hard from soft enforcement, reject booleans/floats/non-finite or 64-bit-overflow values,
and require each epoch to fit both the campaign per-epoch budget and cumulative authority.
Zero network/tool/artifact allowance is explicit; work, concurrency, and time can never be
undefined or zero.

Every in-place campaign contract change requires an exact-content operator decision in v1,
including scope, ceiling enforcement/value, completion criteria, operator policy, and
component versions. The decision request binds the current campaign identity, complete
proposed contract, and normalized reason under its own SHA-256 identity. A changed project
or goal is a new campaign, not an in-place revision.

## Transition and authority matrix

| Current | Allowed next states | Authority of next state |
| --- | --- | --- |
| requested | running, stopped, policy-stopped, failed | scheduler; operator for stopped; validator policy for policy-stopped |
| running | waiting, suspended, completed, exhausted, blocked, stopped, policy-stopped, failed | scheduler except operator/validator stops |
| waiting | running, suspended, stopped, policy-stopped, failed | scheduler except operator/validator stops |
| suspended | running, stopped, policy-stopped, failed | scheduler except operator/validator stops |
| completed, exhausted, blocked, stopped, policy-stopped, failed | none | terminal |

`completed` is not a synonym for exhausted, blocked, stopped, policy-stopped, or failed.
Every terminal outcome carries a stable reason code and at least one canonical evidence
reference. Completed and exhausted outcomes require a final checkpoint. Stopped,
policy-stopped, blocked, or failed outcomes may explicitly omit it only when no epoch has
committed one; the contract never invents a checkpoint. Model prose cannot create a progress
or terminal fact.

Progress signals are restricted to new material evidence, resolved hypotheses, coverage
movement, reproduced findings, and changed capability gaps, each bound to a stable
reference and material digest. W-0053 will decide how these facts affect continuation;
this contract deliberately contains no model calls, random scoring, wall-clock reads, or
mutable persistence.

Each checkpoint is content-bound to one campaign/epoch, a positive sequence, sorted unique
canonical source revisions/digests, and exact cumulative resource usage. Epoch results carry
only that checkpoint identity, typed progress signals, and stable next-action IDs. They do
not contain transcripts or self-reported model activity.
