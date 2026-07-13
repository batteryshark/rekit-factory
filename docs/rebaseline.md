# 2026-07 rebaseline

Factory is an ad-hoc reverse-engineering lab and dark factory, not a catalog of
predefined jobs.

## Current boundary

- Rekit owns reverse-engineering tools, manifests, availability, and safety facts.
- Muster owns durable runs, work leasing, dependencies, coverage, questions, and resume.
- Factory owns target-and-goal investigations, model workers, permission policy,
  status/log events, model profiles, and Mission Control.

## Retired concepts

- the duplicate `rekit` runtime and copied skill tree inside Factory;
- goalpacks and predefined Factory jobs;
- Factory's JSONL ledger and Ralph loop;
- generic harness tiers as the primary model configuration;
- Ordna planning files;
- a UI read model coupled to the retired JSONL layout.

The useful UI intent survives: fleet supervision, worker activity, permission and
direction questions, missing-tool handling, model selection, artifacts, reports,
live logs, and run control. `docs/mockups/e7-mission-control-v3.html` remains the
visual source and must be connected to the Muster-backed API rather than rewritten
from memory.

## First replacement slice

The replacement control plane now proves the core path:

1. create an ad-hoc target + goal investigation;
2. run Rekit tools as durable work;
3. fan out bounded model workers concurrently;
4. suspend gated tools for a durable operator decision;
5. record events, artifacts, model identity, and token usage;
6. distinguish drained coverage from successful completion;
7. expose snapshots and resumable SSE events through a loopback API.

The active backlog is under `.work/tasks/`. The obsolete Ordna board and local
prototype runtime data are cleanup inputs, not the plan of record.
