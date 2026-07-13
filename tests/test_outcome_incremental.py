from __future__ import annotations

from copy import deepcopy
import json
import random

import pytest

from rekit_factory.outcome_incremental import (
    SOURCE_CHANGE_VERSION,
    SOURCE_SNAPSHOT_VERSION,
    IncrementalOutcomeFold,
    OutcomeSourceChangeConflict,
    OutcomeSourceChangeError,
    OutcomeSourceChangeV1,
)
from rekit_factory.outcomes import project_outcomes, verify_outcome_semantic_sha256


def change(change_id, kind, revision, value=None, *, operation="upsert", source_id=None):
    if source_id is None:
        if kind in {"run", "project-memory"}:
            source_id = kind
        elif value is not None:
            source_id = value.get({"campaign": "campaignId", "archive": "archiveId"}.get(
                kind, "id"
            ))
        else:
            raise AssertionError("removals require source_id")
    return OutcomeSourceChangeV1(
        schema_version=1,
        source_version=SOURCE_CHANGE_VERSION,
        change_id=change_id,
        source_kind=kind,
        source_id=source_id,
        source_revision=revision,
        operation=operation,
        value=value,
    )


def remove(change_id, kind, source_id, revision):
    return change(change_id, kind, revision, operation="remove", source_id=source_id)


def full_projection(fold):
    source = fold.source_snapshot()
    return project_outcomes(
        run=source["run"],
        workers=source["workers"],
        work_items=source["workItems"],
        memory=source["projectMemory"],
        dossiers=source["dossiers"],
        pending_questions=source["pendingDecisions"],
        campaigns=source["campaigns"],
        archives=source["archives"],
        source_watermarks=source["sourceWatermarks"],
    )


def assert_full_parity(fold):
    incremental = fold.projection()
    rebuilt = full_projection(fold)
    assert incremental == rebuilt
    assert incremental["semanticSha256"] == rebuilt["semanticSha256"]
    assert verify_outcome_semantic_sha256(incremental)
    return incremental


def entity(projection, kind, identifier):
    return next(
        value for value in projection["entities"]
        if value["entityType"] == kind and value["entityId"] == identifier
    )


def randomized_changes():
    memories = [
        {
            "hypotheses": {"hyp-1": {"id": "hyp-1", "status": "testing"}},
            "findings": {"finding-1": {"id": "finding-1", "status": "candidate"}},
        },
        {
            "hypotheses": {"hyp-1": {"id": "hyp-1", "status": "supported"}},
            "findings": {"finding-1": {"id": "finding-1", "status": "reproduced"}},
            "finding_attempts": {
                "attempt-1": {
                    "id": "attempt-1", "findingId": "finding-1", "outcome": "success",
                },
            },
            "finding_operator_decisions": {
                "decision-1": {
                    "id": "decision-1", "findingId": "finding-1",
                    "decision": "accepted", "_eventSeq": 4,
                },
            },
        },
        {
            "degraded": True,
            "diagnostics": ["sequence discontinuity"],
            "finding_attempts": {
                "attempt-1": {
                    "id": "attempt-1", "findingId": "finding-1",
                    "outcome": "contradictory",
                },
            },
            "finding_operator_decisions": {
                "decision-1": {
                    "id": "decision-1", "findingId": "finding-1",
                    "decision": "waived", "_eventSeq": 5,
                },
            },
        },
        {
            "findings": {"finding-1": {"id": "finding-1", "status": "future-reviewed"}},
            "finding_attempts": {
                "attempt-1": {
                    "id": "attempt-1", "findingId": "finding-1",
                    "outcome": "future-attempt",
                },
            },
            "finding_operator_decisions": {
                "decision-1": {
                    "id": "decision-1", "findingId": "finding-1",
                    "decision": "future-decision", "_eventSeq": 6,
                },
            },
        },
        {
            "findings": {"finding-1": {"id": "finding-1", "status": "reproduced"}},
            "finding_attempts": {},
            "finding_operator_decisions": {},
        },
    ]
    return [
        change("run-1", "run", 1, {"id": "run-a", "status": "queued"}),
        change("run-2", "run", 2, {"id": "run-a", "status": "future-paused"}),
        change("run-3", "run", 3, {"id": "run-a", "status": "running"}),
        change("worker-1", "worker", 1, {"id": "worker-a", "status": "queued"}),
        change("worker-2", "worker", 2, {"id": "worker-a", "status": "done"}),
        remove("worker-3", "worker", "worker-a", 3),
        change("worker-4", "worker", 4, {"id": "worker-a", "status": "failed"}),
        change("worker-b-1", "worker", 1, {"id": "worker-b", "status": "future-idle"}),
        change("work-1", "work-item", 1, {"id": "work-a", "status": "running"}),
        change("work-2", "work-item", 2, {"id": "work-a", "status": "done"}),
        remove("work-3", "work-item", "work-a", 3),
        change("work-4", "work-item", 4, {"id": "work-a", "status": "blocked"}),
        *[
            change(f"memory-{index}", "project-memory", index, memory)
            for index, memory in enumerate(memories, start=1)
        ],
        change("dossier-1", "dossier", 1, {
            "id": "dossier-a", "findingId": "finding-1",
            "verificationStatus": "published",
        }),
        change("dossier-2", "dossier", 2, {
            "id": "dossier-a", "findingId": "finding-1",
            "verificationStatus": "verified",
        }),
        remove("dossier-3", "dossier", "dossier-a", 3),
        change("dossier-4", "dossier", 4, {
            "id": "dossier-a", "findingId": "finding-1",
            "verificationStatus": "stale-or-invalid",
        }),
        change("question-1", "pending-decision", 1, {"id": "question-a"}),
        remove("question-2", "pending-decision", "question-a", 2),
        change("question-3", "pending-decision", 3, {"id": "question-a"}),
    ]


