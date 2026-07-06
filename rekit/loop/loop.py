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
from ..ledger.artifacts import Artifact, from_path
from ..ledger.project import Project

# NOTE (E4 scoping/gate hook): this loop assembles the tool allowlist directly
# from the skill Registry lookups (kinds present → skills_for_kind, plus any
# capabilities the goal names → skills_by_capability). E4's scoping *policy*
# ((kinds ∩ requested-capabilities) filtered by trust tier) and the human gate
# (ask_human / present_choices for network/destructive escalation) will WRAP the
# `_scoped_tools` call below — narrowing the set and routing gated skills through
# the user. We deliberately do NOT import `rekit.human` or `rekit.skills.scoping`
# here: they are being built in parallel and do not exist yet. Keep the wrap point
# at `_scoped_tools` so E4 has one seam to slot into.

_DONE_RE = re.compile(r"^\s*done\b[.! ]*$", re.IGNORECASE)
_FINDING_RE = re.compile(r"^\s*finding\s*[:\-]\s*(.+)$", re.IGNORECASE)
_LEAD_RE = re.compile(r"^\s*lead\s*[:\-]\s*(.+)$", re.IGNORECASE)
_DERIVED_RE = re.compile(r"^\s*derived\s*[:\-]\s*(.+)$", re.IGNORECASE)
# "<capability> for <kind>" inside a LEAD line.
_LEAD_BODY_RE = re.compile(r"^(?P<cap>[\w./-]+)\s+for\s+(?P<kind>[\w./*-]+)", re.IGNORECASE)
# "<transform> -> <path>" inside a DERIVED line.
_DERIVED_BODY_RE = re.compile(r"^(?P<transform>[\w./-]+)\s*(?:->|=>)\s*(?P<path>.+)$")


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
        }


def build_context(project: Project, goal: str) -> str:
    """Render the ledger snapshot into a compact context string for the brain.

    Surfaces the kinds present, which trees are still pending analysis, and the
    findings recorded so far — the state a brain needs to decide the next move
    without re-scanning the target.
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

    return "\n".join(lines)


def _pending_trees(project: Project) -> list[Artifact]:
    """Tree artifacts recorded but not yet marked analyzed."""
    out: list[Artifact] = []
    for h in project.ledger.trees:
        entry = project.ledger.entries.get(h)
        if entry is not None and not entry.analyzed:
            out.append(entry.artifact)
    return out


def _scoped_tools(project: Project, registry: Any, goal: str) -> list[str]:
    """Assemble the per-turn tool allowlist from the skill registry.

    This is the passive half of scoping in primitive form: for every artifact
    kind present in the ledger, gather the skills relevant to it
    (``skills_for_kind``); union in skills providing a capability the goal text
    names (``skills_by_capability``). E4 will wrap this with the (kinds ∩
    requested-capabilities) policy + trust-tier filtering + the human gate.

    Returns skill names, sorted and de-duplicated. With no registry, returns [].
    """
    if registry is None:
        return []

    names: set[str] = set()
    kinds = project.ledger.snapshot().get("kinds", {})
    for kind in kinds:
        for skill in registry.skills_for_kind(kind):
            names.add(skill.name)

    # A light capability pull from the goal wording (e.g. "unpack", "decompile").
    goal_tokens = set(re.findall(r"[a-z][a-z-]+", goal.lower()))
    for skill in getattr(registry, "skills", []):
        cap = getattr(skill, "capability", None)
        if cap and cap.lower() in goal_tokens:
            names.add(skill.name)

    return sorted(names)


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
        tool allowlist. None → no tools handed to the brain.
    system_prompt:
        Override the system prompt (defaults to ``goal``).

    Returns a coverage-aware :class:`LoopSummary`.
    """
    # Seed the ledger with the root artifact so there is context from round one.
    _ensure_root(project)

    sys_prompt = system_prompt or goal
    summary = LoopSummary(project_id=project.id, goal=goal)

    for i in range(max_rounds):
        context = build_context(project, goal)
        tools = _scoped_tools(project, registry, goal)
        step_tier = _pick_tier(project, i, tier)

        result = adapter.invoke(
            sys_prompt,
            _round_ask(i, goal),
            tools=tools,
            context=context,
            tier=step_tier,
        )

        round_result = _fold_result(project, result, index=i, tier=step_tier, tools=tools)
        summary.rounds.append(round_result)

        if round_result.done:
            summary.done = True
            summary.reason = "brain signaled DONE"
            break
    else:
        summary.reason = f"reached max_rounds ({max_rounds})"

    return summary


def _round_ask(index: int, goal: str) -> str:
    """The per-round user input handed to the brain."""
    if index == 0:
        return (
            f"Goal: {goal}\n\n"
            "Work the goal one step. Report structured outcomes as lines: "
            "`FINDING: ...`, `LEAD: <capability> for <kind>`, "
            "`DERIVED: <transform> -> <path>`. Emit `DONE` when the goal is complete."
        )
    return (
        "Continue working the goal from the ledger context above. "
        "Report new FINDING/LEAD/DERIVED lines, and emit DONE when complete."
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
) -> RoundResult:
    """Parse the brain's tagged-line protocol and fold outcomes into the ledger."""
    rr = RoundResult(index=index, tier=tier, tools=list(tools), text=result.text)
    root = _root_or_none(project)

    for raw_line in (result.text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if _DONE_RE.match(line):
            rr.done = True
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
