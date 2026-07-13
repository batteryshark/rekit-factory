from __future__ import annotations

from dataclasses import replace

import pytest

from rekit_factory.campaign_contracts import (
    CampaignCheckpoint,
    CampaignContract,
    CheckpointSource,
    CompletionCriteria,
    ComponentVersion,
    EpochContract,
    EpochResult,
    OperatorPolicy,
    ProgressSignal,
    ResourceBudget,
    ResourceLimit,
    ResourceUsage,
    ScopeBinding,
)
from rekit_factory.campaign_controller import (
    CONTROLLER_BOUNDARIES,
    CampaignController,
    CampaignControllerError,
    CampaignControllerInterrupted,
    CampaignRunRequest,
    EpochExecution,
)
from rekit_factory.campaign_persistence import CampaignPersistence
from rekit_factory.campaign_policy import (
    AttemptFact,
    CampaignPolicyConfig,
    CanonicalOutcomeTotals,
)


DIGEST = "a" * 64


def limit(value: int, unit: str) -> ResourceLimit:
    return ResourceLimit(value, unit)


def budget(*, work: int = 2, concurrency: int = 2, cost: int = 8) -> ResourceBudget:
    return ResourceBudget(
        limit(work, "items"), limit(concurrency, "workers"), limit(2, "attempts"),
        limit(1_000, "tokens"), limit(1_000, "tokens"), limit(cost, "cost-units"),
        limit(60, "seconds"), limit(4, "calls"), limit(0, "calls"),
        limit(10_000, "bytes"),
    )


def contract(project: str = "project-a", *, total_work: int = 6,
             total_cost: int = 24) -> CampaignContract:
    suffix = project[-1]
    return CampaignContract(
        project, "Finish the adversarial bounded campaign",
        ScopeBinding(f"scope-{suffix}", 1, suffix.encode().hex()[0] * 64),
        budget(work=min(2, total_work), cost=min(8, total_cost)),
        budget(work=total_work, cost=total_cost),
        CompletionCriteria(8_000, 1, 1, ("artifact-proof",)),
        OperatorPolicy(risk_threshold=60),
        (ComponentVersion("factory", "0.2.0", DIGEST),),
    )


def first_epoch(parent: CampaignContract, work: tuple[str, ...] = ("work-a",)) -> EpochContract:
    return EpochContract(parent.campaign_id, 1, work, parent.epoch_budget)


def signal(kind: str, reference: str, char: str) -> ProgressSignal:
    return ProgressSignal(kind, reference, char * 64)


class ScriptedRunner:
    def __init__(self, script=None):
        self.script = script
        self.calls: list[CampaignRunRequest] = []

    def run(self, request: CampaignRunRequest) -> EpochExecution:
        self.calls.append(request)
        if isinstance(self.script, BaseException):
            raise self.script
        if self.script is not None:
            return self.script(request)
        return execution(request)


def execution(request: CampaignRunRequest, *, usage: ResourceUsage | None = None,
              progress: tuple[ProgressSignal, ...] = (),
              next_actions: tuple[str, ...] = ()) -> EpochExecution:
    usage = usage or ResourceUsage(work_items=request.epoch.ordinal,
                                   cost_units=request.epoch.ordinal)
    source = CheckpointSource(
        "factory-ledger", request.epoch.ordinal, f"{request.epoch.ordinal:064x}",
    )
    checkpoint = CampaignCheckpoint(
        request.campaign.campaign_id, request.epoch.epoch_id,
        request.epoch.ordinal, (source,), usage,
    )
    result = EpochResult(
        request.epoch.epoch_id, checkpoint.checkpoint_id, progress, next_actions,
    )
    return EpochExecution(
        request.campaign.campaign_id, request.epoch.epoch_id, request.lease_id,
        f"factory-run-{request.epoch.ordinal}", request.campaign.project_id,
        request.campaign.scope, (source,), usage, result,
        (f"evidence-{request.epoch.ordinal}",),
    )


class FailOnce:
    def __init__(self, target: str):
        self.target = target
        self.seen = False

    def __call__(self, boundary: str) -> None:
        if boundary == self.target and not self.seen:
            self.seen = True
            raise CampaignControllerInterrupted(boundary)


def complete_script(request: CampaignRunRequest) -> EpochExecution:
    return execution(
        request,
        progress=(
            signal("coverage-moved", "coverage-proof", "b"),
            signal("hypothesis-resolved", "hypothesis-proof", "c"),
            signal("finding-reproduced", "finding-proof", "d"),
            signal("material-evidence", "artifact-proof", "e"),
        ),
    )


