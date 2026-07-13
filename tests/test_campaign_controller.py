from __future__ import annotations

from dataclasses import replace

import pytest

from rekit_factory.campaign_contracts import (
    CampaignCheckpoint, CampaignContract, CheckpointSource, CompletionCriteria,
    ComponentVersion, EpochContract, EpochResult, OperatorPolicy, ProgressSignal,
    ResourceBudget, ResourceLimit, ResourceUsage, ScopeBinding,
)
from rekit_factory.campaign_controller import (
    CampaignController, CampaignControllerError, CampaignControllerInterrupted,
    EpochExecution,
)
from rekit_factory.campaign_lifecycle import CampaignLifecycleStore
from rekit_factory.campaign_persistence import CampaignPersistence
from rekit_factory.campaign_policy import CanonicalOutcomeTotals


DIGEST = "a" * 64


def limit(value, unit):
    return ResourceLimit(value, unit)


def budget(work=2, cost=20, concurrency=2):
    return ResourceBudget(
        limit(work, "items"), limit(concurrency, "workers"), limit(1, "attempts"),
        limit(100, "tokens"), limit(100, "tokens"), limit(cost, "cost-units"),
        limit(60, "seconds"), limit(4, "calls"), limit(0, "calls"), limit(100, "bytes"),
    )


def contract(*, completion=None):
    return CampaignContract(
        "project-a", "Finish a bounded campaign", ScopeBinding("scope-a", 1, DIGEST),
        budget(), budget(4, 40), completion or CompletionCriteria(1, 0, 0),
        OperatorPolicy(risk_threshold=10),
        (ComponentVersion("factory", "1", DIGEST),),
    )


class Runner:
    def __init__(self, *, progress=True, next_actions=()):
        self.calls = 0
        self.progress = progress
        self.next_actions = next_actions

    def run(self, request):
        self.calls += 1
        usage = ResourceUsage(work_items=len(request.epoch.work_ids), cost_units=5)
        signal = ProgressSignal("coverage-moved", "coverage-a", "b" * 64)
        sources = (CheckpointSource("factory-ledger", request.epoch.ordinal, DIGEST),)
        checkpoint = CampaignCheckpoint(request.campaign.campaign_id, request.epoch.epoch_id,
                                        request.epoch.ordinal, sources, usage)
        result = EpochResult(request.epoch.epoch_id, checkpoint.checkpoint_id,
                             (signal,) if self.progress else (), self.next_actions)
        return EpochExecution(
            request.campaign.campaign_id, request.epoch.epoch_id, request.lease_id,
            f"run-{request.epoch.ordinal}", request.campaign.project_id,
            request.campaign.scope, sources, usage, result, ("evidence-a",),
        )


def setup(tmp_path, runner=None, fault=None):
    store = CampaignPersistence(tmp_path / "factory.db")
    lifecycle = CampaignLifecycleStore(tmp_path / "lifecycle")
    runner = runner or Runner()
    controller = CampaignController(store, runner, owner_id="controller-a",
                                    lifecycle=lifecycle, fault_injector=fault)
    parent = contract()
    controller.start(parent)
    epoch = EpochContract(parent.campaign_id, 1, ("work-a",), budget())
    controller.launch(epoch, ResourceUsage(work_items=1, cost_units=10))
    return controller, store, lifecycle, runner, parent, epoch


def test_success_is_checkpointed_once_and_projects_content_bound_lifecycle(tmp_path):
    controller, store, lifecycle, runner, parent, epoch = setup(tmp_path)
    result = controller.step(
        parent.campaign_id, phase="recon",
        totals=CanonicalOutcomeTotals(coverage_basis_points=1),
    )
    assert result.status == "completed"
    assert result.latest_checkpoint_id
    assert runner.calls == 1
    assert store.rebuild_projection(parent.campaign_id).matches_live
    record = next(item for item in lifecycle.load().campaigns
                  if item.campaign_id == parent.campaign_id)
    assert record.state == "completed"
    assert controller.handoff(parent.campaign_id).factory_run_ids == ("run-1",)


