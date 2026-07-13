from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from rekit_factory.campaign_contracts import (
    CampaignChangeRequest,
    CampaignContract,
    ScopeBinding,
)
from rekit_factory.campaign_controller import CampaignController
from rekit_factory.campaign_persistence import (
    CampaignPersistence,
    CampaignPersistenceError,
)

from test_campaign_controller import Runner, contract
from test_campaign_api import _request as api_request, _setup as api_setup
from test_campaign_api_adversarial import _request as status_request


def _setup(tmp_path):
    store = CampaignPersistence(tmp_path / "campaign-change.db")
    controller = CampaignController(store, Runner(), owner_id="controller-a")
    current = contract()
    controller.start(current)
    return controller, store, current


def _request(current: CampaignContract, *, cost_delta: int = 1,
             reason: str = "Raise the exact bounded ceiling") -> CampaignChangeRequest:
    proposed = replace(
        current,
        cumulative_budget=replace(
            current.cumulative_budget,
            cost_units=replace(
                current.cumulative_budget.cost_units,
                value=current.cumulative_budget.cost_units.value + cost_delta,
            ),
        ),
    )
    return CampaignChangeRequest(current.campaign_id, proposed, reason)


def test_change_request_identity_rejects_changed_forged_and_cross_campaign_content(tmp_path):
    controller, store, current = _setup(tmp_path)
    request = _request(current)
    published = controller.publish_change_request(request)
    assert published["requestId"] == request.request_id

    changed = _request(current, cost_delta=2)
    with pytest.raises(CampaignPersistenceError, match="conflicting reuse"):
        store.publish_change_request(changed, operation_id=f"publish:{request.request_id}")
    with pytest.raises(CampaignPersistenceError, match="does not exist"):
        store.decide_change_request(
            current.campaign_id, "campaign-change-" + "f" * 64,
            approved=True, expected_revision=1, operation_id="forged-decision",
        )

    other = replace(current, project_id="project-other")
    controller.start(other)
    with pytest.raises(CampaignPersistenceError, match="does not exist"):
        store.change_request(other.campaign_id, request.request_id)
    with pytest.raises(CampaignPersistenceError, match="does not exist"):
        store.decide_change_request(
            other.campaign_id, request.request_id,
            approved=True, expected_revision=1, operation_id="cross-campaign-decision",
        )

    forged = request.to_dict()
    forged["reason"] = "changed after identity publication"
    with pytest.raises(ValueError, match="identity does not match"):
        CampaignChangeRequest.from_dict(forged)


@pytest.mark.parametrize("winner", (True, False))
def test_approve_reject_race_has_one_durable_winner_and_conflicting_replay_fails(
    tmp_path, winner,
):
    _controller, store, current = _setup(tmp_path)
    request = _request(current)
    store.publish_change_request(request, operation_id="publish-race")
    first = store.decide_change_request(
        current.campaign_id, request.request_id, approved=winner,
        expected_revision=1, operation_id="decision-winner",
    )
    exact = store.decide_change_request(
        current.campaign_id, request.request_id, approved=winner,
        expected_revision=1, operation_id="decision-winner",
    )
    assert first == exact
    assert first.status == ("approved" if winner else "rejected")
    with pytest.raises(CampaignPersistenceError, match="conflicting reuse"):
        store.decide_change_request(
            current.campaign_id, request.request_id, approved=not winner,
            expected_revision=1, operation_id="decision-loser",
        )
    assert store.conn.execute(
        "select count(*) from factory_campaign_events "
        "where campaign_id=? and kind='operator.decided'",
        (current.campaign_id,),
    ).fetchone()[0] == 1


def test_stale_revision_and_client_type_confusion_fail_closed(tmp_path):
    _controller, store, current = _setup(tmp_path)
    request = _request(current)
    store.publish_change_request(request, operation_id="publish-stale")
    for revision in (0, 2, True, "1"):
        with pytest.raises(CampaignPersistenceError):
            store.decide_change_request(
                current.campaign_id, request.request_id, approved=True,
                expected_revision=revision, operation_id=f"stale-{revision}",
            )
    for approved in (1, 0, "true", None):
        with pytest.raises(CampaignPersistenceError, match="invalid exact types"):
            store.decide_change_request(
                current.campaign_id, request.request_id, approved=approved,
                expected_revision=1, operation_id=f"type-{str(approved).lower()}",
            )
    assert store.change_request(current.campaign_id, request.request_id).status == "pending"


