# E7 — the lab UI ("Mission Control")

> Design of record for rekit's operator surface. Interactive mockups (each frozen at its own URL; repo copies in `docs/mockups/`):
> - **v1** (the loved baseline): https://claude.ai/code/artifact/7fc25037-37a1-47a3-84ff-bbf08bdfa0af
> - **v2** (capability-group tools, target Browse, per-project cost, theme toggle): https://claude.ai/code/artifact/b6390126-791d-4374-bd87-f2eac4de8dcb
> - **v3** (current — forage-under-a-gate exposure, missing-tool suspend/resume, agent skill foraging): https://claude.ai/code/artifact/9ebcc7ab-362c-4db1-9ae0-bcfb88eb9986
>
> Status: design. Supersedes the four-line E7 stub in the migration epic; the
> acceptance criteria there (E7.1–E7.4) are folded into the phasing below.

## Thesis

rekit runs many agent-driven reverse-engineering jobs at once, each grinding a
target down against a persistent ledger with scoped, gated skills. Today the only
window into a run is the CLI it was launched from — one terminal, one goal, and a
`[y/N]` gate that blocks the whole process until someone is looking at that exact
prompt.

Mission Control is the cockpit for **N runs at a time**: what each one is doing
right now, what it has found, and — the part no CLI does well — a single place
where every run's decisions queue up and wait for you, so you can run six targets
and only get pulled in when one genuinely needs a human call.

The one-line test: *glance at the screen and know, in under two seconds, which
project needs you and which are fine to leave alone.*

## Decisions locked (2026-07-06)

| # | Fork | Decision |
|---|------|----------|
| 1 | Surface | **Local web app.** `rekit serve` runs a local server; the operator opens a browser. Richest for always-on, many-project density. A TUI is explicitly out of scope for E7 (revisit later as a read-only subset). |
| 2 | Control | **Full mission control.** Launch / stop / pause / resume / retry runs *and* answer decisions, all from the UI. Requires a supervisor process, not just a viewer. |
| 3 | Decisions when away | **Wait + notify.** A run that hits a decision blocks safely and fires a desktop notification; the decision lands in a cross-project Inbox. No auto-timeout by default (a per-run fail-closed timer is opt-in, see E7.3). |
| 4 | Aesthetic | **Logo-true cyberpunk-terminal.** Deep navy ground, phosphor-green accent, monospace as the data vernacular, circuit/disassembly motifs. See "Visual system." |
| 5 | Skill exposure | **Forage under a gate.** All installed skills are the pool; the loop auto-scopes per round *by kind*; the operator sets a trust **ceiling** (subtractive — "what they DON'T get"), not a tool list. The comfort default for untrusted targets. |
| 6 | Missing tool | **Suspend + ask.** A needed-but-uninstalled tool **suspends** the run and posts a decision: install (automated where possible) & resume · manual instructions · or skip & accept the gap. |
| 7 | Discovery at scale | **Lazy retrieval.** The agent never sees the full rack — it gets the kind-scoped set plus an on-demand `FIND_SKILL "<intent>"` search over an indexed registry. No exhaustive discovery, no over-indexing on tools the target never needed. |

## The core idea: `REKIT_HOME` is the database

rekit already made the load-bearing choice — **state is a fold over an append-only
log**. A project's `ledger.jsonl` is the truth; reopening replays it. E7 does not
add a second source of truth. It extends the same pattern so the UI can be a
**pure read-model over `$REKIT_HOME/projects/*`** plus a thin **supervisor** for
control. Nothing in the UI reaches into rekit internals; it watches files and
appends files.

The ledger captures *what was discovered*, but not two things the cockpit needs:
whether a run is **live** (running vs idle vs blocked) and any **pending
decision**. Those become two sibling logs next to `ledger.jsonl`, each
event-sourced in the exact shape of `ledger/events.py`:

```
$REKIT_HOME/projects/<id>/
  project.json     # meta (exists today)
  ledger.jsonl     # discovery log — artifacts/derivations/findings/leads (exists today)
  run.jsonl        # NEW · run lifecycle: status, rounds, tier/harness heartbeats
  inbox.jsonl      # NEW · human channel: questions out, answers in
```

