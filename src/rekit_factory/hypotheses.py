"""Typed hypothesis and discriminating-test contracts over project memory."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from rekit_factory.memory import EvidenceRef, MemoryAction, ProjectMemory, ProjectMemoryLog


HypothesisStatus = Literal[
    "proposed", "queued", "testing", "supported", "contradicted", "disproved",
    "reproduced", "retired", "blocked",
]
ALLOWED_TRANSITIONS = {
    "proposed": {"queued", "retired", "blocked"},
    "queued": {"testing", "retired", "blocked"},
    "testing": {"supported", "contradicted", "disproved", "reproduced", "blocked", "queued"},
    "supported": {"testing", "reproduced", "contradicted", "retired"},
    "contradicted": {"testing", "disproved", "retired", "blocked"},
    "disproved": {"retired"}, "reproduced": {"retired"},
    "blocked": {"queued", "retired"}, "retired": set(),
}
EVIDENCE_STATUSES = {"supported", "contradicted", "disproved", "reproduced"}


class StopCondition(BaseModel):
    max_attempts: int = Field(default=2, ge=1, le=10)
    max_cost_units: int = Field(default=30, ge=1, le=10_000)


class DiscriminatingTestProposal(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=2_000)
    scope: str = "target"
    method: str = Field(min_length=1, max_length=1_000)
    expected_observation: str = Field(min_length=1, max_length=2_000)
    falsifying_observation: str = Field(min_length=1, max_length=2_000)
    information_gain: int = Field(ge=0, le=100)
    risk: int = Field(ge=0, le=10)
    cost_units: int = Field(ge=1, le=1_000)
    prerequisites: list[str] = Field(default_factory=list)
    authorization: Literal["automatic", "operator"] = "automatic"
    approved: bool = True


class HypothesisProposal(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    claim: str = Field(min_length=1, max_length=4_000)
    scope: str = "target"
    expected_observation: str = Field(min_length=1, max_length=2_000)
    falsifier: str = Field(min_length=1, max_length=2_000)
    confidence: float = Field(ge=0, le=1)
    references: list[EvidenceRef] = Field(min_length=1)
    owner_workstream: str | None = None
    stop_condition: StopCondition = Field(default_factory=StopCondition)
    proposed_test: DiscriminatingTestProposal
    competing_with: list[str] = Field(default_factory=list)
    refines: str | None = None

    @model_validator(mode="after")
    def scope_matches_test(self):
        if self.proposed_test.scope != self.scope:
            raise ValueError("hypothesis and test scope must match")
        if self.proposed_test.cost_units > self.stop_condition.max_cost_units:
            raise ValueError("proposed test exceeds hypothesis cost stop condition")
        return self


class HypothesisUpdate(BaseModel):
    hypothesis_id: str
    test_id: str
    status: HypothesisStatus
    confidence: float = Field(ge=0, le=1)
    observations: list[str] = Field(default_factory=list)
    references: list[EvidenceRef] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def evidence_for_conclusion(self):
        if self.status in EVIDENCE_STATUSES and (not self.observations or not self.references):
            raise ValueError(f"{self.status} requires cited observations")
        return self


def semantic_key(claim: str, scope: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", claim.lower()).strip()
    return hashlib.sha256(f"{scope}\x1f{normalized}".encode()).hexdigest()[:20]


def test_priority(proposal: HypothesisProposal) -> int:
    test = proposal.proposed_test
    competition = len(set(proposal.competing_with))
    return 100 + test.information_gain * 5 + competition * 20 - test.cost_units - test.risk * 10


class HypothesisMemory:
    def __init__(self, log: ProjectMemoryLog):
        self.log = log

    def propose(self, proposal: HypothesisProposal) -> bool:
        memory = self.log.replay()
        key = semantic_key(proposal.claim, proposal.scope)
        duplicate = next((item for item in memory.hypotheses.values()
                          if item.get("semanticKey") == key), None)
        if duplicate is not None:
            return False
        if proposal.refines:
            prior = memory.hypotheses.get(proposal.refines)
            if prior is None or prior.get("semanticKey") == key:
                raise ValueError("refinement must name an existing materially different hypothesis")
        refs = [asdict(reference) for reference in proposal.references]
        self.log.append(MemoryAction("hypothesis_upserted", {
            "id": proposal.id, "claim": proposal.claim, "scope": proposal.scope,
            "expectedObservation": proposal.expected_observation,
            "falsifier": proposal.falsifier, "confidence": proposal.confidence,
            "status": "proposed", "ownerWorkstream": proposal.owner_workstream,
            "stopCondition": proposal.stop_condition.model_dump(mode="json"),
            "semanticKey": key, "competingWith": sorted(set(proposal.competing_with)),
            "refines": proposal.refines, "references": refs,
        }, action_id=f"hypothesis:{proposal.id}:proposed"))
        test = proposal.proposed_test
        self.log.append(MemoryAction("hypothesis_test_upserted", {
            **test.model_dump(mode="json"), "hypothesisId": proposal.id,
            "status": "proposed", "attempts": 0,
            "priority": test_priority(proposal), "references": refs,
        }, action_id=f"hypothesis-test:{proposal.id}:{test.id}:proposed"))
        return True

    def transition(self, update: HypothesisUpdate) -> None:
        memory = self.log.replay()
        current = memory.hypotheses.get(update.hypothesis_id)
        if current is None:
            raise KeyError(update.hypothesis_id)
        previous = current["status"]
        if update.status == previous:
            return
        if update.status not in ALLOWED_TRANSITIONS[previous]:
            raise ValueError(f"invalid hypothesis transition {previous} -> {update.status}")
        references = [asdict(reference) for reference in update.references]
        if update.status in EVIDENCE_STATUSES and (not update.observations or not references):
            raise ValueError(f"{update.status} requires cited observations")
        if not references:
            references = list(current.get("references", []))
        self.log.append(MemoryAction("hypothesis_upserted", {
            **{key: value for key, value in current.items() if not key.startswith("_")},
            "status": update.status, "confidence": update.confidence,
            "lastReason": update.reason, "lastTestId": update.test_id,
            "references": references,
        }, action_id=f"hypothesis:{update.hypothesis_id}:{update.test_id}:{update.status}"))
        if update.status in EVIDENCE_STATUSES or update.observations:
            self.log.append(MemoryAction("hypothesis_observation_recorded", {
                "id": f"observation-{update.hypothesis_id}-{update.test_id}-{update.status}",
                "hypothesisId": update.hypothesis_id, "testId": update.test_id,
                "outcome": update.status, "observations": update.observations,
                "reason": update.reason, "references": references,
            }, action_id=f"hypothesis-observation:{update.hypothesis_id}:{update.test_id}:{update.status}"))
            self.update_test(
                update.test_id, "completed", references=references, outcome=update.status
            )

    def mark_scheduled(self, hypothesis_id: str, test_id: str) -> None:
        current = self.log.replay().hypotheses[hypothesis_id]
        self.transition(HypothesisUpdate(
            hypothesis_id=hypothesis_id, test_id=test_id, status="queued",
            confidence=float(current["confidence"]),
            reason="Approved discriminating test entered the durable scheduler",
        ))
        self.update_test(test_id, "queued")

    def update_test(self, test_id: str, status: str, *, references: list[dict] | None = None,
                    outcome: str | None = None, increment_attempt: bool = False) -> None:
        memory = self.log.replay()
        current = memory.hypothesis_tests.get(test_id)
        if current is None:
            raise KeyError(test_id)
        payload = {key: value for key, value in current.items() if not key.startswith("_")}
        payload["status"] = status
        payload["attempts"] = int(payload.get("attempts", 0)) + int(increment_attempt)
        if references:
            payload["references"] = references
        if outcome:
            payload["outcome"] = outcome
        self.log.append(MemoryAction(
            "hypothesis_test_upserted", payload,
            action_id=f"hypothesis-test:{payload['hypothesisId']}:{test_id}:{status}:{payload['attempts']}",
        ))


def hypothesis_snapshot(memory: ProjectMemory) -> dict:
    return {
        "hypotheses": [memory.hypotheses[key] for key in sorted(memory.hypotheses)],
        "tests": sorted(memory.hypothesis_tests.values(),
                        key=lambda item: (-int(item.get("priority", 0)), item["id"])),
        "observations": [memory.hypothesis_observations[key]
                         for key in sorted(memory.hypothesis_observations)],
    }
