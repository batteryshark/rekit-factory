from __future__ import annotations

import tempfile

import pytest

from rekit_factory.findings import (
    FindingMemory,
    FindingProposal,
    FindingTransition,
    ObservationEvidence,
    OperatorFindingDecision,
    ReproductionAttempt,
    ReproductionRecipe,
    finding_snapshot,
)
from rekit_factory.hypotheses import DiscriminatingTestProposal, HypothesisMemory, HypothesisProposal
from rekit_factory.memory import EvidenceRef, ProjectMemoryLog


def _ref(identifier: str) -> EvidenceRef:
    return EvidenceRef("artifact", f"sha256:{identifier}")


def _finding_memory(tmp: str) -> FindingMemory:
    log = ProjectMemoryLog(tmp)
    HypothesisMemory(log).propose(HypothesisProposal(
        id="h-parser", claim="A length field controls the parser", scope="target",
        expected_observation="Changing the length changes parser bounds",
        falsifier="The field never reaches a bounds calculation", confidence=.7,
        references=[_ref("hypothesis")],
        proposed_test=DiscriminatingTestProposal(
            id="test-h-parser", objective="Trace the length", method="static and fixture trace",
            expected_observation="Length reaches allocation",
            falsifying_observation="Length is ignored", information_gain=90, risk=0,
            cost_units=5,
        ),
    ))
    return FindingMemory(log)


def _proposal(*, finding_type="vulnerability", consequence="high") -> FindingProposal:
    observation = _ref("observation")
    fixture = _ref("fixture")
    return FindingProposal(
        id="f-length", hypothesis_id="h-parser", scope="target",
        observations=[ObservationEvidence(
            observation="Length reaches allocation without an upper bound",
            references=[observation],
        )],
        affected_component="record parser", impact_claim="A crafted record causes over-allocation",
        assumptions=["The record reaches the parser"],
        known_uncertainty="Allocator limits may prevent practical exhaustion",
        finding_type=finding_type, consequence=consequence, confidence=.72,
        references=[observation, fixture],
        recipe=ReproductionRecipe(
            id="recipe-length-v1",
            steps=["Build the fixture parser", "Supply staged oversized record", "Record allocation"],
            staged_inputs=[fixture], expected_observation="Allocation exceeds the input size limit",
            clean_environment_requirements=["fresh build", "no prior process state"],
        ),
    )


def _attempt(identifier: str, *, outcome="success", worker="validator-1",
             session="session:validator-1", environment="clean:one",
             clean=True, profile="fixture") -> ReproductionAttempt:
    return ReproductionAttempt(
        id=identifier, finding_id="f-length", recipe_id="recipe-length-v1",
        outcome=outcome, worker_id=worker, session_id=session,
        environment_id=environment, clean_environment=clean, model_profile=profile,
        observations=[f"observable outcome: {outcome}"], references=[_ref(identifier)],
    )


def test_candidate_is_excluded_until_clean_independent_reproduction():
    with tempfile.TemporaryDirectory() as tmp:
        findings = _finding_memory(tmp)
        findings.propose(
            _proposal(), origin_worker_id="origin", origin_session_id="session:origin",
            origin_model_profile="fixture",
        )
        findings.mark_validation_pending("f-length")
        assert finding_snapshot(findings.log.replay())["validated"] == []

        # A clean success from the originator is retained as evidence but cannot satisfy policy.
        findings.record_attempt(_attempt(
            "origin-reuse", worker="origin", session="session:origin",
        ))
        assert findings.log.replay().findings["f-length"]["status"] == "reproduction-pending"

        findings.record_attempt(_attempt("independent-success"))
        state = finding_snapshot(findings.log.replay())
        assert state["findings"][0]["status"] == "reproduced"
        assert [item["id"] for item in state["validated"]] == ["f-length"]
        assert len(state["attempts"]) == 2


def test_false_positive_records_contradiction_and_never_enters_validated_projection():
    with tempfile.TemporaryDirectory() as tmp:
        findings = _finding_memory(tmp)
        findings.propose(
            _proposal(), origin_worker_id="origin", origin_session_id="session:origin",
            origin_model_profile="fixture",
        )
        findings.mark_validation_pending("f-length")
        findings.record_attempt(_attempt("contradiction", outcome="contradictory"))
        state = finding_snapshot(findings.log.replay())
        assert state["findings"][0]["status"] == "inconclusive"
        assert state["attempts"][0]["outcome"] == "contradictory"
        assert state["validated"] == []


