# Mission Control outcome workspace audit

`docs/mockups/e7-mission-control-v3.html` remains the canonical visual direction. W-0048
extends its run-detail hierarchy; it does not replace or reinterpret the mockup.

## Source-side comparison completed

The live Outcomes tab preserves the mockup's compact tab strip, dark layered cards, narrow
monospace metadata, colored entity signals, dense count bands, progressive disclosure, and
short entrance/hover feedback. It uses the live Mission Control tokens and existing global
keyboard tab behavior. The result is a facet board rather than a generic table: each entity
has a visually distinct type treatment and six equally scoped facet cells.

Responsive source review confirms that controls collapse from six columns to three and then
two, cards collapse to one column, and six facets collapse from three to two columns. The
existing global `prefers-reduced-motion: reduce` rule disables every outcome animation and
transition. Focus-visible treatment applies to the tab, search, selects, reset, canonical
parent filters, and cross-surface actions.

The consumer reads only `outcomeProjection`. Runtime cards expose only its entity identity,
parent, facet state/raw state/owner/known/terminal fields, and diagnostics. Links are emitted
only for ontology-backed relationships: run/worker/work-item to Activity, proof bundle or a
published finding to Dossiers, and operator decision to Decisions. No report, artifact, or
evidence link is guessed.

`semanticCanonicalBase64` carries the exact Python-canonical semantic envelope bytes. The
browser validates and decodes that envelope, renders its semantic projection rather than
unbound outer fields, and hashes those authoritative bytes for `semanticSha256` verification.
This avoids silently substituting JavaScript number formatting or key ordering for the public
identity domain. Outer source watermarks remain observational only.

Verified watermark-only updates retain the existing DOM and focus; a new verified semantic
identity renders once. Older snapshots without the byte envelope are explicitly legacy and
never claim exact verification. Legacy and Web-Crypto-unavailable modes use deterministic
local text or canonical-Base64 equality only to preserve DOM state, while continuing to show
their bounded warning. Run-open and SSE request generations prevent late network or hash
responses from replacing a newer run snapshot.

## Rendered audit still required

A live rendered comparison remains an integration gate because this branch does not create
fake run data or mutate canonical outcome history merely to stage screenshots. Review a real
populated run at desktop, narrow/mobile, and reduced-motion settings against the mockup,
checking two-second type scanning, tab overflow, long canonical IDs, large histories, focus
retention during watermark-only SSE updates, and contrast in degraded/unknown cards. That
audit should use the actual loopback service and canonical snapshot, not a synthetic browser
fixture.