COMPLETE_TOTALS = CanonicalOutcomeTotals(8_000, 1, 1, ("artifact-proof",))


@pytest.mark.parametrize("boundary", CONTROLLER_BOUNDARIES)
def test_restart_at_every_boundary_applies_one_execution_recommendation_and_terminal(
        tmp_path, boundary):
    path = tmp_path / "campaign.db"
    runner = ScriptedRunner(complete_script)
    store = CampaignPersistence(path)
    parent = contract()
    controller = CampaignController(
        store, runner, owner_id="overnight-worker", fault_injector=FailOnce(boundary),
    )
    controller.start(parent)
    epoch = first_epoch(parent)

    if boundary == "launched":
        with pytest.raises(CampaignControllerInterrupted):
            controller.launch(epoch, ResourceUsage(work_items=1, cost_units=2))
    else:
        controller.launch(epoch, ResourceUsage(work_items=1, cost_units=2))
        with pytest.raises(CampaignControllerInterrupted):
            controller.step(parent.campaign_id, phase="recon", totals=COMPLETE_TOTALS)
    store.close()

    restarted_store = CampaignPersistence(path)
    restarted = CampaignController(restarted_store, runner, owner_id="overnight-worker")
    if boundary == "launched":
        restarted.launch(epoch, ResourceUsage(work_items=1, cost_units=2))
        result = restarted.step(parent.campaign_id, phase="recon", totals=COMPLETE_TOTALS)
    else:
        result = restarted.recover(parent.campaign_id)
        if result.status in {"running", "waiting"}:
            result = restarted.step(parent.campaign_id, phase="recon", totals=COMPLETE_TOTALS)

    assert result.status == "completed"
    assert result.terminal is not None
    assert result.terminal.reason_code == "completion-criteria-satisfied"
    assert len(runner.calls) == 1
    row = restarted_store.conn.execute(
        "select recommendation_id,recommendation_applied from "
        "factory_campaign_controller_epochs where epoch_id=?", (epoch.epoch_id,),
    ).fetchone()
    assert row[0].startswith("policy-") and row[1] == 1
    assert restarted_store.conn.execute(
        "select count(*) from factory_campaign_epochs where campaign_id=?",
        (parent.campaign_id,),
    ).fetchone()[0] == 1
    operations = restarted_store.conn.execute(
        "select operation_id from factory_campaign_events where campaign_id=?",
        (parent.campaign_id,),
    ).fetchall()
    assert len(operations) == len({row[0] for row in operations})


def test_reasoned_outcomes_remain_distinct_and_handoffs_preserve_policy_reason(tmp_path):
    store = CampaignPersistence(tmp_path / "campaign.db")
    config = CampaignPolicyConfig(no_novelty_ask_threshold=2, no_novelty_stop_threshold=4)

    def run_case(project: str, attempts: tuple[AttemptFact, ...], *,
                 runner: ScriptedRunner | None = None):
        parent = contract(project)
        active_runner = runner or ScriptedRunner()
        controller = CampaignController(
            store, active_runner, owner_id=f"worker-{project}", policy_config=config,
        )
        controller.start(parent)
        controller.launch(first_epoch(parent), ResourceUsage(work_items=1, cost_units=2))
        snapshot = controller.step(
            parent.campaign_id, phase="recon", totals=CanonicalOutcomeTotals(),
            attempts=attempts,
        )
        return controller, parent, snapshot

    no_progress = tuple(
        AttemptFact(f"np-{i}", f"{i:064x}", "no-novelty") for i in range(2)
    )
    controller, parent, snapshot = run_case("project-b", no_progress)
    assert snapshot.status == "waiting"
    assert controller.handoff(parent.campaign_id).reason_code == "no-novelty-threshold"

    blocked_attempts = tuple(
        AttemptFact(f"blocked-{i}", f"{i + 10:064x}", "dependency-blocked")
        for i in range(2)
    )
    controller, parent, snapshot = run_case("project-c", blocked_attempts)
    assert snapshot.status == "blocked"
    assert controller.handoff(parent.campaign_id).reason_code == "dependency-deadlock"

    stopped_attempts = tuple(
        AttemptFact(f"stop-{i}", f"{i + 20:064x}", "no-novelty") for i in range(4)
    )
    controller, parent, snapshot = run_case("project-d", stopped_attempts)
    assert snapshot.status == "policy-stopped"
    assert snapshot.terminal.reason_code == "no-novelty-policy-limit"

    parent = contract("project-e")
    controller = CampaignController(store, ScriptedRunner(), owner_id="worker-operator")
    controller.start(parent)
    stopped = controller.stop(parent.campaign_id, "operator-requested", ("decision-stop",))
    assert stopped.status == "stopped"
    assert stopped.terminal.reason_code == "operator-requested"

    exhausted_parent = contract("project-f", total_work=2, total_cost=2)
    exhausted_runner = ScriptedRunner(
        lambda request: execution(
            request, usage=ResourceUsage(work_items=2, cost_units=2),
        )
    )
    controller = CampaignController(store, exhausted_runner, owner_id="worker-exhausted")
    controller.start(exhausted_parent)
    controller.launch(
        first_epoch(exhausted_parent), ResourceUsage(work_items=2, cost_units=2),
    )
    exhausted = controller.step(
        exhausted_parent.campaign_id, phase="recon", totals=CanonicalOutcomeTotals(),
    )
    assert exhausted.status == "exhausted"
    assert exhausted.terminal.reason_code == "cumulative-budget-exhausted"

    failed_parent = contract("project-9")
    controller = CampaignController(
        store, ScriptedRunner(RuntimeError("host vanished")), owner_id="worker-failed",
    )
    controller.start(failed_parent)
    controller.launch(first_epoch(failed_parent), ResourceUsage(work_items=1, cost_units=2))
    failed = controller.step(
        failed_parent.campaign_id, phase="recon", totals=CanonicalOutcomeTotals(),
    )
    assert failed.status == "failed"
    assert failed.terminal.reason_code == "infrastructure-failure"


