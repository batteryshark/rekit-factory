from __future__ import annotations

from copy import deepcopy

import pytest

from rekit_factory.campaign_notification_policy import (
    InvalidCampaignNotificationState, campaign_notification_candidates,
)


def _state(*, revision=1, status="running", used=20, terminal=None, degraded=False):
    return {
        "schemaVersion": 1, "campaignId": "campaign-1", "projectId": "project-1",
        "revision": revision, "status": status, "health": {"degraded": degraded},
        "cumulativeUsage": {"costUnits": used},
        "budget": {"cumulative": {
            "costUnits": {"value": 100, "unit": "cost-units", "enforcement": "hard"},
        }},
        "terminal": terminal,
    }


def test_configured_budget_crossings_are_stable_and_progress_chatter_is_silent():
    old, new = _state(), _state(revision=2, used=81)
    candidates = campaign_notification_candidates(
        old, new, budget_thresholds={"costUnits": [5000, 8000]},
    )
    assert [item["kind"] for item in candidates] == [
        "campaign.budget-threshold", "campaign.budget-threshold",
    ]
    assert len({item["dedupeKey"] for item in candidates}) == 2
    assert campaign_notification_candidates(
        new, _state(revision=3, used=81), budget_thresholds={"costUnits": [5000, 8000]},
    ) == []
    assert campaign_notification_candidates(
        old, new, budget_thresholds={"costUnits": [5000, 8000]},
    ) == candidates


def test_terminal_and_infrastructure_transitions_use_fixed_campaign_deep_links():
    completed = _state(revision=2, status="completed", terminal={
        "status": "completed", "reasonCode": "completion-criteria-satisfied",
    })
    [terminal] = campaign_notification_candidates(
        _state(), completed, budget_thresholds={"costUnits": [9000]},
    )
    assert terminal["kind"] == "campaign.terminal"
    assert terminal["deepLink"] == {"view": "mission-control", "tab": "campaigns",
                                    "entityType": "campaign", "entityId": "campaign-1"}

    failed = _state(revision=2, status="failed", terminal={
        "status": "failed", "reasonCode": "infrastructure-failure",
    })
    [alert] = campaign_notification_candidates(
        _state(), failed, budget_thresholds={"costUnits": [9000]},
    )
    assert alert["kind"] == "campaign.infrastructure-action"
    assert alert["severity"] == "action-required"


def test_hydration_degraded_regressed_and_private_content_fail_closed():
    assert campaign_notification_candidates(
        None, _state(), budget_thresholds={"costUnits": [8000]},
    ) == []
    with pytest.raises(InvalidCampaignNotificationState, match="degraded"):
        campaign_notification_candidates(
            _state(), _state(revision=2, degraded=True),
            budget_thresholds={"costUnits": [8000]},
        )
    private = deepcopy(_state())
    private["campaignId"] = "/Users/private/target"
    with pytest.raises(InvalidCampaignNotificationState, match="campaignId"):
        campaign_notification_candidates(
            _state(), private, budget_thresholds={"costUnits": [8000]},
        )
