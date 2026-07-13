# Deterministic campaign policy v1

`campaign_policy.py` is the pure W-0053 decision boundary between canonical campaign
facts and W-0054 execution. It reads no clock, environment, model, filesystem, or
network state. The same content always yields the same content-bound recommendation ID.

## Inputs and progress

The policy binds one campaign and finite epoch to its exact previous/current checkpoint,
epoch result, committed/reserved budget account, canonical outcome totals, known material
digests, and stable attempt facts. Only new typed material evidence, resolved hypotheses,
coverage movement, reproduced findings, or changed capability gaps count as progress.
Coverage, hypothesis, and finding signals must agree bidirectionally with their canonical
total deltas; repeated prose and self-reported productivity cannot invent progress.

Inputs are normalized before evaluation. Reordered attempts, progress signals, known
digests, and limiting resources therefore produce identical recommendations, explanations,
serialization, and IDs. Regressing totals or usage, stale/discontinuous checkpoints,
conflicting identities, malformed digests, overflow, and budget contradictions fail closed.

## Budget accounting and decisions

Reservations have stable IDs. Retrying the same ID and exact usage is idempotent; conflicting
content fails. Refund removes only an extant reservation, and commit consumes it exactly once
while requiring actual usage to fit the reservation and cumulative ceiling. Retry spend is
ordinary authoritative usage, so interrupted work cannot be charged twice by replay.

Hard cumulative thresholds recommend `exhausted`. Soft thresholds recommend
`ask-operator`; they never silently disappear behind reservation enforcement. Explicit
bounded detectors recommend continue, reprioritize, backoff, suspend, ask-operator,
policy-stop, or success with stable reason codes for equivalent attempts, no novelty,
validation churn, dependency deadlock, environment flapping, and notification churn.

Every in-place campaign contract change accepts only the exact content-bound
`CampaignChangeRequest` approval identity, including scope, budgets, completion criteria,
operator policy, and component versions. The policy validates this supplied durable fact;
W-0052 persists it and W-0054 alone may execute the resulting authority or recommendation.
