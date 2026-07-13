# Campaign coverage and archive lifecycle source contract

`factory-campaign-lifecycle/v1` is the minimal durable source contract for the campaign and
archive entities deferred by `factory-outcomes/v1`. It is not itself an outcome projection.
The document contains sorted campaign and archive records, exact schema/source versions, stable
identities, positive revisions, and no timestamps or model-authored prose.

## Authority and transition matrix

| Record | Authority | State | Allowed next states | Terminal |
| --- | --- | --- | --- | --- |
| campaign | `factory-scheduler` | `planned` | `active`, `cancelled` | no |
| campaign | `factory-scheduler` | `active` | `completed`, `cancelled` | no |
| campaign | `factory-scheduler` | `completed` | none | yes |
| campaign | `factory-scheduler` | `cancelled` | none | yes |
| coverage | `muster` | `uncovered` | count-derived `partial` or `covered` | no |
| coverage | `muster` | `partial` | count-derived `partial` or `covered` | no |
| coverage | `muster` | `covered` | may become `partial` only when canonical total scope grows | yes for the observed scope |
| archive | `operator` | `unarchived` | `archived` | no |
| archive | `operator` | `archived` | none | yes |

Coverage is derived exclusively from bounded non-negative `completedUnits` and `totalUnits`:
zero completed units are `uncovered`, equal positive counts are `covered`, and every other valid
pair is `partial`. Counts cannot decrease. Scope can grow, so coverage may move from `covered` to
`partial` without changing campaign completion. This is an observed-scope fact, not a success,
validation, acceptance, report-publication, or archive transition.

Archival is a distinct operator-owned record parented to a campaign. Completing or covering a
campaign never creates an archive record. Likewise, an archived record cannot promote campaign
completion, disposition, proof validation, acceptance, or report publication.

## Durability boundary

`CampaignLifecycleStore` writes one canonical UTF-8 JSON document through a same-directory
temporary file, file `fsync`, atomic replacement, and directory `fsync`. It rejects duplicate
JSON keys, unknown or missing fields, malformed counts, stale revisions, invalid transitions,
wrong authorities, oversized state, non-regular state files, symlinked roots/state files, and
archive records whose campaign parent is absent. Serialization sorts records and object keys and
contains no process-, locale-, time-, or insertion-order-dependent data.

The store does not yet feed `project_outcomes`, the incremental outcome source envelope, the run
API, or Mission Control. It also makes no every-boundary crash, cross-store atomicity, derived
cache recovery, or snapshot/dossier rebuild claim; those remain separate W-0026 integration work.
