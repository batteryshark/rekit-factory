from dataclasses import FrozenInstanceError

import pytest

from rekit_factory.promotion_contracts import EvidenceReference, PromotionCandidate


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64


def _ref(owner="factory", record_id="run-1", digest=SHA_A):
    return EvidenceReference(owner, record_id, digest)


def _candidate(**overrides):
    values = {
        "capability_kind": "behavioral-skill",
        "capability_name": "bounded-symbol-recovery",
        "capability_version": "1.0.0",
        "capability_content_digest": SHA_C,
        "origin_runs": (_ref(),),
        "proof_bundles": (_ref("factory", "dossier-1", SHA_B),),
        "scope_ids": ("native:offline",),
        "prerequisite_ids": ("rekit:ghidra",),
        "risk_ids": ("risk:false-positive",),
    }
    values.update(overrides)
    return PromotionCandidate(**values)


def test_candidate_is_immutable_content_addressed_and_round_trips_exactly():
    candidate = _candidate(evaluation_results=(
        _ref("reversebench", "result-1", SHA_C),
    ))
    record = candidate.to_record()

    assert PromotionCandidate.from_record(record) == candidate
    assert record["candidateId"].startswith("promotion-")
    assert record["subjectHash"].startswith("sha256:")
    assert record["recordHash"].startswith("sha256:")
    assert "eligible" not in record and "approved" not in record
    with pytest.raises(FrozenInstanceError):
        candidate.capability_name = "changed"


def test_same_subject_deduplicates_across_runs_without_erasing_evidence_identity():
    first = _candidate()
    second = _candidate(
        origin_runs=(_ref("factory", "run-2", SHA_B),),
        proof_bundles=(_ref("factory", "dossier-2", SHA_C),),
    )

    assert first.candidate_id == second.candidate_id
    assert first.subject_hash == second.subject_hash
    assert first.record_hash != second.record_hash


def test_target_specific_evidence_can_be_classified_but_not_declared_promotable():
    candidate = _candidate(capability_kind="target-specific-evidence")
    record = candidate.to_record()
    assert record["capabilityKind"] == "target-specific-evidence"
    assert set(record).isdisjoint({"eligible", "promotionStatus", "installable"})


@pytest.mark.parametrize("mutation", [
    "extra", "candidate-id", "subject-hash", "record-hash", "order", "evaluation-prose",
])
def test_forged_or_noncanonical_records_fail_closed(mutation):
    record = _candidate(
        scope_ids=("scope-b", "scope-a"),
        evaluation_results=(_ref("reversebench", "result-1", SHA_C),),
    ).to_record()
    if mutation == "extra":
        record["eligible"] = True
    elif mutation == "candidate-id":
        record["candidateId"] = "promotion-" + "0" * 64
    elif mutation == "subject-hash":
        record["subjectHash"] = "sha256:" + "0" * 64
    elif mutation == "record-hash":
        record["recordHash"] = "sha256:" + "0" * 64
    elif mutation == "order":
        record["scopeIds"].reverse()
    else:
        record["evaluationResults"][0]["score"] = 100
    with pytest.raises(ValueError):
        PromotionCandidate.from_record(record)


@pytest.mark.parametrize(("field", "value"), [
    ("capability_kind", "self-approved-skill"),
    ("capability_kind", {"kind": "behavioral-skill"}),
    ("capability_name", "../../private"),
    ("capability_version", "1 latest"),
    ("capability_content_digest", "trusted"),
    ("origin_runs", ()),
    ("proof_bundles", ()),
])
def test_invalid_candidate_fields_are_rejected(field, value):
    with pytest.raises(ValueError):
        _candidate(**{field: value})


def test_boolean_schema_version_is_not_accepted_as_integer_one():
    record = _candidate().to_record()
    record["schemaVersion"] = True
    with pytest.raises(ValueError, match="malformed"):
        PromotionCandidate.from_record(record)
