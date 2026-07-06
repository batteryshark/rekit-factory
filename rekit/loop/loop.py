"""The ralph loop — drive a goal to termination over the ledger (E2.1).

The loop is the driver, not the brain. Each round it:

1. builds a **context** string from the ledger snapshot (kinds present, pending
   trees, findings so far);
2. assembles the **scoped tool allowlist** from the skill registry (kinds present
   ∩ what the goal needs) — the brain never sees the whole rack;
3. picks a model **tier** for the step (cheap floor vs. beefy judgment) and
   :meth:`~rekit.harness.base.HarnessAdapter.invoke`\\ s the adapter;
4. **folds the result back** into the ledger — findings, leads, and derivations
   the brain reported become durable events;
5. checks **termination**: a bounded round count plus a done signal.

The brain communicates structured outcomes back to the loop through a tiny
tagged-line protocol in its answer text (case-insensitive, one item per line)::

    FINDING: <text>                     # attach a finding to the root artifact
    LEAD: <capability> for <kind>        # a wanted-but-unavailable capability
    DERIVED: <transform> -> <path>       # a transform produced an output file
    DONE                                 # the goal is complete; stop the loop

This keeps the fold deterministic and harness-neutral: any brain that emits these
lines drives the ledger identically, and :class:`~rekit.harness.mock.MockAdapter`
can script them for hermetic tests. Richer structured actions (full tool-result
plumbing, JSON action envelopes) are a FOLLOW-UP.

Termination is guaranteed: the loop always stops at ``max_rounds`` even if no
``DONE`` ever arrives, so it runs to termination deterministically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..harness.base import HarnessAdapter, HarnessResult
from ..harness.tiers import BEEFY, CHEAP
from ..human.channel import CLIHumanChannel, HumanChannel
from ..ledger.artifacts import Artifact, from_path
from ..ledger.project import Project
from ..skills.runner import RunResult, run_skill
from ..skills.scoping import Policy, ScopedSkill, scope_scoped_skills

# E4/E5 scoping + gate + run integration.
#
# The `_scoped_tools` seam below is now the real exposure authority: it builds the
# scoped skill set via `scope_scoped_skills` ((kinds present in the ledger) ∩
# (capabilities the goal requested), filtered by trust tier), and surfaces only the
# *available* ones to the brain. When the brain emits `RUN_SKILL: <name> [on <path>]`,
# the loop calls `run_skill` — which walks the host-gate, the human tier-gate
# (`gate_skill`), and the sandboxed execution, then folds the resulting derivation
# (and any revealed artifacts) back into the ledger. The run's outcome is fed into
# the next round's context so the brain can react.

_DONE_RE = re.compile(r"^\s*done\b[.! ]*$", re.IGNORECASE)
_FINDING_RE = re.compile(r"^\s*finding\s*[:\-]\s*(.+)$", re.IGNORECASE)
_LEAD_RE = re.compile(r"^\s*lead\s*[:\-]\s*(.+)$", re.IGNORECASE)
_DERIVED_RE = re.compile(r"^\s*derived\s*[:\-]\s*(.+)$", re.IGNORECASE)
# "<capability> for <kind>" inside a LEAD line.
_LEAD_BODY_RE = re.compile(r"^(?P<cap>[\w./-]+)\s+for\s+(?P<kind>[\w./*-]+)", re.IGNORECASE)
# "<transform> -> <path>" inside a DERIVED line.
_DERIVED_BODY_RE = re.compile(r"^(?P<transform>[\w./-]+)\s*(?:->|=>)\s*(?P<path>.+)$")
# "RUN_SKILL: <skill-name> [on <path>]" — the brain asks the loop to execute a skill.
_RUN_SKILL_RE = re.compile(r"^\s*run[_ ]?skill\s*[:\-]\s*(.+)$", re.IGNORECASE)
# "<skill-name> [on <path>]" inside a RUN_SKILL line.
_RUN_SKILL_BODY_RE = re.compile(
    r"^(?P<name>[\w./-]+)\s*(?:\s+on\s+(?P<path>.+))?$", re.IGNORECASE
)


@dataclass
class RoundResult:
    """What one round of the loop did (for the summary / observability)."""

    index: int
    tier: str
    tools: list[str]
    text: str
    findings: int = 0
    leads: int = 0
    derivations: int = 0
    done: bool = False
    #: Skill runs the brain requested this round (RUN_SKILL), in order.
    skill_runs: list[RunResult] = field(default_factory=list)

    @property
    def skills_run(self) -> int:
        return len(self.skill_runs)


@dataclass
class LoopSummary:
    """Coverage-aware outcome of a whole loop run."""

    project_id: str
    goal: str
    rounds: list[RoundResult] = field(default_factory=list)
    done: bool = False
    reason: str = ""

    @property
    def round_count(self) -> int:
        return len(self.rounds)

    @property
    def total_findings(self) -> int:
        return sum(r.findings for r in self.rounds)

    @property
    def total_leads(self) -> int:
        return sum(r.leads for r in self.rounds)

    @property
    def total_derivations(self) -> int:
        return sum(r.derivations for r in self.rounds)

    @property
    def total_skill_runs(self) -> int:
        return sum(r.skills_run for r in self.rounds)

    @property
    def skill_runs(self) -> list[RunResult]:
        """Every skill run across all rounds, in order."""
        return [run for r in self.rounds for run in r.skill_runs]

    def as_dict(self) -> dict[str, Any]:
        return {
            "projectId": self.project_id,
            "goal": self.goal,
            "done": self.done,
            "reason": self.reason,
            "rounds": self.round_count,
            "findings": self.total_findings,
            "leads": self.total_leads,
            "derivations": self.total_derivations,
            "skillRuns": self.total_skill_runs,
        }


def build_context(
    project: Project,
    goal: str,
    *,
    scoped: list[ScopedSkill] | None = None,
    run_feedback: str = "",
) -> str:
    """Render the ledger snapshot into a compact context string for the brain.

    Surfaces the kinds present, which trees are still pending analysis, the
    findings recorded so far, the **available scoped skills** the brain may invoke,
    and any **skill-run feedback** from the previous round — the state a brain
    needs to decide the next move without re-scanning the target.
    """
    snap = project.ledger.snapshot()
    kinds = snap.get("kinds", {})
    lines: list[str] = [f"# Project ledger: {project.id}", f"Target: {project.target}"]

    if kinds:
        kinds_str = ", ".join(f"{k}×{n}" for k, n in sorted(kinds.items()))
        lines.append(f"Artifact kinds present: {kinds_str}")
    else:
        lines.append("Artifact kinds present: (none yet)")

    pending = _pending_trees(project)
    if pending:
        lines.append(
            "Trees pending analysis: "
            + ", ".join(a.path for a in pending)
        )
    else:
        lines.append("Trees pending analysis: (none)")

    findings = project.ledger.findings()
    if findings:
        lines.append(f"Findings so far ({len(findings)}):")
        for f in findings[-10:]:  # cap the tail we replay into context
            note = f.get("note") or f.get("text") or f.get("summary") or ""
            lines.append(f"  - [{f.get('artifact', '?')}] {note}")
    else:
        lines.append("Findings so far: (none)")

    if scoped:
        lines.append("Available skills:")
        lines.extend(_render_skill_lines(scoped))
        lines.append(
            "To run one, emit a line: `RUN_SKILL: <skill-name> [on <path>]` "
            "(defaults to the target)."
        )

    if run_feedback:
        lines.append(run_feedback)

    return "\n".join(lines)


def _pending_trees(project: Project) -> list[Artifact]:
    """Tree artifacts recorded but not yet marked analyzed."""
    out: list[Artifact] = []
    for h in project.ledger.trees:
        entry = project.ledger.entries.get(h)
        if entry is not None and not entry.analyzed:
            out.append(entry.artifact)
    return out


def _scoped_tools(
    project: Project,
    registry: Any,
    goal: str,
    policy: Policy,
    *,
    requested_capabilities: list[str] | None = None,
) -> list[ScopedSkill]:
    """Assemble the per-turn scoped skill set — the loop's exposure authority.

    This is the E4 scoping *policy* slotted into the E2 seam: the intersection of
    ``(skills whose accepts match a kind present in the ledger)`` and ``(skills
    whose capability the goal requested)``, filtered by the goalpack ``policy``
    over trust tiers, with each survivor carrying its ``requires_gate`` flag.

    ``requested_capabilities`` names the capabilities the goal wants; when None it
    is inferred from the goal wording (:func:`_capabilities_from_goal`). Only
    *available* skills (host tool resolves) are exposed — a scoped-but-unavailable
    skill becomes an "install X" lead when actually invoked, not a surfaced tool.

    Returns :class:`ScopedSkill` objects, sorted by name. With no registry, ``[]``.
    """
    if registry is None:
        return []

    present_kinds = list(project.ledger.kinds.keys())
    if requested_capabilities is None:
        requested_capabilities = _capabilities_from_goal(registry, goal)

    return scope_scoped_skills(
        registry,
        present_kinds=present_kinds,
        requested_capabilities=requested_capabilities,
        policy=policy,
        available_only=True,
    )


def _capabilities_from_goal(registry: Any, goal: str) -> list[str]:
    """Infer the requested capabilities from the goal wording.

    A capability counts as requested when the goal text mentions either the
    capability name itself (``unpack``, ``decompile``) or any of a skill's search
    keywords that map to it. Keeps scoping honest without a goalpack manifest yet
    (that is E6); the goal string is the de-facto request surface today.
    """
    goal_tokens = set(re.findall(r"[a-z][a-z-]+", goal.lower()))
    requested: set[str] = set()
    for skill in getattr(registry, "skills", []):
        cap = getattr(skill, "capability", None)
        if not cap:
            continue
        if cap.lower() in goal_tokens:
            requested.add(cap)
            continue
        # A keyword the goal names also pulls the capability in.
        for kw in getattr(skill, "keywords", ()):  # type: ignore[union-attr]
            if kw.lower() in goal_tokens:
                requested.add(cap)
                break
    return sorted(requested)


def _render_skill_lines(scoped: list[ScopedSkill]) -> list[str]:
    """Render scoped skills for the brain's context: one legible line each.

    ``<name> — <capability> — accepts <kinds>`` plus a gate note so the brain knows
    a run may pause for a human. Only ungated-or-will-gate (i.e. in-scope) skills
    reach here; forbidden tiers were already dropped by scoping.
    """
    lines: list[str] = []
    for sc in scoped:
        skill = sc.skill
        cap = skill.capability or "(uncategorized)"
        kinds = ", ".join(skill.accepts) if skill.accepts else "any"
        note = " [gated: asks before running]" if sc.requires_gate else ""
        lines.append(f"  - {skill.name} — {cap} — accepts {kinds}{note}")
    return lines


def _pick_tier(project: Project, round_index: int, default_tier: str) -> str:
    """Choose the model tier for a round (a loop decision, not per-skill).

    Heuristic first slice: the cheap floor drives the high-volume triage rounds
    while the brain is still discovering the target (no findings recorded yet).
    Once findings have accumulated there is real state to *synthesize/adjudicate*
    over, which is the beefy judgment tier's job — so subsequent rounds escalate.
    This keeps tier a loop decision (state-driven), not a per-skill constant. A
    caller-forced ``default_tier`` other than cheap is respected as the floor.
    """
    if default_tier and default_tier != CHEAP:
        return default_tier
    # Findings exist → this round is synthesis/judgment over accumulated state.
    if project.ledger.findings():
        return BEEFY
    return CHEAP


def run(
    project: Project,
    goal: str,
    adapter: HarnessAdapter,
    *,
    max_rounds: int = 8,
    tier: str = CHEAP,
    registry: Any = None,
    system_prompt: str | None = None,
    channel: HumanChannel | None = None,
    policy: Policy | None = None,
    requested_capabilities: list[str] | None = None,
) -> LoopSummary:
    """Run the ralph loop for ``goal`` against ``project`` via ``adapter``.

    Parameters
    ----------
    project:
        The persistent :class:`~rekit.ledger.project.Project` (the shared state).
    goal:
        The goal text (becomes the adapter's system prompt unless overridden).
    adapter:
        Any :class:`~rekit.harness.base.HarnessAdapter` (pi, mock, …).
    max_rounds:
        Hard bound on rounds — the loop *always* terminates here (guarantees
        determinism even if the brain never signals DONE).
    tier:
        The default/floor tier; the loop may escalate per round (see :func:`_pick_tier`).
    registry:
        Optional skill :class:`~rekit.skills.registry.Registry` for scoping the
        skill set. None → no skills scoped or surfaced to the brain.
    system_prompt:
        Override the system prompt (defaults to ``goal``).
    channel:
        The :class:`~rekit.human.channel.HumanChannel` a gated-tier skill run routes
        through (defaults to :class:`~rekit.human.channel.CLIHumanChannel`).
    policy:
        The goalpack :class:`~rekit.skills.scoping.Policy` over trust tiers
        (defaults to :meth:`Policy.default`): read-only auto-runs, network /
        executes-untrusted / destructive are gated.
    requested_capabilities:
        Capabilities the goalpack wants; None → inferred from the goal wording.

    Returns a coverage-aware :class:`LoopSummary`.
    """
    # Seed the ledger with the root artifact so there is context from round one.
    _ensure_root(project)

    sys_prompt = system_prompt or goal
    channel = channel if channel is not None else CLIHumanChannel()
    policy = policy if policy is not None else Policy.default()
    summary = LoopSummary(project_id=project.id, goal=goal)

    # A short digest of the previous round's skill runs, fed forward so the brain
    # can react to what a RUN_SKILL actually produced.
    run_feedback: str = ""

    for i in range(max_rounds):
        # Scope the skill set from the *current* ledger kinds ∩ requested caps,
        # filtered by policy — recomputed each round because a prior run may have
        # revealed new kinds (a fresh tree re-entering the ledger).
        scoped = _scoped_tools(
            project, registry, goal, policy,
            requested_capabilities=requested_capabilities,
        )
        tools = [sc.name for sc in scoped]
        context = build_context(project, goal, scoped=scoped, run_feedback=run_feedback)
        step_tier = _pick_tier(project, i, tier)

        result = adapter.invoke(
            sys_prompt,
            _round_ask(i, goal),
            tools=tools,
            context=context,
            tier=step_tier,
        )

        round_result = _fold_result(
            project, result, index=i, tier=step_tier, tools=tools,
            scoped=scoped, channel=channel, policy=policy,
        )
        summary.rounds.append(round_result)
        run_feedback = _run_feedback(round_result)

        if round_result.done:
            summary.done = True
            summary.reason = "brain signaled DONE"
            break
    else:
        summary.reason = f"reached max_rounds ({max_rounds})"

    return summary


def _run_feedback(round_result: RoundResult) -> str:
    """A compact digest of this round's skill runs to feed into the next round."""
    if not round_result.skill_runs:
        return ""
    lines = ["Skill run results from the last round:"]
    lines.extend(f"  - {r.summary()}" for r in round_result.skill_runs)
    return "\n".join(lines)


def _round_ask(index: int, goal: str) -> str:
    """The per-round user input handed to the brain."""
    if index == 0:
        return (
            f"Goal: {goal}\n\n"
            "Work the goal one step. Report structured outcomes as lines: "
            "`FINDING: ...`, `LEAD: <capability> for <kind>`, "
            "`DERIVED: <transform> -> <path>`. To execute one of the available "
            "skills, emit `RUN_SKILL: <skill-name> [on <path>]` — the loop runs it "
            "(gating + sandboxing) and reports the result next round. "
            "Emit `DONE` when the goal is complete."
        )
    return (
        "Continue working the goal from the ledger context above. "
        "Report new FINDING/LEAD/DERIVED lines, emit `RUN_SKILL: <name> [on <path>]` "
        "to run a skill, and emit DONE when complete."
    )


def _ensure_root(project: Project) -> None:
    """Add the target itself as the root artifact if the ledger is empty of it."""
    try:
        root = project.root_artifact()
    except (OSError, ValueError):
        return
    is_tree = root.kind == "tree"
    project.add_artifact(root, is_tree=is_tree)


def _fold_result(
    project: Project,
    result: HarnessResult,
    *,
    index: int,
    tier: str,
    tools: list[str],
    scoped: list[ScopedSkill] | None = None,
    channel: HumanChannel | None = None,
    policy: Policy | None = None,
) -> RoundResult:
    """Parse the brain's tagged-line protocol and fold outcomes into the ledger.

    Handles FINDING / LEAD / DERIVED / DONE as before, plus the new
    ``RUN_SKILL: <name> [on <path>]`` action: it looks the skill up in the scoped
    set, calls :func:`~rekit.skills.runner.run_skill` (which host-gates, tier-gates
    through ``channel``, sandboxes, and records the derivation), and attaches the
    :class:`RunResult` to the round so the next round's context reports it.
    """
    rr = RoundResult(index=index, tier=tier, tools=list(tools), text=result.text)
    root = _root_or_none(project)
    by_name = {sc.name: sc.skill for sc in (scoped or [])}

    for raw_line in (result.text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if _DONE_RE.match(line):
            rr.done = True
            continue

        m = _RUN_SKILL_RE.match(line)
        if m:
            run_result = _dispatch_run_skill(
                project, m.group(1).strip(), by_name, channel, policy
            )
            if run_result is not None:
                rr.skill_runs.append(run_result)
                if run_result.recorded_derivation:
                    rr.derivations += 1
            continue

        m = _FINDING_RE.match(line)
        if m and root is not None:
            project.record_finding(root, {"note": m.group(1).strip(), "round": index})
            rr.findings += 1
            continue

        m = _LEAD_RE.match(line)
        if m:
            body = _LEAD_BODY_RE.match(m.group(1).strip())
            if body:
                project.record_lead(body.group("cap"), body.group("kind"))
            else:
                # Fall back: whole body as capability, unknown kind.
                project.record_lead(m.group(1).strip(), "unknown")
            rr.leads += 1
            continue

        m = _DERIVED_RE.match(line)
        if m and root is not None:
            body = _DERIVED_BODY_RE.match(m.group(1).strip())
            if body:
                out = _artifact_for(body.group("path").strip())
                project.record_derivation(body.group("transform"), root, [out])
                rr.derivations += 1
            continue

    # A round that produced findings and signaled done marks the root analyzed.
    if rr.done and root is not None:
        project.mark_analyzed(root)

    return rr


def _dispatch_run_skill(
    project: Project,
    body: str,
    by_name: dict[str, Any],
    channel: HumanChannel | None,
    policy: Policy | None,
) -> RunResult | None:
    """Execute a ``RUN_SKILL`` action: resolve the skill in scope, then run it.

    Returns a :class:`RunResult` — including for the not-in-scope case (a
    synthetic ``skipped`` result) so the brain always gets legible feedback rather
    than silence. Returns None only if the line names nothing parseable.
    """
    m = _RUN_SKILL_BODY_RE.match(body)
    if not m:
        return None
    name = m.group("name")
    path = (m.group("path") or "").strip() or None

    skill = by_name.get(name)
    if skill is None:
        # The brain named a skill that is not in the current scope. Do NOT run it
        # (defence in depth: scoping is the exposure authority); report why.
        return RunResult(
            skill=name,
            status="skipped",
            detail="not in the scoped skill set (out of scope or unavailable)",
        )

    target = path if path is not None else project.target
    return run_skill(
        skill,
        target,
        project,
        channel=channel if channel is not None else CLIHumanChannel(),
        policy=policy if policy is not None else Policy.default(),
    )


def _root_or_none(project: Project) -> Artifact | None:
    for h in project.ledger.entries:
        # The first-added artifact is the root (seeded by _ensure_root).
        return project.ledger.entries[h].artifact
    return None


def _artifact_for(path: str) -> Artifact:
    """Build an Artifact for a DERIVED output path.

    If the path exists on disk, hash + classify it for real; otherwise record a
    lightweight placeholder artifact (path-addressed) so the derivation is still
    captured — the brain may name an output it intends to produce.
    """
    from pathlib import Path

    p = Path(path)
    if p.exists():
        return from_path(p)
    import hashlib

    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
    return Artifact(kind="file", content_hash=digest, path=path, meta={"placeholder": True})