def test_strict_change_envelope_round_trips_and_detaches_caller_value():
    raw = {"id": "worker-a", "status": {"future": ["state"]}}
    current = change("worker-1", "worker", 1, raw)
    decoded = OutcomeSourceChangeV1.from_dict(current.to_dict())
    assert decoded == current
    assert decoded.canonical_bytes == current.canonical_bytes

    fold = IncrementalOutcomeFold()
    assert fold.apply(current)
    before = fold.projection()
    raw["status"]["future"].append("mutated")
    current.value["status"]["future"].append("also-mutated")
    assert fold.projection() == before

    malformed = current.to_dict()
    malformed["extra"] = True
    with pytest.raises(OutcomeSourceChangeError, match="fields"):
        OutcomeSourceChangeV1.from_dict(malformed)
    with pytest.raises(OutcomeSourceChangeError, match="sourceId"):
        change("bad-worker", "worker", 1, {"id": "worker-a"}, source_id="worker-b")
    with pytest.raises(OutcomeSourceChangeError, match="key must match"):
        change("bad-memory", "project-memory", 1, {
            "findings": {"finding-a": {"id": "finding-b", "status": "candidate"}},
        })


def test_exact_retry_conflicts_and_batch_failure_are_transactional():
    fold = IncrementalOutcomeFold()
    original = change("worker-1", "worker", 1, {"id": "worker-a", "status": "queued"})
    assert fold.apply(original) is True
    after_original = fold.projection()
    assert fold.apply(original) is False
    assert fold.last_refolded_entities == ()
    assert fold.projection() == after_original

    reused_id = change("worker-1", "worker", 2, {"id": "worker-a", "status": "done"})
    with pytest.raises(OutcomeSourceChangeConflict, match="changeId"):
        fold.apply(reused_id)
    same_revision = change(
        "worker-conflict", "worker", 1, {"id": "worker-a", "status": "failed"},
    )
    with pytest.raises(OutcomeSourceChangeConflict, match="source revision"):
        fold.apply(same_revision)
    assert fold.projection() == after_original

    valid = change("work-1", "work-item", 1, {"id": "work-a", "status": "queued"})
    conflict = change("work-conflict", "work-item", 1, {"id": "work-a", "status": "done"})
    with pytest.raises(OutcomeSourceChangeConflict):
        fold.apply_batch([valid, conflict])
    assert fold.projection() == after_original
    assert not any(value["entityId"] == "work-a" for value in fold.projection()["entities"])