def test_campaigns_cannot_borrow_owner_runner_scope_or_storage(tmp_path):
    store = CampaignPersistence(tmp_path / "campaign.db")
    first, second = contract("project-a"), contract("project-b")
    runner_a, runner_b = ScriptedRunner(complete_script), ScriptedRunner(complete_script)
    controller_a = CampaignController(store, runner_a, owner_id="owner-a")
    controller_b = CampaignController(store, runner_b, owner_id="owner-b")
    for controller, parent in ((controller_a, first), (controller_b, second)):
        controller.start(parent)
        controller.launch(first_epoch(parent), ResourceUsage(work_items=1, cost_units=2))

    with pytest.raises(CampaignControllerError, match="owner"):
        controller_a.step(second.campaign_id, phase="recon", totals=COMPLETE_TOTALS)
    assert runner_a.calls == []
    assert store.campaign(first.campaign_id).latest_checkpoint_id is None
    assert store.campaign(second.campaign_id).latest_checkpoint_id is None

    completed_b = controller_b.step(second.campaign_id, phase="recon", totals=COMPLETE_TOTALS)
    assert completed_b.status == "completed"
    assert store.campaign(first.campaign_id).status == "running"
    assert controller_a.step(first.campaign_id, phase="recon", totals=COMPLETE_TOTALS).status \
        == "completed"
    assert {request.campaign.campaign_id for request in runner_a.calls} == {first.campaign_id}
    assert {request.campaign.campaign_id for request in runner_b.calls} == {second.campaign_id}


def test_budget_checks_fail_before_spend_and_actual_usage_cannot_exceed_reservation(tmp_path):
    store = CampaignPersistence(tmp_path / "campaign.db")
    parent = contract(total_work=2, total_cost=3)
    runner = ScriptedRunner(
        lambda request: execution(
            request, usage=ResourceUsage(work_items=1, cost_units=3),
        )
    )
    controller = CampaignController(store, runner, owner_id="budget-owner")
    controller.start(parent)
    epoch = first_epoch(parent)

    with pytest.raises(CampaignControllerError, match="epoch budget"):
        controller.launch(epoch, ResourceUsage(work_items=1, cost_units=9))
    assert store.conn.execute(
        "select count(*) from factory_campaign_epochs where campaign_id=?",
        (parent.campaign_id,),
    ).fetchone()[0] == 0

    controller.launch(epoch, ResourceUsage(work_items=1, cost_units=2))
    with pytest.raises(CampaignControllerError, match="actual usage exceeds"):
        controller.step(parent.campaign_id, phase="recon", totals=CanonicalOutcomeTotals())
    assert store.campaign(parent.campaign_id).cumulative_usage == ResourceUsage()
    assert store.campaign(parent.campaign_id).latest_checkpoint_id is None


