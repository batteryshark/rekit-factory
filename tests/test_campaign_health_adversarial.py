from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest

from rekit_factory.campaign_contracts import (
    CampaignCheckpoint,
    CheckpointSource,
    CompletionCriteria,
    EpochContract,
    ResourceUsage,
)
from rekit_factory.campaign_controller import (
    CampaignController,
    CampaignControllerInterrupted,
)
from rekit_factory.campaign_persistence import (
    CampaignHealthRollup,
    CampaignPersistence,
    CampaignPersistenceError,
)
from rekit_factory.campaign_policy import AttemptFact, CanonicalOutcomeTotals

from test_campaign_controller import DIGEST, Runner, budget, contract, setup


def _staged_health(store, epoch_id: str):
    row = store.conn.execute(
        "select recommendation_json,policy_input_json,health_rollup_json from "
        "factory_campaign_controller_epochs where epoch_id=?",
        (epoch_id,),
    ).fetchone()
    assert row is not None
    return row[0], row[1], CampaignHealthRollup.from_dict(json.loads(row[2]))


def test_health_projection_boundary_rolls_back_event_and_retries_exactly(tmp_path):
    class StopAfterEffect:
        fired = False

        def __call__(self, boundary):
            if boundary == "recommendation-effect" and not self.fired:
                self.fired = True
                raise CampaignControllerInterrupted(boundary)

    controller, store, _lifecycle, _runner, campaign, epoch = setup(
        tmp_path, fault=StopAfterEffect(),
    )
    with pytest.raises(CampaignControllerInterrupted):
        controller.step(
            campaign.campaign_id,
            phase="recon",
            totals=CanonicalOutcomeTotals(coverage_basis_points=1),
        )
    recommendation_json, policy_input_json, rollup = _staged_health(store, epoch.epoch_id)

    class CrashProjection:
        def __call__(self, boundary):
            if boundary == "health-projected":
                raise CampaignControllerInterrupted(boundary)

    operation_id = f"adversarial-health-{rollup.recommendation_id}"
    with pytest.raises(CampaignControllerInterrupted):
        store.record_health_rollup(
            rollup,
            policy_input_json=policy_input_json,
            recommendation_json=recommendation_json,
            operation_id=operation_id,
            failure_injector=CrashProjection(),
        )
    assert store.health(campaign.campaign_id).total_observations == 0
    assert store.conn.execute(
        "select count(*) from factory_campaign_events "
        "where campaign_id=? and operation_id=?",
        (campaign.campaign_id, operation_id),
    ).fetchone()[0] == 0

    first = store.record_health_rollup(
        rollup,
        policy_input_json=policy_input_json,
        recommendation_json=recommendation_json,
        operation_id=operation_id,
    )
    retry = store.record_health_rollup(
        rollup,
        policy_input_json=policy_input_json,
        recommendation_json=recommendation_json,
        operation_id=operation_id,
    )
    assert first.current == retry.current == rollup
    assert first.total_observations == retry.total_observations == 1


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: replace(value, recommendation_id="policy-forged"),
        lambda value: replace(value, policy_input_digest="f" * 64),
        lambda value: replace(value, epoch_id="epoch-forged"),
        lambda value: replace(value, checkpoint_id="checkpoint-forged"),
        lambda value: replace(value, sequence=value.sequence + 1),
        lambda value: replace(value, phase="hypothesis"),
        lambda value: replace(
            value, coverage_basis_points=value.coverage_basis_points + 1,
        ),
        lambda value: replace(
            value, resolved_hypotheses=value.resolved_hypotheses + 1,
        ),
        lambda value: replace(
            value, reproduced_findings=value.reproduced_findings + 1,
        ),
        lambda value: replace(value, artifact_ids=("artifact-forged",)),
        lambda value: replace(
            value, epoch_novel_progress=value.epoch_novel_progress + 1,
        ),
        lambda value: replace(value, retry_count=value.retry_count + 1),
        lambda value: replace(value, no_progress_count=value.no_progress_count + 1),
        (lambda value: replace(
            value, cumulative_novel_progress=value.cumulative_novel_progress + 1,
        )),
        lambda value: replace(
            value, elapsed_wall_seconds=value.elapsed_wall_seconds + 1,
        ),
        lambda value: replace(value, next_checkpoint_expected_wall_seconds=1),
        lambda value: replace(value, campaign_id="campaign-other"),
    ),
)
def test_health_rejects_content_forgery_and_cross_campaign_rebinding(
    tmp_path, mutation,
):
    controller, store, _lifecycle, _runner, campaign, epoch = setup(tmp_path)
    controller.step(
        campaign.campaign_id,
        phase="recon",
        totals=CanonicalOutcomeTotals(coverage_basis_points=1),
    )
    recommendation_json, policy_input_json, rollup = _staged_health(store, epoch.epoch_id)

    with pytest.raises(CampaignPersistenceError):
        store.record_health_rollup(
            mutation(rollup),
            policy_input_json=policy_input_json,
            recommendation_json=recommendation_json,
            operation_id="forged-health-content",
        )
    assert store.health(campaign.campaign_id).total_observations == 1


