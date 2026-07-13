"""Strict, pure contracts for bounded campaigns and finite research epochs.

This module owns vocabulary and content identity only. Persistence belongs to W-0052,
progress policy to W-0053, and controller execution to W-0054.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, ClassVar, Literal, Mapping, Self


SCHEMA_VERSION = 1
CONTRACT_VERSION = "factory-campaign-contract/v1"
MAX_INTEGER = 2**63 - 1

CampaignStatus = Literal[
    "requested", "running", "waiting", "suspended", "completed", "exhausted",
    "blocked", "stopped", "policy-stopped", "failed",
]
CampaignAuthority = Literal["factory-scheduler", "operator", "validator-policy"]
Enforcement = Literal["hard", "soft"]
ProgressKind = Literal[
    "material-evidence", "hypothesis-resolved", "coverage-moved",
    "finding-reproduced", "capability-gap-changed",
]
TerminalStatus = Literal[
    "completed", "exhausted", "blocked", "stopped", "policy-stopped", "failed",
]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_LIMIT_FIELDS = (
    "workItems", "concurrency", "retries", "inputTokens", "outputTokens",
    "costUnits", "wallSeconds", "toolCalls", "networkCalls", "artifactBytes",
)
_LIMIT_UNITS = {
    "workItems": "items", "concurrency": "workers", "retries": "attempts",
    "inputTokens": "tokens", "outputTokens": "tokens", "costUnits": "cost-units",
    "wallSeconds": "seconds", "toolCalls": "calls", "networkCalls": "calls",
    "artifactBytes": "bytes",
}
_POSITIVE_LIMITS = frozenset({"workItems", "concurrency", "wallSeconds"})
_TERMINAL = frozenset({
    "completed", "exhausted", "blocked", "stopped", "policy-stopped", "failed",
})
CAMPAIGN_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "requested": frozenset({"running", "stopped", "policy-stopped", "failed"}),
    "running": frozenset({
        "waiting", "suspended", "completed", "exhausted", "blocked", "stopped",
        "policy-stopped", "failed",
    }),
    "waiting": frozenset({"running", "suspended", "stopped", "policy-stopped", "failed"}),
    "suspended": frozenset({"running", "stopped", "policy-stopped", "failed"}),
    "completed": frozenset(), "exhausted": frozenset(), "blocked": frozenset(),
    "stopped": frozenset(), "policy-stopped": frozenset(), "failed": frozenset(),
}
TRANSITION_AUTHORITY: Mapping[str, str] = {
    "requested": "factory-scheduler", "running": "factory-scheduler",
    "waiting": "factory-scheduler", "suspended": "factory-scheduler",
    "completed": "factory-scheduler", "exhausted": "factory-scheduler",
    "blocked": "factory-scheduler", "failed": "factory-scheduler",
    "stopped": "operator", "policy-stopped": "validator-policy",
}


def _strict(value: object, label: str, fields: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ValueError(f"{label} must be a JSON object with string fields")
    missing, unknown = fields - set(value), set(value) - fields
    if missing:
        raise ValueError(f"{label} is missing fields: {sorted(missing)}")
    if unknown:
        raise ValueError(f"{label} has unknown fields: {sorted(unknown)}")
    return value


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a bounded stable identifier")
    return value


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: object, label: str, maximum: int = 2048) -> str:
    if type(value) is not str or not value.strip() or value != " ".join(value.split()) \
            or len(value) > maximum:
        raise ValueError(f"{label} must be normalized non-empty text of at most {maximum} characters")
    return value


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise ValueError(f"{label} must be an integer between {minimum} and {MAX_INTEGER}")
    return value


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _items(value: object, label: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be an array")
    result = tuple(_identifier(item, f"{label} item") for item in value)
    if not allow_empty and not result:
        raise ValueError(f"{label} must not be empty")
    if len(set(result)) != len(result):
        raise ValueError(f"{label} must contain unique values")
    return tuple(sorted(result))


def _canonical(value: object) -> bytes:
    return (json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ) + "\n").encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True)
class ResourceLimit:
    value: int
    unit: str
    enforcement: Enforcement = "hard"

    def __post_init__(self) -> None:
        _integer(self.value, "resource limit")
        _identifier(self.unit, "resource unit")
        if self.enforcement not in {"hard", "soft"}:
            raise ValueError("resource limit enforcement must be hard or soft")

    def to_dict(self) -> dict[str, object]:
        return {"enforcement": self.enforcement, "unit": self.unit, "value": self.value}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _strict(value, "resource limit", {"enforcement", "unit", "value"})
        return cls(_integer(raw["value"], "resource limit"), raw["unit"], raw["enforcement"])


@dataclass(frozen=True)
class ResourceBudget:
    work_items: ResourceLimit
    concurrency: ResourceLimit
    retries: ResourceLimit
    input_tokens: ResourceLimit
    output_tokens: ResourceLimit
    cost_units: ResourceLimit
    wall_seconds: ResourceLimit
    tool_calls: ResourceLimit
    network_calls: ResourceLimit
    artifact_bytes: ResourceLimit

    def __post_init__(self) -> None:
        for external, attribute in zip(_LIMIT_FIELDS, self._attributes(), strict=True):
            limit = getattr(self, attribute)
            if type(limit) is not ResourceLimit:
                raise ValueError(f"{external} must be a ResourceLimit")
            if limit.unit != _LIMIT_UNITS[external]:
                raise ValueError(f"{external} must use unit {_LIMIT_UNITS[external]}")
            if external in _POSITIVE_LIMITS and limit.value < 1:
                raise ValueError(f"{external} must be finite and at least 1")

    @staticmethod
    def _attributes() -> tuple[str, ...]:
        return (
            "work_items", "concurrency", "retries", "input_tokens", "output_tokens",
            "cost_units", "wall_seconds", "tool_calls", "network_calls", "artifact_bytes",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            external: getattr(self, attribute).to_dict()
            for external, attribute in zip(_LIMIT_FIELDS, self._attributes(), strict=True)
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _strict(value, "resource budget", set(_LIMIT_FIELDS))
        return cls(*(ResourceLimit.from_dict(raw[field]) for field in _LIMIT_FIELDS))

    def bounded_by(self, outer: Self) -> bool:
        return all(
            getattr(self, name).value <= getattr(outer, name).value
            for name in self._attributes()
        )


@dataclass(frozen=True)
class ScopeBinding:
    scope_id: str
    revision: int
    digest: str

    def __post_init__(self) -> None:
        _identifier(self.scope_id, "scope_id")
        _integer(self.revision, "scope revision", 1)
        _digest(self.digest, "scope digest")

    def to_dict(self) -> dict[str, object]:
        return {"digest": self.digest, "revision": self.revision, "scopeId": self.scope_id}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _strict(value, "scope binding", {"digest", "revision", "scopeId"})
        return cls(raw["scopeId"], _integer(raw["revision"], "scope revision", 1), raw["digest"])


@dataclass(frozen=True)
class ComponentVersion:
    name: str
    version: str
    digest: str

    def __post_init__(self) -> None:
        _identifier(self.name, "component name")
        _identifier(self.version, "component version")
        _digest(self.digest, "component digest")

    def to_dict(self) -> dict[str, str]:
        return {"digest": self.digest, "name": self.name, "version": self.version}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _strict(value, "component version", {"digest", "name", "version"})
        return cls(raw["name"], raw["version"], raw["digest"])


@dataclass(frozen=True)
class CompletionCriteria:
    coverage_basis_points: int
    resolved_hypotheses: int
    reproduced_findings: int
    required_artifact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= _integer(self.coverage_basis_points, "coverage basis points") <= 10_000:
            raise ValueError("coverage basis points must not exceed 10000")
        _integer(self.resolved_hypotheses, "resolved hypotheses")
        _integer(self.reproduced_findings, "reproduced findings")
        object.__setattr__(
            self, "required_artifact_ids", _items(self.required_artifact_ids, "artifact ids"),
        )
        if not any((
            self.coverage_basis_points, self.resolved_hypotheses,
            self.reproduced_findings, self.required_artifact_ids,
        )):
            raise ValueError("completion criteria must require at least one canonical outcome")

    def to_dict(self) -> dict[str, object]:
        return {
            "coverageBasisPoints": self.coverage_basis_points,
            "reproducedFindings": self.reproduced_findings,
            "requiredArtifactIds": list(self.required_artifact_ids),
            "resolvedHypotheses": self.resolved_hypotheses,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _strict(value, "completion criteria", {
            "coverageBasisPoints", "reproducedFindings", "requiredArtifactIds",
            "resolvedHypotheses",
        })
        return cls(
            _integer(raw["coverageBasisPoints"], "coverage basis points"),
            _integer(raw["resolvedHypotheses"], "resolved hypotheses"),
            _integer(raw["reproducedFindings"], "reproduced findings"),
            _items(raw["requiredArtifactIds"], "artifact ids"),
        )


@dataclass(frozen=True)
class OperatorPolicy:
    scope_expansion_requires_approval: bool = True
    hard_ceiling_increase_requires_approval: bool = True
    continue_after_risk_requires_approval: bool = True
    risk_threshold: int = 0

    def __post_init__(self) -> None:
        for label, value in (
            ("scope expansion approval", self.scope_expansion_requires_approval),
            ("hard ceiling approval", self.hard_ceiling_increase_requires_approval),
            ("risk continuation approval", self.continue_after_risk_requires_approval),
        ):
            _boolean(value, label)
        if not all((
            self.scope_expansion_requires_approval,
            self.hard_ceiling_increase_requires_approval,
            self.continue_after_risk_requires_approval,
        )):
            raise ValueError("v1 campaigns must require approval for every authority expansion")
        if not 0 <= _integer(self.risk_threshold, "risk threshold") <= 100:
            raise ValueError("risk threshold must not exceed 100")

    def to_dict(self) -> dict[str, object]:
        return {
            "continueAfterRiskRequiresApproval": self.continue_after_risk_requires_approval,
            "hardCeilingIncreaseRequiresApproval": self.hard_ceiling_increase_requires_approval,
            "riskThreshold": self.risk_threshold,
            "scopeExpansionRequiresApproval": self.scope_expansion_requires_approval,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _strict(value, "operator policy", {
            "continueAfterRiskRequiresApproval", "hardCeilingIncreaseRequiresApproval",
            "riskThreshold", "scopeExpansionRequiresApproval",
        })
        return cls(
            _boolean(raw["scopeExpansionRequiresApproval"], "scope expansion approval"),
            _boolean(raw["hardCeilingIncreaseRequiresApproval"], "hard ceiling approval"),
            _boolean(raw["continueAfterRiskRequiresApproval"], "risk continuation approval"),
            _integer(raw["riskThreshold"], "risk threshold"),
        )


@dataclass(frozen=True)
class CampaignContract:
    project_id: str
    goal: str
    scope: ScopeBinding
    epoch_budget: ResourceBudget
    cumulative_budget: ResourceBudget
    completion: CompletionCriteria
    operator_policy: OperatorPolicy
    components: tuple[ComponentVersion, ...]
    schema_version: ClassVar[int] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.project_id, "project_id")
        _text(self.goal, "goal")
        if type(self.scope) is not ScopeBinding:
            raise ValueError("scope must be a ScopeBinding")
        if type(self.epoch_budget) is not ResourceBudget \
                or type(self.cumulative_budget) is not ResourceBudget:
            raise ValueError("campaign budgets must be ResourceBudget contracts")
        if not self.epoch_budget.bounded_by(self.cumulative_budget):
            raise ValueError("epoch budget must not exceed cumulative budget")
        if type(self.completion) is not CompletionCriteria \
                or type(self.operator_policy) is not OperatorPolicy:
            raise ValueError("campaign completion and operator policy contracts are required")
        components = tuple(sorted(self.components, key=lambda item: item.name))
        if not components or any(type(item) is not ComponentVersion for item in components):
            raise ValueError("campaign must bind at least one component version")
        if len({item.name for item in components}) != len(components):
            raise ValueError("component names must be unique")
        object.__setattr__(self, "components", components)

    def _identity_dict(self) -> dict[str, object]:
        return {
            "completion": self.completion.to_dict(),
            "components": [item.to_dict() for item in self.components],
            "contractVersion": CONTRACT_VERSION,
            "cumulativeBudget": self.cumulative_budget.to_dict(),
            "epochBudget": self.epoch_budget.to_dict(),
            "goal": self.goal,
            "operatorPolicy": self.operator_policy.to_dict(),
            "projectId": self.project_id,
            "schemaVersion": SCHEMA_VERSION,
            "scope": self.scope.to_dict(),
        }

    @property
    def digest(self) -> str:
        return _sha256(self._identity_dict())

    @property
    def campaign_id(self) -> str:
        return f"campaign-{self.digest}"

    def to_dict(self) -> dict[str, object]:
        return {"campaignId": self.campaign_id, **self._identity_dict()}

    def canonical_bytes(self) -> bytes:
        return _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> Self:
        fields = {
            "campaignId", "completion", "components", "contractVersion",
            "cumulativeBudget", "epochBudget", "goal", "operatorPolicy", "projectId",
            "schemaVersion", "scope",
        }
        raw = _strict(value, "campaign contract", fields)
        if raw["schemaVersion"] != SCHEMA_VERSION or raw["contractVersion"] != CONTRACT_VERSION:
            raise ValueError("unsupported campaign contract version")
        if type(raw["components"]) is not list:
            raise ValueError("components must be an array")
        result = cls(
            raw["projectId"], raw["goal"], ScopeBinding.from_dict(raw["scope"]),
            ResourceBudget.from_dict(raw["epochBudget"]),
            ResourceBudget.from_dict(raw["cumulativeBudget"]),
            CompletionCriteria.from_dict(raw["completion"]),
            OperatorPolicy.from_dict(raw["operatorPolicy"]),
            tuple(ComponentVersion.from_dict(item) for item in raw["components"]),
        )
        if raw["campaignId"] != result.campaign_id:
            raise ValueError("campaign identity does not match canonical content")
        return result


@dataclass(frozen=True)
class EpochContract:
    campaign_id: str
    ordinal: int
    work_ids: tuple[str, ...]
    budget: ResourceBudget
    previous_checkpoint_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.campaign_id, "campaign_id")
        _integer(self.ordinal, "epoch ordinal", 1)
        object.__setattr__(self, "work_ids", _items(self.work_ids, "work ids", allow_empty=False))
        if type(self.budget) is not ResourceBudget:
            raise ValueError("epoch budget must be a ResourceBudget")
        if self.ordinal == 1 and self.previous_checkpoint_id is not None:
            raise ValueError("the first epoch cannot have a previous checkpoint")
        if self.ordinal > 1 and self.previous_checkpoint_id is None:
            raise ValueError("later epochs require a previous checkpoint")
        if self.previous_checkpoint_id is not None:
            _identifier(self.previous_checkpoint_id, "previous checkpoint id")

    def _identity_dict(self) -> dict[str, object]:
        return {
            "budget": self.budget.to_dict(), "campaignId": self.campaign_id,
            "ordinal": self.ordinal, "previousCheckpointId": self.previous_checkpoint_id,
            "schemaVersion": SCHEMA_VERSION, "workIds": list(self.work_ids),
        }

    @property
    def epoch_id(self) -> str:
        return f"epoch-{_sha256(self._identity_dict())}"

    def to_dict(self) -> dict[str, object]:
        return {"epochId": self.epoch_id, **self._identity_dict()}

    def validate_for(self, campaign: CampaignContract) -> None:
        if self.campaign_id != campaign.campaign_id:
            raise ValueError("epoch campaign identity does not match")
        if not self.budget.bounded_by(campaign.epoch_budget):
            raise ValueError("epoch exceeds the campaign per-epoch budget")

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _strict(value, "epoch contract", {
            "budget", "campaignId", "epochId", "ordinal", "previousCheckpointId",
            "schemaVersion", "workIds",
        })
        if raw["schemaVersion"] != SCHEMA_VERSION:
            raise ValueError("unsupported epoch contract version")
        result = cls(
            raw["campaignId"], _integer(raw["ordinal"], "epoch ordinal", 1),
            _items(raw["workIds"], "work ids", allow_empty=False),
            ResourceBudget.from_dict(raw["budget"]), raw["previousCheckpointId"],
        )
        if raw["epochId"] != result.epoch_id:
            raise ValueError("epoch identity does not match canonical content")
        return result


@dataclass(frozen=True)
class ProgressSignal:
    kind: ProgressKind
    reference_id: str
    material_digest: str

    def __post_init__(self) -> None:
        if self.kind not in {
            "material-evidence", "hypothesis-resolved", "coverage-moved",
            "finding-reproduced", "capability-gap-changed",
        }:
            raise ValueError("unsupported progress signal")
        _identifier(self.reference_id, "progress reference")
        _digest(self.material_digest, "progress material digest")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "materialDigest": self.material_digest,
                "referenceId": self.reference_id}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _strict(value, "progress signal", {"kind", "materialDigest", "referenceId"})
        return cls(raw["kind"], raw["referenceId"], raw["materialDigest"])


@dataclass(frozen=True)
class ResourceUsage:
    work_items: int = 0
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_units: int = 0
    wall_seconds: int = 0
    tool_calls: int = 0
    network_calls: int = 0
    artifact_bytes: int = 0

    _FIELDS: ClassVar[tuple[str, ...]] = (
        "workItems", "retries", "inputTokens", "outputTokens", "costUnits",
        "wallSeconds", "toolCalls", "networkCalls", "artifactBytes",
    )
    _ATTRIBUTES: ClassVar[tuple[str, ...]] = (
        "work_items", "retries", "input_tokens", "output_tokens", "cost_units",
        "wall_seconds", "tool_calls", "network_calls", "artifact_bytes",
    )

    def __post_init__(self) -> None:
        for external, attribute in zip(self._FIELDS, self._ATTRIBUTES, strict=True):
            _integer(getattr(self, attribute), external)

    def to_dict(self) -> dict[str, int]:
        return {
            external: getattr(self, attribute)
            for external, attribute in zip(self._FIELDS, self._ATTRIBUTES, strict=True)
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _strict(value, "resource usage", set(cls._FIELDS))
        return cls(*(_integer(raw[field], field) for field in cls._FIELDS))


@dataclass(frozen=True)
class CheckpointSource:
    source: str
    revision: int
    digest: str

    def __post_init__(self) -> None:
        _identifier(self.source, "checkpoint source")
        _integer(self.revision, "checkpoint source revision")
        _digest(self.digest, "checkpoint source digest")

    def to_dict(self) -> dict[str, object]:
        return {"digest": self.digest, "revision": self.revision, "source": self.source}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _strict(value, "checkpoint source", {"digest", "revision", "source"})
        return cls(raw["source"], _integer(raw["revision"], "source revision"), raw["digest"])


@dataclass(frozen=True)
class CampaignCheckpoint:
    campaign_id: str
    epoch_id: str
    sequence: int
    sources: tuple[CheckpointSource, ...]
    cumulative_usage: ResourceUsage

    def __post_init__(self) -> None:
        _identifier(self.campaign_id, "campaign_id")
        _identifier(self.epoch_id, "epoch_id")
        _integer(self.sequence, "checkpoint sequence", 1)
        sources = tuple(sorted(self.sources, key=lambda item: item.source))
        if not sources or any(type(item) is not CheckpointSource for item in sources):
            raise ValueError("checkpoint requires canonical source references")
        if len({item.source for item in sources}) != len(sources):
            raise ValueError("checkpoint source names must be unique")
        if type(self.cumulative_usage) is not ResourceUsage:
            raise ValueError("checkpoint cumulative usage must be ResourceUsage")
        object.__setattr__(self, "sources", sources)

    def _identity_dict(self) -> dict[str, object]:
        return {
            "campaignId": self.campaign_id,
            "cumulativeUsage": self.cumulative_usage.to_dict(),
            "epochId": self.epoch_id,
            "schemaVersion": SCHEMA_VERSION,
            "sequence": self.sequence,
            "sources": [item.to_dict() for item in self.sources],
        }

    @property
    def checkpoint_id(self) -> str:
        return f"checkpoint-{_sha256(self._identity_dict())}"

    def to_dict(self) -> dict[str, object]:
        return {"checkpointId": self.checkpoint_id, **self._identity_dict()}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _strict(value, "campaign checkpoint", {
            "campaignId", "checkpointId", "cumulativeUsage", "epochId",
            "schemaVersion", "sequence", "sources",
        })
        if raw["schemaVersion"] != SCHEMA_VERSION or type(raw["sources"]) is not list:
            raise ValueError("unsupported campaign checkpoint version")
        result = cls(
            raw["campaignId"], raw["epochId"],
            _integer(raw["sequence"], "checkpoint sequence", 1),
            tuple(CheckpointSource.from_dict(item) for item in raw["sources"]),
            ResourceUsage.from_dict(raw["cumulativeUsage"]),
        )
        if raw["checkpointId"] != result.checkpoint_id:
            raise ValueError("checkpoint identity does not match canonical content")
        return result


@dataclass(frozen=True)
class EpochResult:
    epoch_id: str
    checkpoint_id: str
    progress: tuple[ProgressSignal, ...]
    next_action_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.epoch_id, "epoch_id")
        _identifier(self.checkpoint_id, "checkpoint_id")
        progress = tuple(sorted(self.progress, key=lambda item: (item.kind, item.reference_id)))
        if any(type(item) is not ProgressSignal for item in progress):
            raise ValueError("epoch progress must contain ProgressSignal values")
        if len({(item.kind, item.reference_id) for item in progress}) != len(progress):
            raise ValueError("epoch progress signals must be unique")
        object.__setattr__(self, "progress", progress)
        object.__setattr__(
            self, "next_action_ids", _items(self.next_action_ids, "next action ids"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "checkpointId": self.checkpoint_id, "epochId": self.epoch_id,
            "nextActionIds": list(self.next_action_ids),
            "progress": [item.to_dict() for item in self.progress],
            "schemaVersion": SCHEMA_VERSION,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _strict(value, "epoch result", {
            "checkpointId", "epochId", "nextActionIds", "progress", "schemaVersion",
        })
        if raw["schemaVersion"] != SCHEMA_VERSION or type(raw["progress"]) is not list:
            raise ValueError("unsupported epoch result version")
        return cls(
            raw["epochId"], raw["checkpointId"],
            tuple(ProgressSignal.from_dict(item) for item in raw["progress"]),
            _items(raw["nextActionIds"], "next action ids"),
        )


@dataclass(frozen=True)
class TerminalOutcome:
    campaign_id: str
    status: TerminalStatus
    reason_code: str
    evidence_ids: tuple[str, ...]
    final_checkpoint_id: str

    def __post_init__(self) -> None:
        _identifier(self.campaign_id, "campaign_id")
        if self.status not in _TERMINAL:
            raise ValueError("terminal outcome requires a terminal campaign status")
        _identifier(self.reason_code, "terminal reason code")
        object.__setattr__(
            self, "evidence_ids", _items(self.evidence_ids, "terminal evidence", allow_empty=False),
        )
        _identifier(self.final_checkpoint_id, "final checkpoint id")

    def to_dict(self) -> dict[str, object]:
        return {
            "campaignId": self.campaign_id, "evidenceIds": list(self.evidence_ids),
            "finalCheckpointId": self.final_checkpoint_id, "reasonCode": self.reason_code,
            "schemaVersion": SCHEMA_VERSION, "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _strict(value, "terminal outcome", {
            "campaignId", "evidenceIds", "finalCheckpointId", "reasonCode",
            "schemaVersion", "status",
        })
        if raw["schemaVersion"] != SCHEMA_VERSION:
            raise ValueError("unsupported terminal outcome version")
        return cls(
            raw["campaignId"], raw["status"], raw["reasonCode"],
            _items(raw["evidenceIds"], "terminal evidence", allow_empty=False),
            raw["finalCheckpointId"],
        )


def validate_campaign_transition(
        current: CampaignStatus, target: CampaignStatus, *, authority: CampaignAuthority) -> None:
    if current not in CAMPAIGN_TRANSITIONS or target not in CAMPAIGN_TRANSITIONS:
        raise ValueError("unsupported campaign state")
    if target not in CAMPAIGN_TRANSITIONS[current]:
        raise ValueError(f"invalid campaign transition {current} -> {target}")
    expected = TRANSITION_AUTHORITY[target]
    if authority != expected:
        raise ValueError(f"campaign transition to {target} requires {expected} authority")


def requires_operator_decision(
        current: CampaignContract, proposed: CampaignContract) -> bool:
    """Return whether exact-content operator approval gates a proposed authority change."""
    if current.project_id != proposed.project_id or current.goal != proposed.goal:
        raise ValueError("campaign goal/project identity cannot be revised in place")
    scope_changed = current.scope != proposed.scope
    hard_increase = any(
        getattr(new_budget, field).enforcement == "hard"
        and getattr(new_budget, field).value > getattr(old_budget, field).value
        for old_budget, new_budget in (
            (current.epoch_budget, proposed.epoch_budget),
            (current.cumulative_budget, proposed.cumulative_budget),
        )
        for field in ResourceBudget._attributes()
    )
    return scope_changed or hard_increase


@dataclass(frozen=True)
class CampaignChangeRequest:
    current_campaign_id: str
    proposed: CampaignContract
    reason: str

    def __post_init__(self) -> None:
        _identifier(self.current_campaign_id, "current campaign id")
        if type(self.proposed) is not CampaignContract:
            raise ValueError("proposed campaign must be a CampaignContract")
        if self.current_campaign_id == self.proposed.campaign_id:
            raise ValueError("campaign change request must change canonical content")
        _text(self.reason, "campaign change reason", 512)

    def _identity_dict(self) -> dict[str, object]:
        return {
            "currentCampaignId": self.current_campaign_id,
            "proposed": self.proposed.to_dict(),
            "reason": self.reason,
            "schemaVersion": SCHEMA_VERSION,
        }

    @property
    def request_id(self) -> str:
        return f"campaign-change-{_sha256(self._identity_dict())}"

    def to_dict(self) -> dict[str, object]:
        return {"requestId": self.request_id, **self._identity_dict()}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _strict(value, "campaign change request", {
            "currentCampaignId", "proposed", "reason", "requestId", "schemaVersion",
        })
        if raw["schemaVersion"] != SCHEMA_VERSION:
            raise ValueError("unsupported campaign change request version")
        result = cls(
            raw["currentCampaignId"], CampaignContract.from_dict(raw["proposed"]),
            raw["reason"],
        )
        if raw["requestId"] != result.request_id:
            raise ValueError("campaign change request identity does not match canonical content")
        return result
