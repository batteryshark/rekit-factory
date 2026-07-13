from __future__ import annotations

import json

from rekit_factory.outcomes import project_outcomes


def _project(*, run_status="queued", workers=(), work=(), memory=None, dossiers=(), questions=()):
    return project_outcomes(
        run={"id": "run-1", "status": run_status},
        workers=workers,
        work_items=work,
        memory=memory or {},
        dossiers=dossiers,
        pending_questions=questions,
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
        },
    }
    first = _project(
        run_status="running",
        workers=[{"id": "worker-z", "status": "running"},
                 {"id": "worker-a", "status": "done"}],
        work=[{"id": "work-z", "status": "running", "operation": "model-worker"},
              {"id": "work-a", "status": "done", "operation": "rekit-tool"}],
        memory=memory,
        dossiers=[{"id": "dossier-z", "findingId": "finding-z",
                   "verificationStatus": "published"}],
        questions=[{"id": "question-z"}],
    )
    rebuilt = _project(
        run_status="running",
        workers=[{"id": "worker-a", "status": "done"},
                 {"id": "worker-z", "status": "running"}],
        work=[{"id": "work-a", "status": "done", "operation": "rekit-tool"},
              {"id": "work-z", "status": "running", "operation": "model-worker"}],
        memory={key: dict(reversed(list(value.items()))) for key, value in memory.items()},
        dossiers=[{"id": "dossier-z", "findingId": "finding-z",
                   "verificationStatus": "published"}],
        questions=[{"id": "question-z"}],
    )

    assert json.dumps(first, sort_keys=True) == json.dumps(rebuilt, sort_keys=True)
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
        work=[{"id": "tool-work", "status": "done", "operation": "rekit-tool"}],
        questions=[{"id": "question-1"}],
    )
    assert set(projection["authorities"]) == {
        "factory-dossier-publisher", "factory-scheduler", "muster", "offline-proof-verifier",
        "operator", "rekit-tool-result", "validator-policy",
    }
    for entity in projection["entities"]:
        assert set(entity["facets"]) == set(projection["facets"])
        assert all(facet["owner"] in projection["authorities"]
                   for facet in entity["facets"].values())
    tool = _entity(projection, "work-item", "tool-work")
    assert tool["facets"]["execution"]["owner"] == "muster"
    assert tool["facets"]["completion"]["owner"] == "muster"
    assert tool["facets"]["disposition"]["owner"] == "rekit-tool-result"


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
