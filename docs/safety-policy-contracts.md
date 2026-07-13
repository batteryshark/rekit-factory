# Safety policy and strategy metadata contracts

`policy_contracts.py` defines the contracts that W-0060 will later persist and enforce.
It does not yet change launch, resume, API, or Mission Control behavior.

A named safety policy is an immutable versioned document. Its `policy_id` is the SHA-256
identity of the complete canonical document, including its name and revision, exact
allowed tool IDs, approval mode and approval-required tools, run ceilings, exact scope
revision/digest binding, and compatibility mode. Consequently, changing any authority
input produces a different identity; matching labels cannot overwrite one another.

Parsing is fail closed. Unknown or missing fields, non-integral ceilings (including
non-finite numbers and booleans), unsorted or duplicate set-like arrays, partial scope
bindings, and unsupported schema values are rejected. Requiring canonical arrays avoids
silently accepting two different serialized representations as the same authority.

Pre-policy runs can be represented only by `legacy-deny-all-v1`. That compatibility
document has no tools, no approval path, and no scope authority. It makes old records
readable without inventing permissions that were never durably recorded.

Strategy metadata is also immutable and versioned. It describes role objectives and an
acyclic dependency graph, default ceilings, compatible model profiles, exact compatible
policy identities, required tools, and whether scope binding is required. These are
compatibility claims, not grants: later controller integration must resolve a persisted
policy and independently validate that its authority satisfies the selected strategy.
