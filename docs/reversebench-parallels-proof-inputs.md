# ReverseBench Parallels proof inputs

This is the exact host-side preparation for the authorized W-0050 proof. It is not an
isolation result and does not authorize or perform a VM operation.

The canonical prepared-input identity (package, stage basename, opaque canary references,
and all probe specifications) is
`c22575940eef6aee361cada0d34e1ce83e721c1905f988ab2cfd18fdba82370b`. The final probe-plan
identity is computed later because it must additionally bind the real adapter, image, worker,
scope, evidence policy, mount/network policy, and reset identity.

## Public package and staging

The owned ReverseBench `branch-signal` fixture produces one canonical, uncompressed POSIX
USTAR archive:

- package ID: `branch-signal-public-v1`
- size: `10240` bytes
- SHA-256: `c4f020b1fc4704f07645ccbd7f87058a4604e96b1dc97f0ac9060c8bd4d11886`
- stage basename: `sha256-c4f020b1fc4704f07645ccbd7f87058a4604e96b1dc97f0ac9060c8bd4d11886.tar`
- `target/branch-signal.rbvm`: 26 bytes, mode `0555`, SHA-256
  `a1e1f9f039413c58b73f6e960e77e190a918aaef6415766b8fa5ddd99e1a0502`
- `task.json`: 1448 bytes, mode `0444`, SHA-256
  `4c5b81a1ead94b8c6f2c3a92a068eebba0af31e7a110c19ca5187efe8f7a3773`

The host `rekit-input` staging directory must contain exactly that one regular file. The VM
maps it read-only to the binding's `/input/<basename>` identity. Scratch and output are
distinct, fresh locations mapped to `/scratch` and `/output`; neither is inside the input
share. No repository directory, private fixture directory, host home, socket, credential,
retrieval store, prior artifact/log store, or second file is staged.

The archive is rebuilt from ReverseBench's descriptor-relative public allowlist immediately
before staging. The preparer verifies archive size/digest, exact member names/content,
canonical metadata, and that none of seven non-empty host-side canary byte strings occurs in
the archive. The Parallels candidate assessor must then report the single staged file with
the same archive identity.

`materialize_branch_signal_package` is the only repository-provided publication boundary. Its
caller must supply an already-created, separately authorized destination directory. It opens
that directory with `O_DIRECTORY|O_NOFOLLOW`, requires it to be empty, creates a fixed
temporary basename with `O_EXCL|O_NOFOLLOW`, writes and fsyncs the exact archive, changes it to
mode `0444`, and publishes it through an exclusive hard-link operation that cannot replace an
existing final name. It removes the temporary name, fsyncs the directory, then reopens and
verifies the sole final file's type, link count, mode, size, and digest. It never creates the
destination and performs no share or VM operation.

## Opaque canaries

The seven values remain on the trusted controller and never enter the package, worker prompt,
environment, or plan JSON. Their plan records contain only stable IDs, kinds, and SHA-256
digests. The owned values come from the fixture-private source, ground truth, private test,
prior dossier, held-out identifier, reset recipe, and build recipe. They cover the contract
kinds `source`, `truth`, `private-test`, `dossier`, `credential`, `sibling`, and `residue`.

## Exact two-trial observation matrix

Trials `branch-signal-trial-a` and `branch-signal-trial-b` each execute all eleven denial
channels: artifact, cache, credential, environment, log, network, path, process, retrieval,
sibling, and socket. Every denial probe checks all seven opaque canary IDs. Each trial also
has one `path/public-readable` probe proving that the package remains usable. Trial B adds the
required `post-reset/empty` probe after Trial A's mutable state is reset. This yields 25 exact
probe specifications.

For each denial probe, evidence must record the bounded query/action, exit or observation
state, redacted output digest, and absence of every canary. Network evidence must separately
show no configured adapter, route, DNS resolver, listening/connected socket, LAN reachability,
or internet reachability. Mount evidence must enumerate the input, scratch, and output roots;
prove input is read-only; reject traversal/reparse/path aliases; and prove no other share or
host path. The sibling probe attempts only opaque trial-owned handles and paths, without
placing canary plaintext in the worker request. The post-reset probe checks files, caches,
environment, processes, credentials, sockets, and output handles before Trial B receives its
fresh package.

Both trials must return complete content-addressed evidence. Any missing result, failed or
not-run probe, plan mismatch, unknown canary reference, canary leak, extra staged file, network
capability, mount expansion, or non-empty reset observation blocks qualification. Passing the
matrix is necessary but still requires independent review of the real adapter evidence.
