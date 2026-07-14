from __future__ import annotations

import json

from rekit_factory.api import MAX_CAMPAIGN_TYPED_LINK_SCAN, _campaign_typed_links
from rekit_factory.outcomes import project_outcomes
from rekit_factory.scope import ScopeEnvelope, TargetGrant


def _scope():
    return ScopeEnvelope(
        scope_id="scope-a", revision=3,
        valid_from="2026-07-13T00:00:00Z", valid_until="2026-07-14T00:00:00Z",
        targets=(TargetGrant("a" * 64, "target-path:opaque"),),
    ).public_dict()


class _Controller:
    def __init__(self, storage_root, snapshot):
        self.storage_root = storage_root
        self._snapshot = snapshot

    def snapshot(self, _run_dir):
        return self._snapshot


def _fixture(tmp_path, entities):
    run_id = "run-safe"
    run_dir = tmp_path / "projects" / "project-a" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "runId": run_id, "creationComplete": True,
    }))
    scope = _scope()
    snapshot = {
        "run": {"id": run_id},
        "meta": {"runId": run_id, "projectId": "project-a", "scope": scope},
        "outcomeProjection": {"entities": entities},
    }
    campaign = {
        "projectId": "project-a",
        "scope": {"scopeId": scope["scopeId"], "revision": scope["revision"],
                  "digest": scope["digest"]},
        "handoff": {"factoryRunIds": [run_id], "evidenceIds": []},
    }
    return _Controller(tmp_path, snapshot), campaign


def test_typed_links_use_real_public_scope_shape_and_only_supported_stable_ids(tmp_path):
    controller, campaign = _fixture(tmp_path, [
        {"entityType": "hypothesis", "entityId": "hypothesis-a"},
        {"entityType": "finding", "entityId": "finding-a"},
        {"entityType": "operator-decision", "entityId": "decision-a"},
        {"entityType": "proof-bundle", "entityId": "dossier-a"},
        {"entityType": "worker", "entityId": "worker-a"},
        {"entityType": "finding", "entityId": "/Users/private/target"},
        {"entityType": "finding", "entityId": "<img-onerror>"},
    ])
    result = _campaign_typed_links(controller, campaign, limit=128)
    assert result == {
        "schemaVersion": 1,
        "references": [
            {"kind": "finding", "entityId": "finding-a", "runId": "run-safe",
             "surface": "outcomes"},
            {"kind": "hypothesis", "entityId": "hypothesis-a", "runId": "run-safe",
             "surface": "outcomes"},
            {"kind": "operator-decision", "entityId": "decision-a", "runId": "run-safe",
             "surface": "outcomes"},
            {"kind": "proof-bundle", "entityId": "dossier-a", "runId": "run-safe",
             "surface": "dossiers"},
        ],
        "totalCount": 4, "truncated": False, "sourceTruncated": False,
        "strongestReproducedResult": None,
    }

    campaign["scope"]["digest"] = "b" * 64
    assert _campaign_typed_links(controller, campaign, limit=128)["references"] == []


def test_typed_link_scan_and_output_are_independently_bounded(tmp_path):
    entities = [
        {"entityType": "finding", "entityId": f"finding-{index:04d}"}
        for index in range(MAX_CAMPAIGN_TYPED_LINK_SCAN + 20)
    ]
    controller, campaign = _fixture(tmp_path, entities)
    result = _campaign_typed_links(controller, campaign, limit=7)
    assert result["totalCount"] == MAX_CAMPAIGN_TYPED_LINK_SCAN
    assert len(result["references"]) == 7
    assert result["references"][0]["entityId"] == "finding-0505"
    assert result["truncated"] is True
    assert result["sourceTruncated"] is True


def test_strongest_result_prefers_exact_published_proof_for_accepted_finding(tmp_path):
    controller, campaign = _fixture(tmp_path, [])
    controller._snapshot["outcomeProjection"] = project_outcomes(
        run={"id": "run-safe", "status": "running"}, workers=(), work_items=(),
        memory={
            "findings": {"finding-a": {"id": "finding-a", "status": "reproduced"}},
            "finding_operator_decisions": {
                "decision-a": {"id": "decision-a", "findingId": "finding-a",
                               "decision": "accepted", "_eventSeq": 1},
            },
        },
        dossiers=[{"id": "dossier-a", "findingId": "finding-a",
                   "verificationStatus": "published"}], pending_questions=(),
    )
    result = _campaign_typed_links(controller, campaign, limit=128)
    assert result["strongestReproducedResult"] == {
        "kind": "proof-bundle", "entityId": "dossier-a", "runId": "run-safe",
        "surface": "dossiers", "findingId": "finding-a",
        "basis": "operator-accepted-published-proof",
    }
