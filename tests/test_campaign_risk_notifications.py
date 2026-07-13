from __future__ import annotations

import json

import pytest

from rekit_factory.campaign_contracts import (
    CampaignContract, CampaignRiskMeasurement, CheckpointSource, CompletionCriteria,
    ComponentVersion, OperatorPolicy, ResourceBudget, ResourceLimit, ScopeBinding,
)
from rekit_factory.campaign_controller import CampaignController
from rekit_factory.campaign_persistence import CampaignPersistence, CampaignPersistenceError


DIGEST = "a" * 64


class _UnusedRunner:
    def run(self, _request):
        raise AssertionError("risk observation must not run an epoch")


def _budget() -> ResourceBudget:
    limit = ResourceLimit
    return ResourceBudget(
        limit(2, "items"), limit(1, "workers"), limit(1, "attempts"),
        limit(100, "tokens"), limit(100, "tokens"), limit(20, "cost-units"),
        limit(60, "seconds"), limit(4, "calls"), limit(0, "calls"),
        limit(100, "bytes"),
    )


def _contract(*, threshold: int = 40) -> CampaignContract:
    budget = _budget()
    return CampaignContract(
        "project-risk", "Canonical measured risk",
        ScopeBinding("scope-risk", 1, DIGEST), budget, budget,
        CompletionCriteria(1, 0, 0), OperatorPolicy(risk_threshold=threshold),
        (ComponentVersion("factory", "1", DIGEST),),
    )


def _setup(path, *, threshold: int = 40):
    persistence = CampaignPersistence(path)
    controller = CampaignController(persistence, _UnusedRunner(), owner_id="risk-observer")
    contract = _contract(threshold=threshold)
    controller.start(contract)
    controller.public_state(contract.campaign_id)
    return controller, persistence, contract


def _measurement(contract: CampaignContract, sequence: int, score: int) -> CampaignRiskMeasurement:
    return CampaignRiskMeasurement(
        contract.campaign_id, sequence, score,
        CheckpointSource("risk-engine", sequence, f"{sequence:064x}"),
    )


def test_canonical_measurement_crosses_configured_threshold_exactly_once(tmp_path):
    path = tmp_path / "campaigns.db"
    controller, persistence, contract = _setup(path)
    controller.record_risk_measurement(_measurement(contract, 1, 20), operation_id="risk-1")
    controller.public_state(contract.campaign_id)
    assert persistence.conn.execute(
        "select count(*) from factory_notification_outbox"
    ).fetchone()[0] == 0

    controller.record_risk_measurement(_measurement(contract, 2, 40), operation_id="risk-2")
    state = controller.public_state(contract.campaign_id)
    row = persistence.conn.execute(
        "select kind,payload_json from factory_notification_outbox"
    ).fetchone()
    assert row[0] == "campaign.risk-threshold"
    payload = json.loads(row[1])
    assert payload["transitionMarker"] == "risk:40"
    assert "score" not in payload["message"].lower()
    assert state["measuredRisk"] == _measurement(contract, 2, 40).to_dict()

    restarted = CampaignController(
        CampaignPersistence(path), _UnusedRunner(), owner_id="risk-observer",
    )
    assert restarted.public_state(contract.campaign_id) == state
    assert restarted.persistence.conn.execute(
        "select count(*) from factory_notification_outbox"
    ).fetchone()[0] == 1
    rebuild = restarted.persistence.rebuild_projection(contract.campaign_id)
    assert rebuild.matches_live and not rebuild.degraded
    assert rebuild.projection.measured_risk == _measurement(contract, 2, 40)


def test_risk_source_and_sequence_cannot_change_or_regress(tmp_path):
    controller, persistence, contract = _setup(tmp_path / "campaigns.db")
    controller.record_risk_measurement(_measurement(contract, 1, 20), operation_id="risk-1")
    with pytest.raises(CampaignPersistenceError, match="source authority changed"):
        persistence.record_risk_measurement(CampaignRiskMeasurement(
            contract.campaign_id, 2, 50, CheckpointSource("other-engine", 2, "b" * 64),
        ), operation_id="risk-2")
    with pytest.raises(CampaignPersistenceError, match="sequence must be contiguous"):
        persistence.record_risk_measurement(_measurement(contract, 3, 50), operation_id="risk-3")


def test_threshold_zero_notifies_only_after_explicit_positive_measurement(tmp_path):
    controller, persistence, contract = _setup(tmp_path / "campaigns.db", threshold=0)
    controller.record_risk_measurement(_measurement(contract, 1, 0), operation_id="risk-1")
    controller.public_state(contract.campaign_id)
    assert persistence.conn.execute(
        "select count(*) from factory_notification_outbox"
    ).fetchone()[0] == 0
    controller.record_risk_measurement(_measurement(contract, 2, 1), operation_id="risk-2")
    controller.public_state(contract.campaign_id)
    assert persistence.conn.execute(
        "select kind from factory_notification_outbox"
    ).fetchone()[0] == "campaign.risk-threshold"

