# Gated knowledge contribution architecture

## Decision

Factory treats investigation evidence and durable knowledge as different data
products. Run logs, model messages, tool output, artifacts, and events remain in
the append-only Factory/Muster evidence store. They are never copied wholesale
into rekit-kb. A knowledge contribution begins as a small structured candidate
that cites that evidence, passes classification and deduplication, and receives
an explicit operator decision.

This slice defines the pure records in `rekit_factory.knowledge_ingestion`. It
does not read or write rekit-kb, create branches or worktrees, run git, render
Markdown, or connect knowledge retrieval to ingestion.

## Boundaries

| Product | Owns | Must not own |
| --- | --- | --- |
| Run evidence | Raw events, logs, model messages, artifacts, hashes | Curated claims or canonical RE guidance |
| Candidate store | Classification, cited observations and theories, dedupe result, review state | A copy of raw logs/binaries or an unreviewed OKF document |
| rekit-kb staging worktree | Approved concept/index/log diff | Investigation runtime state or credentials |
| rekit-kb main branch | Reviewed, validated durable knowledge | Automatic Factory output |

Candidate records belong with the originating run until a later integration
explicitly exports them. Deleting or compacting high-frequency logs must not
silently invalidate accepted citations; cited events and artifact manifests need
the retention policy chosen under **Open decisions**.

## Extraction and rejection

An extractor classifies each source fragment before it can enter a candidate:

- `durable_knowledge` may continue;
- target-specific evidence remains a citation source, not generalized prose;
- behavioral instructions remain in skills/personas;
- executable code remains code;
- raw logs remain run evidence.

Credentials, private keys, personal data, raw binaries, persistence payloads,
and weaponized exploit chains are prohibited. The schema performs a conservative
deterministic secret/private-key screen and requires semantic safety classifiers
to supply explicit prohibited-content flags for categories that cannot be found
reliably with regex. Any flag rejects construction before review. Operators
cannot override this rejection; safe recognition/how-it-works material must be
extracted into a new candidate instead.

A candidate uses a rekit-kb taxonomy type, controlled tags, a kebab-case slug,
and structured claims. Observations and theories are separate arrays and their
claim kind must agree with the array. Every claim names one or more citations;
every citation names the Factory run plus an artifact or event. Artifact hashes
are retained when available. The candidate contains concise claims, not quoted
logs or binary payloads.

## Deduplication

Deduplication is a mandatory read-only phase before review:

1. Build search terms from normalized title, slug, type, and tags.
2. Read rekit-kb `index.md`, then the relevant domain index, following its
   progressive-disclosure contract.
3. Search frontmatter and bodies with case-insensitive exact terms and useful
   synonyms. Do not load the whole bundle into model context.
4. Record matched concept paths and one disposition:
   `no_match`, `enrich_existing`, or `overlap_distinct`.
5. For the same scope, enrich the existing concept and add citations. For a
   distinct overlap, create a concept and require reciprocal relationships in
   the eventual diff.

`DedupeQuery.rg_arguments` is inert query data for the future read-only adapter.
This design does not execute it. A candidate cannot become reviewable while the
dedupe disposition is `not_run`.

## Review state machine

```text
extracted --dedupe complete--> ready_for_operator
ready_for_operator --explicit approve--> approved --future integration--> staged
ready_for_operator --explicit deny-----> denied   --terminal; no mutation-->
```

An operator decision records identity, an ISO 8601 UTC timestamp, and rationale.
Only approval permits creation of a `StagingPlan`. Denial cannot produce a plan
and performs no filesystem, git, index, log, or bundle mutation. Editing a
proposal should create a revised immutable candidate and require dedupe/review
again; it must not mutate a previously approved record in place.

## Future staging lifecycle

The staging plan is descriptive only. A later, separately reviewed integration
would perform these steps:

1. Verify the approval is current and the rekit-kb source checkout is clean.
2. Fetch the configured rekit-kb remote and create a dedicated branch and
   worktree from the configured base commit. Never stage in the investigation
   worktree or directly on the default branch.
3. For `enrich_existing`, update the recorded existing concept. Otherwise create
   exactly one concept at the taxonomy-derived path.
4. Render OKF v0.1 Markdown with parseable YAML frontmatter. Include non-empty
   `type`, title, description, controlled tags, ISO timestamp, observations,
   clearly labelled theories, relationships, and a trailing `# Citations`
   section with run/artifact/event provenance.
5. Update the parent `index.md` description and prepend a same-day entry to root
   `log.md`. Preserve unknown frontmatter keys and mark removed knowledge as
   deprecated rather than silently deleting it.
6. Run `python3 scripts/okf_validate.py . --strict`. rekit-kb's contract requires
   fixing both errors and warnings even though OKF v0.1's hard conformance core
   is parseable frontmatter plus non-empty `type` and valid reserved files.
7. Present the complete diff and validation output for a final merge/publish
   decision. A staging approval is not permission to merge or push.
8. On denial or failure, retain the Factory review/evidence record but remove the
   disposable staging worktree and branch only through an explicitly authorized
   cleanup action. The base rekit-kb checkout remains byte-for-byte unchanged.

Only the concept, its parent index, root log, and justified reciprocal links may
change. Unexpected files in the diff fail closed.

## Failure and concurrency rules

- Candidate extraction, dedupe, and review are retryable and make no bundle
  changes.
- Candidate IDs are idempotency keys. A staging service must reject reuse for a
  different run or base commit.
- Approval binds to the candidate content hash, dedupe result, and future base
  commit. If any changes, approval becomes stale.
- Concurrent candidates targeting the same concept serialize at staging and
  rerun dedupe after rebasing.
- Validation failure never falls back to a partial concept-only commit.
- A missing bundle, dirty checkout, changed base, broken approval, prohibited
  content, or unexpected diff stops before merge and does not touch the default
  branch.

## Open decisions

1. Choose a portable citation representation for private Factory evidence:
   `factory://` URIs, an exported immutable evidence manifest, or both.
2. Define retention for cited events and artifacts so accepted knowledge never
   points at expired evidence.
3. Decide whether staging and merge require two distinct operator approvals. The
   recommended policy is yes.
4. Choose the trusted source of rekit-kb taxonomy tags and how taxonomy changes
   invalidate queued candidates.
5. Define content hashing and signing for the approval binding.
6. Choose worktree cleanup retention for rejected or validation-failed diffs.