def test_out_of_order_revisions_do_not_rewind_and_invalid_cross_source_ids_fail_closed():
    fold = IncrementalOutcomeFold()
    newest = change("worker-3", "worker", 3, {"id": "worker-a", "status": "done"})
    stale = change("worker-1", "worker", 1, {"id": "worker-a", "status": "queued"})
    assert fold.apply(newest)
    newest_projection = assert_full_parity(fold)
    assert fold.apply(stale)
    assert fold.last_refolded_entities == ()
    assert fold.projection() == newest_projection
    assert fold.apply(stale) is False
    with pytest.raises(OutcomeSourceChangeConflict, match="source revision"):
        fold.apply(change(
            "worker-1-conflict", "worker", 1,
            {"id": "worker-a", "status": "failed"},
        ))
    assert fold.projection() == newest_projection

    memory = change("memory-1", "project-memory", 1, {
        "finding_operator_decisions": {
            "decision-a": {
                "id": "decision-a", "findingId": "finding-a",
                "decision": "accepted", "_eventSeq": 1,
            },
        },
    })
    assert fold.apply(memory)
    before_collision = fold.projection()
    with pytest.raises(OutcomeSourceChangeError, match="identities must be unique"):
        fold.apply(change(
            "pending-1", "pending-decision", 1, {"id": "decision-a"},
        ))
    assert fold.projection() == before_collision

    with pytest.raises(ValueError, match="identities must be unique"):
        project_outcomes(
            run={"id": "run-a", "status": "running"},
            workers=(), work_items=(), memory=memory.value, dossiers=(),
            pending_questions=({"id": "decision-a"},),
        )


def test_selective_refold_handles_cross_entity_deletes_reappearance_and_unknowns():
    fold = IncrementalOutcomeFold(source_watermarks={"factoryEventRowid": 9})
    steps = [
        change("run-1", "run", 1, {"id": "run-a", "status": "running"}),
        change("memory-1", "project-memory", 1, {
            "findings": {"finding-a": {"id": "finding-a", "status": "reproduced"}},
            "finding_attempts": {
                "attempt-a": {
                    "id": "attempt-a", "findingId": "finding-a", "outcome": "success",
                },
            },
            "finding_operator_decisions": {
                "decision-a": {
                    "id": "decision-a", "findingId": "finding-a",
                    "decision": "accepted", "_eventSeq": 1,
                },
            },
        }),
        change("dossier-1", "dossier", 1, {
            "id": "dossier-a", "findingId": "finding-a", "verificationStatus": "verified",
        }),
    ]
    for step in steps:
        assert fold.apply(step)
        assert_full_parity(fold)

    projection = fold.projection()
    finding = entity(projection, "finding", "finding-a")
    assert finding["facets"]["acceptance"]["state"] == "accepted"
    assert finding["facets"]["publication"]["state"] == "published"
    assert not any(value["code"] == "dangling-parent" for value in projection["diagnostics"])
    assert fold.last_refolded_entities == (
        ("finding", "finding-a"), ("proof-bundle", "dossier-a"),
    )

    assert fold.apply(change("memory-2", "project-memory", 2, {
        "degraded": True,
        "diagnostics": ["memory gap"],
        "finding_attempts": {
            "attempt-a": {
                "id": "attempt-a", "findingId": "finding-a", "outcome": "future-outcome",
            },
        },
        "finding_operator_decisions": {
            "decision-a": {
                "id": "decision-a", "findingId": "finding-a",
                "decision": "waived", "_eventSeq": 2,
            },
        },
    }))
    projection = assert_full_parity(fold)
    assert not any(value["entityType"] == "finding" for value in projection["entities"])
    dangling = [value for value in projection["diagnostics"] if value["code"] == "dangling-parent"]
    assert {(value["entityType"], value["entityId"]) for value in dangling} == {
        ("operator-decision", "decision-a"),
        ("proof-bundle", "dossier-a"),
        ("validation", "attempt-a"),
    }
    assert projection["degraded"] is True
    assert entity(projection, "validation", "attempt-a")["facets"]["validation"] == {
        "known": False, "owner": "validator-policy", "rawState": "future-outcome",
        "state": "unknown", "terminal": False,
    }

    assert fold.apply(change("memory-3", "project-memory", 3, {
        "findings": {"finding-a": {"id": "finding-a", "status": "future-reviewed"}},
        "finding_attempts": {
            "attempt-a": {
                "id": "attempt-a", "findingId": "finding-a", "outcome": "success",
            },
        },
        "finding_operator_decisions": {
            "decision-a": {
                "id": "decision-a", "findingId": "finding-a",
                "decision": "rejected", "_eventSeq": 3,
            },
        },
    }))
    projection = assert_full_parity(fold)
    finding = entity(projection, "finding", "finding-a")
    assert finding["facets"]["validation"]["state"] == "unknown"
    assert finding["facets"]["acceptance"]["state"] == "rejected"
    assert finding["facets"]["publication"]["state"] == "published"
    assert not any(value["code"] == "dangling-parent" for value in projection["diagnostics"])

    assert fold.apply(remove("dossier-2", "dossier", "dossier-a", 2))
    projection = assert_full_parity(fold)
    assert entity(projection, "finding", "finding-a")["facets"]["publication"][
        "state"
    ] == "unpublished"
    assert not any(value["entityType"] == "proof-bundle" for value in projection["entities"])
    assert fold.apply(change("dossier-3", "dossier", 3, {
        "id": "dossier-a", "findingId": "finding-a",
        "verificationStatus": "stale-or-invalid",
    }))
    assert_full_parity(fold)
    assert fold.apply(change("dossier-4", "dossier", 4, {
        "id": "dossier-a", "findingId": "finding-a", "verificationStatus": "verified",
    }))
    assert_full_parity(fold)
    assert fold.last_refolded_entities == (("proof-bundle", "dossier-a"),)

    assert fold.apply(change("memory-4", "project-memory", 4, {
        "findings": {"finding-a": {"id": "finding-a", "status": "future-reviewed"}},
        "finding_attempts": {
            "attempt-a": {
                "id": "attempt-a", "findingId": "finding-a", "outcome": "success",
            },
        },
        "finding_operator_decisions": {},
    }))
    projection = assert_full_parity(fold)
    assert entity(projection, "finding", "finding-a")["facets"]["acceptance"][
        "state"
    ] == "undecided"
    assert not any(
        value["entityType"] == "operator-decision" and value["entityId"] == "decision-a"
        for value in projection["entities"]
    )

    assert fold.apply(remove("run-2", "run", "run", 2))
    projection = assert_full_parity(fold)
    assert any(value["code"] == "missing-run" for value in projection["diagnostics"])
    assert fold.apply(change("run-3", "run", 3, {"id": "run-b", "status": "running"}))
    projection = assert_full_parity(fold)
    assert entity(projection, "finding", "finding-a")["parent"]["entityId"] == "run-b"
    assert fold.apply(change("run-4", "run", 4, {"id": "run-b", "status": "completed"}))
    assert_full_parity(fold)
    assert fold.last_refolded_entities == (("run", "run-b"),)


