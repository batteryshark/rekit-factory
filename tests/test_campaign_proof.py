from __future__ import annotations

import json
from types import SimpleNamespace

from rekit_factory.campaign_proof import CampaignOwnedProofResolver
from rekit_factory.outcomes import project_outcomes


DIGEST = "a" * 64
SCOPE = {"scopeId": "scope-a", "revision": 1, "digest": DIGEST}


def _projection(*, run_id="run-proof", proof_id="dossier-a", published=True,
                reproduced=True, with_proof=True):
    return project_outcomes(
        run={"id": run_id, "status": "completed"}, workers=(), work_items=(),
        memory={"findings": {"finding-a": {
            "id": "finding-a", "status": "reproduced" if reproduced else "candidate",
        }}},
        dossiers=([{
            "id": proof_id, "findingId": "finding-a",
            "verificationStatus": "published" if published else "draft",
        }] if with_proof else []), pending_questions=(),
    )


class _Campaigns:
    def __init__(self, contexts):
        self.contexts = contexts

    def notification_proof_contexts(self, _source_run_id):
        return self.contexts


class _Factory:
    def __init__(self, root, snapshots):
        self.storage_root = root
        self.snapshots = snapshots
        self.admission_flags = []

    def snapshot(self, run_dir, *, admit_notifications=True):
        self.admission_flags.append(admit_notifications)
        return self.snapshots[run_dir.name]


def _factory(tmp_path, projections):
    snapshots = {}
    for run_id, projection in projections.items():
        run_dir = tmp_path / "projects" / "project-a" / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(json.dumps({
            "runId": run_id, "creationComplete": True,
        }))
        snapshots[run_id] = {
            "run": {"id": run_id},
            "meta": {"runId": run_id, "projectId": "project-a", "scope": SCOPE},
            "outcomeProjection": projection,
        }
    return _Factory(tmp_path, snapshots)


def _context(*run_ids):
    return ({
        "campaignId": "campaign-a", "projectId": "project-a", "scope": SCOPE,
        "factoryRunIds": list(run_ids),
    },)


def test_resolves_one_exact_published_cross_run_proof_without_notification_recursion(tmp_path):
    factory = _factory(tmp_path, {
        "run-source": _projection(run_id="run-source", with_proof=False),
        "run-proof": _projection(),
    })
    resolver = CampaignOwnedProofResolver(
        _Campaigns(_context("run-source", "run-proof")), factory,
    )

    assert resolver("run-source", "finding-a") == ("run-proof", "dossier-a")
    assert factory.admission_flags == [False, False]


def test_missing_ownership_unproven_and_cross_scope_proofs_fail_closed(tmp_path):
    factory = _factory(tmp_path, {
        "run-source": _projection(run_id="run-source", with_proof=False),
        "run-proof": _projection(reproduced=False),
    })
    resolver = CampaignOwnedProofResolver(
        _Campaigns(_context("run-source", "run-proof")), factory,
    )
    assert resolver("run-source", "finding-a") is None

    factory.snapshots["run-proof"]["outcomeProjection"] = _projection()
    factory.snapshots["run-proof"]["meta"]["scope"] = {**SCOPE, "digest": "b" * 64}
    assert resolver("run-source", "finding-a") is None
    assert CampaignOwnedProofResolver(
        _Campaigns(_context("run-unrelated", "run-proof")), factory,
    )("run-source", "finding-a") is None


def test_multiple_campaigns_or_multiple_proofs_never_guess(tmp_path):
    factory = _factory(tmp_path, {
        "run-source": _projection(run_id="run-source", with_proof=False),
        "run-proof": _projection(proof_id="dossier-a"),
        "run-proof-two": project_outcomes(
            run={"id": "run-proof-two", "status": "completed"},
            workers=(), work_items=(),
            memory={"findings": {"finding-a": {
                "id": "finding-a", "status": "reproduced",
            }}},
            dossiers=[{"id": "dossier-b", "findingId": "finding-a",
                       "verificationStatus": "published"}], pending_questions=(),
        ),
    })
    ambiguous_proofs = CampaignOwnedProofResolver(
        _Campaigns(_context("run-source", "run-proof", "run-proof-two")), factory,
    )
    assert ambiguous_proofs("run-source", "finding-a") is None

    two_campaigns = (_context("run-source", "run-proof")[0], {
        **_context("run-source", "run-proof")[0], "campaignId": "campaign-b",
    })
    assert CampaignOwnedProofResolver(
        _Campaigns(two_campaigns), factory,
    )("run-source", "finding-a") is None
