# Durable project memory

> Status: design note and implementation plan.
>
> Source: distilled from the former `re-project-tracker` Agent Skill after its
> reusable methodology was captured in the `re-kb` project-tracking playbook.

## Decision

Project tracking is a runtime capability of rekit-factory, not a bundled RE
skill and not a second hand-maintained folder beside the runtime state.

The factory should preserve project intent and reasoning in an append-only
`memory.jsonl` stream under each project directory. Mission Control, harness
context, and Markdown exports should all be projections over that stream joined
with the existing ledger, run log, and human inbox.

```text
$REKIT_HOME/projects/<id>/
  project.json       identity and target metadata
  ledger.jsonl       artifacts, derivations, findings, and capability leads
  run.jsonl          run lifecycle, status, rounds, and cost
  inbox.jsonl        questions that require a human answer
  memory.jsonl       goals, pivots, workstreams, attempts, decisions,
                     research questions, theories, and next actions
```

This keeps one canonical source for each concern. A generated
`project-tracker/` Markdown tree is a portable view or interchange format, not
another database that the runtime must keep synchronized by hand.

## Why this belongs in the factory

Project memory allows a harness to resume intelligently after a context boundary
or across multiple runs. It affects prompt construction, the loop's choice of
next work, project read models, and Mission Control. Those are kernel and
operator-surface responsibilities.

The behavior is tool-independent. It does not analyze a binary, require a private
payload, or provide a target-specific technique, so placing it in a tool/skill
catalog would obscure ownership and make durable state depend on whether an agent
happened to invoke a skill.

## Existing coverage and the gap

| Project-memory concept | Existing source | Remaining requirement |
|---|---|---|
| Artifact and observed result | `ledger.jsonl` | Reuse; do not duplicate payloads in memory events. |
| Run/session activity | `run.jsonl` plus the merged activity view | Reference relevant event ranges from attempts or summaries. |
| Current run goal and status | `run.jsonl` | Add durable project goal history and explicit pivots. |
| Human decision request | `inbox.jsonl` | Keep separate from research questions that do not block on a person. |
| Missing capability | ledger lead | Allow a next action to reference a lead. |
| Attempts and failed approaches | Partial derivation/run evidence | Record intent, outcome, evidence references, and follow-up. |
| Workstreams | None | Add stable identities and lifecycle status. |
| Decisions and rationale | None after an inbox answer is consumed | Preserve choice, alternatives, rationale, and reconsideration trigger. |
| Theories | None | Preserve confidence, evidence for/against, and validation state. |
| Prioritized next actions | Partial leads only | Add an explicit actionable queue with blockers and completion state. |

## Event model

Use the existing event envelope: monotonically increasing `seq`, a stable event
`type`, ISO-8601 `ts`, and a JSON-serializable `payload`. Unknown event types must
remain replay-safe no-ops for older readers.

Suggested initial vocabulary:

| Event | Important payload fields |
|---|---|
| `goal_set` | `text`, `reason`, `scope`, optional `supersedes` |
| `workstream_upserted` | `id`, `title`, `status`, `goal`, `nextAction`, `stopCondition`, references |
| `attempt_recorded` | `id`, `workstreamId`, `intent`, `method`, `status`, `result`, evidence references, `followUp` |
| `decision_recorded` | `id`, `choice`, `rationale`, `alternatives`, `reconsiderWhen`, references |
| `research_question_upserted` | `id`, `question`, `status`, `impact`, `evidenceNeeded`, `answer`, references |
| `theory_upserted` | `id`, `claim`, `status`, `confidence`, evidence references, `validationStep`, `outcome` |
| `next_action_upserted` | `id`, `text`, `priority`, `status`, `blockers`, `workstreamId`, references |
| `session_compacted` | `summary`, `unknowns`, and event/artifact references; no unbounded raw transcript |

`upserted` means that the event updates the folded state for a stable entity ID;
history remains in the stream. Status transitions are therefore append-only and
resolved, disproven, rejected, or completed items remain inspectable.

Evidence should normally be a reference to an artifact hash, ledger sequence,
run sequence, path, or external URI. Avoid copying large command outputs and
transcripts into `memory.jsonl`; material evidence should be an artifact.

## Read model and harness contract

Add a pure fold that produces a `ProjectMemory` snapshot containing:

- current and prior goals;
- active, paused, candidate, rejected, and completed workstreams;
- attempts ordered by time and grouped by workstream;
- active and historical decisions, research questions, and theories;
- prioritized pending actions;
- the latest bounded session compaction.

The harness should receive a bounded `memory_context` projection, not the entire
event stream. Prefer the current goal, active workstreams, supported/testing
theories, open questions, and highest-priority unblocked actions. Include history
only when it explains a rejected approach or prevents repeated work.

The factory, rather than a prompt convention, must own writes. Harness adapters
can return structured memory actions in the same spirit as findings and leads;
the loop validates and appends them through a narrow `ProjectMemoryLog` API.

## Markdown projection

Provide an explicit export such as:

```sh
rekit project export <project-id> --format tracker-markdown --output <dir>
```

The projection may contain `README.md`, `goals.md`, `workstreams.md`,
`attempts.md`, `artifacts.md`, `decisions.md`, `questions.md`, `theories.md`,
`next-actions.md`, and bounded session summaries. It should be deterministic for
the same folded state and safe to commit or hand to another harness.

Importing an existing tracker is useful for migration, but should be explicitly
lossy and provenance-marked: Markdown does not provide stable IDs or guarantee
that every statement can be classified confidently. Unknowns must remain
unknown rather than being inferred.

## Mission Control shape

Project detail should gain a compact **Memory** view:

- objective and most recent pivot;
- active workstreams with next action and stop condition;
- open research questions and theories being tested;
- decisions with rationale;
- prioritized queue and blockers;
- history for failed attempts and retired theories on demand.

The fleet view only needs a few derived signals: current objective, count of
active workstreams, count of unblocked next actions, and count of unresolved
research questions. Human-inbox state remains the sole source of `needsYou`.

## Delivery slices

1. **Protocol and fold:** `memory.jsonl`, typed write API, replay, snapshot, and
   forward-compatibility tests.
2. **Loop integration:** structured harness actions, validation, bounded context
   injection, and evidence references.
3. **Portable projection:** deterministic Markdown export, then a conservative
   legacy-tracker importer if real migration demand exists.
4. **Mission Control:** memory detail panels and small fleet-level signals.

## Non-goals

- Replacing `ledger.jsonl`, `run.jsonl`, or `inbox.jsonl`.
- Storing raw chat transcripts or unlimited command output.
- Making Markdown and JSONL co-equal writable sources of truth.
- Encoding workflow-specific report schemas in the kernel.
- Requiring every short run to populate every project-memory entity.

## Acceptance principles

- Reopening a project reconstructs identical memory state by replay alone.
- A second harness can resume without rereading the original conversation.
- Failed attempts and rejected theories remain visible and prevent repeated work.
- Evidence references resolve to durable ledger events or artifacts when present.
- Exporting the same state twice produces the same Markdown.
- Older readers tolerate newer memory event types.