@pytest.mark.parametrize("boundary", (
    "execution-staged", "checkpointed", "recommendation-staged", "recommendation-effect",
    "recommendation-applied",
))
def test_restart_reconciles_every_post_runner_boundary_without_duplicate_run(tmp_path, boundary):
    class Once:
        fired = False
        def __call__(self, value):
            if value == boundary and not self.fired:
                self.fired = True
                raise CampaignControllerInterrupted(value)

    fault = Once()
    controller, store, lifecycle, runner, parent, epoch = setup(tmp_path, fault=fault)
    with pytest.raises(CampaignControllerInterrupted):
        controller.step(parent.campaign_id, phase="recon",
                        totals=CanonicalOutcomeTotals(coverage_basis_points=1))
    restarted = CampaignController(store, runner, owner_id="controller-a", lifecycle=lifecycle)
    if store.campaign(parent.campaign_id).status == "running":
        restarted.recover(parent.campaign_id)
    result = restarted.step(parent.campaign_id, phase="recon",
                            totals=CanonicalOutcomeTotals(coverage_basis_points=1))
    assert result.status == "completed"
    assert runner.calls == 1
    assert store.conn.execute(
        "select count(*) from factory_campaign_checkpoints where campaign_id=?",
        (parent.campaign_id,),
    ).fetchone()[0] == 1
    assert store.rebuild_projection(parent.campaign_id).matches_live


def test_orphan_without_staged_execution_waits_and_never_reinvokes_runner(tmp_path):
    controller, store, lifecycle, runner, parent, epoch = setup(tmp_path)
    assert controller.recover(parent.campaign_id).status == "waiting"
    with pytest.raises(CampaignControllerError, match="exact durable"):
        controller.step(parent.campaign_id, phase="recon", totals=CanonicalOutcomeTotals())
    assert runner.calls == 0


def test_reservations_parallel_scope_and_budget_fail_closed_before_publish(tmp_path):
    controller, store, lifecycle, runner, parent, first = setup(tmp_path)
    invalid = EpochContract(parent.campaign_id, 2, ("work-b", "work-c"),
                            budget(concurrency=1), "checkpoint-placeholder")
    with pytest.raises(CampaignControllerError, match="concurrency"):
        controller.launch(invalid, ResourceUsage(work_items=2, cost_units=1))
    oversized = EpochContract(parent.campaign_id, 2, ("work-b",), budget(),
                              "checkpoint-placeholder")
    with pytest.raises(CampaignControllerError, match="epoch budget"):
        controller.launch(oversized, ResourceUsage(work_items=1, cost_units=21))
    assert store.conn.execute(
        "select count(*) from factory_campaign_epochs where campaign_id=?", (parent.campaign_id,),
    ).fetchone()[0] == 1


def test_pause_resume_operator_stop_and_infrastructure_failure_are_distinct(tmp_path):
    controller, store, lifecycle, runner, parent, epoch = setup(tmp_path)
    assert controller.pause(parent.campaign_id).status == "suspended"
    assert controller.resume(parent.campaign_id).status == "running"
    stopped = controller.stop(parent.campaign_id, "operator-requested", ("decision-a",))
    assert stopped.status == "stopped" and stopped.terminal.reason_code == "operator-requested"

    class Broken:
        def run(self, request):
            raise RuntimeError("host unavailable")
    second_store = CampaignPersistence(tmp_path / "broken.db")
    broken = CampaignController(second_store, Broken(), owner_id="controller-b")
    second = replace(parent, goal="Finish another bounded campaign")
    broken.start(second)
    second_epoch = EpochContract(second.campaign_id, 1, ("work-a",), budget())
    broken.launch(second_epoch, ResourceUsage(work_items=1, cost_units=10))
    failed = broken.step(second.campaign_id, phase="recon", totals=CanonicalOutcomeTotals())
    assert failed.status == "failed"
    assert failed.terminal.reason_code == "infrastructure-failure"
