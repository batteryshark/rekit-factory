from __future__ import annotations

import json
from types import SimpleNamespace

from rekit_factory.api import (
    MAX_CAMPAIGN_TYPED_LINK_SCAN, _campaign_typed_links,
)


DIGEST = "a" * 64


def _run_dir(root, run_id):
    path = root / "projects" / "project" / "runs" / run_id
    path.mkdir(parents=True)
    (path / "run.json").write_text(json.dumps({
        "runId": run_id, "creationComplete": True,
    }))
    return path


def _snapshot(run_id, *, project="project-a", digest=DIGEST, entities=()):
    return {
        "run": {"id": run_id},
        "meta": {
            "runId": run_id, "projectId": project,
            "scope": {"scopeId": "scope-a", "revision": 1, "digest": digest},
        },
        "outcomeProjection": {"entities": list(entities)},
    }


def _campaign(run_ids):
    return {
        "campaignId": "campaign-a", "projectId": "project-a",
        "scope": {"scopeId": "scope-a", "revision": 1, "digest": DIGEST},
        "handoff": {"factoryRunIds": list(run_ids), "evidenceIds": []},
    }


def test_typed_links_require_exact_campaign_run_scope_and_stable_public_ids(tmp_path):
    valid = _run_dir(tmp_path, "run-valid")
    wrong_project = _run_dir(tmp_path, "run-wrong-project")
    wrong_scope = _run_dir(tmp_path, "run-wrong-scope")
    snapshots = {
        valid: _snapshot("run-valid", entities=(
            {"entityType": "hypothesis", "entityId": "hypothesis-good"},
            {"entityType": "finding", "entityId": "finding-good"},
            {"entityType": "operator-decision", "entityId": "decision-good"},
            {"entityType": "proof-bundle", "entityId": "dossier-good"},
            {"entityType": "hypothesis", "entityId": "/Users/private/target"},
            {"entityType": "finding", "entityId": "<script>alert(1)</script>"},
            {"entityType": "worker", "entityId": "worker-not-a-campaign-link"},
        )),
        wrong_project: _snapshot(
            "run-wrong-project", project="project-b",
            entities=({"entityType": "finding", "entityId": "finding-cross-project"},),
        ),
        wrong_scope: _snapshot(
            "run-wrong-scope", digest="b" * 64,
            entities=({"entityType": "finding", "entityId": "finding-cross-scope"},),
        ),
    }
    controller = SimpleNamespace(
        storage_root=tmp_path,
        snapshot=lambda run_dir: snapshots[run_dir],
    )
    links = _campaign_typed_links(
        controller,
        _campaign(("run-valid", "run-missing", "run-wrong-project", "run-wrong-scope")),
        limit=128,
    )
    assert links == {
        "schemaVersion": 1,
        "references": [
            {"kind": "finding", "entityId": "finding-good", "runId": "run-valid",
             "surface": "outcomes"},
            {"kind": "hypothesis", "entityId": "hypothesis-good", "runId": "run-valid",
             "surface": "outcomes"},
            {"kind": "operator-decision", "entityId": "decision-good",
             "runId": "run-valid", "surface": "outcomes"},
            {"kind": "proof-bundle", "entityId": "dossier-good", "runId": "run-valid",
             "surface": "dossiers"},
        ],
        "totalCount": 4,
        "truncated": False,
        "sourceTruncated": False,
        "strongestReproducedResult": None,
        "currentResearchFocus": None,
    }
    encoded = json.dumps(links)
    assert "/Users/" not in encoded and "<script>" not in encoded


def test_typed_link_scan_and_output_are_deterministically_bounded(tmp_path):
    first = _run_dir(tmp_path, "run-first")
    later = _run_dir(tmp_path, "run-later")
    entities = tuple(
        {"entityType": "finding", "entityId": f"finding-{index:04d}"}
        for index in range(MAX_CAMPAIGN_TYPED_LINK_SCAN + 200)
    )
    snapshots = {
        first: _snapshot("run-first", entities=entities),
        later: _snapshot("run-later", entities=(
            {"entityType": "finding", "entityId": "finding-later-run"},
        )),
    }
    controller = SimpleNamespace(
        storage_root=tmp_path,
        snapshot=lambda run_dir: snapshots[run_dir],
    )
    links = _campaign_typed_links(
        controller, _campaign(("run-first", "run-later")), limit=128,
    )
    assert links["sourceTruncated"] is True and links["truncated"] is True
    assert links["totalCount"] == MAX_CAMPAIGN_TYPED_LINK_SCAN
    assert len(links["references"]) == 128
    assert {item["runId"] for item in links["references"]} == {"run-first"}
    assert links["references"] == sorted(
        links["references"], key=lambda item: (item["kind"], item["entityId"]),
    )[-128:]
