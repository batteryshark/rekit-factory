from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sqlite3
from unittest.mock import patch

import pytest

from rekit_factory.campaign_contracts import (
    CampaignCheckpoint, CampaignContract, CheckpointSource, CompletionCriteria,
    ComponentVersion, EpochContract, OperatorPolicy, ResourceBudget, ResourceLimit,
    ResourceUsage, ScopeBinding, TerminalOutcome,
)
from rekit_factory.campaign_controller import (
    CampaignController, CampaignControllerInterrupted,
)
from rekit_factory.campaign_persistence import CampaignPersistence, CampaignPersistenceError
from rekit_factory.notification_outbox import NotificationOutbox
from rekit_factory.notification_supervisor import NotificationDeliverySupervisor


DIGEST = "a" * 64
THRESHOLDS = {"costUnits": [8000]}


def _budget() -> ResourceBudget:
    limit = ResourceLimit
    return ResourceBudget(
        limit(2, "items"), limit(1, "workers"), limit(1, "attempts"),
        limit(100, "tokens"), limit(100, "tokens"), limit(20, "cost-units"),
        limit(60, "seconds"), limit(4, "calls"), limit(0, "calls"),
        limit(100, "bytes"),
    )


def _contract() -> CampaignContract:
    return CampaignContract(
        "project-a", "Campaign notification admission",
        ScopeBinding("scope-a", 1, DIGEST), _budget(), _budget(),
        CompletionCriteria(1, 0, 0), OperatorPolicy(risk_threshold=10),
        (ComponentVersion("factory", "1", DIGEST),),
    )


class _UnusedRunner:
    def run(self, _request):
        raise AssertionError("notification observation must not run work")


def _setup(path, *, fault=None):
    persistence = CampaignPersistence(path)
    controller = CampaignController(
        persistence, _UnusedRunner(), owner_id="observer",
        notification_budget_thresholds=THRESHOLDS, fault_injector=fault,
    )
    contract = _contract()
    controller.start(contract)
    controller.public_state(contract.campaign_id)  # Initial hydration is intentionally silent.
    return controller, persistence, contract


def _finish(persistence: CampaignPersistence, contract: CampaignContract) -> None:
    persistence.transition_campaign(
        contract.campaign_id, "failed", authority="factory-scheduler",
        operation_id="fail", terminal=TerminalOutcome(
            contract.campaign_id, "failed", "infrastructure-failure",
            ("evidence-a",), None,
        ),
    )


def test_exact_public_transition_admits_once_and_reconnect_is_silent(tmp_path):
    path = tmp_path / "campaigns.db"
    controller, persistence, contract = _setup(path)
    assert persistence.conn.execute(
        "select count(*) from factory_notification_outbox"
    ).fetchone()[0] == 0
    _finish(persistence, contract)

    state = controller.public_state(contract.campaign_id)
    assert state["status"] == "failed"
    row = persistence.conn.execute(
        "select id,kind,entity_type,entity_id from factory_notification_outbox"
    ).fetchone()
    assert tuple(row)[1:] == (
        "campaign.infrastructure-action", "campaign", contract.campaign_id,
    )
    record = NotificationOutbox(persistence.conn).get(row["id"])
    assert record["payload"]["deepLink"] == {
        "view": "mission-control", "tab": "campaigns",
        "entityType": "campaign", "entityId": contract.campaign_id,
    }
    schedule = persistence.conn.execute(
        "select notification_id from factory_notification_schedules"
    ).fetchone()
    delivery = persistence.conn.execute(
        "select notification_id,channel_ref from factory_notification_deliveries"
    ).fetchone()
    assert schedule[0] == row["id"]
    assert tuple(delivery) == (row["id"], "desktop-primary")

    restarted = CampaignController(
        CampaignPersistence(path), _UnusedRunner(), owner_id="observer",
        notification_budget_thresholds=THRESHOLDS,
    )
    assert restarted.public_state(contract.campaign_id) == state
    assert restarted.persistence.conn.execute(
        "select count(*) from factory_notification_outbox"
    ).fetchone()[0] == 1


def test_exact_checkpoint_usage_crossing_admits_configured_budget_threshold(tmp_path):
    controller, persistence, contract = _setup(tmp_path / "campaigns.db")
    epoch = EpochContract(contract.campaign_id, 1, ("work-a",), _budget())
    persistence.publish_epoch(epoch, operation_id="publish")
    persistence.acquire_epoch_lease(
        contract.campaign_id, epoch.epoch_id, "observer", operation_id="lease",
    )
    checkpoint = CampaignCheckpoint(
        contract.campaign_id, epoch.epoch_id, 1,
        (CheckpointSource("factory-ledger", 1, DIGEST),),
        ResourceUsage(work_items=1, cost_units=17),
    )
    persistence.record_checkpoint(checkpoint, operation_id="checkpoint")

    state = controller.public_state(contract.campaign_id)
    assert state["cumulativeUsage"]["costUnits"] == 17
    row = persistence.conn.execute(
        "select kind from factory_notification_outbox"
    ).fetchone()
    assert row[0] == "campaign.budget-threshold"