def test_report_entity_has_exact_full_incremental_parity_across_render_and_removal():
    fold = IncrementalOutcomeFold()
    assert fold.apply(change("run-1", "run", 1, {"id": "run-a", "status": "running"}))
    assert fold.apply(change("work-1", "work-item", 1, {
        "id": "work-a", "status": "running",
        "result": {"summary": "draft", "status_update": "accepted by model"},
    }))
    projection = assert_full_parity(fold)
    report = entity(projection, "report", "work-a")
    assert report["parent"] == {"entityType": "work-item", "entityId": "work-a"}
    assert report["facets"]["publication"]["state"] == "rendered"
    assert report["facets"]["validation"]["state"] == "not-applicable"
    assert report["facets"]["acceptance"]["state"] == "not-applicable"
    assert fold.last_refolded_entities == (("report", "work-a"), ("work-item", "work-a"))

    assert fold.apply(change("work-2", "work-item", 2, {
        "id": "work-a", "status": "done", "result": {"status_update": "validated"},
    }))
    projection = assert_full_parity(fold)
    assert not any(item["entityType"] == "report" for item in projection["entities"])

    assert fold.apply(remove("work-3", "work-item", "work-a", 3))
    assert_full_parity(fold)
    assert not any(item["entityId"] == "work-a" for item in fold.projection()["entities"])


