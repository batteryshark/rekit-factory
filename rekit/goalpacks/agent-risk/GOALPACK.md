---
name: agent-risk
title: Map my agent's capability risk
goal: What can my agent be tricked into, and what does it become once it holds these tools? Inventory this agent surface and map its dangerous capability compositions.
requestedCapabilities: []
renderer: renderer:render_report
---

# agent-risk

A deterministic, agent-surface goalpack. The target is not a single program but an
**agent surface**: a directory of skills, tools, prompts, MCP-server configs,
subagents, and hooks that an agent could be wired with. The question is the
enablement question — *what does an agent become once it holds this set of tools,
and what can it be tricked into?*

The brain inventories the surface and reports two kinds of finding through the
loop's `FINDING:` protocol, each tagged in square brackets:

- **combo** — a dangerous capability **composition**: a source capability and a
  sink capability that, held together, complete an abuse path. The three that
  matter most:
  - **exfil** — reads secrets/credentials AND makes network calls (read then send).
  - **rce** — takes network/agent-directed input AND executes shell or loads code.
  - **drop** — writes a file AND executes it (write then run).
  A composition is dangerous whether one skill supplies both halves, or two
  co-enabled skills each supply one half. Capability is separate from intent.
- **posture** — a standing surface issue: an over-broad capability grant, a
  steerable (agent-directed) input that can hijack a reachable capability, an
  undeclared reach, a missing gate, or a credential that widens the blast radius.

It may also emit **recommend** findings — concrete gate/sandbox/do-not-co-enable
actions — through the same protocol.

The goalpack's own renderer buckets those generic ledger findings by tag into
agent-risk's report shape (`combos`, `posture`, `recommendations`, a review plan,
and a headline). The ledger holds only the substrate; this goalpack owns the shape.
There is no shared report_model.