@pytest.mark.parametrize("field", ResourceUsage._ATTRIBUTES)
def test_every_reserved_resource_dimension_is_rejected_before_epoch_publication(tmp_path, field):
    store = CampaignPersistence(tmp_path / f"{field}.db")
    parent = contract()
    controller = CampaignController(store, ScriptedRunner(), owner_id=f"owner-{field}")
    controller.start(parent)
    epoch = first_epoch(parent)
    over = replace(
        ResourceUsage(work_items=1),
        **{field: getattr(epoch.budget, field).value + 1},
    )
    with pytest.raises(CampaignControllerError, match="epoch budget"):
        controller.launch(epoch, over)
    assert store.conn.execute(
        "select count(*) from factory_campaign_epochs where campaign_id=?",
        (parent.campaign_id,),
    ).fetchone()[0] == 0


def test_concurrency_and_remaining_cumulative_budget_never_publish_extra_work(tmp_path):
    store = CampaignPersistence(tmp_path / "campaign.db")
    base = contract(total_work=3, total_cost=12)
    wider_epoch_budget = replace(base.epoch_budget, work_items=limit(3, "items"))
    parent = replace(base, epoch_budget=wider_epoch_budget)
    controller = CampaignController(store, ScriptedRunner(), owner_id="ceiling-owner")
    controller.start(parent)
    too_parallel = EpochContract(
        parent.campaign_id, 1, ("work-a", "work-b", "work-c"), parent.epoch_budget,
    )
    with pytest.raises(CampaignControllerError, match="concurrency ceiling"):
        controller.launch(too_parallel, ResourceUsage(work_items=3, cost_units=3))
    assert store.conn.execute(
        "select count(*) from factory_campaign_epochs where campaign_id=?",
        (parent.campaign_id,),
    ).fetchone()[0] == 0

    bounded = contract("project-z", total_work=2, total_cost=4)
    runner = ScriptedRunner(lambda request: execution(
        request, usage=ResourceUsage(work_items=1, cost_units=1),
        next_actions=("next-a", "next-b"),
    ))
    campaign = CampaignController(store, runner, owner_id="remaining-owner")
    campaign.start(bounded)
    campaign.launch(first_epoch(bounded), ResourceUsage(work_items=1, cost_units=1))
    result = campaign.step(
        bounded.campaign_id, phase="recon", totals=CanonicalOutcomeTotals(),
    )
    assert result.status == "exhausted"
    assert result.terminal.reason_code == "insufficient-next-epoch-budget"
    assert store.conn.execute(
        "select count(*) from factory_campaign_epochs where campaign_id=?",
        (bounded.campaign_id,),
    ).fetchone()[0] == 1


def test_durable_recommendation_wins_retry_with_changed_inputs_exactly_once(tmp_path):
    store = CampaignPersistence(tmp_path / "campaign.db")
    parent = contract()
    runner = ScriptedRunner()
    fault = FailOnce("recommendation-staged")
    controller = CampaignController(
        store, runner, owner_id="policy-owner", fault_injector=fault,
    )
    controller.start(parent)
    controller.launch(first_epoch(parent), ResourceUsage(work_items=1, cost_units=2))
    with pytest.raises(CampaignControllerInterrupted):
        controller.step(parent.campaign_id, phase="recon", totals=CanonicalOutcomeTotals())

    restarted = CampaignController(store, runner, owner_id="policy-owner")
    result = restarted.step(
        parent.campaign_id, phase="recon", totals=CanonicalOutcomeTotals(),
        known_progress_digests=("f" * 64,),
    )
    assert result.status == "blocked"
    assert len(runner.calls) == 1
    row = store.conn.execute(
        "select policy_input_digest,recommendation_applied from "
        "factory_campaign_controller_epochs where campaign_id=?", (parent.campaign_id,),
    ).fetchone()
    assert len(row[0]) == 64 and row[1] == 1


def test_orphaned_lease_never_reinvokes_runner_or_infers_completion(tmp_path):
    path = tmp_path / "campaign.db"
    store = CampaignPersistence(path)
    parent = contract()
    first_runner = ScriptedRunner()
    controller = CampaignController(store, first_runner, owner_id="orphan-owner")
    controller.start(parent)
    controller.launch(first_epoch(parent), ResourceUsage(work_items=1, cost_units=2))
    store.close()

    restarted_store = CampaignPersistence(path)
    restarted_runner = ScriptedRunner()
    restarted = CampaignController(
        restarted_store, restarted_runner, owner_id="orphan-owner",
    )
    assert restarted.recover(parent.campaign_id).status == "waiting"
    assert restarted.recover(parent.campaign_id).status == "waiting"
    with pytest.raises(CampaignControllerError, match="orphaned execution"):
        restarted.step(parent.campaign_id, phase="recon", totals=CanonicalOutcomeTotals())
    assert first_runner.calls == [] and restarted_runner.calls == []
    projection = restarted_store.campaign(parent.campaign_id)
    assert projection.latest_checkpoint_id is None and projection.terminal is None


