from pathlib import Path
import tempfile

import pytest

from rekit_factory.hypotheses import (
    DiscriminatingTestProposal,
    HypothesisMemory,
    HypothesisProposal,
    HypothesisUpdate,
    StopCondition,
    semantic_key,
    test_priority as priority_for_test,
)
from rekit_factory.memory import EvidenceRef, ProjectMemoryLog
from rekit_factory.memory import memory_context


def proposal(identifier="h-a", claim="The table controls validation", **changes):
    values = dict(
        id=identifier, claim=claim, scope="target",
        expected_observation="Changing table index changes verdict",
        falsifier="Verdict is independent of indexed reads", confidence=0.45,
        references=[EvidenceRef("artifact", "sha256:seed")], owner_workstream="ws-validation",
        stop_condition=StopCondition(max_attempts=2, max_cost_units=20),
        proposed_test=DiscriminatingTestProposal(
            id=f"test-{identifier}", objective="Trace indexed reads", method="bounded static slice",
            expected_observation="Indexed value reaches verdict",
            falsifying_observation="No indexed value reaches verdict",
            information_gain=90, risk=1, cost_units=10,
        ),
        competing_with=["h-b"],
    )
    values.update(changes)
    return HypothesisProposal(**values)


def test_competing_hypotheses_replay_independently_and_require_evidence_for_conclusions():
    with tempfile.TemporaryDirectory() as tmp:
        hypotheses = HypothesisMemory(ProjectMemoryLog(tmp))
        assert hypotheses.propose(proposal())
        assert hypotheses.propose(proposal("h-b", "The checksum controls validation"))
        hypotheses.mark_scheduled("h-a", "test-h-a")
        hypotheses.transition(HypothesisUpdate(
            hypothesis_id="h-a", test_id="test-h-a", status="testing", confidence=.45,
            reason="leased",
        ))
        with pytest.raises(ValueError, match="cited observations"):
            HypothesisUpdate(
                hypothesis_id="h-a", test_id="test-h-a", status="supported",
                confidence=.8, reason="model assertion",
            )
        hypotheses.transition(HypothesisUpdate(
            hypothesis_id="h-a", test_id="test-h-a", status="supported", confidence=.8,
            observations=["Indexed value reaches verdict"],
            references=[EvidenceRef("artifact", "sha256:test-a")], reason="test matched",
        ))
        memory = ProjectMemoryLog(tmp).replay()
        assert memory.hypotheses["h-a"]["status"] == "supported"
        assert memory.hypotheses["h-b"]["status"] == "proposed"
        assert memory.hypothesis_tests["test-h-a"]["status"] == "completed"
        assert len(memory.hypothesis_observations) == 1


def test_disproved_duplicate_is_suppressed_but_material_refinement_is_allowed():
    with tempfile.TemporaryDirectory() as tmp:
        hypotheses = HypothesisMemory(ProjectMemoryLog(tmp))
        hypotheses.propose(proposal())
        hypotheses.mark_scheduled("h-a", "test-h-a")
        hypotheses.transition(HypothesisUpdate(
            hypothesis_id="h-a", test_id="test-h-a", status="testing", confidence=.45,
            reason="leased",
        ))
        hypotheses.transition(HypothesisUpdate(
            hypothesis_id="h-a", test_id="test-h-a", status="disproved", confidence=.05,
            observations=["No indexed value reaches verdict"],
            references=[EvidenceRef("artifact", "sha256:negative")], reason="falsifier observed",
        ))
        assert not hypotheses.propose(proposal("h-duplicate"))
        revised = proposal(
            "h-revised", "The table controls validation only in compatibility mode",
            refines="h-a", references=[EvidenceRef("artifact", "sha256:new-mode-evidence")],
        )
        assert hypotheses.propose(revised)
        memory = ProjectMemoryLog(tmp).replay()
        assert memory.hypotheses["h-a"]["status"] == "disproved"
        assert memory.hypotheses["h-revised"]["refines"] == "h-a"
        assert semantic_key(revised.claim, revised.scope) != memory.hypotheses["h-a"]["semanticKey"]
        context = memory_context(memory)
        assert "HYPOTHESIS NEGATIVE" in context
        assert "No indexed value reaches verdict" in context


def test_priority_rewards_discrimination_and_competition_but_penalizes_risk_and_cost():
    strong = proposal()
    weak = proposal(
        "h-weak", "Weak explanation", competing_with=[],
        proposed_test=DiscriminatingTestProposal(
            id="test-weak", objective="Broad fuzz", method="fuzz",
            expected_observation="Maybe crashes", falsifying_observation="No crash",
            information_gain=20, risk=8, cost_units=20,
        ),
    )
    assert priority_for_test(strong) > priority_for_test(weak)


def test_scope_and_cost_stop_conditions_are_validated_before_scheduling():
    with pytest.raises(ValueError, match="scope must match"):
        proposal(proposed_test=DiscriminatingTestProposal(
            id="bad", objective="scan other target", scope="other", method="scan",
            expected_observation="x", falsifying_observation="y",
            information_gain=10, risk=1, cost_units=1,
        ))
    with pytest.raises(ValueError, match="cost stop condition"):
        proposal(proposed_test=DiscriminatingTestProposal(
            id="costly", objective="costly", method="scan", expected_observation="x",
            falsifying_observation="y", information_gain=10, risk=1, cost_units=21,
        ))
