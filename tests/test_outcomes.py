from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import subprocess
import sys

import pytest

from rekit_factory.outcomes import (
    SEMANTIC_CANONICAL_BASE64_FIELD,
    SEMANTIC_IDENTITY_DOMAIN,
    canonical_outcome_semantic_bytes,
    decode_outcome_semantic_canonical_base64,
    outcome_semantic_sha256,
    project_outcomes,
    verify_outcome_semantic_sha256,
)


def _project(*, run_status="queued", workers=(), work=(), memory=None, dossiers=(), questions=(),
             source_watermarks=None):
    return project_outcomes(
        run={"id": "run-1", "status": run_status},
        workers=workers,
        work_items=work,
        memory=memory or {},
        dossiers=dossiers,
        pending_questions=questions,
        source_watermarks=source_watermarks,
    )


def _entity(projection, kind, entity_id):
    return next(item for item in projection["entities"]
                if item["entityType"] == kind and item["entityId"] == entity_id)


def test_full_fold_is_deterministic_and_input_order_independent():
    memory = {
        "hypotheses": {
            "hyp-z": {"id": "hyp-z", "status": "testing"},
            "hyp-a": {"id": "hyp-a", "status": "supported"},
        },
        "findings": {
            "finding-z": {"id": "finding-z", "status": "reproduced"},
            "finding-a": {"id": "finding-a", "status": "candidate"},
        },
        "finding_attempts": {
            "attempt-z": {"id": "attempt-z", "findingId": "finding-z", "outcome": "success"},
        },
        "finding_operator_decisions": {
            "decision-z": {"id": "decision-z", "findingId": "finding-z",
                           "decision": "accepted", "_eventSeq": 8},
            "decision-a": {"id": "decision-a", "findingId": "finding-a",
                           "decision": "rejected", "_eventSeq": 2},
        },
    }
    first = _project(
        run_status="running",
        workers=[{"id": "worker-z", "status": "running"},
                 {"id": "worker-a", "status": "done"}],
        work=[{"id": "work-z", "status": "running", "operation": "model-worker"},
              {"id": "work-a", "status": "done", "operation": "rekit-tool"}],
        memory=memory,
        dossiers=[
            {"id": "dossier-z", "findingId": "finding-z",
             "verificationStatus": "published"},
            {"id": "dossier-a", "findingId": "finding-a",
             "verificationStatus": "verified"},
        ],
        questions=[{"id": "question-z"}],
    )
    rebuilt = _project(
        run_status="running",
        workers=[{"id": "worker-a", "status": "done"},
                 {"id": "worker-z", "status": "running"}],
        work=[{"id": "work-a", "status": "done", "operation": "rekit-tool"},
              {"id": "work-z", "status": "running", "operation": "model-worker"}],
        memory={key: dict(reversed(list(value.items()))) for key, value in memory.items()},
        dossiers=[
            {"id": "dossier-a", "findingId": "finding-a",
             "verificationStatus": "verified"},
            {"id": "dossier-z", "findingId": "finding-z",
             "verificationStatus": "published"},
        ],
        questions=[{"id": "question-z"}],
    )

    assert json.dumps(first, sort_keys=True) == json.dumps(rebuilt, sort_keys=True)
    assert canonical_outcome_semantic_bytes(first) == canonical_outcome_semantic_bytes(rebuilt)
    assert first["semanticSha256"] == rebuilt["semanticSha256"]
    assert verify_outcome_semantic_sha256(first)
    assert verify_outcome_semantic_sha256(rebuilt)
    assert [(item["entityType"], item["entityId"]) for item in first["entities"]] == sorted(
        (item["entityType"], item["entityId"]) for item in first["entities"]
    )


def test_children_never_promote_parent_outcomes():
    projection = _project(
        run_status="running",
        workers=[{"id": "worker-1", "status": "done"}],
        work=[{"id": "work-1", "status": "done", "operation": "model-worker",
               "result": {"summary": "rendered report"}}],
        memory={"findings": {"finding-1": {"id": "finding-1", "status": "candidate"}}},
        dossiers=[{"id": "dossier-1", "findingId": "finding-1",
                   "verificationStatus": "verified", "verified": True}],
    )

    run = _entity(projection, "run", "run-1")
    finding = _entity(projection, "finding", "finding-1")
    assert run["facets"]["execution"]["state"] == "active"
    assert run["facets"]["completion"]["state"] == "incomplete"
    assert finding["facets"]["validation"]["state"] == "unvalidated"
    assert finding["facets"]["acceptance"]["state"] == "undecided"
    assert finding["facets"]["publication"]["state"] == "published"