def test_approved_successor_has_exact_immutable_contract_and_retry_converges(tmp_path):
    controller, store, current = _setup(tmp_path)
    request = _request(current)
    controller.publish_change_request(request)
    first, successor_id = controller.decide_change_request(
        current.campaign_id, request.request_id, approved=True,
        expected_revision=1, operation_id="approve-exact",
    )
    retry, retry_successor = controller.decide_change_request(
        current.campaign_id, request.request_id, approved=True,
        expected_revision=1, operation_id="approve-exact",
    )
    assert first == retry and successor_id == retry_successor == request.proposed.campaign_id
    assert first["applicationStatus"] == "applied"
    assert store.campaign(current.campaign_id).status == "stopped"
    assert store.campaign(request.proposed.campaign_id).status == "running"
    old_json = store.conn.execute(
        "select contract_json from factory_campaigns where campaign_id=?",
        (current.campaign_id,),
    ).fetchone()[0]
    new_json = store.conn.execute(
        "select contract_json from factory_campaigns where campaign_id=?",
        (request.proposed.campaign_id,),
    ).fetchone()[0]
    assert CampaignContract.from_dict(json.loads(old_json)) == current
    assert CampaignContract.from_dict(json.loads(new_json)) == request.proposed
    assert store.conn.execute(
        "select count(*) from factory_campaigns where campaign_id=?",
        (request.proposed.campaign_id,),
    ).fetchone()[0] == 1
    assert store.rebuild_projection(current.campaign_id).matches_live
    assert store.rebuild_projection(request.proposed.campaign_id).matches_live


@pytest.mark.parametrize("boundary", ("after-decision", "after-successor-start"))
def test_restart_reconciles_approved_decision_to_one_successor(tmp_path, monkeypatch, boundary):
    controller, store, current = _setup(tmp_path)
    request = _request(current)
    controller.publish_change_request(request)

    if boundary == "after-decision":
        original = controller.stop

        def crash(*_args, **_kwargs):
            raise RuntimeError("crash after durable decision")

        monkeypatch.setattr(controller, "stop", crash)
    else:
        original = store.mark_change_applied

        def crash(*_args, **_kwargs):
            raise RuntimeError("crash after successor start")

        monkeypatch.setattr(store, "mark_change_applied", crash)

    with pytest.raises(RuntimeError, match="crash after"):
        controller.decide_change_request(
            current.campaign_id, request.request_id, approved=True,
            expected_revision=1, operation_id="approve-before-crash",
        )
    if boundary == "after-decision":
        monkeypatch.setattr(controller, "stop", original)
    else:
        monkeypatch.setattr(store, "mark_change_applied", original)

    restarted = CampaignController(store, Runner(), owner_id="controller-a")
    projection, successor_id = restarted.decide_change_request(
        current.campaign_id, request.request_id, approved=True,
        expected_revision=1, operation_id="approve-before-crash",
    )
    assert projection["applicationStatus"] == "applied"
    assert successor_id == request.proposed.campaign_id
    assert store.campaign(current.campaign_id).status == "stopped"
    assert store.campaign(successor_id).status == "running"
    assert store.conn.execute(
        "select count(*) from factory_campaigns where campaign_id=?", (successor_id,),
    ).fetchone()[0] == 1


def test_second_pending_request_cannot_create_a_competing_successor(tmp_path):
    controller, store, current = _setup(tmp_path)
    first, second = _request(current, cost_delta=1), _request(current, cost_delta=2)
    controller.publish_change_request(first)
    with pytest.raises(CampaignPersistenceError, match="already has a pending"):
        controller.publish_change_request(second)
    controller.decide_change_request(
        current.campaign_id, first.request_id, approved=True,
        expected_revision=1, operation_id="approve-first",
    )
    assert store.conn.execute(
        "select count(*) from factory_campaigns"
    ).fetchone()[0] == 2
    with pytest.raises(CampaignPersistenceError, match="does not exist"):
        store.change_request(current.campaign_id, second.request_id)


def test_public_change_diff_is_exact_bounded_and_private(tmp_path):
    controller, _store, current = _setup(tmp_path)
    private = "<script>alert(1)</script> /Users/private TOKEN=s3cr3t"
    request = CampaignChangeRequest(
        current.campaign_id,
        replace(current, scope=ScopeBinding("scope-b", 2, "b" * 64)),
        private,
    )
    public = controller.publish_change_request(request)
    assert set(public) == {
        "applicationStatus", "baseCampaignRevision", "currentCampaignId", "diff",
        "proposedCampaignId", "requestId", "revision", "status",
    }
    assert public["baseCampaignRevision"] == 2
    assert set(public["diff"]) == {
        "scope", "epochBudget", "cumulativeBudget", "completion",
        "operatorPolicy", "componentVersions",
    }
    assert public["diff"]["scope"] == {
        "current": current.scope.to_dict(),
        "proposed": request.proposed.scope.to_dict(),
    }
    encoded = json.dumps(public, sort_keys=True)
    for forbidden in (private, "<script>", "/Users/private", "s3cr3t", "reason", "goal"):
        assert forbidden not in encoded