def test_invalid_health_projection_fails_closed_without_leaking_staged_content(tmp_path):
    controller, store, _lifecycle, _runner, campaign, epoch = setup(tmp_path)
    controller.step(
        campaign.campaign_id,
        phase="recon",
        totals=CanonicalOutcomeTotals(coverage_basis_points=1),
    )
    recommendation_json, policy_input_json, rollup = _staged_health(store, epoch.epoch_id)
    private_marker = "private-policy-envelope-marker"
    assert private_marker not in policy_input_json
    corrupted = {**rollup.to_dict(), "retryCount": rollup.retry_count + 1}
    with store.conn:
        store.conn.execute(
            "update factory_campaign_health set rollup_json=? "
            "where campaign_id=? and sequence=?",
            (json.dumps(corrupted, sort_keys=True, separators=(",", ":")),
             campaign.campaign_id, rollup.sequence),
        )

    public = controller.public_state(campaign.campaign_id)
    health = public["health"]
    assert health["degraded"] is True
    assert health["current"] is health["previous"] is None
    assert "campaign-health-invalid" in health["problemCodes"]
    assert len(health["problemCodes"]) <= 16
    assert health["totalObservations"] == 1
    assert public["allowedActions"] == []
    encoded = json.dumps(public, sort_keys=True)
    for forbidden in (
        recommendation_json, policy_input_json, rollup.policy_input_digest,
    ):
        assert forbidden not in encoded


@pytest.mark.parametrize("limit", (0, 33, True, "2"))
def test_health_history_limit_is_strict_and_bounded(tmp_path, limit):
    controller, store, _lifecycle, _runner, campaign, _epoch = setup(tmp_path)
    controller.step(
        campaign.campaign_id,
        phase="recon",
        totals=CanonicalOutcomeTotals(coverage_basis_points=1),
    )
    with pytest.raises(CampaignPersistenceError, match="between 1 and 32"):
        store.health(campaign.campaign_id, history_limit=limit)


def test_controller_migrates_pre_health_epoch_table_without_losing_staged_rows(tmp_path):
    store = CampaignPersistence(tmp_path / "pre-health.db")
    store.conn.execute(
        "create table factory_campaign_controller_epochs ("
        "campaign_id text not null,epoch_id text primary key,owner_id text not null,"
        "lease_id text not null unique,reservation_id text not null unique,"
        "reservation_json text not null,execution_json text,recommendation_id text,"
        "recommendation_json text,policy_input_digest text,factory_run_id text,"
        "recommendation_applied integer not null default 0,"
        "recommendation_disposition text not null default 'pending')"
    )
    store.conn.execute(
        "insert into factory_campaign_controller_epochs "
        "(campaign_id,epoch_id,owner_id,lease_id,reservation_id,reservation_json,"
        "policy_input_digest,recommendation_disposition) values (?,?,?,?,?,?,?,?)",
        ("campaign-old", "epoch-old", "owner-old", "lease-old", "reserve-old", "{}",
         "a" * 64, "pending"),
    )
    store.conn.commit()

    CampaignController(store, Runner(), owner_id="controller-a")
    columns = {row[1] for row in store.conn.execute(
        "pragma table_info(factory_campaign_controller_epochs)"
    )}
    assert {"policy_input_json", "health_rollup_json"}.issubset(columns)
    assert tuple(store.conn.execute(
        "select campaign_id,epoch_id,policy_input_digest,recommendation_disposition "
        "from factory_campaign_controller_epochs"
    ).fetchone()) == ("campaign-old", "epoch-old", "a" * 64, "pending")


def _canonical(value: object) -> str:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )


