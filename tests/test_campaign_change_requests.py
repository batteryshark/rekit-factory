from dataclasses import replace

import pytest

from rekit_factory.campaign_contracts import (
    CampaignChangeRequest, CompletionCriteria, ComponentVersion, ScopeBinding,
)
from rekit_factory.campaign_controller import CampaignController
from rekit_factory.campaign_persistence import CampaignPersistence, CampaignPersistenceError

from test_campaign_controller import DIGEST, Runner, contract
from test_campaign_api import _request, _setup


def setup_change(tmp_path):
    store = CampaignPersistence(tmp_path / "factory.db")
    controller = CampaignController(store, Runner(), owner_id="controller-a")
    current = contract()
    controller.start(current)
    proposed = replace(current, scope=ScopeBinding("scope-a", 2, "b" * 64))
    request = CampaignChangeRequest(current.campaign_id, proposed,
                                    "private /path must never reach the browser")
    return controller, store, current, proposed, request


def test_server_published_request_exposes_only_safe_exact_diff(tmp_path):
    controller, store, current, proposed, request = setup_change(tmp_path)
    public = controller.publish_change_request(request)
    assert public["requestId"] == request.request_id
    assert public["baseCampaignRevision"] == 2
    assert public["status"] == "pending" and public["revision"] == 1
    assert public["diff"]["scope"] == {
        "current": current.scope.to_dict(), "proposed": proposed.scope.to_dict(),
    }
    encoded = str(public)
    assert "private" not in encoded and "/path" not in encoded and current.goal not in encoded
    assert controller.public_state(current.campaign_id)["changeRequests"] == [public]
    assert store.rebuild_projection(current.campaign_id).matches_live


def test_exact_reject_replay_and_cross_campaign_forgery_fail_closed(tmp_path):
    controller, _store, current, _proposed, request = setup_change(tmp_path)
    controller.publish_change_request(request)
    rejected, approved_id = controller.decide_change_request(
        current.campaign_id, request.request_id, approved=False,
        expected_revision=1, operation_id="decision-a",
    )
    assert approved_id is None and rejected["status"] == "rejected"
    assert controller.decide_change_request(
        current.campaign_id, request.request_id, approved=False,
        expected_revision=1, operation_id="decision-a",
    )[0] == rejected
    with pytest.raises(CampaignPersistenceError, match="conflicting"):
        controller.decide_change_request(
            current.campaign_id, request.request_id, approved=True,
            expected_revision=1, operation_id="decision-a",
        )
    with pytest.raises(CampaignPersistenceError, match="does not exist"):
        controller.decide_change_request(
            "campaign-other", request.request_id, approved=False,
            expected_revision=1, operation_id="decision-b",
        )


def test_approval_restart_reconciliation_stops_old_before_starting_successor(tmp_path):
    controller, store, current, proposed, request = setup_change(tmp_path)
    controller.publish_change_request(request)
    # Simulate a process exit after the durable approval but before authority application.
    store.decide_change_request(
        current.campaign_id, request.request_id, approved=True,
        expected_revision=1, operation_id="decision-a",
    )
    restarted = CampaignController(store, Runner(), owner_id="controller-a")
    recovered = restarted.recover(current.campaign_id)
    assert recovered.status == "stopped"
    change, approved_id = restarted.decide_change_request(
        current.campaign_id, request.request_id, approved=True,
        expected_revision=1, operation_id="decision-a",
    )
    assert approved_id == proposed.campaign_id
    assert change["applicationStatus"] == "applied"
    assert store.campaign(current.campaign_id).status == "stopped"
    assert store.campaign(proposed.campaign_id).status == "running"
    # Exact reconnect converges after the application journal reached applied.
    retry, retry_id = restarted.decide_change_request(
        current.campaign_id, request.request_id, approved=True,
        expected_revision=1, operation_id="decision-a",
    )
    assert retry == change and retry_id == proposed.campaign_id


def test_decision_rejects_stale_base_campaign_revision(tmp_path):
    controller, _store, current, _proposed, request = setup_change(tmp_path)
    controller.publish_change_request(request)
    controller.pause(current.campaign_id)
    with pytest.raises(CampaignPersistenceError, match="base campaign revision"):
        controller.decide_change_request(
            current.campaign_id, request.request_id, approved=True,
            expected_revision=1, operation_id="decision-a",
        )


def test_http_decision_accepts_only_identity_and_decision_fields(tmp_path):
    campaigns, server, thread, campaign_id = _setup(tmp_path)
    try:
        current = campaigns._contract(campaign_id)
        request = CampaignChangeRequest(
            campaign_id, replace(current, scope=ScopeBinding("scope-a", 2, "c" * 64)),
            "server-private reason",
        )
        campaigns.publish_change_request(request)
        base = f"http://127.0.0.1:{server.server_port}"
        payload = {"requestId": request.request_id, "approved": False,
                   "expectedRevision": 1, "operationId": "decision-http"}
        response = _request(base, f"/api/campaigns/{campaign_id}/change-decisions", payload)
        assert response["approvedCampaignId"] is None
        assert response["changeRequest"]["status"] == "rejected"
        forged = {**payload, "proposed": request.proposed.to_dict(),
                  "operationId": "decision-forged"}
        error = _request(base, f"/api/campaigns/{campaign_id}/change-decisions",
                         forged, expected=400)
        assert "only exact decision fields" in error["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("kind", ("components", "artifacts"))
def test_publication_rejects_unbounded_exact_authority_diff(tmp_path, kind):
    controller, _store, current, _proposed, _request = setup_change(tmp_path)
    if kind == "components":
        proposed = replace(current, components=tuple(
            ComponentVersion(f"component-{index}", "1", DIGEST) for index in range(65)
        ))
        message = "64 component"
    else:
        proposed = replace(
            current,
            completion=CompletionCriteria(
                1, 0, 0, tuple(f"artifact-{index}" for index in range(257)),
            ),
        )
        message = "256 required artifacts"
    request = CampaignChangeRequest(current.campaign_id, proposed, "boundedness test")
    with pytest.raises(CampaignPersistenceError, match=message):
        controller.publish_change_request(request)
    assert controller.public_state(current.campaign_id)["changeRequests"] == []