def test_campaign_archive_full_incremental_parity_removal_readd_unknown_and_dangling():
    fold = IncrementalOutcomeFold()
    assert fold.apply(change("campaign-1", "campaign", 1, {
        "campaignId": "campaign-a", "state": "active",
        "coverage": {"state": "covered"},
    }))
    assert fold.apply(change("archive-1", "archive", 1, {
        "archiveId": "archive-a", "campaignId": "campaign-a", "state": "unarchived",
    }))
    projection = assert_full_parity(fold)
    campaign = entity(projection, "campaign", "campaign-a")
    archive = entity(projection, "archive", "archive-a")
    assert campaign["facets"]["coverage"]["state"] == "covered"
    assert campaign["facets"]["completion"]["state"] == "incomplete"
    assert archive["facets"]["archival"]["state"] == "unarchived"

    assert fold.apply(remove("campaign-2", "campaign", "campaign-a", 2))
    projection = assert_full_parity(fold)
    assert entity(projection, "archive", "archive-a")["diagnostics"][0]["code"] \
        == "dangling-parent"

    assert fold.apply(change("campaign-3", "campaign", 3, {
        "campaignId": "campaign-a", "state": "future-paused",
        "coverage": {"state": "future-scoped"},
    }))
    projection = assert_full_parity(fold)
    assert not entity(projection, "archive", "archive-a")["diagnostics"]
    assert entity(projection, "campaign", "campaign-a")["facets"]["coverage"][
        "state"
    ] == "unknown"

    assert fold.apply(change("archive-2", "archive", 2, {
        "archiveId": "archive-a", "campaignId": "campaign-a", "state": "archived",
    }))
    projection = assert_full_parity(fold)
    assert entity(projection, "archive", "archive-a")["facets"]["archival"][
        "state"
    ] == "archived"
    assert fold.last_refolded_entities == (("archive", "archive-a"),)

    restarted = IncrementalOutcomeFold.from_source_snapshot(fold.source_snapshot())
    assert restarted.projection() == projection


def test_randomized_differential_parity_after_every_arrival_and_batch_order():
    changes = randomized_changes()
    final_projections = []
    for seed in range(12):
        shuffled = list(changes)
        random.Random(seed).shuffle(shuffled)
        fold = IncrementalOutcomeFold(source_watermarks={
            "factoryEventRowid": 41, "memorySequence": 12,
        })
        for current in shuffled:
            fold.apply(current)
            assert_full_parity(fold)
        final_projections.append(fold.projection())
    assert all(value == final_projections[0] for value in final_projections)

    batch_results = []
    for seed in range(8):
        shuffled = list(changes)
        random.Random(seed + 100).shuffle(shuffled)
        fold = IncrementalOutcomeFold(source_watermarks={
            "factoryEventRowid": 41, "memorySequence": 12,
        })
        assert fold.apply_batch(shuffled) == len(changes)
        batch_results.append(assert_full_parity(fold))
    assert all(value == batch_results[0] for value in batch_results)
    assert batch_results[0] == final_projections[0]


def test_source_snapshot_restart_is_canonical_byte_identical_and_detached():
    fold = IncrementalOutcomeFold(source_watermarks={
        "memorySequence": 8, "factoryEventRowid": 19,
    })
    fold.apply_batch(randomized_changes())
    snapshot = fold.source_snapshot()
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    decoded = json.loads(encoded)
    assert decoded["sourceVersion"] == SOURCE_SNAPSHOT_VERSION

    restarted = IncrementalOutcomeFold.from_source_snapshot(decoded)
    assert restarted.source_snapshot() == snapshot
    assert json.dumps(restarted.projection(), sort_keys=True) == json.dumps(
        fold.projection(), sort_keys=True,
    )
    assert restarted.projection()["semanticSha256"] == fold.projection()["semanticSha256"]
    assert_full_parity(restarted)

    exact_retry = randomized_changes()[-1]
    assert restarted.apply(exact_retry) is False
    with pytest.raises(OutcomeSourceChangeConflict, match="changeId"):
        restarted.apply(change(
            exact_retry.change_id, "pending-decision", 4, {"id": "question-a"},
        ))
    with pytest.raises(OutcomeSourceChangeConflict, match="source revision"):
        restarted.apply(change(
            "question-revision-conflict", "pending-decision", 3,
            {"id": "question-a", "prompt": "tampered"},
        ))

    before_stale = restarted.projection()
    assert restarted.apply(randomized_changes()[0]) is False
    assert restarted.last_refolded_entities == ()
    assert restarted.projection() == before_stale

    snapshot["projectMemory"].clear()
    snapshot["workers"].clear()
    assert restarted.source_snapshot() == decoded