def test_health_projection_retains_32_tail_but_replays_long_canonical_history(tmp_path):
    store = CampaignPersistence(tmp_path / "long-health.db")
    parent = replace(contract(), cumulative_budget=budget(work=40, cost=40))
    store.create_campaign(parent, operation_id="create-long")
    store.transition_campaign(
        parent.campaign_id, "running", authority="factory-scheduler",
        operation_id="start-long",
    )
    previous_checkpoint_id = None
    source = CheckpointSource("factory-ledger", 1, DIGEST)
    for sequence in range(1, 41):
        epoch = EpochContract(
            parent.campaign_id, sequence, (f"work-{sequence}",),
            budget(work=1, cost=1), previous_checkpoint_id,
        )
        store.publish_epoch(epoch, operation_id=f"publish-long-{sequence}")
        store.acquire_epoch_lease(
            parent.campaign_id, epoch.epoch_id, "owner-long",
            operation_id=f"lease-long-{sequence}",
        )
        checkpoint = CampaignCheckpoint(
            parent.campaign_id, epoch.epoch_id, sequence,
                (replace(source, revision=sequence),),
            ResourceUsage(work_items=sequence, cost_units=sequence, wall_seconds=sequence),
        )
        store.record_checkpoint(checkpoint, operation_id=f"checkpoint-long-{sequence}")
        recommendation_body = {
            "action": "continue", "novelProgress": [], "sequence": sequence,
        }
        recommendation = {
            **recommendation_body,
            "recommendationId": "policy-" + hashlib.sha256(
                _canonical(recommendation_body).encode()
            ).hexdigest(),
        }
        policy_input = {
            "attempts": [], "campaignDigest": parent.digest,
            "campaignId": parent.campaign_id, "checkpointId": checkpoint.checkpoint_id,
            "epochId": epoch.epoch_id, "epochResult": {},
            "nextCheckpointExpectedWallSeconds": None, "phase": "recon",
            "totals": {
                "artifactIds": [], "coverageBasisPoints": sequence,
                "reproducedFindings": 0, "resolvedHypotheses": 0,
            },
        }
        policy_input_json = _canonical(policy_input)
        rollup = CampaignHealthRollup(
            parent.campaign_id, epoch.epoch_id, checkpoint.checkpoint_id,
            hashlib.sha256(policy_input_json.encode()).hexdigest(),
            recommendation["recommendationId"], sequence, "recon", sequence,
            0, 0, (), 0, 0, 0, 0, sequence,
        )
        store.record_health_rollup(
            rollup, policy_input_json=policy_input_json,
            recommendation_json=_canonical(recommendation),
            operation_id=f"health-long-{sequence}",
        )
        previous_checkpoint_id = checkpoint.checkpoint_id

    health = store.health(parent.campaign_id, history_limit=32)
    assert health.total_observations == 40
    assert len(health.history) == 32
    assert health.current.sequence == 40 and health.previous.sequence == 39
    assert health.history[-1].sequence == 9
    assert store.conn.execute(
        "select count(*) from factory_campaign_health where campaign_id=?",
        (parent.campaign_id,),
    ).fetchone()[0] == 32
    rebuilt = store.rebuild_projection(parent.campaign_id)
    assert rebuilt.matches_live and not rebuilt.degraded


def test_epoch_local_counters_reset_while_cumulative_novelty_persists(tmp_path):
    store = CampaignPersistence(tmp_path / "counter-reset.db")
    runner = Runner(progress=True, next_actions=("work-b",))
    controller = CampaignController(store, runner, owner_id="controller-a")
    parent = contract(completion=CompletionCriteria(2, 0, 0))
    controller.start(parent)
    first_epoch = EpochContract(parent.campaign_id, 1, ("work-a",), budget())
    controller.launch(first_epoch, ResourceUsage(work_items=1, cost_units=10))
    attempts = (
        AttemptFact("attempt-a", "f" * 64, "no-novelty"),
        AttemptFact("attempt-b", "f" * 64, "no-novelty"),
    )
    first_totals = CanonicalOutcomeTotals(coverage_basis_points=1)
    controller.step(
        parent.campaign_id, phase="recon", totals=first_totals, attempts=attempts,
    )
    controller.step(
        parent.campaign_id, phase="hypothesis",
        totals=CanonicalOutcomeTotals(coverage_basis_points=2),
        previous_totals=first_totals,
        known_progress_digests=("b" * 64,),
    )

    health = store.health(parent.campaign_id)
    assert health.total_observations == 2
    assert health.previous.retry_count == 1
    assert health.previous.epoch_novel_progress == 1
    assert health.previous.cumulative_novel_progress == 1
    assert health.current.retry_count == 0
    assert health.current.no_progress_count == 0
    assert health.current.epoch_novel_progress == 0
    assert health.current.cumulative_novel_progress == 1
    assert store.rebuild_projection(parent.campaign_id).matches_live


@pytest.mark.parametrize(
    ("boundary", "expected_health"),
    (
        ("execution-staged", 0),
        ("checkpointed", 0),
        ("recommendation-staged", 0),
        ("recommendation-effect", 1),
        ("health-recorded", 1),
        ("recommendation-applied", 1),
    ),
)
def test_operator_stop_reconciles_each_crash_state_without_rerunning_work(
    tmp_path, boundary, expected_health,
):
    class CrashAt:
        fired = False

        def __call__(self, value):
            if value == boundary and not self.fired:
                self.fired = True
                raise CampaignControllerInterrupted(value)

    runner = Runner(progress=False, next_actions=("work-b",))
    controller, store, lifecycle, _runner, parent, _epoch = setup(
        tmp_path, runner=runner, fault=CrashAt(),
    )
    with pytest.raises(CampaignControllerInterrupted):
        controller.step(
            parent.campaign_id, phase="recon", totals=CanonicalOutcomeTotals(),
        )

    stopped = CampaignController(
        store, runner, owner_id="controller-a", lifecycle=lifecycle,
    ).stop(parent.campaign_id, "operator-requested", ("decision-a",))
    assert stopped.status == "stopped"
    assert runner.calls == 1
    assert store.health(parent.campaign_id).total_observations == expected_health
    assert store.rebuild_projection(parent.campaign_id).matches_live
