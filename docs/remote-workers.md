# Isolated and remote Rekit workers

## Decision

Factory uses one transport-neutral invocation envelope for local tools, disposable
VM workers, and interactive reverse-engineering stations. A worker advertises its
platform, architecture, installed Rekit tool IDs, isolation type, and whether an
operator can attach to it. Muster owns the work item; the worker owns only one
leased invocation.

The first Windows target should be a remote **x86-64 Windows analysis host** with a
snapshot-backed FLARE-VM image and an optional Windows Sandbox execution layer. An
Apple Silicon Windows VM remains useful for ordinary Windows tools, but it should
not be treated as proof of native x86-64 debugger fidelity. UTM documents Windows 11
on Apple Silicon as the supported path; full x86-64 emulation can be evaluated later
as a slower fallback. See [UTM's Windows guide](https://docs.getutm.app/guides/windows/).

## Why this shape

FLARE-VM is explicitly designed for a Windows VM and recommends taking snapshots
before and after installation, then switching to host-only networking. Its current
requirements include Windows 10 or newer and at least 60 GB of disk. That makes it a
good durable analysis image, not a per-invocation container. See the
[FLARE-VM project documentation](https://github.com/mandiant/flare-vm/blob/main/README.md).

On a compatible Windows 11 24H2 host, the newer `wsb` CLI can start a sandbox,
execute a command, share a folder, connect interactively, and stop the environment.
This is a useful disposable layer for bounded invocations. Microsoft also documents
that networking is enabled by default and writable mapped folders can affect the
host, so Factory must generate an explicit network policy and never map a project
workspace writable. See [Windows Sandbox CLI](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-cli)
and [Sandbox configuration](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file).

Containers remain useful for Linux command-line tools, deterministic build
environments, and service emulation. They are not the Windows GUI/debugger worker and
must not claim Windows kernel or x64dbg coverage.

## Invocation contract

`rekit_factory.remote` defines the first version of the envelope:

- stable invocation, Muster run, and work-item IDs;
- Rekit tool ID and arguments;
- target path plus optional content hash;
- explicit network policy;
- durable approval ID for any gated invocation;
- ordered status/log events;
- terminal exit status and artifact manifest;
- optional attach URL for RDP, VM console, or a terminal session.

Every capability, request, event, result, and artifact envelope crosses the wire as
JSON with `schema_version: 1`. Receivers reject unknown versions instead of silently
guessing. Tuple-valued Python fields are JSON arrays on the wire and round-trip back
to their immutable in-process representation.

The provenance tuple is `(run_id, work_item_id, invocation_id, worker_id)`. Events
and terminal results carry the complete tuple; requests carry the first three and
the accepting worker supplies its advertised `worker_id`. An adapter must not copy
identifiers from a worker-supplied payload over the controller's leased work. Event
sequence numbers begin at one and increase within an invocation, allowing an event
consumer to resume after its last durable sequence.

Artifacts are manifests, not arbitrary worker paths. Each record contains a path
relative to the invocation's `output/` root, a lowercase SHA-256 digest, byte size,
and optional media type. Absolute and parent-traversing paths are invalid. The
controller verifies the size and digest after transfer before attaching an artifact
to the run.

`target_path` names the staged target as seen by the selected transport. The local
proof accepts a host path; a remote adapter maps it to its immutable `input/` path.
The optional target digest identifies the bytes independent of that mapping. The
arguments field is an opaque string array rather than a shell command: adapters
must invoke tools without introducing an extra shell parsing step.

The contract deliberately contains no credentials or host mount configuration.
Authentication and staging belong to the transport configuration, while the
invocation contains only the explicit network policy and, for a gated operation,
the durable approval identifier.

The remote implementation should expose:

```text
GET  /v1/capabilities
POST /v1/invocations
GET  /v1/invocations/<id>/events       SSE, resumable sequence
POST /v1/invocations/<id>/cancel
GET  /v1/invocations/<id>/result
POST /v1/invocations/<id>/reset
GET  /v1/invocations/<id>/attach
```

The controller authenticates the worker with a pinned certificate or an SSH host
key. The worker never receives provider API keys, Factory's whole storage root, or a
general-purpose host credential.

`LocalRekitWorker` is the conformance proof for the envelope and approval boundary;
it is not an isolation boundary. Native Windows, VM, container, and other adapters
must implement the same `WorkerTransport` behavior without changing the envelopes.
Machine-specific lifecycle and reset behavior stays behind those adapters.

### Minimal HTTP transport

`rekit_factory.remote_http` provides the first deployable transport boundary. The
server exposes authenticated capability discovery, asynchronous invocation
submission, ordered event retrieval with an `after` cursor, and terminal result
retrieval. The matching client implements `WorkerTransport` and polls the bounded
result endpoint while also exposing resumable events.

The server requires an explicit bearer token and staged input root. Networked
requests may name only regular files beneath that root; absolute paths and parent
traversal are rejected. The default allowed invocation network policy is only
`none`, so accepting a request cannot silently relax worker networking. Request and
response sizes and client connect/read/result waits are bounded. Worker identity is
pinned when the server starts, and returned provenance must match the leased
invocation before a result becomes visible.

Bearer authentication does not provide transport encryption. A deployment beyond
loopback must terminate TLS with a pinned/private trust configuration or place the
endpoint inside an authenticated tunnel. Token rotation, rate limiting, durable
server-side event storage, target upload/content-hash verification, cancellation,
artifact transfer, and interactive attachment remain adapter/deployment work. The
in-memory HTTP proof must not be treated as the Windows worker proof or as an
isolation boundary.

## Files and isolation

Each invocation gets three directories inside the worker boundary:

```text
input/      immutable staged target, addressed by hash
scratch/    disposable writable working directory
output/     artifacts collected after termination
```

Defaults:

- network `none`;
- clipboard, camera, microphone, printer, and host drive sharing disabled;
- no writable host mapping except a newly-created invocation output directory;
- output is collected only after the tool exits or is stopped;
- every returned artifact is hashed and attributed to the worker and invocation;
- the sandbox or VM is reset after collection;
- interactive attachment is audited as an event and does not bypass the tool's
  existing Factory permission decision.

Microsoft's own sample for testing an unknown file disables networking and vGPU and
maps the input read-only, which matches these defaults. See the
[Windows Sandbox sample configurations](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-sample-configuration).

## Delivery plan

1. **Envelope proof:** use `LocalRekitWorker` for one read-only tool and one denied
   gated tool; persist the same request and result shape used by the controller.
2. **Headless Windows worker:** stand up a clean x86-64 Windows VM, install Rekit and
   a minimal tool set, expose capability/invoke/events/result, and return a benign PE
   analysis artifact.
3. **Disposable execution:** add Windows Sandbox orchestration with networking off,
   immutable input, dedicated output, stop, and cleanup.
4. **Interactive debugger:** add an attach URL and x64dbg session workflow on the
   snapshot-backed FLARE-VM image. Keep this separate from headless invocations.
5. **Other platforms:** Linux containers/VMs, Android device workers, and an Apple
   Silicon Windows worker where its architecture limitations are acceptable.

## Proof acceptance

- A benign PE fixture submitted from the Mac is hashed, staged, analyzed on Windows,
  and returned with logs and artifact hashes.
- The same invocation can be followed in Mission Control without exposing the worker
  filesystem directly.
- Network-disabled execution cannot reach the LAN or internet.
- Cancelling stops the tool and leaves a terminal, reasoned work state.
- Reset removes scratch state before the next invocation.
- An x64dbg invocation exposes an audited attach action and never mounts the Factory
  workspace writable.