### `run.jsonl` — the liveness log

One append per lifecycle transition, folded into a small `RunState` the same way
`Ledger` folds events. Event types:

- `run_started`   — goal, harness, provider/model per tier, policy, scoped tools, max_rounds.
- `round_started` — index, tier, tools; drives the round pips + "Round 3/8 · beefy".
- `round_ended`   — findings/leads/derivations/skill-runs deltas **+ a `cost`
  delta** (tokens × the tier's rate) for the round; folds into the Spend panel.
- `status_changed`— `running | blocked | suspended | idle | done | failed` (+ reason);
  drives the status pill. `suspended` = waiting on a missing tool (see below).
- `step`          — a freeform current-activity caption ("running jadx on classes.dex").
- `run_ended`     — terminal reason (LoopSummary.reason).

This is a straight projection of `loop.RoundResult` / `LoopSummary` and
`HarnessResult.{tier,provider,model}` — the loop already computes every field; E7
adds a writer that appends them. A run started from the plain CLI writes this log
too, so it shows up on the board **whether or not the UI launched it**.

### `inbox.jsonl` — the human channel, file-backed

A new `HumanChannel` implementation — `LedgerHumanChannel` — makes the three
existing question shapes (`ask` / `present_choices` / `confirm`) UI-routable
without the loop knowing a UI exists:

1. `ask(...)` / `present_choices(...)` / `confirm(...)` append a `question_posted`
   event (id, kind, text, options, and the skill/tier/round context) and **block**
   the loop thread on that question id.
2. The supervisor, when the operator answers in the UI, appends `answer_recorded`
   (question id → answer).
3. The blocked call wakes (a file-watch / condition on the id), validates, and
   returns — `present_choices` still guarantees the return is one of the options,
   `confirm` still parses yes/no, exactly as `channel.py` specifies today.

`gate_skill()` is unchanged: it still calls `channel.confirm(...)`; the confirm
just happens to be answered from a browser instead of stdin. Everything the loop
and sandbox already route through the channel routes through the UI for free.

Fail-closed still holds: if `serve` is down or an optional per-run timeout elapses,
the channel resolves the confirm to **no** — matching the CLI's empty-line default.

### `rekit serve` — the supervisor (the only new privileged piece)

A small local process (stdlib `http.server` + a websocket, or a tiny ASGI app —
kept dependency-light in the rekit spirit) that:

- **Serves the read-model.** Tails every `projects/*/{ledger,run,inbox}.jsonl`,
  folds them, and pushes deltas to the browser over one websocket. Reload =
  replay, same resilience as the ledger.
- **Owns run lifecycle** for control. `POST /runs` calls `run_goal(project, goal,
  adapter, tools=...)` (the ad-hoc first-class entry) or `run_goalpack(...)` in a
  worker; stop/pause/resume/retry act on runs it spawned. Runs it did *not* spawn
  are observable but not stoppable (it can still answer their questions via the
  inbox writeback — that path is just a file append).
- **Writes answers** into `inbox.jsonl` to unblock `LedgerHumanChannel`.
- **Fires notifications** on `question_posted` — `osascript`/`notify-send`
  natively so you're pinged even with the tab backgrounded, plus the browser
  Notification API when the tab is focused.

Clean separation: **observe works for everything; control works for runs the
daemon owns.** No coupling to rekit internals beyond the public `run_goal` /
`run_goalpack` / `discover_skills` / `list_projects` surface.

## Skill exposure & discovery

The hardest question this design answered: with hundreds — soon *thousands* — of
skills, how does the operator decide what agents may use, when you often don't know
what a target needs until an agent has looked at it? (You don't know a binary is
.NET until something inspects it.) The answer separates two things the first mock
wrongly merged:

- **Availability** — what an agent is *allowed* to reach for. The operator's call.
- **Selection** — what is *active this round*. **Not** the operator's call — the
  loop already computes it, per round, as `(kinds present) ∩ (available) ∩ (trust
  allows)`. As the target is understood the ledger gains kinds, so the scoped set
  *grows on its own*: unpack an APK → `dex` appears → jadx scopes itself next round.

So the operator does **not** pre-pick tools. Three moving parts:

**1. Forage under a gate (default).** The whole installed rack is the pool.
`requested_capabilities` defaults to *everything available*, so scoping reduces to
`kind ∩ available ∩ tier`. Discovering the kind is what enables the tool — you
never pre-enable ".NET." Goal-wording inference (`_capabilities_from_goal`) becomes
an optional *narrowing*, not the gate.

**2. The trust ceiling is the real control — and it's subtractive.** You can't
enumerate thousands of skills, but you can set a ceiling on the `Policy` tiers:
read-only auto-runs, network / executes-untrusted **ask**, destructive is
**forbidden**. That is the operator's "what they DON'T get access to" — the safe,
scalable knob for untrusted binaries. Bundles / capability-groups survive only as
optional *pre-warm hints*, never as a wall between an agent and a tool it needs.

**3. Discovery is lazy — the agent searches, it is not shown everything.** Two
bounded channels reach the brain: (a) the **kind-scoped set** (already capped —
only skills accepting a kind actually in the ledger), and (b) an on-demand
**`FIND_SKILL "<intent>"`** action that runs a retrieval over an *indexed*
registry and returns the top few matches. The brain requests a capability it
reasons it needs; the loop either scopes it, gates it, or records an install lead.
This makes `registry.find_skills` graduate from a linear scan into a real **skill
index** (by capability, accepts-kind, description) — a small addition to rekit
*core* (an E3/E4 follow-up) that E7 surfaces but does not own.

### Missing tool → suspend + resume

When an agent reaches for a capability whose host tool isn't installed (ilspy for a
.NET assembly; ghidra; retdec), the run enters a distinct **suspended** state
(`status_changed: suspended`, reason = the wanted tool) and posts a **missing-tool
decision** to the Inbox with three resolutions:

