# Campaign API v1

Mission Control reads bounded campaign state from the loopback Factory service. The browser
does not rebuild policy, infer a terminal result, or write campaign tables directly.

`GET /api/campaigns` returns `{schemaVersion: 1, campaigns: [...]}`. A campaign object contains
only stable identifiers, public scope identity, the current epoch reference, canonical usage,
epoch/cumulative/remaining budgets, the exact durable policy recommendation and disposition,
the exact terminal outcome, bounded rebuild health, allowed operator actions, and a W-0054
bounded handoff. Health exposes only `degraded` and a problem count, not diagnostic content. Raw goals,
events, transcripts, paths, artifacts, and evidence contents are deliberately absent.
Degraded replay/live state exposes no allowed actions, and direct control requests fail closed.

`GET /api/campaigns/{campaignId}` returns `{campaign: ...}` and
`GET /api/campaigns/{campaignId}/handoff` returns `{handoff: ...}`. An unavailable campaign
authority yields an empty list and cannot accept controls.

Pause, resume, and stop use `POST /api/campaigns/{campaignId}/{action}`. Every request supplies
the stable `operationId` and the `expectedRevision` read with the campaign. Stop also supplies a
normalized `reasonCode` and optional bounded evidence identifiers. The server always binds the
stop to `operator-control:{operationId}` as its minimum durable evidence reference. An exact retry returns the current
canonical projection; a stale revision, invalid transition, or reuse of an operation identity
with different content returns HTTP 409. The controller serializes these operations with epoch
scheduling on the same durable SQLite authority.
