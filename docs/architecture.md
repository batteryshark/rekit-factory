# Investigation control plane

## Product model

Factory runs ad-hoc investigations. There is no predefined job catalog.

```text
target + goal + Rekit tools + worker roles + model profile + safety policy
                                      |
                                      v
                            durable Muster run
                                      |
                    +-----------------+-----------------+
                    |                                   |
               Rekit tool work                    model worker work
                    |                                   |
             permission gate                     bounded analysis
                    +-----------------+-----------------+
                                      |
                           events, logs, artifacts
                                      |
                               Mission Control
```

## Ownership

Muster owns run identity, SQLite storage, work-item leasing, dependencies, coverage,
questions/answers, and resume. Rekit owns tool manifests, availability, safety facts,
and execution. Factory owns worker state, event logging, model profiles, permission
policy, tool-to-run association, and the operator surface.

Factory must not copy Rekit skills or build a parallel lifecycle ledger.

## Semantic authority invariant

Rekit manifests separately declare a versioned semantic authority ceiling using exact
W-0022 `ActionAuthority` names plus a distinct `credential_use` boolean. Safety tier,
isolation facts, and operator consent do not grant target/action authority. Factory
validates the declaration at catalog load, requires the engagement scope to cover every
declared action and credential floor before exposing a tool, and rechecks the exact
runtime endpoint/account/action intent at dispatch. A model may narrow which declared
operation it requests but cannot add an endpoint, credential use, or action absent from
the manifest.

The safe effective manifest contains the tool identity/version, safety facts, authority
version/actions/credential flag, and a canonical SHA-256 digest. It contains no catalog
path or credential value. Factory pins that digest in run configuration and durable work,
rejects catalog changes between creation and dispatch, and cites the digest in permission,
tool-call, and evidence/proof metadata. Static offline legacy manifests narrow to
`read_local_target`; risky legacy or contradictory declarations are unavailable pending
review.

## Durable permission invariant

A tool manifest determines whether permission is required. When a gated tool reaches
the front of the queue, Factory:

1. creates a content-addressed Muster question;
2. links the question to the tool work item;
3. marks that item `blocked / needs_permission`;
4. stops the drain before dependency-blocked workers can start;
5. exposes the question in the Mission Control snapshot;
6. records `allow` or `deny` in Muster's durable answer table;
7. explicitly re-queues the linked work item and resumes.

An allow decision is consent, not isolation. Dynamic tools still require a worker
environment whose isolation and network policy fit the target.

## Worker model

Each worker has a stable ID, role, status, current step, model profile, timestamps,
and append-only events. Each model call is bounded to one work item and records its
provider, model, purpose, and usage. The model produces a structured worker report;
Muster's coverage oracle decides run completion.

The initial backend uses Pydantic AI with an OpenAI-compatible provider. A profile is
loaded from named environment variables such as `MINIMAX_API_KEY`,
`MINIMAX_API_BASEURL`, and `MINIMAX_API_MODEL`. API keys are never written to the run
database or `run.json`.

## Mission Control read model

The JSON snapshot intentionally contains the pieces already present in the v3 mockup:

- run status, goal, target, model profile, and coverage;
- workers and current steps;
- work items and dependencies;
- ordered status/log events;
- pending permission questions;
- model-call and Rekit-tool metadata;
- artifacts and reports.

The next UI slice can consume this shape before the API adds deltas or streaming.
