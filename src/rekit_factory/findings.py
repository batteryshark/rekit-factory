"""Proof-gated findings and independent, replayable reproduction attempts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from rekit_factory.memory import EvidenceRef, MemoryAction, ProjectMemory, ProjectMemoryLog


FindingStatus = Literal[
    "lead", "candidate", "demonstrated", "reproduction-pending", "reproduced",
    "rejected", "withdrawn", "inconclusive",
]
FindingType = Literal[
    "informational", "defect", "vulnerability", "exploitability", "destructive-impact",
]
Consequence = Literal["low", "medium", "high", "critical"]
ReproductionOutcome = Literal["success", "negative", "flaky", "contradictory", "inconclusive"]

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "lead": {"candidate", "rejected", "withdrawn"},
    "candidate": {"demonstrated", "rejected", "withdrawn", "inconclusive"},
    "demonstrated": {"reproduction-pending", "rejected", "withdrawn", "inconclusive"},
    "reproduction-pending": {"reproduced", "rejected", "withdrawn", "inconclusive"},
    "reproduced": {"demonstrated", "inconclusive", "rejected", "withdrawn"},
    "inconclusive": {"demonstrated", "reproduction-pending", "rejected", "withdrawn"},
    "rejected": set(),
    "withdrawn": set(),
}


class ObservationEvidence(BaseModel):
    observation: str = Field(min_length=1, max_length=4_000)
    references: list[EvidenceRef] = Field(min_length=1)


class ReproductionStep(BaseModel):
    action: Literal["stage-input", "invoke", "observe", "compare"]
    description: str = Field(min_length=1, max_length=2_000)
    tool_id: str | None = Field(default=None, max_length=128)
    argv: list[str] = Field(default_factory=list, max_length=64)
    environment: dict[str, str] = Field(default_factory=dict)
    references: list[EvidenceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def structured_invocation(self):
        if self.action == "invoke" and (not self.tool_id or not self.argv):
            raise ValueError("invoke reproduction steps require tool_id and argv")
        if self.action != "invoke" and (self.tool_id is not None or self.argv):
            raise ValueError("only invoke reproduction steps may carry tool_id or argv")
        return self


class ReproductionRecipe(BaseModel):
    schema_version: Literal[1] = 1
    id: str = Field(min_length=1, max_length=128)
    steps: list[str | ReproductionStep] = Field(min_length=1, max_length=50)
    staged_inputs: list[EvidenceRef] = Field(min_length=1)
    expected_observation: str = Field(min_length=1, max_length=4_000)
    clean_environment_requirements: list[str] = Field(min_length=1, max_length=20)


class ProofPolicy(BaseModel):
    schema_version: Literal[1] = 1
    successful_clean_reproductions: int = Field(ge=1, le=5)
    require_independent_worker: bool
    require_independent_session: bool
    require_clean_environment: bool = True
    require_distinct_model_profile: bool = False


def proof_policy(finding_type: FindingType, consequence: Consequence) -> ProofPolicy:
    informational_low = finding_type == "informational" and consequence == "low"
    highest_assurance = finding_type == "destructive-impact" or consequence == "critical"
    return ProofPolicy(
        successful_clean_reproductions=2 if highest_assurance else 1,
        require_independent_worker=not informational_low,
        require_independent_session=not informational_low,
        # The policy records profile separation explicitly. The default does not require it
        # because a project may only have one approved profile; higher-assurance deployments
        # can persist a stricter policy without weakening worker/session/environment separation.
        require_distinct_model_profile=False,
    )


class FindingProposal(BaseModel):
    schema_version: Literal[1] = 1
    id: str = Field(min_length=1, max_length=128)
    hypothesis_id: str = Field(min_length=1, max_length=128)
    scope: str = Field(min_length=1, max_length=512)
    observations: list[ObservationEvidence] = Field(min_length=1)
    affected_component: str = Field(min_length=1, max_length=1_000)
    impact_claim: str = Field(min_length=1, max_length=4_000)
    assumptions: list[str] = Field(min_length=1)
    known_uncertainty: str = Field(min_length=1, max_length=4_000)
    finding_type: FindingType
    consequence: Consequence
    confidence: float = Field(ge=0, le=1)
    references: list[EvidenceRef] = Field(min_length=1)
    recipe: ReproductionRecipe

    @model_validator(mode="after")
    def references_cover_observations_and_inputs(self):
        cited = {(reference.kind, reference.id) for reference in self.references}
        material = {
            (reference.kind, reference.id)
            for observation in self.observations for reference in observation.references
        } | {(reference.kind, reference.id) for reference in self.recipe.staged_inputs} | {
            (reference.kind, reference.id)
            for step in self.recipe.steps if isinstance(step, ReproductionStep)
            for reference in step.references
        }
        if not material <= cited:
            raise ValueError("finding references must include all observation and staged-input evidence")
        return self


class ReproductionResultProposal(BaseModel):
    """The validator's explicit observable result; runtime identity is controller-owned."""

    schema_version: Literal[1] = 1
    finding_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    outcome: ReproductionOutcome
    observations: list[str] = Field(min_length=1)
    references: list[EvidenceRef] = Field(min_length=1)
    environmental_differences: list[str] = Field(default_factory=list)


