from __future__ import annotations

from dataclasses import replace
import json

import pytest

from rekit_factory.campaign_contracts import (
    CampaignCheckpoint, CheckpointSource, EpochContract, ResourceUsage,
)
from rekit_factory.campaign_controller import (
    CampaignController, CampaignControllerError, CampaignControllerInterrupted,
)
from rekit_factory.campaign_persistence import CampaignPersistenceError
from rekit_factory.campaign_policy import (
    AttemptFact, CampaignPolicyInput, CanonicalOutcomeTotals, MAX_POLICY_FACTS,
)

from test_campaign_controller import DIGEST, Runner, budget, contract, setup
from test_campaign_policy import policy_input


def test_applied_recommendation_persists_exact_bounded_health(tmp_path):
    controller, store, _lifecycle, _runner, parent, _epoch = setup(tmp_path)
    snapshot = controller.step(
        parent.campaign_id, phase="recon",
        totals=CanonicalOutcomeTotals(coverage_basis_points=1),
    )
    health = store.health(parent.campaign_id)
    assert snapshot.status == "completed"
    assert health.total_observations == 1
    assert health.current is not None
    assert health.current.coverage_basis_points == 1
    assert health.current.artifact_ids == ()
    assert health.current.elapsed_wall_seconds == 0
    assert health.current.next_checkpoint_expected_wall_seconds is None
    assert health.current.policy_input_digest
    assert health.current.recommendation_id == snapshot.recommendation_id
    assert store.rebuild_projection(parent.campaign_id).matches_live
    public = controller.public_state(parent.campaign_id)["health"]
    assert public["totalObservations"] == 1
    assert public["current"]["artifactCount"] == 0
    assert "artifactIds" not in public["current"]
    assert "policyInputDigest" not in public["current"]
    assert public["previous"] is None


def test_terminal_recommendation_rejects_a_fabricated_next_checkpoint(tmp_path):
    controller, _store, _lifecycle, _runner, parent, _epoch = setup(tmp_path)
    with pytest.raises(CampaignControllerError, match="schedules work"):
        controller.step(
            parent.campaign_id, phase="recon",
            totals=CanonicalOutcomeTotals(coverage_basis_points=1),
            next_checkpoint_expected_wall_seconds=20,
        )


def test_exact_epoch_launch_retry_does_not_double_count_its_reservation(tmp_path):
    controller, store, _lifecycle, _runner, _parent, epoch = setup(tmp_path)
    reservation = ResourceUsage(work_items=1, cost_units=10)
    first = controller.launch(epoch, reservation)
    retry = controller.launch(epoch, reservation)
    assert retry == first
    assert store.conn.execute(
        "select count(*) from factory_campaign_controller_epochs where epoch_id=?",
        (epoch.epoch_id,),
    ).fetchone()[0] == 1
    with pytest.raises(CampaignControllerError, match="conflicts"):
        controller.launch(epoch, ResourceUsage(work_items=1, cost_units=9))


def test_novel_progress_resets_no_progress_while_retry_count_is_input_local(tmp_path):
    attempts = (
        AttemptFact("attempt-a", "f" * 64, "no-novelty"),
        AttemptFact("attempt-b", "f" * 64, "no-novelty"),
    )
    controller, store, _lifecycle, _runner, parent, _epoch = setup(tmp_path)
    controller.step(parent.campaign_id, phase="recon",
                    totals=CanonicalOutcomeTotals(coverage_basis_points=1),
                    attempts=attempts)
    health = store.health(parent.campaign_id).current
    assert health is not None
    assert health.retry_count == 1
    assert health.no_progress_count == 0


@pytest.mark.parametrize("boundary", ("health-recorded", "recommendation-applied"))
def test_health_crash_recovery_is_exact_and_never_reruns_work(tmp_path, boundary):
    class Once:
        fired = False

        def __call__(self, value):
            if value == boundary and not self.fired:
                self.fired = True
                raise CampaignControllerInterrupted(value)

    runner = Runner()
    controller, store, lifecycle, _runner, parent, _epoch = setup(
        tmp_path, runner=runner, fault=Once(),
    )
    with pytest.raises(CampaignControllerInterrupted):
        controller.step(parent.campaign_id, phase="recon",
                        totals=CanonicalOutcomeTotals(coverage_basis_points=1))
    restarted = CampaignController(store, runner, owner_id="controller-a", lifecycle=lifecycle)
    restarted.recover(parent.campaign_id)
    assert runner.calls == 1
    assert store.health(parent.campaign_id).total_observations == 1
    assert store.rebuild_projection(parent.campaign_id).matches_live


def test_stop_supersedes_uneffected_health_but_reconciles_started_effect(tmp_path):
    class At:
        def __init__(self, boundary):
            self.boundary = boundary
            self.fired = False

        def __call__(self, value):
            if value == self.boundary and not self.fired:
                self.fired = True
                raise CampaignControllerInterrupted(value)

    attempts = tuple(AttemptFact(f"attempt-{i}", f"{i:064x}", "no-novelty")
                     for i in range(3))
    (tmp_path / "before").mkdir()
    before, store, lifecycle, runner, parent, _epoch = setup(
        tmp_path / "before", runner=Runner(progress=False), fault=At("recommendation-staged"),
    )
    with pytest.raises(CampaignControllerInterrupted):
        before.step(parent.campaign_id, phase="recon", totals=CanonicalOutcomeTotals(),
                    attempts=attempts)
    CampaignController(store, runner, owner_id="controller-a", lifecycle=lifecycle).stop(
        parent.campaign_id, "operator-requested", ("decision-a",),
    )
    assert store.health(parent.campaign_id).total_observations == 0

    (tmp_path / "after").mkdir()
    after, store2, lifecycle2, runner2, parent2, _epoch2 = setup(
        tmp_path / "after", runner=Runner(progress=False), fault=At("recommendation-effect"),
    )
    with pytest.raises(CampaignControllerInterrupted):
        after.step(parent2.campaign_id, phase="recon", totals=CanonicalOutcomeTotals(),
                   attempts=attempts)
    CampaignController(store2, runner2, owner_id="controller-a", lifecycle=lifecycle2).stop(
        parent2.campaign_id, "operator-requested", ("decision-a",),
    )
    assert store2.health(parent2.campaign_id).total_observations == 1