def test_worker_report_is_rendered_child_without_model_authored_outcome_inference():
    projection = _project(work=[{
        "id": "work-1", "status": "running", "operation": "model-worker",
        "result": {
            "summary": "candidate narrative", "status_update": "validated and accepted",
        },
    }])

    report = _entity(projection, "report", "work-1")
    assert report["parent"] == {"entityType": "work-item", "entityId": "work-1"}
    assert report["facets"]["publication"] == {
        "rawState": "rendered", "state": "rendered", "known": True,
        "terminal": True, "owner": "factory-report-renderer",
    }
    assert report["facets"]["validation"]["state"] == "not-applicable"
    assert report["facets"]["acceptance"]["state"] == "not-applicable"
    assert "validated and accepted" not in json.dumps(report)


def test_non_report_work_result_does_not_create_report_entity():
    projection = _project(work=[{
        "id": "work-1", "status": "done", "result": {"status_update": "complete"},
    }])
    assert not any(item["entityType"] == "report" for item in projection["entities"])


def test_unknown_raw_state_is_preserved_and_degrades_without_guessing():
    projection = _project(run_status="future-paused")
    run = _entity(projection, "run", "run-1")

    assert projection["degraded"] is True
    assert run["facets"]["execution"] == {
        "rawState": "future-paused", "state": "unknown", "known": False,
        "terminal": False, "owner": "factory-scheduler",
    }
    assert {item["facet"] for item in projection["diagnostics"]} == {
        "execution", "completion", "disposition",
    }


def test_publication_and_explicit_verification_remain_orthogonal():
    published = _project(dossiers=[{
        "id": "dossier-1", "findingId": "finding-1", "verificationStatus": "published",
    }])
    verified = _project(dossiers=[{
        "id": "dossier-1", "findingId": "finding-1", "verificationStatus": "verified",
        "verified": True,
    }])
    stale = _project(dossiers=[{
        "id": "dossier-1", "findingId": "finding-1",
        "verificationStatus": "stale-or-invalid", "verified": False,
    }])

    published_dossier = _entity(published, "proof-bundle", "dossier-1")
    verified_dossier = _entity(verified, "proof-bundle", "dossier-1")
    stale_dossier = _entity(stale, "proof-bundle", "dossier-1")
    assert published_dossier["facets"]["publication"]["state"] == "published"
    assert published_dossier["facets"]["validation"]["state"] == "unknown"
    assert published_dossier["facets"]["validation"]["known"] is False
    assert verified_dossier["facets"]["validation"]["state"] == "verified"
    assert stale_dossier["facets"]["validation"]["state"] == "stale"
    for dossier in (published_dossier, verified_dossier, stale_dossier):
        assert dossier["facets"]["publication"] == {
            "rawState": "published", "state": "published", "known": True,
            "terminal": True, "owner": "factory-dossier-publisher",
        }
        assert dossier["facets"]["validation"]["owner"] == "offline-proof-verifier"


def test_authority_is_explicit_for_every_facet():
    projection = _project(
        work=[
            {"id": "tool-queued", "status": "queued", "operation": "rekit-tool"},
            {"id": "tool-blocked", "status": "blocked", "operation": "rekit-tool"},
            {"id": "tool-failed", "status": "failed", "operation": "rekit-tool"},
        ],
        questions=[{"id": "question-1"}],
    )
    assert set(projection["authorities"]) == {
        "factory-dossier-publisher", "factory-report-renderer", "factory-scheduler", "muster",
        "offline-proof-verifier", "operator", "rekit-tool-result", "validator-policy",
    }
    for entity in projection["entities"]:
        assert set(entity["facets"]) == set(projection["facets"])
        assert all(facet["owner"] in projection["authorities"]
                   for facet in entity["facets"].values())
    for work_id in ("tool-queued", "tool-blocked", "tool-failed"):
        tool = _entity(projection, "work-item", work_id)
        assert tool["facets"]["execution"]["owner"] == "muster"
        assert tool["facets"]["completion"]["owner"] == "muster"
        assert tool["facets"]["disposition"]["owner"] == "muster"


def test_degraded_project_memory_diagnostics_are_propagated_deterministically():
    projection = _project(memory={
        "degraded": True,
        "diagnostics": ["sequence discontinuity: expected 2, found 4", "bad record", "bad record"],
    })

    source = [item for item in projection["diagnostics"]
              if item["code"] == "project-memory-source-degraded"]
    assert projection["degraded"] is True
    assert [item["message"] for item in source] == [
        "bad record", "sequence discontinuity: expected 2, found 4",
    ]
    assert all(item["source"] == "project-memory" for item in source)


def test_source_watermarks_are_not_exposed_as_projection_identity():
    projection = _project(source_watermarks={"factoryEventRowid": 19, "memorySequence": 7})

    assert projection["sourceWatermarks"] == {"factoryEventRowid": 19, "memorySequence": 7}
    assert projection["consistency"]["watermarksAreProjectionIdentity"] is False
    assert "cursor" not in projection["consistency"]