class ReproductionAttempt(BaseModel):
    schema_version: Literal[1] = 1
    id: str = Field(min_length=1, max_length=128)
    finding_id: str = Field(min_length=1, max_length=128)
    recipe_id: str = Field(min_length=1, max_length=128)
    outcome: ReproductionOutcome
    worker_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    environment_id: str = Field(min_length=1, max_length=256)
    clean_environment: bool
    model_profile: str = Field(min_length=1, max_length=128)
    platform: str = Field(default="unknown", min_length=1, max_length=128)
    architecture: str = Field(default="unknown", min_length=1, max_length=128)
    isolation: str = Field(default="unknown", min_length=1, max_length=128)
    observations: list[str] = Field(min_length=1)
    environmental_differences: list[str] = Field(default_factory=list)
    references: list[EvidenceRef] = Field(min_length=1)


class FindingTransition(BaseModel):
    schema_version: Literal[1] = 1
    finding_id: str
    to_status: FindingStatus
    reason: str = Field(min_length=1, max_length=4_000)
    references: list[EvidenceRef] = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    consequence: Consequence | None = None


class OperatorFindingDecision(BaseModel):
    schema_version: Literal[1] = 1
    finding_id: str
    decision: Literal["accepted", "rejected", "waived"]
    rationale: str = Field(min_length=1, max_length=4_000)
    unmet_criteria: list[str] = Field(default_factory=list)
    references: list[EvidenceRef] = Field(min_length=1)

    @model_validator(mode="after")
    def waiver_preserves_unmet_criteria(self):
        if self.decision == "waived" and not self.unmet_criteria:
            raise ValueError("a waiver must preserve the unmet proof criteria")
        return self


def _refs(references: list[EvidenceRef]) -> list[dict[str, str]]:
    return [asdict(reference) for reference in references]


def _stable_id(kind: str, *parts: object) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return f"{kind}-{hashlib.sha256(encoded.encode()).hexdigest()[:20]}"


