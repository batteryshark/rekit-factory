from __future__ import annotations

import json

import pytest

from rekit_factory.campaign_contracts import (
    CampaignCheckpoint, CampaignRiskAssessment, CheckpointSource, CompletionCriteria,
    EpochContract, EpochResult, ProgressSignal, ResourceUsage,
)
from rekit_factory.campaign_controller import (
    CampaignController, CampaignControllerInterrupted, EpochExecution,
)
from rekit_factory.campaign_persistence import CampaignPersistence, CampaignPersistenceError
from rekit_factory.campaign_policy import CanonicalOutcomeTotals

from test_campaign_controller import DIGEST, Runner, budget, contract, setup


class RiskRunner:
    """Explicit checkpoint producer used in place of any inferred risk heuristic."""

    def __init__(self, scores=(5, 20), revisions=(1, 2)):
        self.scores = scores
        self.revisions = revisions
        self.calls = 0

    def run(self, request):
        index = request.epoch.ordinal - 1
        self.calls += 1
        source = CheckpointSource(
            "risk-engine", self.revisions[index], f"{self.revisions[index]:064x}",
        )
        usage = ResourceUsage(
            work_items=request.committed_usage.work_items + 1,
            cost_units=request.committed_usage.cost_units + 5,
        )
        checkpoint = CampaignCheckpoint(
            request.campaign.campaign_id, request.epoch.epoch_id,
            request.epoch.ordinal, (source,), usage,
        )
        signal = ProgressSignal(
            "coverage-moved", f"coverage-{request.epoch.ordinal}",
            f"{request.epoch.ordinal + 20:064x}",
        )
        result = EpochResult(
            request.epoch.epoch_id, checkpoint.checkpoint_id, (signal,),
            ("work-b",) if request.epoch.ordinal == 1 else (),
        )
        evidence_id = f"private-risk-evidence-{request.epoch.ordinal}"
        return EpochExecution(
            request.campaign.campaign_id, request.epoch.epoch_id, request.lease_id,
            f"run-{request.epoch.ordinal}", request.campaign.project_id,
            request.campaign.scope, (source,), usage, result,
            (f"epoch-evidence-{request.epoch.ordinal}",),
            CampaignRiskAssessment(
                self.scores[index], "risk-engine", (evidence_id,),
            ),
        )


def test_real_checkpoint_progress_automatically_publishes_private_risk_and_notifies_once(
        tmp_path):
    runner = RiskRunner()
    path = tmp_path / "automatic.db"
    persistence = CampaignPersistence(path)
    controller = CampaignController(persistence, runner, owner_id="risk-producer")
    parent = contract(completion=CompletionCriteria(2, 0, 0))
    controller.start(parent)
    controller.public_state(parent.campaign_id)  # canonical notification hydration
    epoch = EpochContract(parent.campaign_id, 1, ("work-a",), budget())
    controller.launch(epoch, ResourceUsage(work_items=1, cost_units=10))

    controller.step(
        parent.campaign_id, phase="recon",
        totals=CanonicalOutcomeTotals(coverage_basis_points=1),
    )
    controller.step(
        parent.campaign_id, phase="recon",
        totals=CanonicalOutcomeTotals(coverage_basis_points=2),
        previous_totals=CanonicalOutcomeTotals(coverage_basis_points=1),
    )
    state = controller.public_state(parent.campaign_id)

    rows = persistence.conn.execute(
        "select measurement_json from factory_campaign_risk_measurements order by sequence"
    ).fetchall()
    assert len(rows) == 2
    assert [json.loads(row[0])["score"] for row in rows] == [5, 20]
    assert [json.loads(row[0])["source"]["revision"] for row in rows] == [1, 2]
    assert "private-risk-evidence" not in json.dumps(state)
    assert all("evidence" not in json.dumps(json.loads(row[0])).lower() for row in rows)
    assert "private-risk-evidence-2" in persistence.conn.execute(
        "select execution_json from factory_campaign_controller_epochs "
        "where campaign_id=? order by rowid desc limit 1", (parent.campaign_id,),
    ).fetchone()[0]
    assert persistence.conn.execute(
        "select count(*) from factory_notification_outbox "
        "where kind='campaign.risk-threshold'"
    ).fetchone()[0] == 1

    restarted = CampaignController(
        type(persistence)(path), runner, owner_id="risk-producer",
    )
    restarted.public_state(parent.campaign_id)
    assert restarted.persistence.conn.execute(
        "select count(*) from factory_notification_outbox "
        "where kind='campaign.risk-threshold'"
    ).fetchone()[0] == 1
    assert restarted.persistence.rebuild_projection(parent.campaign_id).matches_live


def test_risk_commit_restart_replays_without_rerunning_or_duplicate_measurement(tmp_path):
    class Once:
        fired = False

        def __call__(self, boundary):
            if boundary == "risk-recorded" and not self.fired:
                self.fired = True
                raise CampaignControllerInterrupted(boundary)

    runner = RiskRunner(scores=(20,), revisions=(1,))
    controller, store, lifecycle, _runner, parent, _epoch = setup(
        tmp_path, runner=runner, fault=Once(),
    )
    with pytest.raises(CampaignControllerInterrupted):
        controller.step(
            parent.campaign_id, phase="recon",
            totals=CanonicalOutcomeTotals(coverage_basis_points=1),
        )
    assert store.conn.execute(
        "select count(*) from factory_campaign_risk_measurements"
    ).fetchone()[0] == 1

    restarted = CampaignController(
        store, runner, owner_id="controller-a", lifecycle=lifecycle,
    )
    result = restarted.step(
        parent.campaign_id, phase="recon",
        totals=CanonicalOutcomeTotals(coverage_basis_points=1),
    )
    assert result.status == "completed"
    assert runner.calls == 1
    assert store.conn.execute(
        "select count(*) from factory_campaign_risk_measurements"
    ).fetchone()[0] == 1


def test_regressed_checkpoint_source_cannot_change_measurement_stream(tmp_path):
    runner = RiskRunner(scores=(5, 20), revisions=(2, 1))
    store = CampaignPersistence(tmp_path / "regression.db")
    controller = CampaignController(store, runner, owner_id="risk-producer")
    parent = contract(completion=CompletionCriteria(2, 0, 0))
    controller.start(parent)
    epoch = EpochContract(parent.campaign_id, 1, ("work-a",), budget())
    controller.launch(epoch, ResourceUsage(work_items=1, cost_units=10))
    controller.step(
        parent.campaign_id, phase="recon",
        totals=CanonicalOutcomeTotals(coverage_basis_points=1),
    )
    with pytest.raises(CampaignPersistenceError, match="source revision regressed"):
        controller.step(
            parent.campaign_id, phase="recon",
            totals=CanonicalOutcomeTotals(coverage_basis_points=2),
            previous_totals=CanonicalOutcomeTotals(coverage_basis_points=1),
        )
    assert store.conn.execute(
        "select count(*) from factory_campaign_risk_measurements"
    ).fetchone()[0] == 1
    assert store.campaign(parent.campaign_id).measured_risk.score == 5


def test_missing_explicit_assessment_never_fabricates_score_from_progress(tmp_path):
    controller, store, _lifecycle, _runner, parent, _epoch = setup(
        tmp_path, runner=Runner(),
    )
    controller.step(
        parent.campaign_id, phase="recon",
        totals=CanonicalOutcomeTotals(coverage_basis_points=1),
    )
    assert store.campaign(parent.campaign_id).measured_risk is None
    assert store.conn.execute(
        "select count(*) from factory_campaign_risk_measurements"
    ).fetchone()[0] == 0