@pytest.mark.parametrize("boundary", (
    "notification-outbox-admitted", "notification-baseline-advanced",
))
def test_outbox_and_observation_baseline_roll_back_at_every_crash_boundary(tmp_path, boundary):
    path = tmp_path / f"{boundary}.db"
    controller, persistence, contract = _setup(path)
    _finish(persistence, contract)

    class Once:
        fired = False

        def __call__(self, value):
            if value == boundary and not self.fired:
                self.fired = True
                raise CampaignControllerInterrupted(value)

    crashing = CampaignController(
        persistence, _UnusedRunner(), owner_id="observer",
        notification_budget_thresholds=THRESHOLDS, fault_injector=Once(),
    )
    with pytest.raises(CampaignControllerInterrupted, match=boundary):
        crashing.public_state(contract.campaign_id)
    assert persistence.conn.execute(
        "select count(*) from factory_notification_outbox"
    ).fetchone()[0] == 0
    assert persistence.conn.execute(
        "select revision from factory_campaign_notification_observations"
    ).fetchone()[0] == 2

    CampaignController(
        persistence, _UnusedRunner(), owner_id="observer",
        notification_budget_thresholds=THRESHOLDS,
    ).public_state(contract.campaign_id)
    assert persistence.conn.execute(
        "select count(*) from factory_notification_outbox"
    ).fetchone()[0] == 1


def test_degraded_observation_cannot_replace_trusted_baseline(tmp_path):
    controller, persistence, contract = _setup(tmp_path / "campaigns.db")
    trusted = controller.public_state(contract.campaign_id)
    stored_before = persistence.conn.execute(
        "select state_json from factory_campaign_notification_observations"
    ).fetchone()[0]
    with persistence.conn:
        persistence.conn.execute(
            "update factory_campaign_events set payload_json='{}' where campaign_id=? "
            "and campaign_seq=1", (contract.campaign_id,),
        )
    degraded = controller.public_state(contract.campaign_id)
    assert degraded["health"]["degraded"] is True
    stored_after = persistence.conn.execute(
        "select state_json from factory_campaign_notification_observations"
    ).fetchone()[0]
    assert stored_before == stored_after == __import__("json").dumps(
        trusted, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )


def test_concurrent_observers_converge_on_one_durable_record(tmp_path):
    path = tmp_path / "campaigns.db"
    controller, first, contract = _setup(path)
    _finish(first, contract)
    with patch.object(first, "admit_notification_state", return_value=[]):
        state = controller.public_state(contract.campaign_id)
    second = CampaignPersistence(path)

    def observe(persistence):
        return persistence.admit_notification_state(
            contract.campaign_id, state, budget_thresholds=THRESHOLDS,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(observe, (first, second)))
    assert sorted(map(len, results)) == [0, 1]
    assert first.conn.execute(
        "select count(*) from factory_notification_outbox"
    ).fetchone()[0] == 1


def test_forged_state_fails_closed_but_notification_failure_does_not_block_read(tmp_path):
    controller, persistence, contract = _setup(tmp_path / "campaigns.db")
    state = controller.public_state(contract.campaign_id)
    forged = deepcopy(state)
    forged["status"] = "completed"
    with pytest.raises(CampaignPersistenceError, match="not canonical"):
        persistence.admit_notification_state(
            contract.campaign_id, forged, budget_thresholds=THRESHOLDS,
        )

    with patch.object(
        persistence, "admit_notification_state",
        side_effect=sqlite3.OperationalError("outbox unavailable"),
    ):
        assert controller.public_state(contract.campaign_id)["status"] == "running"


def test_campaign_hydration_recovers_schedule_after_post_admission_failure(tmp_path):
    path = tmp_path / "campaigns.db"
    controller, persistence, contract = _setup(path)
    _finish(persistence, contract)
    with patch.object(
        NotificationDeliverySupervisor, "schedule",
        side_effect=sqlite3.OperationalError("schedule write interrupted"),
    ):
        assert controller.public_state(contract.campaign_id)["status"] == "failed"
    assert persistence.conn.execute(
        "select count(*) from factory_notification_outbox"
    ).fetchone()[0] == 1
    assert persistence.conn.execute(
        "select count(*) from factory_notification_schedules"
    ).fetchone()[0] == 0

    restarted = CampaignController(
        CampaignPersistence(path), _UnusedRunner(), owner_id="observer",
        notification_budget_thresholds=THRESHOLDS,
    )
    assert restarted.public_state(contract.campaign_id)["status"] == "failed"
    assert restarted.persistence.conn.execute(
        "select count(*) from factory_notification_schedules"
    ).fetchone()[0] == 1