class FindingMemory:
    def __init__(self, log: ProjectMemoryLog):
        self.log = log

    def propose(self, proposal: FindingProposal, *, origin_worker_id: str,
                origin_session_id: str, origin_model_profile: str) -> bool:
        memory = self.log.replay()
        hypothesis = memory.hypotheses.get(proposal.hypothesis_id)
        if hypothesis is None:
            raise ValueError("finding must cite a canonical hypothesis")
        if proposal.scope != hypothesis.get("scope"):
            raise ValueError("finding scope must match its hypothesis scope")
        policy = proof_policy(proposal.finding_type, proposal.consequence)
        proposal_identity = {
            "proposal": proposal.model_dump(mode="json"),
            "originWorkerId": origin_worker_id,
            "originSessionId": origin_session_id,
            "originModelProfile": origin_model_profile,
        }
        proposal_digest = hashlib.sha256(json.dumps(
            proposal_identity, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        payload = {
            "id": proposal.id,
            "schemaVersion": proposal.schema_version,
            "hypothesisId": proposal.hypothesis_id,
            "scope": proposal.scope,
            "observations": [item.model_dump(mode="json") for item in proposal.observations],
            "affectedComponent": proposal.affected_component,
            "impactClaim": proposal.impact_claim,
            "assumptions": proposal.assumptions,
            "knownUncertainty": proposal.known_uncertainty,
            "findingType": proposal.finding_type,
            "consequence": proposal.consequence,
            "confidence": proposal.confidence,
            "proofPolicy": policy.model_dump(mode="json"),
            "recipe": proposal.recipe.model_dump(mode="json"),
            "status": "candidate",
            "originWorkerId": origin_worker_id,
            "originSessionId": origin_session_id,
            "originModelProfile": origin_model_profile,
            "proposalDigest": proposal_digest,
            "references": _refs(proposal.references),
        }
        current = memory.findings.get(proposal.id)
        if current is not None:
            current_digest = current.get("proposalDigest")
            if current_digest is not None and current_digest != proposal_digest:
                raise ValueError("finding id is already bound to a different proposal")
            if current_digest is None:
                immutable_keys = {
                    "hypothesisId", "scope", "observations", "affectedComponent",
                    "impactClaim", "assumptions", "knownUncertainty", "findingType",
                    "proofPolicy", "recipe", "originWorkerId", "originSessionId",
                    "originModelProfile", "references",
                }
                if any(current.get(key) != payload[key] for key in immutable_keys):
                    raise ValueError("finding id is already bound to a different proposal")
            return False
        self.log.append(MemoryAction(
            "finding_upserted", payload,
            action_id=f"finding:{proposal.id}:candidate",
        ))
        return True

    def transition(self, update: FindingTransition) -> None:
        memory = self.log.replay()
        current = memory.findings.get(update.finding_id)
        if current is None:
            raise KeyError(update.finding_id)
        previous = current["status"]
        if update.to_status == previous:
            return
        if update.to_status not in ALLOWED_TRANSITIONS[previous]:
            raise ValueError(f"invalid finding transition {previous} -> {update.to_status}")
        refs = _refs(update.references)
        transition_id = _stable_id(
            "finding-transition", update.finding_id, previous, update.to_status,
            update.reason, update.confidence, update.consequence, refs,
        )
        self.log.append(MemoryAction("finding_transition_recorded", {
            "id": transition_id,
            "findingId": update.finding_id,
            "fromStatus": previous,
            "toStatus": update.to_status,
            "reason": update.reason,
            "previousConfidence": current["confidence"],
            "nextConfidence": (update.confidence if update.confidence is not None
                               else current["confidence"]),
            "previousConsequence": current["consequence"],
            "nextConsequence": update.consequence or current["consequence"],
            "references": refs,
        }, action_id=transition_id))
        payload = {key: value for key, value in current.items() if not key.startswith("_")}
        payload.update({"status": update.to_status, "lastTransitionId": transition_id})
        if update.confidence is not None:
            payload["confidence"] = update.confidence
        if update.consequence is not None:
            payload["consequence"] = update.consequence
        self.log.append(MemoryAction(
            "finding_upserted", payload,
            action_id=f"finding:{update.finding_id}:{transition_id}",
        ))

    def mark_validation_pending(self, finding_id: str) -> None:
        current = self.log.replay().findings[finding_id]
        refs = [EvidenceRef(**item) for item in current["references"]]
        if current["status"] in {"candidate", "inconclusive"}:
            self.transition(FindingTransition(
                finding_id=finding_id, to_status="demonstrated",
                reason="Cited observations and a bounded reproduction recipe passed validation",
                references=refs,
            ))
        self.transition(FindingTransition(
            finding_id=finding_id, to_status="reproduction-pending",
            reason="Independent clean reproduction entered the durable scheduler",
            references=refs,
        ))

    def record_attempt(self, attempt: ReproductionAttempt) -> None:
        memory = self.log.replay()
        finding = memory.findings.get(attempt.finding_id)
        if finding is None:
            raise KeyError(attempt.finding_id)
        if attempt.recipe_id != finding["recipe"]["id"]:
            raise ValueError("attempt does not use the finding's recorded recipe")
        payload = attempt.model_dump(mode="json")
        payload["findingId"] = payload.pop("finding_id")
        payload["recipeId"] = payload.pop("recipe_id")
        payload["workerId"] = payload.pop("worker_id")
        payload["sessionId"] = payload.pop("session_id")
        payload["environmentalDifferences"] = payload.pop("environmental_differences")
        payload["environment"] = {
            "id": payload.pop("environment_id"),
            "clean": payload.pop("clean_environment"),
            "platform": payload.pop("platform"),
            "architecture": payload.pop("architecture"),
            "isolation": payload.pop("isolation"),
        }
        payload["modelProfile"] = payload.pop("model_profile")
        payload["references"] = _refs(attempt.references)
        payload["schemaVersion"] = payload.pop("schema_version")
        self.log.append(MemoryAction(
            "finding_attempt_recorded", payload,
            action_id=f"finding-attempt:{attempt.finding_id}:{attempt.id}",
        ))
        current = self.log.replay().findings[attempt.finding_id]
        if attempt.outcome == "success" and self._proof_satisfied(current):
            self.transition(FindingTransition(
                finding_id=attempt.finding_id, to_status="reproduced",
                reason="Consequence-sensitive proof policy satisfied by clean reproduction",
                references=attempt.references,
            ))
        elif attempt.outcome in {"negative", "flaky", "contradictory", "inconclusive"}:
            self.transition(FindingTransition(
                finding_id=attempt.finding_id, to_status="inconclusive",
                reason=f"Independent reproduction was {attempt.outcome}",
                references=attempt.references,
            ))

    def _proof_satisfied(self, finding: dict) -> bool:
        policy = ProofPolicy.model_validate(finding["proofPolicy"])
        return self.qualifying_reproduction_count(finding["id"]) \
               >= policy.successful_clean_reproductions

    def qualifying_reproduction_count(self, finding_id: str) -> int:
        """Count distinct successes that satisfy the finding's persisted proof policy."""
        memory = self.log.replay()
        finding = memory.findings.get(finding_id)
        if finding is None:
            raise KeyError(finding_id)
        policy = ProofPolicy.model_validate(finding["proofPolicy"])
        attempts = [
            item for item in memory.finding_attempts.values()
            if item["findingId"] == finding["id"] and item["outcome"] == "success"
        ]
        qualifying = []
        for attempt in attempts:
            if policy.require_clean_environment and not attempt["environment"]["clean"]:
                continue
            if policy.require_independent_worker and attempt["workerId"] == finding["originWorkerId"]:
                continue
            if policy.require_independent_session and attempt["sessionId"] == finding["originSessionId"]:
                continue
            if (policy.require_distinct_model_profile
                    and attempt["modelProfile"] == finding["originModelProfile"]):
                continue
            qualifying.append(attempt)
        identities = {
            (item["workerId"], item["sessionId"], item["environment"]["id"])
            for item in qualifying
        }
        return len(identities)

    def decide(self, decision: OperatorFindingDecision) -> None:
        current = self.log.replay().findings.get(decision.finding_id)
        if current is None:
            raise KeyError(decision.finding_id)
        if decision.decision == "accepted" and current["status"] != "reproduced":
            raise ValueError("operator acceptance requires a technically reproduced finding")
        refs = _refs(decision.references)
        decision_id = _stable_id(
            "finding-decision", decision.finding_id, decision.decision,
            decision.rationale, decision.unmet_criteria, refs,
        )
        self.log.append(MemoryAction("finding_operator_decision_recorded", {
            "id": decision_id,
            "findingId": decision.finding_id,
            "decision": decision.decision,
            "rationale": decision.rationale,
            "unmetCriteria": decision.unmet_criteria,
            "references": refs,
        }, action_id=decision_id))
        if decision.decision == "rejected" and current["status"] != "rejected":
            self.transition(FindingTransition(
                finding_id=decision.finding_id, to_status="rejected",
                reason=decision.rationale, references=decision.references,
            ))


def finding_snapshot(memory: ProjectMemory) -> dict:
    decisions_by_finding: dict[str, dict] = {}
    for decision in sorted(memory.finding_operator_decisions.values(),
                           key=lambda item: item["_eventSeq"]):
        decisions_by_finding[decision["findingId"]] = decision
    findings = []
    for identifier in sorted(memory.findings):
        item = memory.findings[identifier]
        decision = decisions_by_finding.get(identifier)
        lifecycle = (
            "operator-accepted"
            if item["status"] == "reproduced" and decision and decision["decision"] == "accepted"
            else item["status"]
        )
        findings.append({**item, "lifecycleStatus": lifecycle,
                         "operatorDecision": decision})
    return {
        "findings": findings,
        "attempts": [memory.finding_attempts[key] for key in sorted(memory.finding_attempts)],
        "transitions": [memory.finding_transitions[key]
                        for key in sorted(memory.finding_transitions)],
        "operatorDecisions": [memory.finding_operator_decisions[key]
                              for key in sorted(memory.finding_operator_decisions)],
        "validated": [item for item in findings if item["status"] == "reproduced"],
    }
