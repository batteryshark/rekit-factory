# Exact campaign change approvals v1

Campaign changes are server-published `CampaignChangeRequest` documents. The canonical document,
including its private reason and proposed contract, is stored in the campaign event stream. The
operator projection exposes only stable identities, status, request/base revisions, application
status, and structured current/proposed values for scope, epoch/cumulative budgets, completion,
operator policy, and component versions. Goal and reason text are never public.
Publication rejects either the current or proposed authority when it contains more than 64
component versions or 256 required artifact IDs. Exact authority details are never truncated.

The decision endpoint is `POST /api/campaigns/{campaignId}/change-decisions`. Its JSON object must
contain exactly `requestId`, boolean `approved`, integer `expectedRevision`, and `operationId`.
Unknown fields—including proposed contract content—are rejected. The request must belong to the
route campaign and still bind the campaign revision captured at publication. Exact operation
retries converge before stale checks; changed decisions, cross-campaign identities, and stale
base/request revisions fail closed.

Approval is a durable two-phase application journal. The accepted request first becomes
`approved` with `applicationStatus: pending`. Reconciliation stops the old campaign under an
exact request-derived operation, creates and starts the proposed content-bound successor, then
marks application `applied`. Stopping first prevents concurrent old/new execution; a crash leaves
the accepted authority durable and an exact retry resumes the remaining steps. Rejection uses
`applicationStatus: not-applicable` and creates no authority.
