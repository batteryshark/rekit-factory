"""Pure, deterministic worker-team planning for Factory investigations.

This module deliberately has no scheduler or persistence dependencies.  It defines the
contract that the durable control plane can later translate into ledger work items.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Iterable, Mapping


def _stable_id(kind: str, *parts: object) -> str:
    encoded = json.dumps([kind, *parts], separators=(",", ":"), sort_keys=True)
    return f"{kind}-{hashlib.sha256(encoded.encode()).hexdigest()[:16]}"


def _clean(value: str, label: str) -> str:
    result = " ".join(value.split())
    if not result:
        raise ValueError(f"{label} must not be empty")
    return result


@dataclass(frozen=True)
class RunCeilings:
    """Hard limits supplied to the runtime alongside a strategy plan."""

    concurrency: int = 4
    retries_per_worker: int = 2
    cost_units: int = 100
    max_workers: int = 8

    def __post_init__(self) -> None:
        for name, value in (
            ("concurrency", self.concurrency),
            ("cost_units", self.cost_units),
            ("max_workers", self.max_workers),
        ):
            if value < 1:
                raise ValueError(f"{name} must be at least 1")
        if self.retries_per_worker < 0:
            raise ValueError("retries_per_worker must not be negative")
        if self.concurrency > self.max_workers:
            raise ValueError("concurrency must not exceed max_workers")


@dataclass(frozen=True)
class WorkerSeed:
    role: str
    objective: str
    cost_units: int = 10
    depends_on_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Strategy:
    name: str
    description: str
    workers: tuple[WorkerSeed, ...]
    ceilings: RunCeilings

    def __post_init__(self) -> None:
        if not self.workers:
            raise ValueError("a strategy must seed at least one worker")
        roles = [worker.role for worker in self.workers]
        if len(roles) != len(set(roles)):
            raise ValueError("strategy worker roles must be unique")
        known = set(roles)
        for worker in self.workers:
            if worker.cost_units < 1:
                raise ValueError("worker cost_units must be at least 1")
            unknown = set(worker.depends_on_roles) - known
            if unknown:
                raise ValueError(f"unknown dependency roles: {sorted(unknown)}")
            if worker.role in worker.depends_on_roles:
                raise ValueError("a worker cannot depend on itself")
        if len(self.workers) > self.ceilings.max_workers:
            raise ValueError("initial workers exceed max_workers")
        if sum(worker.cost_units for worker in self.workers) > self.ceilings.cost_units:
            raise ValueError("initial workers exceed cost_units")


@dataclass(frozen=True)
class PlannedWork:
    id: str
    dedupe_key: str
    role: str
    objective: str
    cost_units: int
    depends_on: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    origin: str = "strategy"


@dataclass(frozen=True)
class InvestigationPlan:
    strategy: str
    goal: str
    ceilings: RunCeilings
    work: tuple[PlannedWork, ...]


@dataclass(frozen=True)
class FollowUpProposal:
    """A worker proposal for more work; it conveys no completion authority."""

    role: str
    objective: str
    evidence_ids: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    cost_units: int = 10


DEFAULT_STRATEGIES: Mapping[str, Strategy] = MappingProxyType({
    "recon-analysis": Strategy(
        name="recon-analysis",
        description="Independent surface reconnaissance and focused analysis.",
        workers=(
            WorkerSeed("recon", "Map observable structure and collect concrete evidence."),
            WorkerSeed("analyst", "Analyze likely behavior and test competing hypotheses."),
        ),
        ceilings=RunCeilings(),
    ),
    "recon-then-analysis": Strategy(
        name="recon-then-analysis",
        description="Reconnaissance followed by evidence-dependent analysis.",
        workers=(
            WorkerSeed("recon", "Map observable structure and collect concrete evidence."),
            WorkerSeed(
                "analyst",
                "Analyze reconnaissance evidence and test competing hypotheses.",
                depends_on_roles=("recon",),
            ),
        ),
        ceilings=RunCeilings(concurrency=2),
    ),
})


def plan_investigation(
    goal: str,
    strategy: str | Strategy = "recon-analysis",
    *,
    ceilings: RunCeilings | None = None,
) -> InvestigationPlan:
    """Expand a strategy into stable work nodes without touching durable state."""

    clean_goal = _clean(goal, "goal")
    selected = DEFAULT_STRATEGIES[strategy] if isinstance(strategy, str) else strategy
    limits = ceilings or selected.ceilings
    if len(selected.workers) > limits.max_workers:
        raise ValueError("initial workers exceed max_workers")
    if sum(worker.cost_units for worker in selected.workers) > limits.cost_units:
        raise ValueError("initial workers exceed cost_units")

    role_ids = {
        worker.role: _stable_id("work", selected.name, clean_goal, worker.role, worker.objective)
        for worker in selected.workers
    }
    work = tuple(
        PlannedWork(
            id=role_ids[worker.role],
            dedupe_key=_stable_id(
                "dedupe", selected.name, clean_goal, worker.role, worker.objective
            ),
            role=worker.role,
            objective=worker.objective,
            cost_units=worker.cost_units,
            depends_on=tuple(role_ids[role] for role in worker.depends_on_roles),
        )
        for worker in selected.workers
    )
    return InvestigationPlan(selected.name, clean_goal, limits, work)


def propose_follow_up(
    plan: InvestigationPlan,
    proposal: FollowUpProposal,
    *,
    existing_work: Iterable[PlannedWork] = (),
    existing_dedupe_keys: Iterable[str] = (),
) -> PlannedWork | None:
    """Validate and deterministically deduplicate one evidence-driven follow-up.

    Returning ``None`` means equivalent work is already planned.  Runtime integration
    remains responsible for atomically enforcing the returned ``dedupe_key``.
    """

    role = _clean(proposal.role, "role")
    objective = _clean(proposal.objective, "objective")
    evidence = tuple(sorted(set(proposal.evidence_ids)))
    if not evidence:
        raise ValueError("follow-up work requires at least one evidence id")
    if proposal.cost_units < 1:
        raise ValueError("cost_units must be at least 1")

    adaptive_work = tuple(existing_work)
    all_work = plan.work + adaptive_work
    known_ids = {item.id for item in all_work}
    unknown = set(proposal.depends_on) - known_ids
    if unknown:
        raise ValueError(f"unknown work dependencies: {sorted(unknown)}")
    dedupe_key = _stable_id("dedupe", plan.strategy, plan.goal, role, objective, evidence)
    if dedupe_key in set(existing_dedupe_keys) or any(
        item.dedupe_key == dedupe_key for item in all_work
    ):
        return None

    spent = sum(item.cost_units for item in all_work)
    if len(all_work) + 1 > plan.ceilings.max_workers:
        raise ValueError("follow-up exceeds max_workers")
    if spent + proposal.cost_units > plan.ceilings.cost_units:
        raise ValueError("follow-up exceeds cost_units")

    work_id = _stable_id("work", dedupe_key)
    return PlannedWork(
        id=work_id,
        dedupe_key=dedupe_key,
        role=role,
        objective=objective,
        cost_units=proposal.cost_units,
        depends_on=tuple(sorted(set(proposal.depends_on))),
        evidence_ids=evidence,
        origin="worker-proposal",
    )