def test_destructive_or_critical_claim_requires_two_distinct_clean_reproductions():
    with tempfile.TemporaryDirectory() as tmp:
        findings = _finding_memory(tmp)
        findings.propose(
            _proposal(finding_type="destructive-impact", consequence="critical"),
            origin_worker_id="origin", origin_session_id="session:origin",
            origin_model_profile="fixture",
        )
        findings.mark_validation_pending("f-length")
        findings.record_attempt(_attempt("clean-one"))
        assert findings.log.replay().findings["f-length"]["status"] == "reproduction-pending"
        findings.record_attempt(_attempt(
            "clean-two", worker="validator-2", session="session:validator-2",
            environment="clean:two",
        ))
        assert findings.log.replay().findings["f-length"]["status"] == "reproduced"


def test_acceptance_is_separate_and_downgrade_withdrawal_preserve_provenance():
    with tempfile.TemporaryDirectory() as tmp:
        findings = _finding_memory(tmp)
        findings.propose(
            _proposal(), origin_worker_id="origin", origin_session_id="session:origin",
            origin_model_profile="fixture",
        )
        findings.mark_validation_pending("f-length")
        with pytest.raises(ValueError, match="technically reproduced"):
            findings.decide(OperatorFindingDecision(
                finding_id="f-length", decision="accepted", rationale="sounds convincing",
                references=[_ref("operator")],
            ))
        findings.record_attempt(_attempt("clean-success"))
        findings.decide(OperatorFindingDecision(
            finding_id="f-length", decision="accepted", rationale="reviewed reproduced proof",
            references=[_ref("operator")],
        ))
        assert finding_snapshot(findings.log.replay())["findings"][0]["lifecycleStatus"] \
               == "operator-accepted"

        findings.transition(FindingTransition(
            finding_id="f-length", to_status="demonstrated",
            reason="New allocator evidence reduces confidence",
            references=[_ref("downgrade")],
            confidence=.35, consequence="medium",
        ))
        findings.transition(FindingTransition(
            finding_id="f-length", to_status="withdrawn",
            reason="Impact claim no longer follows from the retained observations",
            references=[_ref("withdrawal")],
        ))
        state = finding_snapshot(findings.log.replay())
        assert state["validated"] == []
        assert state["findings"][0]["status"] == "withdrawn"
        assert state["findings"][0]["confidence"] == .35
        assert state["findings"][0]["consequence"] == "medium"
        assert len(state["attempts"]) == 1
        assert len(state["operatorDecisions"]) == 1
        assert {item["toStatus"] for item in state["transitions"]} >= {
            "reproduced", "demonstrated", "withdrawn",
        }
        downgrade = next(item for item in state["transitions"]
                         if item["fromStatus"] == "reproduced"
                         and item["toStatus"] == "demonstrated")
        assert downgrade["previousConfidence"] == .72
        assert downgrade["nextConfidence"] == .35


def test_waiver_preserves_unmet_criterion_and_never_relabels_candidate_reproduced():
    with tempfile.TemporaryDirectory() as tmp:
        findings = _finding_memory(tmp)
        findings.propose(
            _proposal(), origin_worker_id="origin", origin_session_id="session:origin",
            origin_model_profile="fixture",
        )
        with pytest.raises(ValueError, match="unmet proof criteria"):
            OperatorFindingDecision(
                finding_id="f-length", decision="waived", rationale="time constrained",
                references=[_ref("operator")],
            )
        findings.decide(OperatorFindingDecision(
            finding_id="f-length", decision="waived", rationale="time constrained",
            unmet_criteria=["independent clean reproduction not performed"],
            references=[_ref("operator")],
        ))
        state = finding_snapshot(findings.log.replay())
        assert state["findings"][0]["status"] == "candidate"
        assert state["validated"] == []
        assert state["operatorDecisions"][0]["unmetCriteria"]