def test_semantic_identity_domain_is_public_recomputable_and_restart_stable():
    projection = _project(
        run_status="running",
        workers=[{"id": "worker-1", "status": "running"}],
        work=[{"id": "work-1", "status": "done"}],
    )
    before = deepcopy(projection)
    claimed = projection["semanticSha256"]
    without_identity = deepcopy(projection)
    without_identity.pop("semanticSha256")

    assert outcome_semantic_sha256(projection) == claimed
    assert outcome_semantic_sha256(without_identity) == claimed
    assert verify_outcome_semantic_sha256(projection) is True
    assert verify_outcome_semantic_sha256(without_identity) is False
    assert projection == before

    reordered_mappings = dict(reversed(list(projection.items())))
    reordered_mappings["authorities"] = dict(reversed(list(
        reordered_mappings["authorities"].items()
    )))
    assert canonical_outcome_semantic_bytes(reordered_mappings) == \
        canonical_outcome_semantic_bytes(projection)

    envelope = json.loads(canonical_outcome_semantic_bytes(projection))
    assert envelope["domain"] == SEMANTIC_IDENTITY_DOMAIN
    assert set(envelope["projection"]) == set(projection) - {
        "semanticCanonicalBase64", "semanticSha256", "sourceWatermarks",
    }

    recomputed = subprocess.check_output(
        [
            sys.executable, "-c",
            "import json,sys; from rekit_factory.outcomes import outcome_semantic_sha256; "
            "print(outcome_semantic_sha256(json.load(sys.stdin)))",
        ],
        input=json.dumps(projection), text=True,
    ).strip()
    assert recomputed == claimed


def test_watermark_movement_is_visible_but_semantically_excluded():
    first = _project(source_watermarks={"factoryEventRowid": 19, "memorySequence": 7})
    moved = _project(source_watermarks={"memorySequence": 8, "factoryEventRowid": 23})

    assert first["sourceWatermarks"] != moved["sourceWatermarks"]
    assert canonical_outcome_semantic_bytes(first) == canonical_outcome_semantic_bytes(moved)
    assert first["semanticSha256"] == moved["semanticSha256"]
    assert verify_outcome_semantic_sha256(first)
    assert verify_outcome_semantic_sha256(moved)

    observed_later = deepcopy(first)
    observed_later["sourceWatermarks"]["factoryEventRowid"] = 10_000
    assert verify_outcome_semantic_sha256(observed_later)
    assert observed_later["consistency"]["watermarksAreProjectionIdentity"] is False


def _semantic_fixture():
    return _project(
        run_status="future-paused",
        memory={
            "findings": {"finding-1": {"id": "finding-1", "status": "reproduced"}},
            "finding_operator_decisions": {
                "decision-1": {
                    "id": "decision-1", "findingId": "finding-1",
                    "decision": "accepted", "_eventSeq": 1,
                },
            },
        },
        dossiers=[{
            "id": "dossier-1", "findingId": "finding-1",
            "verificationStatus": "verified",
        }],
    )


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        ("schema", lambda value: value.__setitem__("schemaVersion", 2)),
        ("vocabulary", lambda value: value.__setitem__(
            "vocabularyVersion", "factory-outcomes/v2",
        )),
        ("facet-list", lambda value: value["facets"].__setitem__(0, "phase")),
        ("authority", lambda value: value["authorities"].__setitem__(
            "operator", "Different authority meaning.",
        )),
        ("entity", lambda value: value["entities"].append({
            **deepcopy(value["entities"][0]), "entityId": "additional-entity",
        })),
        ("raw-state", lambda value: _entity(value, "run", "run-1")["facets"][
            "execution"
        ].__setitem__("rawState", "another-raw-state")),
        ("normalized-state", lambda value: _entity(value, "run", "run-1")["facets"][
            "execution"
        ].__setitem__("state", "waiting")),
        ("parent", lambda value: _entity(value, "finding", "finding-1")[
            "parent"
        ].__setitem__("entityId", "run-other")),
        ("publication", lambda value: _entity(value, "finding", "finding-1")["facets"][
            "publication"
        ].__setitem__("state", "unpublished")),
        ("acceptance", lambda value: _entity(value, "finding", "finding-1")["facets"][
            "acceptance"
        ].__setitem__("state", "rejected")),
        ("diagnostic", lambda value: value["diagnostics"][0].__setitem__(
            "message", "Different diagnostic meaning.",
        )),
        ("degraded", lambda value: value.__setitem__("degraded", False)),
        ("consistency", lambda value: value["consistency"].__setitem__(
            "mode", "incremental-fold",
        )),
    ],
)
def test_every_public_semantic_class_changes_identity(name, mutate):
    original = _semantic_fixture()
    changed = deepcopy(original)
    mutate(changed)

    assert outcome_semantic_sha256(changed) != original["semanticSha256"], name
    assert verify_outcome_semantic_sha256(changed) is False