- **Install & resume** — automated where the skill declares an installer or the
  tool is fetchable; the supervisor installs into `$REKIT_HOME/bin` and the loop
  re-scopes it next round.
- **Install manually** — instructions + the drop path; a "re-check & resume" once
  you've placed it.
- **Skip & accept the gap** — record the lead, continue without that capability.

The substrate already exists — an unavailable capability is a `lead_recorded`
today; this makes it a first-class, resolvable decision instead of a passive note.
The v3 mock's Overview tab shows the whole forage loop ("Skills the agent reached
for": auto-scoped · searched · install-lead · **withheld** by the ceiling), so the
"what they DON'T get" is visible, not implicit.

## Screens

The mockup is the source of truth for layout; this section maps each screen to the
data and to the epic's E7.1–E7.4.

### 1 · Mission Control — the fleet grid *(new; the home)*

A responsive grid of **project cards**, sorted needs-you → running → idle →
failed → done, with filter chips per status and a fleet **health ring**
(running/blocked/done donut) in the top bar. Each card carries, from real shapes:

- target basename + kind glyph (`ledger/artifacts` kind), status pill (`run.jsonl`
  status), the current-activity caption (`step`), the four counters
  (findings/leads/derivations/skill-runs from `LoopSummary` totals), a round-pip
  strip colored by tier (`RoundResult.tier`, cheap=green / beefy=teal), an
  event-rate sparkline (ledger event timestamps), harness + model chip, elapsed.
- if blocked: an inline **Answer decision** button that jumps to the Inbox item.

### 2 · Decision Inbox — cross-project triage *(realizes E7.3)*

One stack of pending decisions across all projects, newest/most-urgent first —
the payoff of "wait + notify." Each card renders per question **kind**:

- `confirm` (a gate): **Allow & run / Deny — skip it**, with the skill named, the
  trust tier, and a red **risk chip** (network / executes-untrusted / destructive)
  plus what it will do.
- `present_choices` (a direction fork): the option buttons; the answer is always
  one of them.