@pytest.mark.parametrize(
    "forbidden_field",
    ("proposed", "contract", "reason", "decidedBy", "campaignId", "status"),
)
def test_http_client_cannot_submit_contract_or_authority_fields(tmp_path, forbidden_field):
    controller, server, thread, campaign_id = api_setup(tmp_path)
    try:
        current = controller._contract(campaign_id)
        request = _request(current)
        controller.publish_change_request(request)
        base = f"http://127.0.0.1:{server.server_port}"
        body = {
            "requestId": request.request_id, "approved": False,
            "expectedRevision": 1, "operationId": f"reject-extra-{forbidden_field}",
            forbidden_field: request.proposed.to_dict(),
        }
        error = api_request(
            base, f"/api/campaigns/{campaign_id}/change-decisions", body, expected=400,
        )
        assert "only exact decision fields" in error["error"]
        assert controller.persistence.change_request(
            campaign_id, request.request_id,
        ).status == "pending"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_exact_retry_cross_campaign_and_hostile_identity_fail_closed(tmp_path):
    controller, server, thread, campaign_id = api_setup(tmp_path)
    try:
        current = controller._contract(campaign_id)
        request = _request(current)
        controller.publish_change_request(request)
        other = replace(current, project_id="project-other")
        controller.start(other)
        base = f"http://127.0.0.1:{server.server_port}"
        path = f"/api/campaigns/{campaign_id}/change-decisions"
        body = {
            "requestId": request.request_id, "approved": False,
            "expectedRevision": 1, "operationId": "http-reject-exact",
        }
        first = api_request(base, path, body)
        retry = api_request(base, path, body)
        assert first["changeRequest"] == retry["changeRequest"]
        assert first["approvedCampaignId"] is retry["approvedCampaignId"] is None

        cross = {**body, "operationId": "http-cross-campaign"}
        api_request(
            base, f"/api/campaigns/{other.campaign_id}/change-decisions",
            cross, expected=404,
        )
        hostile = {
            **body, "requestId": "../../campaign-change-<script>",
            "operationId": "http-hostile-id",
        }
        api_request(base, path, hostile, expected=404)
        assert controller.persistence.change_request(
            campaign_id, request.request_id,
        ).status == "rejected"
        assert controller.persistence.conn.execute(
            "select count(*) from factory_campaign_events "
            "where campaign_id=? and kind='operator.decided'",
            (campaign_id,),
        ).fetchone()[0] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_concurrent_http_approve_reject_race_commits_exactly_one_decision(tmp_path):
    controller, server, thread, campaign_id = api_setup(tmp_path)
    try:
        current = controller._contract(campaign_id)
        request = _request(current)
        controller.publish_change_request(request)
        url = (
            f"http://127.0.0.1:{server.server_port}/api/campaigns/"
            f"{campaign_id}/change-decisions"
        )
        decisions = (
            {
                "requestId": request.request_id, "approved": True,
                "expectedRevision": 1, "operationId": "race-approve",
            },
            {
                "requestId": request.request_id, "approved": False,
                "expectedRevision": 1, "operationId": "race-reject",
            },
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda body: status_request(url, body), decisions))
        assert sorted(status for status, _body in results) == [200, 409]
        projection = controller.persistence.change_request(campaign_id, request.request_id)
        assert projection.status in {"approved", "rejected"}
        assert controller.persistence.conn.execute(
            "select count(*) from factory_campaign_events "
            "where campaign_id=? and kind='operator.decided'",
            (campaign_id,),
        ).fetchone()[0] == 1
        if projection.status == "approved":
            assert controller.persistence.campaign(campaign_id).status == "stopped"
            assert controller.persistence.campaign(request.proposed.campaign_id).status == "running"
        else:
            assert controller.persistence.campaign(campaign_id).status == "running"
            assert controller.persistence.conn.execute(
                "select count(*) from factory_campaigns where campaign_id=?",
                (request.proposed.campaign_id,),
            ).fetchone()[0] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
