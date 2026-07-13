# Analysis range contract v1

`rekit_factory.ranges` defines the provider-neutral identity and lifecycle boundary for a
bounded, multi-node analysis range. The module uses only the Python standard library. It
does not import a cloud, hypervisor, container, remote-worker, or controller implementation.

## Contract and authority boundary

A `RangeTemplateV1` identifies the ordered-by-identity node and topology definition. Each
node binds its platform, architecture, image digest, capabilities, and declared services.
Links name exact source/destination nodes and destination services; they do not represent
ambient network access.

A `RangeSpecV1` binds the template digest and all per-range security inputs:

- immutable input digests, sizes, media types, normalized relative mount locations, and a
  mandatory read-only flag;
- the exact W-0022-derived scope ID and revision, permitted range actions, exact HTTPS
  origins, opaque credential references, and input IDs;
- isolated networking with an empty egress allowlist by default;
- node, CPU, memory, scratch, output, and work-item ceilings;
- whole-second requested/expiry times and bounded reset/destroy policy.

Every decoder rejects missing and unknown fields. Canonical JSON sorts object keys and all
set-like collections are normalized by the contracts. SHA-256 content identities therefore
remain stable under mapping/set-like reorder and change when a security-relevant field
changes.

Provider handles are bounded opaque identifiers with only `range` or `node` kind. They
cannot contain URLs, filesystem paths, credentials, or general provider authority. Host
paths and provider credentials have no field in any v1 contract. Credential intent uses
only an opaque `credential:` reference already present in the exact scope revision.

## Lifecycle matrix

Only the following transitions are valid. `destroyed` is the sole terminal state. `expired`
and `failed` reject work but remain cleanup/recovery states so the adapter can perform an
explicit, idempotent destroy; a failed lease may instead be reset into a new generation.

| Target | Owner | Allowed predecessors | Terminal |
| --- | --- | --- | --- |
| `requested` | requester | none | no |
| `provisioning` | adapter | `requested` | no |
| `ready` | adapter | `provisioning`, `resetting` | no |
| `in-use` | scheduler | `ready` | no |
| `resetting` | adapter | `ready`, `in-use`, `failed` | no |
| `expired` | clock | `requested`, `provisioning`, `ready`, `in-use`, `resetting`, `failed` | no |
| `failed` | adapter | `requested`, `provisioning`, `ready`, `in-use`, `resetting`, `expired` | no |
| `destroyed` | adapter | every non-destroyed state | yes |

The v1 status vocabulary intentionally has no separate `cancelled` state. Cancellation is
an explicit destroy operation with terminal reason `cancelled`, preserving the exact state
matrix while making cancelled leases reject new work.

Failures carry a bounded code, reason, attempted transition, and retryability. Revisions are
contiguous. Reset increments the lease generation exactly once and replaces node handles,
invalidating handles from older generations.

## Deterministic adapters

`DeterministicFakeRangeAdapter` is a serializable conformance fake. The included benign
fixture has two Linux nodes, one declared node-to-node service, one immutable input, an
isolated network with no egress, and no credentials. Its two inert actions create canonical
JSON bytes from already-declared metadata, then record scratch and verified-output metadata.
It does not run a command or interpret target bytes.

`DeterministicManifestRangeAdapter` is a distinct second adapter type over the same v1
contract. It assigns a separate opaque provider-handle namespace and materializes a compact
provider-manifest evidence shape that binds the lease/spec, generation, action, node, input
digests, and topology digest. Parameterized conformance tests run the same provision,
execute, checkpoint/restart, exact retry, reset, generation rollover, destroy, injected
failure, and recovery assertions against both types. This demonstrates that the v1
investigation contract is not coupled to one adapter's handles or evidence encoding.

The shared deterministic conformance engine enforces:

- exact template/spec/range identity and resource ceilings;
- one range generation's node handles and inputs, rejecting cross-range access;
- exact input mount, endpoint, and credential intent against the bound scope revision;
- no egress and no credentials unless both the scope and range allow the exact reference;
- per-generation scratch/output cleanup with durable evidence metadata;
- expiration, reset, cancellation-as-destroy, and idempotent cleanup;
- deterministic failure injection before each lifecycle target;
- exact operation retries and fail-closed conflicting range/operation-ID reuse.

Its canonical checkpoint contains range state, transition history, current scratch/output,
evidence metadata, operation outcomes, clock, and pending one-shot failure injections. On
restore it strictly decodes UTF-8 JSON without duplicate keys and validates identities,
template binding, transition/revision/generation history, deterministic handles, lease-local
ownership, evidence counts, and stored result/error shapes. Every operation persists its
canonical typed request envelope and digest; successful results must resolve to the same
materialized range history or exact evidence record, and evidence must resolve back to a
successful execute request with matching node, handle, inputs, path, generation, and bytes. Exact
retries after restart return or re-raise the recorded outcome without duplicating a lease.

## Cleanup supervision

`rekit_factory.range_supervisor` adds a provider-neutral reconciliation boundary above an
adapter's independently durable lifecycle. It registers only a range identity and exact
expiry time, plus an optional bounded cleanup reason; it has no provider credential,
connection, host-path, provisioning, or execution fields.

Every expiration or destruction effect uses a write-ahead ordering. The supervisor first
atomically persists the pending operation kind, attempt, and stable operation ID, then calls
the adapter, and only then persists an audit acknowledgement. A restart in the ambiguous
window replays that exact operation ID. A new attempt ID is allocated only after a
retryable adapter failure has itself been durably recorded. Non-retryable failures enter an
inspectable blocked state rather than spinning or silently changing intent.

The small POSIX state store rejects symlink roots and state files, non-regular or oversized
state, invalid UTF-8, duplicate JSON keys, unknown fields, and inconsistent range identity.
Writes use a mode-0600 temporary file, file `fsync`, atomic replacement, and directory
`fsync`; a failed replacement retains the last good checkpoint. This storage makes the
supervisor's intent durable. It does not substitute for adapter-side durable idempotency or
prove that a provider performed a real expiration, reset, destruction, or secure disposal.

## Explicit nonclaims

Passing either deterministic adapter lifecycle proves only deterministic contract
conformance. It is **not**:

- real Windows execution or native Windows fidelity;
- a VM, hypervisor, container, cloud, or commercial-range provisioning proof;
- packet-level isolation, egress-filtering, credential-broker, or host-mount enforcement;
- reset, secure deletion, forensic disposal, or cross-tenant isolation proof;
- production controller integration, target acquisition, or infrastructure authority; or
- completion of W-0045 or the deferred Frenzy Express capstone.

Those claims require a real adapter and independent execution/isolation evidence in later
W-0031/W-0045 work. The v1 fake makes those future adapters testable without pretending the
in-memory model supplies their proof.
