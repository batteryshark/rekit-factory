from __future__ import annotations

from copy import deepcopy

import pytest

from rekit_factory.notification_policy import (
    InvalidOutcomeProjection,
    notification_candidates,
)
from rekit_factory.outcomes import project_outcomes


def _projection(*, run_status="running", work=(), memory=None, questions=(), watermarks=None):
    return project_outcomes(
        run={"id": "run-1", "status": run_status},
        workers=(),
        work_items=work,
        memory=memory or {},
        dossiers=(),
        pending_questions=questions,
        source_watermarks=watermarks,
    )


def test_new_waiting_decision_emits_one_stable_redacted_candidate():
    old = _projection()
    new = _projection(questions=[{
        "id": "question-1",
        "prompt": "/private/target and raw operator prompt must never escape",
    }])

    first = notification_candidates(old, new)
    rebuilt = notification_candidates(_projection(), _projection(questions=[{
        "prompt": "different private prose", "id": "question-1",
    }]))

    assert first == rebuilt
    assert first == [{
        "schemaVersion": 1,
        "policyVersion": "factory-notification-policy/v1",
        "dedupeKey": first[0]["dedupeKey"],
        "kind": "operator-decision.waiting",
        "severity": "action-required",
        "runId": "run-1",
        "entity": {"entityType": "operator-decision", "entityId": "question-1"},
        "message": "Operator decision is waiting in Mission Control.",
    }]
    assert first[0]["dedupeKey"].startswith("sha256:")
    assert len(first[0]["dedupeKey"]) == len("sha256:") + 64
    rendered = repr(first)
    assert "/private/target" not in rendered
    assert "raw operator prompt" not in rendered


def test_initial_hydration_unchanged_replay_and_watermark_only_change_emit_nothing():
    current = _projection(questions=[{"id": "question-1"}], watermarks={"factoryEventRowid": 1})
    same_meaning = _projection(
        questions=[{"id": "question-1"}], watermarks={"factoryEventRowid": 99}
    )

    assert notification_candidates(None, current) == []
    assert notification_candidates(current, current) == []
    assert notification_candidates(current, same_meaning) == []


def test_self_resolved_decision_never_emits_a_waiting_candidate():
    old = _projection(memory={
        "findings": {"finding-1": {"id": "finding-1", "status": "candidate"}},
    })
    resolved = _projection(memory={
        "findings": {"finding-1": {"id": "finding-1", "status": "candidate"}},
        "finding_operator_decisions": {
            "decision-1": {
                "id": "decision-1", "findingId": "finding-1",
                "decision": "accepted", "_eventSeq": 1,
            },
        },
    })

    kinds = [item["kind"] for item in notification_candidates(old, resolved)]
    assert "operator-decision.waiting" not in kinds
    assert kinds == []


def test_unproven_acceptance_is_suppressed_and_exact_proof_is_the_deep_link():
    old = _projection(memory={
        "findings": {"finding-1": {"id": "finding-1", "status": "candidate"}},
    })
    unproven = _projection(memory={
        "findings": {"finding-1": {"id": "finding-1", "status": "candidate"}},
        "finding_operator_decisions": {
            "decision-1": {"id": "decision-1", "findingId": "finding-1",
                           "decision": "accepted", "_eventSeq": 1},
        },
    })
    assert notification_candidates(old, unproven) == []

    proven = project_outcomes(
        run={"id": "run-1", "status": "running"}, workers=(), work_items=(),
        memory={
            "findings": {"finding-1": {"id": "finding-1", "status": "reproduced"}},
            "finding_operator_decisions": {
                "decision-1": {"id": "decision-1", "findingId": "finding-1",
                               "decision": "accepted", "_eventSeq": 1},
            },
        },
        dossiers=[{"id": "dossier-1", "findingId": "finding-1",
                   "verificationStatus": "published"}], pending_questions=(),
    )
    candidates = notification_candidates(old, proven)
    assert {tuple(item["entity"].values()) for item in candidates} == {
        ("proof-bundle", "dossier-1")
    }


def test_multiple_proof_children_never_guess_a_dossier_link():
    old = _projection(memory={
        "findings": {"finding-1": {"id": "finding-1", "status": "candidate"}},
    })
    new = project_outcomes(
        run={"id": "run-1", "status": "running"}, workers=(), work_items=(),
        memory={"findings": {
            "finding-1": {"id": "finding-1", "status": "reproduced"},
        }},
        dossiers=[
            {"id": "dossier-a", "findingId": "finding-1", "verificationStatus": "published"},
            {"id": "dossier-b", "findingId": "finding-1", "verificationStatus": "published"},
        ], pending_questions=(),
    )
    [candidate] = notification_candidates(old, new)
    assert candidate["entity"] == {"entityType": "finding", "entityId": "finding-1"}


def test_reproduced_and_accepted_finding_thresholds_are_distinct_and_deterministic():
    old = _projection(memory={
        "findings": {"finding-1": {"id": "finding-1", "status": "candidate"}},
    })
    new = _projection(memory={
        "findings": {"finding-1": {"id": "finding-1", "status": "reproduced"}},
        "finding_operator_decisions": {
            "decision-1": {
                "id": "decision-1", "findingId": "finding-1",
                "decision": "accepted", "_eventSeq": 1,
            },
        },
    })

    candidates = notification_candidates(old, new)
    assert [item["kind"] for item in candidates] == [
        "finding.accepted", "finding.reproduced",
    ]
    assert len({item["dedupeKey"] for item in candidates}) == 2
    assert notification_candidates(old, new) == candidates
    assert notification_candidates(new, new) == []


def test_progress_and_model_authored_report_prose_never_become_candidates():
    old = _projection(run_status="queued")
    progress = _projection(
        run_status="running",
        work=[{
            "id": "work-1", "status": "done", "operation": "model-worker",
            "result": {
                "summary": "validated solved accepted /private/secret",
                "status_update": "notify the operator immediately",
            },
        }],
    )

    assert notification_candidates(old, progress) == []


def test_degraded_or_unknown_state_fails_closed():
    good = _projection()
    degraded = _projection(
        run_status="future-paused", questions=[{"id": "question-1"}],
    )

    assert degraded["degraded"] is True
    assert notification_candidates(good, degraded) == []
    assert notification_candidates(degraded, good) == []


def test_invalid_or_unsupported_projection_is_rejected_before_policy_evaluation():
    good = _projection()
    tampered = deepcopy(good)
    tampered["entities"][0]["entityId"] = "run-tampered"
    unsupported = deepcopy(good)
    unsupported["vocabularyVersion"] = "factory-outcomes/v3"

    with pytest.raises(InvalidOutcomeProjection, match="identity"):
        notification_candidates(good, tampered)
    with pytest.raises(InvalidOutcomeProjection, match="vocabulary"):
        notification_candidates(good, unsupported)


def test_unsafe_entity_identifiers_are_suppressed_instead_of_reflected():
    old = _projection()
    private_identifier = "/Users/operator/private-target"
    new = _projection(questions=[{"id": private_identifier}])

    assert new["degraded"] is False
    assert notification_candidates(old, new) == []