- `ask` (free-form): a text box + any suggested options as quick-fills.
- **missing tool** (v3): a suspended run whose agent needs an uninstalled tool —
  resolves with **Install & resume / Install manually / Skip & accept the gap**
  (see "Missing tool → suspend + resume").

Each shows the ledger/round context that prompted it and how long the run has been
paused. Empty state: "All clear — nothing needs you." Answering appends
`answer_recorded`, the run resumes, the badge decrements.

### 3 · Project Detail — the workspace *(realizes E7.1 + E7.2)*

A left summary rail (run meta + a vertical **round timeline**: started → triage →
tier-escalated → consulted-you → synthesizing) and a tabbed main pane:

- **Overview** — the four counters + the rendered report artifact inline (a
  `report/*` ledger artifact, if a renderer produced one).
- **Ledger** (E7.1) — the browsable state: an **artifact tree** (the logo's
  stacked-layers motif: root → derived trees → files → report), a **findings**
  browser grouped by artifact with severity stripes and the parallax atom
  (`EXEC.SHELL`, `NET.FETCH`…), and **install leads** ("wanted `taint-trace` for
  kind tree — no provider") as actionable cards. *"View of the ledger; do better
  than Parallax."*
- **Activity** (E7.2) — the raw **event stream**, live-tailing `ledger.jsonl`,
  colored by type, interleaved with agent messages, tool calls, and skill script
  I/O (from `HarnessResult.raw` / `RunResult`). Filter by type; round headers
  break the flow.
- **Conversation** — the human-channel thread for this one project: orchestrator
  asks in bubbles, you answer inline (choice buttons / yes-no / free text).

Header controls (full control): Pause / Stop / Resume / Retry / View report by
status.

### 4 · New Run — the composer *(the ad-hoc primary interface)*

Mirrors `run_goal(project, goal, adapter, *, tools=...)` exactly:

- **Target** — a path field (always editable — **works from a phone**) plus a
  **Browse…** native file/folder picker on desktop; detected kind shown.
- **Goal** — a free-text goal box (ad-hoc, the primary path) **or** a Goalpack
  toggle listing discovered goalpacks (understand / mcd / agent-risk) with their
  bundled skills and whether they render a report.
- **Exposure & tools (v3).** Leads with the **forage-under-a-gate** posture and the
  **trust ceiling** (read-only auto / network + executes-untrusted ask / destructive
  forbidden) — the operator sets the ceiling, not a tool list (see "Skill exposure &
  discovery"). The capability picker is tucked behind an *Advanced* expander for the
  rare narrow/pre-warm case; because a flat list dies at scale, it is organized **by
  capability**:
  - **Bundles** — one-click presets (Native kit / Android kit / .NET kit /
    Read-only recon / Everything) that set a curated selection.
  - **Capability groups** — collapsible sections (`decompile (5)`, `unpack (4)`…)
    each with a **master toggle** (select-all / none / indeterminate), a tier mix
    badge (`n auto` / `n gated`), and a search box across all skills.
  - **Individual skills** are the drill-down inside a group, each showing trust
    tier and host resolution; an unavailable host surfaces as an install lead.
  - A live **selection summary** ("N scoped · R read-only auto · G gated") and a
    "+ attach a skill directory" affordance (the `tools=[dirs]` / `extra_roots`
    param). The key point: **you pick capabilities/bundles; rekit auto-scopes the
    individual skills per round** (`scope_scoped_skills`), so the UI never asks you
    to babysit 200 checkboxes.
- **Config** — harness picker, per-tier provider/model (cheap = minimax/MiniMax-M3,
  beefy = zai/glm-5.2), max rounds, trust policy. A small **rekit-stack** legend
  (target → ledger → skills → subagents → harness) echoes the logo.

### Cost (v2)

Every project carries a **spend** figure — total USD, tokens in/out, and a
cheap-vs-beefy split — shown as a chip on the fleet card and a full breakdown in
the Overview tab. Sourced from a `cost` delta on each `round_ended` event in
`run.jsonl` (the harness already returns provider/model per invocation; metering
is a token count × the tier's rate). This makes the cheap-floor / beefy-judgment
tiering legible in dollars, per project and per round.

### 5 · Skills & Harnesses — management *(realizes E7.4)*

The skill rack as cards (capability, tier, accepts, host availability — the
"install X" surface when a host tool is missing), and the harness roster with a
picker: *swap the brain without touching goalpacks or skills.* The multi-harness
sidecar shape (opencode-ensemble-like ensembles) lives here as a later slice.

## Data → UI mapping (nothing invented)

| UI element | rekit source |
|---|---|
| Project card identity | `list_projects()` meta + `project.json` |
| Status pill / current step | `run.jsonl` `status_changed` / `step` |
| Round pips + "R 3/8 · beefy" | `RoundResult.index/tier`, `run_started.max_rounds` |
| Counters (find/lead/deriv/skill) | `LoopSummary.total_*` |
| Activity sparkline | `ledger.jsonl` event timestamps |
| Findings browser | `finding_recorded` payloads (+ parallax atom in note) |
| Artifact tree | `artifact_added` / `derivation_recorded` (`isTree`, outputs) |
| Install leads | `lead_recorded` (capability, kind, requires, envHints) |
| Report tab | `report/*` artifact from a goalpack renderer |
| Event stream | raw `ledger.jsonl` + `HarnessResult.raw` / `RunResult` |
| Harness/model chip | `HarnessResult.{provider,model,tier}` |
| Inbox cards | `inbox.jsonl` `question_posted` (kind = ask/present_choices/confirm) |
| Answer → resume | `answer_recorded` → `LedgerHumanChannel` unblocks |
| New Run submit | `run_goal` / `run_goalpack` via `POST /runs` |
| Cost chip / Spend panel | `run.jsonl` `round_ended.cost` (cheap/beefy split) |
| Tool bundles + capability groups | `discover_skills` grouped by `capability`; `scope_scoped_skills` auto-scopes per round |
| Trust ceiling (auto/ask/forbid) | goalpack `Policy` over trust tiers |
| Foraging panel (scoped/searched/lead/withheld) | scope set + `FIND_SKILL` results + `lead_recorded` + `Policy` denials |
| Missing-tool decision + Suspended pill | `inbox.jsonl` `question_posted` kind=tool + `status_changed: suspended` |

## Visual system

- **Ground / panels** — `#080d17` deep navy-black; panels `#0e1626` / `#121d31`; a
  faint circuit-grid texture. Neutrals are blue-biased greys, never pure.
- **Accent** — phosphor green `#46e08a`, the single bold hue (brand + healthy +
  running). Teal `#35d6c3` = beefy tier & code; violet `#a78bfa` = harness brains.
- **Semantic (separate from accent)** — amber `#f5b13d` = needs-you, red `#f25563`
  = failed / destructive risk, blue `#5aa8f2` = done.
- **Type** — monospace *is* the character face (the native vernacular of
  disassembly and terminals): all data, hex, counters, labels, the event stream. A
  system grotesque carries human prose (goals, buttons). No webfont (CSP-safe).
- **Motion** — restrained: a pulse only on running / needs-you indicators and the
  current round pip; everything else still. Respects `prefers-reduced-motion`.

## Phasing

Each phase ships something usable; observe lands before control.

- **E7.0 · the read-model spine.** ✅ *file-backed spine built + tested (30 tests)* —
  `ledger/runlog.py` (`RunLog` writer + `RunState` fold, wired optionally into
  `loop.run`), `human/inbox.py` (`LedgerHumanChannel` + supervisor
  `post_question`/`answer`/`pending_questions`), and `lab/readmodel.py`
  (`fleet` / `project_view` / `health` folding all three logs, with the
  blocked/suspended join), and ✅ `rekit serve` — `lab/server.py`: a stdlib
  `ThreadingHTTPServer` over the read-model (`GET /api/fleet`, `GET /api/project`,
  `POST /api/answer` writeback), a self-contained live browser client that polls
  the API and renders the fleet + a cross-project decision inbox, a `rekit serve`
  CLI subcommand, and a best-effort desktop notifier. (Client **polls** every 1.5s;
  an SSE/websocket push is an optional upgrade.) *Acc met:* a running CLI job
  appears live on the board with correct status/round/counters; answering a
  decision in the browser unblocks the run.
- **E7.1 · Mission Control + Project Detail (Ledger).** The fleet grid and the
  ledger view (artifacts / findings / leads). *Acc:* browse any project's
  findings and artifacts without touching the loop.
- **E7.2 · Activity pane.** Live event stream + agent messages + skill I/O,
  filterable. *Acc:* watch a run's events stream in real time; scrub history.
- **E7.3 · Decision Inbox + `LedgerHumanChannel`.** ✅ *channel + fail-closed timer
  built with E7.0* (`human/inbox.py`: blocks on a posted question, resumes on
  `answer`, `confirm` fail-closes on timeout; missing-tool `request_tool` → suspend).
  ⏳ *remaining:* the cross-project inbox UI wired to `pending_questions`, and
  desktop notifications on `question_posted`. *Acc:* a gated skill run blocks,
  notifies, and resumes on an answer from the browser; fail-closed matches the CLI.
- **E7.4 · Full control + New Run.** Supervisor lifecycle (launch/stop/pause/
  resume/retry) and the composer over `run_goal`/`run_goalpack`. *Acc:* start a
  run from the UI with target + tools + goal; stop and resume it.
- **E7.5 · Harness picker + sidecar.** Multi-harness selection and the ensemble
  sidecar shape. *Acc:* switch the brain without changing goalpack or skills.

## Risks / open questions

- **Concurrency of the writeback.** `answer_recorded` is a single-writer append
  from `serve`; the loop only reads it. Fan-out already proved the "one writer,
  many readers" discipline — keep the inbox on the same rule.
- **Blocked-thread liveness.** `LedgerHumanChannel` blocks a loop thread; long
  waits are fine (that's the point), but `serve` must survive restarts without
  losing the pending question — hence file-backed, not in-memory. A *suspended*
  (missing-tool) run is the same mechanism: the loop blocks on an `inbox.jsonl`
  question until you install/skip.
- **Skill discovery / `FIND_SKILL`.** ✅ *built* — `registry.find_skills` already
  is an intent-ranked search, and the `FIND_SKILL: <intent>` loop action now wires
  it in (`loop._handle_find_skill`): available matches widen scope, uninstalled
  ones become install leads, forbidden ones are reported. A linear scored scan is
  fine to thousands of skills; an inverted index (capability / accepts-kind /
  description postings) is a later optimization only, not a correctness need.
- **Non-daemon runs.** Observable but not controllable. Acceptable; documented in
  the card ("external run — observe only"). Revisit if it bites.
- **Notification reach.** Desktop only for E7. Mobile/remote push is a natural
  extension of the inbox model but explicitly deferred. (Mission Control itself is
  responsive and usable from a phone — hence the always-editable path field.)
- **TUI.** Deferred. The read-model is transport-agnostic, so a later TUI is a
  second consumer of the same websocket, not a rewrite.

## Deferred / future work (logged)

- **Headless-first stays first-class.** The UI is a read-model + supervisor over
  the *same* public entry points; runs work fully from the CLI / harness with no
  `serve` process. Nothing in E7 may make Mission Control a requirement to run a
  goal. *Distribution to build later, each just packaging those entry points:*
  an **npm package** for driving rekit under pi, an **opencode plugin**, and
  adapters for other harnesses (claude-code, codex) as they land.
- **Light theme.** A second palette for less-technical operators. Deferred; the v2
  mock ships a visible toggle affordance (stubbed). Needs a light token set + a
  semantic-color remap (the accent green and status hues must stay legible on a
  light ground).
- **Mobile/remote decision push.** Route `question_posted` to a phone so decisions
  can be answered away from the desk — a second consumer of `inbox.jsonl`.
