"""Pure deterministic progress and budget policy for bounded campaigns.

The policy consumes only canonical W-0051 contracts and explicit durable facts. It
does not read clocks, call models, perform I/O, or execute its recommendation.
W-0054 owns execution and W-0052 owns persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
from typing import Literal

from .campaign_contracts import (
    MAX_INTEGER,
    CampaignChangeRequest,
    CampaignCheckpoint,
    CampaignContract,
    EpochContract,
    EpochResult,
    ProgressSignal,
    ResourceBudget,
    ResourceUsage,
    requires_operator_decision,
)


CampaignPhase = Literal["recon", "hypothesis", "validation"]
AttemptOutcome = Literal[
    "productive", "no-novelty", "validation-rejected", "dependency-blocked",
    "environment-failed", "notification-only",
]
PolicyAction = Literal[
    "continue", "reprioritize", "backoff", "suspend", "ask-operator",
    "exhausted", "policy-stop", "success",
]

_USAGE_FIELDS = ResourceUsage._ATTRIBUTES
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a bounded stable identifier")
    return value


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _checked_sum(left: int, right: int, label: str) -> int:
    value = left + right
    if value > MAX_INTEGER:
        raise ValueError(f"{label} overflows the bounded integer range")
    return value


def _usage_add(left: ResourceUsage, right: ResourceUsage) -> ResourceUsage:
    return ResourceUsage(*(
        _checked_sum(getattr(left, field), getattr(right, field), field)
        for field in _USAGE_FIELDS
    ))


def _usage_subtract(current: ResourceUsage, previous: ResourceUsage) -> ResourceUsage:
    values: list[int] = []
    for field in _USAGE_FIELDS:
        value = getattr(current, field) - getattr(previous, field)
        if value < 0:
            raise ValueError(f"cumulative usage regressed for {field}")
        values.append(value)
    return ResourceUsage(*values)


def _usage_fits(usage: ResourceUsage, budget: ResourceBudget) -> bool:
    return all(
        getattr(usage, field) <= getattr(budget, field).value
        for field in _USAGE_FIELDS
    )


def _usage_reaches(usage: ResourceUsage, budget: ResourceBudget,
                   enforcement: str) -> tuple[str, ...]:
    return tuple(
        field for field in _USAGE_FIELDS
        if getattr(budget, field).enforcement == enforcement
        and getattr(budget, field).value > 0
        and getattr(usage, field) >= getattr(budget, field).value
    )


@dataclass(frozen=True)
class BudgetReservation:
    """An exact, durable reservation; identity prevents retry double counting."""

    reservation_id: str
    usage: ResourceUsage

    def __post_init__(self) -> None:
        _identifier(self.reservation_id, "reservation_id")
        if type(self.usage) is not ResourceUsage:
            raise ValueError("reservation usage must be ResourceUsage")
        if not any(getattr(self.usage, field) for field in _USAGE_FIELDS):
            raise ValueError("reservation usage must reserve at least one resource")


@dataclass(frozen=True)
class BudgetAccount:
    """Pure committed/reserved accounting with idempotent retry semantics."""

    committed: ResourceUsage = ResourceUsage()
    reservations: tuple[BudgetReservation, ...] = ()

    def __post_init__(self) -> None:
        if type(self.committed) is not ResourceUsage:
            raise ValueError("committed usage must be ResourceUsage")
        ordered = tuple(sorted(self.reservations, key=lambda item: item.reservation_id))
        if any(type(item) is not BudgetReservation for item in ordered):
            raise ValueError("reservations must contain BudgetReservation values")
        if len({item.reservation_id for item in ordered}) != len(ordered):
            raise ValueError("reservation ids must be unique")
        object.__setattr__(self, "reservations", ordered)
        self.total_reserved  # eagerly detect aggregate overflow

    @property
    def total_reserved(self) -> ResourceUsage:
        total = ResourceUsage()
        for item in self.reservations:
            total = _usage_add(total, item.usage)
        return total

    @property
    def allocated(self) -> ResourceUsage:
        return _usage_add(self.committed, self.total_reserved)

    def reserve(self, reservation: BudgetReservation, budget: ResourceBudget) -> "BudgetAccount":
        existing = next((item for item in self.reservations
                         if item.reservation_id == reservation.reservation_id), None)
        if existing is not None:
            if existing != reservation:
                raise ValueError("reservation retry conflicts with durable content")
            return self
        proposed = replace(self, reservations=(*self.reservations, reservation))
        if not _usage_fits(proposed.allocated, budget):
            raise ValueError("reservation exceeds cumulative campaign budget")
        return proposed

    def refund(self, reservation_id: str) -> "BudgetAccount":
        if not any(item.reservation_id == reservation_id for item in self.reservations):
            return self
        return replace(
            self,
            reservations=tuple(item for item in self.reservations
                               if item.reservation_id != reservation_id),
        )

    def commit(self, reservation_id: str, actual: ResourceUsage,
               budget: ResourceBudget) -> "BudgetAccount":
        reservation = next((item for item in self.reservations
                            if item.reservation_id == reservation_id), None)
        if reservation is None:
            raise ValueError("cannot commit an unknown or already-consumed reservation")
        if any(getattr(actual, field) > getattr(reservation.usage, field)
               for field in _USAGE_FIELDS):
            raise ValueError("actual usage exceeds its exact reservation")
        proposed = BudgetAccount(
            _usage_add(self.committed, actual),
            tuple(item for item in self.reservations if item.reservation_id != reservation_id),
        )
        if not _usage_fits(proposed.allocated, budget):
            raise ValueError("committed usage exceeds cumulative campaign budget")
        return proposed


@dataclass(frozen=True)
class CanonicalOutcomeTotals:
    coverage_basis_points: int = 0
    resolved_hypotheses: int = 0
    reproduced_findings: int = 0
    artifact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.coverage_basis_points) is not int \
                or not 0 <= self.coverage_basis_points <= 10_000:
            raise ValueError("coverage total must be an integer from 0 through 10000")
        for name in ("resolved_hypotheses", "reproduced_findings"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= MAX_INTEGER:
                raise ValueError(f"{name} must be a bounded non-negative integer")
        ordered = tuple(sorted(self.artifact_ids))
        if any(not isinstance(item, str) or not item for item in ordered):
            raise ValueError("artifact ids must be non-empty strings")
        if len(set(ordered)) != len(ordered):
            raise ValueError("artifact ids must be unique")
        object.__setattr__(self, "artifact_ids", ordered)


@dataclass(frozen=True)
class AttemptFact:
    attempt_id: str
    equivalence_digest: str
    outcome: AttemptOutcome

    def __post_init__(self) -> None:
        _identifier(self.attempt_id, "attempt_id")
        _digest(self.equivalence_digest, "attempt equivalence digest")
        if self.outcome not in {
            "productive", "no-novelty", "validation-rejected", "dependency-blocked",
            "environment-failed", "notification-only",
        }:
            raise ValueError("unsupported attempt outcome")


@dataclass(frozen=True)
class CampaignPolicyConfig:
    no_novelty_ask_threshold: int = 3
    no_novelty_stop_threshold: int = 6
    equivalent_attempt_threshold: int = 2
    validation_churn_threshold: int = 3
    dependency_deadlock_threshold: int = 2
    environment_flap_threshold: int = 3
    notification_churn_threshold: int = 3

    def __post_init__(self) -> None:
        for field in self.__dataclass_fields__:
            value = getattr(self, field)
            if type(value) is not int or not 1 <= value <= MAX_INTEGER:
                raise ValueError(f"{field} must be a positive bounded integer")
        if self.no_novelty_stop_threshold < self.no_novelty_ask_threshold:
            raise ValueError("no-novelty stop threshold must not precede ask threshold")


@dataclass(frozen=True)
class CampaignPolicyInput:
    campaign: CampaignContract
    epoch: EpochContract
    checkpoint: CampaignCheckpoint
    result: EpochResult
    account: BudgetAccount
    phase: CampaignPhase
    totals: CanonicalOutcomeTotals
    previous_checkpoint: CampaignCheckpoint | None = None
    previous_totals: CanonicalOutcomeTotals = CanonicalOutcomeTotals()
    known_progress_digests: tuple[str, ...] = ()
    attempts: tuple[AttemptFact, ...] = ()

    def __post_init__(self) -> None:
        if self.phase not in {"recon", "hypothesis", "validation"}:
            raise ValueError("unsupported campaign phase")
        known = tuple(sorted(
            _digest(item, "known progress digest") for item in self.known_progress_digests
        ))
        if len(set(known)) != len(known):
            raise ValueError("known progress digests must be unique")
        attempts = tuple(sorted(self.attempts, key=lambda item: item.attempt_id))
        if any(type(item) is not AttemptFact for item in attempts):
            raise ValueError("attempts must contain AttemptFact values")
        if len({item.attempt_id for item in attempts}) != len(attempts):
            raise ValueError("attempt ids must be unique")
        object.__setattr__(self, "known_progress_digests", known)
        object.__setattr__(self, "attempts", attempts)


@dataclass(frozen=True)
class CampaignPolicyRecommendation:
    action: PolicyAction
    reason_code: str
    explanation: str
    next_phase: CampaignPhase | None
    novel_progress: tuple[ProgressSignal, ...] = ()
    limiting_resources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.action not in {
            "continue", "reprioritize", "backoff", "suspend", "ask-operator",
            "exhausted", "policy-stop", "success",
        }:
            raise ValueError("unsupported policy action")
        _identifier(self.reason_code, "policy reason code")
        if not self.explanation or self.explanation != " ".join(self.explanation.split()):
            raise ValueError("policy explanation must be normalized non-empty text")
        if self.next_phase is not None and self.next_phase not in {
            "recon", "hypothesis", "validation",
        }:
            raise ValueError("unsupported next campaign phase")
        progress = tuple(sorted(self.novel_progress,
                                key=lambda item: (item.kind, item.reference_id)))
        resources = tuple(sorted(self.limiting_resources))
        object.__setattr__(self, "novel_progress", progress)
        object.__setattr__(self, "limiting_resources", resources)

    def to_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "action": self.action,
            "explanation": self.explanation,
            "limitingResources": list(self.limiting_resources),
            "nextPhase": self.next_phase,
            "novelProgress": [item.to_dict() for item in self.novel_progress],
            "reasonCode": self.reason_code,
            "schemaVersion": 1,
        }
        canonical = json.dumps(body, allow_nan=False, ensure_ascii=False,
                               separators=(",", ":"), sort_keys=True).encode("utf-8")
        return {"recommendationId": "policy-" + hashlib.sha256(canonical).hexdigest(), **body}

    @property
    def recommendation_id(self) -> str:
        return self.to_dict()["recommendationId"]  # type: ignore[return-value]


def validate_campaign_change_approval(
        current: CampaignContract,
        request: CampaignChangeRequest,
        approved_request_id: str | None,
) -> CampaignContract:
    """Validate an exact durable decision before returning a proposed authority."""
    if request.current_campaign_id != current.campaign_id:
        raise ValueError("campaign change request does not target the current campaign")
    gated = requires_operator_decision(current, request.proposed)
    if gated and approved_request_id != request.request_id:
        raise ValueError("scope or hard ceiling increase requires exact durable approval")
    if approved_request_id is not None and approved_request_id != request.request_id:
        raise ValueError("campaign change approval identity does not match request")
    return request.proposed


def _validate_policy_input(value: CampaignPolicyInput) -> ResourceUsage:
    campaign, epoch, checkpoint = value.campaign, value.epoch, value.checkpoint
    epoch.validate_for(campaign)
    if checkpoint.campaign_id != campaign.campaign_id \
            or checkpoint.epoch_id != epoch.epoch_id:
        raise ValueError("checkpoint authority does not match campaign epoch")
    if value.result.epoch_id != epoch.epoch_id \
            or value.result.checkpoint_id != checkpoint.checkpoint_id:
        raise ValueError("epoch result does not match the current checkpoint")
    previous = value.previous_checkpoint
    if epoch.ordinal == 1:
        if previous is not None or checkpoint.sequence != 1:
            raise ValueError("first epoch requires the first checkpoint and no predecessor")
        epoch_usage = checkpoint.cumulative_usage
    else:
        if previous is None or previous.checkpoint_id != epoch.previous_checkpoint_id:
            raise ValueError("later epoch requires its exact previous checkpoint")
        if previous.campaign_id != campaign.campaign_id:
            raise ValueError("previous checkpoint belongs to another campaign")
        if checkpoint.sequence != previous.sequence + 1:
            raise ValueError("checkpoint sequence is stale or discontinuous")
        epoch_usage = _usage_subtract(checkpoint.cumulative_usage, previous.cumulative_usage)
    if checkpoint.cumulative_usage != value.account.committed:
        raise ValueError("checkpoint usage contradicts committed budget account totals")
    if not _usage_fits(epoch_usage, epoch.budget):
        raise ValueError("epoch usage exceeds its finite budget")
    if not _usage_fits(value.account.allocated, campaign.cumulative_budget):
        raise ValueError("allocated usage exceeds cumulative campaign budget")

    old, new = value.previous_totals, value.totals
    if new.coverage_basis_points < old.coverage_basis_points \
            or new.resolved_hypotheses < old.resolved_hypotheses \
            or new.reproduced_findings < old.reproduced_findings \
            or not set(old.artifact_ids).issubset(new.artifact_ids):
        raise ValueError("canonical outcome totals regressed")
    kinds = {signal.kind for signal in value.result.progress}
    required: set[str] = set()
    changed_kinds: set[str] = set()
    if new.coverage_basis_points > old.coverage_basis_points:
        required.add("coverage-moved")
        changed_kinds.add("coverage-moved")
    if new.resolved_hypotheses > old.resolved_hypotheses:
        required.add("hypothesis-resolved")
        changed_kinds.add("hypothesis-resolved")
    if new.reproduced_findings > old.reproduced_findings:
        required.add("finding-reproduced")
        changed_kinds.add("finding-reproduced")
    if set(new.artifact_ids) - set(old.artifact_ids):
        required.add("material-evidence")
    if not required.issubset(kinds):
        raise ValueError("canonical totals changed without matching progress facts")
    total_bound_kinds = {"coverage-moved", "hypothesis-resolved", "finding-reproduced"}
    if kinds & total_bound_kinds != changed_kinds:
        raise ValueError("progress facts contradict unchanged canonical totals")
    return epoch_usage


def evaluate_campaign_policy(
        value: CampaignPolicyInput,
        config: CampaignPolicyConfig = CampaignPolicyConfig(),
) -> CampaignPolicyRecommendation:
    """Return one deterministic recommendation from exact canonical facts."""
    _validate_policy_input(value)
    novel = tuple(signal for signal in value.result.progress
                  if signal.material_digest not in value.known_progress_digests)
    if len({item.material_digest for item in novel}) != len(novel):
        raise ValueError("epoch repeats the same material progress digest")

    criteria, totals = value.campaign.completion, value.totals
    complete = (
        totals.coverage_basis_points >= criteria.coverage_basis_points
        and totals.resolved_hypotheses >= criteria.resolved_hypotheses
        and totals.reproduced_findings >= criteria.reproduced_findings
        and set(criteria.required_artifact_ids).issubset(totals.artifact_ids)
    )
    if complete:
        return CampaignPolicyRecommendation(
            "success", "completion-criteria-satisfied",
            "Canonical outcome totals satisfy every campaign completion criterion.",
            None, novel,
        )

    hard_reached = _usage_reaches(
        value.account.allocated, value.campaign.cumulative_budget, "hard",
    )
    if hard_reached:
        return CampaignPolicyRecommendation(
            "exhausted", "cumulative-budget-exhausted",
            "The finite cumulative allocation reached: " + ", ".join(hard_reached) + ".",
            None, novel, hard_reached,
        )
    soft_reached = _usage_reaches(
        value.account.allocated, value.campaign.cumulative_budget, "soft",
    )
    if soft_reached:
        return CampaignPolicyRecommendation(
            "ask-operator", "soft-budget-threshold",
            "The soft cumulative allocation reached: " + ", ".join(soft_reached) + ".",
            None, novel, soft_reached,
        )

    outcomes = tuple(item.outcome for item in value.attempts)
    if "productive" in outcomes and not novel:
        raise ValueError("productive attempt contradicts absence of novel canonical progress")
    no_novelty = sum(outcome != "productive" for outcome in outcomes) if not novel else 0
    if no_novelty >= config.no_novelty_stop_threshold:
        return CampaignPolicyRecommendation(
            "policy-stop", "no-novelty-policy-limit",
            f"{no_novelty} canonical attempts produced no new material progress.", None, novel,
        )
    if outcomes.count("dependency-blocked") >= config.dependency_deadlock_threshold:
        return CampaignPolicyRecommendation(
            "ask-operator", "dependency-deadlock",
            "Repeated dependency-blocked attempts require an explicit operator decision.",
            None, novel,
        )
    if outcomes.count("environment-failed") >= config.environment_flap_threshold:
        return CampaignPolicyRecommendation(
            "suspend", "environment-flapping",
            "Repeated canonical environment failures make continued work unsafe.",
            None, novel,
        )
    if outcomes.count("notification-only") >= config.notification_churn_threshold:
        return CampaignPolicyRecommendation(
            "backoff", "notification-churn",
            "Notification-only activity crossed the bounded backoff threshold.",
            value.phase, novel,
        )
    if outcomes.count("validation-rejected") >= config.validation_churn_threshold:
        return CampaignPolicyRecommendation(
            "reprioritize", "validation-churn",
            "Repeated validation rejection requires a different bounded approach.",
            "hypothesis", novel,
        )
    equivalence_counts: dict[str, int] = {}
    for attempt in value.attempts:
        equivalence_counts[attempt.equivalence_digest] = (
            equivalence_counts.get(attempt.equivalence_digest, 0) + 1
        )
    if equivalence_counts and max(equivalence_counts.values()) \
            >= config.equivalent_attempt_threshold:
        return CampaignPolicyRecommendation(
            "reprioritize", "equivalent-attempt-repeated",
            "Equivalent canonical attempts crossed the bounded repetition threshold.",
            value.phase, novel,
        )
    if no_novelty >= config.no_novelty_ask_threshold:
        return CampaignPolicyRecommendation(
            "ask-operator", "no-novelty-threshold",
            f"{no_novelty} canonical attempts produced no new material progress.", None, novel,
        )

    next_phase: CampaignPhase = value.phase
    if any(item.kind == "material-evidence" for item in novel) and value.phase == "recon":
        next_phase = "hypothesis"
    if any(item.kind == "hypothesis-resolved" for item in novel):
        next_phase = "validation"
    return CampaignPolicyRecommendation(
        "continue", "canonical-progress" if novel else "bounded-work-remains",
        ("New canonical material progress permits the next bounded epoch."
         if novel else "Thresholds remain untripped and finite budget remains."),
        next_phase, novel,
    )