def test_source_snapshot_rejects_missing_or_incoherent_heads_and_receipts():
    fold = IncrementalOutcomeFold()
    fold.apply(change("run-1", "run", 1, {"id": "run-a", "status": "running"}))
    fold.apply(change("worker-1", "worker", 1, {"id": "worker-a", "status": "done"}))
    fold.apply(remove("worker-2", "worker", "worker-a", 2))
    snapshot = fold.source_snapshot()

    missing_receipt = deepcopy(snapshot)
    missing_receipt["changeReceipts"] = [
        item for item in missing_receipt["changeReceipts"] if item["changeId"] != "run-1"
    ]
    with pytest.raises(OutcomeSourceChangeError, match="source head requires"):
        IncrementalOutcomeFold.from_source_snapshot(missing_receipt)

    missing_head = deepcopy(snapshot)
    missing_head["sourceHeads"] = [
        item for item in missing_head["sourceHeads"] if item["sourceKind"] != "run"
    ]
    with pytest.raises(OutcomeSourceChangeError, match="no matching source head"):
        IncrementalOutcomeFold.from_source_snapshot(missing_head)

    head_without_receipt = deepcopy(snapshot)
    next(item for item in head_without_receipt["sourceHeads"]
         if item["sourceKind"] == "run")["sourceRevision"] = 2
    with pytest.raises(OutcomeSourceChangeError, match="source head requires"):
        IncrementalOutcomeFold.from_source_snapshot(head_without_receipt)

    future_receipt = deepcopy(snapshot)
    next(item for item in future_receipt["changeReceipts"]
         if item["changeId"] == "run-1")["sourceRevision"] = 2
    with pytest.raises(OutcomeSourceChangeError, match="exceeds its source head"):
        IncrementalOutcomeFold.from_source_snapshot(future_receipt)

    tampered_value = deepcopy(snapshot)
    tampered_value["run"]["status"] = "failed"
    with pytest.raises(OutcomeSourceChangeError, match="materialized source state"):
        IncrementalOutcomeFold.from_source_snapshot(tampered_value)

    broken_tombstone = deepcopy(snapshot)
    broken_tombstone["workers"] = [{"id": "worker-a", "status": "done"}]
    with pytest.raises(OutcomeSourceChangeError, match="materialized source state"):
        IncrementalOutcomeFold.from_source_snapshot(broken_tombstone)

    unheaded_record = deepcopy(snapshot)
    unheaded_record["workItems"] = [{"id": "work-a", "status": "queued"}]
    with pytest.raises(OutcomeSourceChangeError, match="no source head"):
        IncrementalOutcomeFold.from_source_snapshot(unheaded_record)


@pytest.mark.parametrize("invalid_parent", [None, ["finding-a"]])
def test_source_snapshot_validates_dossier_parent_identity_like_live_changes(invalid_parent):
    fold = IncrementalOutcomeFold()
    fold.apply(change("dossier-1", "dossier", 1, {
        "id": "dossier-a", "findingId": "finding-a", "verificationStatus": "published",
    }))
    snapshot = fold.source_snapshot()
    snapshot["dossiers"][0]["findingId"] = invalid_parent
    with pytest.raises(OutcomeSourceChangeError, match="dossier.findingId"):
        IncrementalOutcomeFold.from_source_snapshot(snapshot)


def test_shared_consistency_contract_is_path_neutral_not_derivation_metadata():
    fold = IncrementalOutcomeFold()
    fold.apply(change("run-1", "run", 1, {"id": "run-a", "status": "running"}))
    incremental = assert_full_parity(fold)
    assert incremental["consistency"] == {
        "mode": "canonical-source-state",
        "sourceRead": "external-to-projection",
        "crossStoreRevision": "not-claimed",
        "watermarksAreProjectionIdentity": False,
        "incrementalParity": "in-memory-reference",
    }
    assert "ledgerRead" not in incremental["consistency"]
    assert "full-fold" not in incremental["consistency"].values()
