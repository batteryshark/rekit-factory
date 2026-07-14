# Factory automation hooks v1

Factory exposes an optional loopback-only automation gateway at
`/api/automation/v1`. It is a deterministic integration shell around Factory; it is
not a scheduler and does not own Muster work, campaign/run state, permission
questions, model sessions, credentials, evidence, or dossiers.

The operator configures clients, approved templates, target aliases, scope aliases,
and an `AutomationOwner` adapter in the service process. Remote payloads select an
approved template by ID. They cannot submit a host path, scope envelope, provider
configuration, credential, or answer to a gated question.

## Authentication and retries

Each request includes:

- `X-Factory-Client`
- `X-Factory-Timestamp` as Unix seconds
- `X-Factory-Nonce` as a unique bounded ID
- `Idempotency-Key` for every `POST` (and no idempotency header for `GET`)
- `X-Factory-Signature`, an HMAC-SHA256 hex digest

The signed bytes are the newline-joined timestamp, nonce, uppercase method, exact
path including query, idempotency key, and SHA-256 of canonical JSON. The helper
`rekit_factory.automation.signature` implements that contract.

Retry a command with the same idempotency key and body but a fresh nonce, timestamp,
and signature. Reusing a key with different content fails closed. Reusing a nonce is
a replay and fails closed. The durable command journal is only an integration
idempotency/audit record; the canonical owner must also honor the supplied stable
`operation_id`, which closes a crash after canonical commit but before HTTP response.

Keys remain environment-owned and never enter the journal or request body. Rotate by
temporarily configuring old and new client IDs, migrating callers, inspecting the
redacted audit stream, and removing the old principal. This first version refuses
non-loopback server binds. Any future non-loopback deployment requires a documented
threat model, TLS with client authentication at a trusted proxy or equivalent mutual
authentication, bounded proxy request sizes/rates, and a tested revocation path.

## Generic launch and cursor workflow

Canonical launch body:

```json
{
  "schedule": {
    "scheduleId": "nightly-fixture",
    "scheduledFor": "2026-07-14T04:00:00Z"
  },
  "templateId": "approved-fixture"
}
```

After computing the headers, a generic CLI can send it without vendor-specific
fields:

```sh
curl --fail-with-body http://127.0.0.1:8769/api/automation/v1/launch \
  -H 'Content-Type: application/json' \
  -H "X-Factory-Client: $FACTORY_CLIENT_ID" \
  -H "X-Factory-Timestamp: $FACTORY_TIMESTAMP" \
  -H "X-Factory-Nonce: $FACTORY_NONCE" \
  -H "Idempotency-Key: nightly-fixture-20260714" \
  -H "X-Factory-Signature: $FACTORY_SIGNATURE" \
  --data-binary @launch.json
```

The response supplies stable run/campaign IDs and a Mission Control deep link. Poll
the redacted feed with a signed `GET`:

```text
GET /api/automation/v1/events?after=0&limit=50
```

Persist `nextCursor` only after processing the page. Re-reading a page may duplicate
transport bytes, but `(runId,eventId)` is stable and each logical source event is
stored once. A consumer or source-projection outage cannot roll back canonical work;
already materialized feed rows remain available and later polls reconcile missing
events. On `proof.available`, fetch only the verified dossier route returned for the
owned run. Handoff acknowledgement and cancellation are idempotent commands delegated
to their canonical owner. There is intentionally no automation endpoint for answering
human gates.