def test_projection_detaches_mutable_inputs_and_verifier_detects_public_mutation():
    raw_state = ["future-state"]
    watermarks = {"factoryEventRowid": 1, "observer": {"sequence": 2}}
    projection = project_outcomes(
        run={"id": "run-1", "status": raw_state}, workers=(), work_items=(), memory={},
        dossiers=(), pending_questions=(), source_watermarks=watermarks,
    )
    before = deepcopy(projection)

    raw_state.append("mutated")
    watermarks["observer"]["sequence"] = 99
    assert projection == before
    assert verify_outcome_semantic_sha256(projection)

    _entity(projection, "run", "run-1")["facets"]["execution"]["state"] = "active"
    assert verify_outcome_semantic_sha256(projection) is False


@pytest.mark.parametrize(
    ("bad_value", "error"),
    [
        (("python", "tuple"), TypeError),
        ({1: "non-string-key"}, TypeError),
        (float("nan"), ValueError),
        (float("inf"), ValueError),
    ],
)
def test_canonical_domain_rejects_non_json_and_nonfinite_values(bad_value, error):
    projection = _project()
    projection["adversarialField"] = bad_value
    with pytest.raises(error):
        canonical_outcome_semantic_bytes(projection)


def test_semantic_canonical_base64_is_byte_exact_for_adversarial_json_numbers_and_keys():
    projection = _project()
    projection["adversarialSemantic"] = {
        "2": 2.0,
        "10": 10 ** 100,
        "é": 1e-7,
        "雪": -0.0,
    }
    canonical = canonical_outcome_semantic_bytes(projection)
    projection[SEMANTIC_CANONICAL_BASE64_FIELD] = base64.b64encode(canonical).decode("ascii")
    projection["semanticSha256"] = hashlib.sha256(canonical).hexdigest()

    decoded = decode_outcome_semantic_canonical_base64(projection)
    assert decoded == canonical
    assert decoded == canonical_outcome_semantic_bytes(projection)
    assert str(10 ** 100).encode("ascii") in decoded
    assert b'"2":2.0' in decoded
    assert b'1e-07' in decoded
    assert b'-0.0' in decoded
    assert '"é"'.encode() in decoded
    assert '"雪"'.encode() in decoded
    assert verify_outcome_semantic_sha256(projection)


def test_semantic_canonical_transport_order_and_mutation_do_not_change_identity():
    projection = _project(run_status="future-paused")
    canonical = canonical_outcome_semantic_bytes(projection)
    identity = projection["semanticSha256"]
    assert decode_outcome_semantic_canonical_base64(projection) == canonical

    without_transport = deepcopy(projection)
    without_transport.pop(SEMANTIC_CANONICAL_BASE64_FIELD)
    assert canonical_outcome_semantic_bytes(without_transport) == canonical
    assert outcome_semantic_sha256(without_transport) == identity

    reordered = {
        key: projection[key]
        for key in reversed(tuple(projection))
    }
    assert outcome_semantic_sha256(reordered) == identity
    assert decode_outcome_semantic_canonical_base64(reordered) == canonical

    mutated = deepcopy(projection)
    mutated[SEMANTIC_CANONICAL_BASE64_FIELD] = base64.b64encode(
        canonical + b"transport mutation"
    ).decode("ascii")
    assert outcome_semantic_sha256(mutated) == identity
    assert verify_outcome_semantic_sha256(mutated)
    with pytest.raises(ValueError, match="does not match"):
        decode_outcome_semantic_canonical_base64(mutated)

    malformed = deepcopy(projection)
    malformed[SEMANTIC_CANONICAL_BASE64_FIELD] += "\n"
    assert outcome_semantic_sha256(malformed) == identity
    with pytest.raises(ValueError, match="canonical standard Base64"):
        decode_outcome_semantic_canonical_base64(malformed)


def test_dangling_parent_is_diagnostic_and_does_not_create_parent_state():
    projection = _project(memory={
        "finding_attempts": {
            "attempt-orphan": {
                "id": "attempt-orphan", "findingId": "missing-finding", "outcome": "success",
            },
        },
    })

    assert projection["degraded"] is True
    assert any(item["code"] == "dangling-parent" for item in projection["diagnostics"])
    assert not any(item["entityType"] == "finding" for item in projection["entities"])
    run = _entity(projection, "run", "run-1")
    assert run["facets"]["completion"]["state"] == "incomplete"