def test_pause_and_stop_win_without_leaking_runner_work(tmp_path):
    store = CampaignPersistence(tmp_path / "campaign.db")

    paused_parent = contract("project-p")
    paused_runner = ScriptedRunner(complete_script)
    paused = CampaignController(store, paused_runner, owner_id="pause-owner")
    paused.start(paused_parent)
    paused.launch(first_epoch(paused_parent), ResourceUsage(work_items=1, cost_units=2))
    assert paused.pause(paused_parent.campaign_id).status == "suspended"
    with pytest.raises(CampaignControllerError, match="not runnable"):
        paused.step(paused_parent.campaign_id, phase="recon", totals=COMPLETE_TOTALS)
    assert paused_runner.calls == []
    assert paused.resume(paused_parent.campaign_id).status == "running"
    assert paused.step(paused_parent.campaign_id, phase="recon", totals=COMPLETE_TOTALS).status \
        == "completed"

    stopped_parent = contract("project-s")
    stopped_runner = ScriptedRunner(complete_script)
    stopped = CampaignController(store, stopped_runner, owner_id="stop-owner")
    stopped.start(stopped_parent)
    stopped.launch(first_epoch(stopped_parent), ResourceUsage(work_items=1, cost_units=2))
    result = stopped.stop(stopped_parent.campaign_id, "operator-requested", ("decision-s",))
    assert result.status == "stopped"
    with pytest.raises(CampaignControllerError, match="not runnable"):
        stopped.step(stopped_parent.campaign_id, phase="recon", totals=COMPLETE_TOTALS)
    assert stopped_runner.calls == []


def test_pause_gates_a_staged_recommendation_until_explicit_resume(tmp_path):
    store = CampaignPersistence(tmp_path / "campaign.db")
    parent = contract()
    runner = ScriptedRunner()
    controller = CampaignController(
        store, runner, owner_id="staged-pause-owner",
        fault_injector=FailOnce("recommendation-staged"),
    )
    controller.start(parent)
    controller.launch(first_epoch(parent), ResourceUsage(work_items=1, cost_units=2))
    with pytest.raises(CampaignControllerInterrupted):
        controller.step(parent.campaign_id, phase="recon", totals=CanonicalOutcomeTotals())

    restarted = CampaignController(store, runner, owner_id="staged-pause-owner")
    assert restarted.pause(parent.campaign_id).status == "suspended"
    with pytest.raises(CampaignControllerError, match="not runnable"):
        restarted.step(parent.campaign_id, phase="recon", totals=CanonicalOutcomeTotals())
    row = store.conn.execute(
        "select recommendation_applied from factory_campaign_controller_epochs "
        "where campaign_id=?", (parent.campaign_id,),
    ).fetchone()
    assert row[0] == 0
    assert restarted.resume(parent.campaign_id).status == "running"
    assert restarted.step(
        parent.campaign_id, phase="recon", totals=CanonicalOutcomeTotals(),
    ).status == "blocked"
    assert len(runner.calls) == 1


def test_operator_stop_supersedes_staged_recommendation_without_policy_override(tmp_path):
    store = CampaignPersistence(tmp_path / "campaign.db")
    parent = contract()
    runner = ScriptedRunner()
    controller = CampaignController(
        store, runner, owner_id="staged-stop-owner",
        fault_injector=FailOnce("recommendation-staged"),
    )
    controller.start(parent)
    controller.launch(first_epoch(parent), ResourceUsage(work_items=1, cost_units=2))
    with pytest.raises(CampaignControllerInterrupted):
        controller.step(parent.campaign_id, phase="recon", totals=CanonicalOutcomeTotals())

    restarted = CampaignController(store, runner, owner_id="staged-stop-owner")
    stopped = restarted.stop(
        parent.campaign_id, "operator-requested", ("decision-staged-stop",),
    )
    assert stopped.status == "stopped"
    assert stopped.terminal.reason_code == "operator-requested"
    assert restarted.recover(parent.campaign_id).status == "stopped"
    row = store.conn.execute(
        "select recommendation_applied,recommendation_disposition from "
        "factory_campaign_controller_epochs where campaign_id=?", (parent.campaign_id,),
    ).fetchone()
    assert tuple(row) == (1, "superseded-by-operator-stop")
    assert store.conn.execute(
        "select count(*) from factory_campaign_events where campaign_id=? "
        "and kind='campaign.transitioned' and operation_id like 'apply:%'",
        (parent.campaign_id,),
    ).fetchone()[0] == 0
    assert len(runner.calls) == 1
