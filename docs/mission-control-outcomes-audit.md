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

## Rendered audit completed

The integrated loopback service was reviewed against the canonical mockup at the normal desktop
viewport and at 560×900 using real completed and failed README investigations. Both views showed
an exact-byte **Semantic projection verified** state. The completed run rendered distinct run,
work-item, and worker cards with execution, completion, and disposition visibly separate:
`completed` never substituted for `successful`, and validation, acceptance, and publication
remained independently not-applicable.

The desktop layout retained the e7 density, layered framing, type signals, count band, controls,
and two-column cards. At 560px the detail rail stacked, the tab strip used intentional horizontal
overflow, controls collapsed, and outcome content remained legible without flattening the visual
hierarchy. Search narrowed three cards to one, kept focus on `outcomeSearch`, and updated the
bounded live announcement to “Showing 1 of 3 canonical outcomes”; reset restored all cards. The
browser console had no warnings or errors. The environment did not expose reduced-motion media
emulation, so that policy remains verified by the packaged global rule and deterministic source
assertion rather than an emulated screenshot.
