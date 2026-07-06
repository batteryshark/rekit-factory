---
name: understand
title: Understand this code
goal: Understand this target — what it does, what it decides, where it is brittle, and what is surprising.
requestedCapabilities: [code-reading]
renderer: renderer:render_report
---

# understand

A deterministic, assessment-shaped goalpack: read the target and answer four
questions about it, no adjudication required.

- **does** — what the code actually does (its capabilities / behaviours).
- **decides** — the decisions it makes (branches, policies, configuration it keys on).
- **brittle** — where it is fragile (unhandled edges, sharp assumptions, hazards).
- **surprising** — anything unexpected given what the target claims to be.

The brain records findings through the loop's `FINDING:` protocol, tagging each
with its lens (`FINDING: [does] ...`). The goalpack's own renderer groups those
generic ledger findings into the four-section understand report — the ledger holds
only the substrate; this goalpack owns the shape.