def test_rollup_forgery_and_checkpoint_expectation_fail_closed(tmp_path):
    controller, store, _lifecycle, _runner, parent, epoch = setup(tmp_path)
    controller.step(parent.campaign_id, phase="recon",
                    totals=CanonicalOutcomeTotals(coverage_basis_points=1))
    row = store.conn.execute(
        "select recommendation_json,policy_input_json,health_rollup_json from "
        "factory_campaign_controller_epochs where epoch_id=?", (epoch.epoch_id,),
    ).fetchone()
    from rekit_factory.campaign_persistence import CampaignHealthRollup
    rollup = CampaignHealthRollup.from_dict(json.loads(row[2]))
    with pytest.raises(CampaignPersistenceError, match="canonical policy facts"):
        store.record_health_rollup(
            replace(rollup, coverage_basis_points=2), policy_input_json=row[1],
            recommendation_json=row[0], operation_id="forged-health",
        )
    with pytest.raises(CampaignPersistenceError, match="canonical policy facts"):
        store.record_health_rollup(
            replace(rollup, next_checkpoint_expected_wall_seconds=61),
            policy_input_json=row[1], recommendation_json=row[0],
            operation_id="forged-expectation",
        )


def test_corrupt_live_health_projection_never_matches_canonical_replay(tmp_path):
    controller, store, _lifecycle, _runner, parent, _epoch = setup(tmp_path)
    controller.step(parent.campaign_id, phase="recon",
                    totals=CanonicalOutcomeTotals(coverage_basis_points=1))
    row = store.conn.execute(
        "select sequence,rollup_json from factory_campaign_health where campaign_id=?",
        (parent.campaign_id,),
    ).fetchone()
    forged = json.loads(row[1])
    forged["coverageBasisPoints"] = 2
    with store.conn:
        store.conn.execute(
            "update factory_campaign_health set rollup_json=? "
            "where campaign_id=? and sequence=?",
            (json.dumps(forged, separators=(",", ":"), sort_keys=True),
             parent.campaign_id, row[0]),
        )
    rebuild = store.rebuild_projection(parent.campaign_id)
    assert rebuild.degraded
    assert not rebuild.matches_live
    assert "campaign-health-invalid" in rebuild.problem_codes
    public = controller.public_state(parent.campaign_id)["health"]
    assert public["current"] is None
    assert "campaign-health-invalid" in public["problemCodes"]


def test_upgraded_campaign_may_start_health_after_preexisting_checkpoint(tmp_path):
    from rekit_factory.campaign_persistence import CampaignPersistence
    store = CampaignPersistence(tmp_path / "factory.db")
    parent = contract()
    store.create_campaign(parent, operation_id="create")
    store.transition_campaign(parent.campaign_id, "running", authority="factory-scheduler",
                              operation_id="start")
    first = EpochContract(parent.campaign_id, 1, ("legacy-work",), budget())
    store.publish_epoch(first, operation_id="legacy-publish")
    store.acquire_epoch_lease(parent.campaign_id, first.epoch_id, "legacy-owner",
                              operation_id="legacy-lease")
    legacy = CampaignCheckpoint(
        parent.campaign_id, first.epoch_id, 1,
        (CheckpointSource("factory-ledger", 1, DIGEST),), ResourceUsage(),
    )
    store.record_checkpoint(legacy, operation_id="legacy-checkpoint")

    controller = CampaignController(store, Runner(), owner_id="controller-a")
    second = EpochContract(parent.campaign_id, 2, ("work-a",), budget(),
                           legacy.checkpoint_id)
    controller.launch(second, ResourceUsage(work_items=1, cost_units=10))
    controller.step(parent.campaign_id, phase="recon",
                    totals=CanonicalOutcomeTotals(coverage_basis_points=1))
    health = store.health(parent.campaign_id)
    assert health.total_observations == 1
    assert health.current is not None and health.current.sequence == 2
    assert store.rebuild_projection(parent.campaign_id).matches_live


def test_policy_input_collections_have_explicit_256_item_boundary():
    artifacts = tuple(f"artifact-{i}" for i in range(MAX_POLICY_FACTS))
    CanonicalOutcomeTotals(artifact_ids=artifacts)
    with pytest.raises(ValueError, match="finite policy-input limit"):
        CanonicalOutcomeTotals(artifact_ids=(*artifacts, "artifact-overflow"))

    base = policy_input()
    known = tuple(f"{i:064x}" for i in range(MAX_POLICY_FACTS))
    CampaignPolicyInput(
        base.campaign, base.epoch, base.checkpoint, base.result, base.account,
        base.phase, base.totals, known_progress_digests=known,
    )
    with pytest.raises(ValueError, match="finite policy-input limit"):
        CampaignPolicyInput(
            base.campaign, base.epoch, base.checkpoint, base.result, base.account,
            base.phase, base.totals,
            known_progress_digests=(*known, "f" * 64),
        )
