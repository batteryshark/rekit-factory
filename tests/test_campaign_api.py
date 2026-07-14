from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rekit_factory.api import FactoryServer
from rekit_factory.campaign_contracts import (
    CampaignCheckpoint, CampaignContract, CheckpointSource, CompletionCriteria,
    ComponentVersion, EpochContract, EpochResult, OperatorPolicy, ProgressSignal,
    ResourceBudget, ResourceLimit, ResourceUsage, ScopeBinding,
)
from rekit_factory.campaign_controller import CampaignController, EpochExecution
from rekit_factory.campaign_persistence import CampaignPersistence


DIGEST = "a" * 64


def _limit(value, unit):
    return ResourceLimit(value, unit)


def _budget(work=2, cost=20):
    return ResourceBudget(
        _limit(work, "items"), _limit(2, "workers"), _limit(1, "attempts"),
        _limit(100, "tokens"), _limit(100, "tokens"), _limit(cost, "cost-units"),
        _limit(60, "seconds"), _limit(4, "calls"), _limit(0, "calls"),
        _limit(100, "bytes"),
    )


class _Runner:
    def run(self, request):
        usage = ResourceUsage(work_items=1, cost_units=5)
        source = CheckpointSource("factory-ledger", request.epoch.ordinal, DIGEST)
        checkpoint = CampaignCheckpoint(
            request.campaign.campaign_id, request.epoch.epoch_id,
            request.epoch.ordinal, (source,), usage,
        )
        result = EpochResult(
            request.epoch.epoch_id, checkpoint.checkpoint_id,
            (ProgressSignal("coverage-moved", "coverage-a", "b" * 64),), (),
        )
        return EpochExecution(
            request.campaign.campaign_id, request.epoch.epoch_id, request.lease_id,
            "run-a", request.campaign.project_id, request.campaign.scope,
            (source,), usage, result, ("evidence-a",),
        )


class _InvestigationStub:
    def __init__(self, storage_root):
        self.storage_root = storage_root


def _setup(tmp_path):
    persistence = CampaignPersistence(tmp_path / "campaigns.db")
    campaigns = CampaignController(persistence, _Runner(), owner_id="api-controller")
    contract = CampaignContract(
        "project-a", "Inspect /Users/private/target with TOKEN=s3cr3t",
        ScopeBinding("scope-a", 1, DIGEST), _budget(), _budget(4, 40),
        CompletionCriteria(1, 0, 0), OperatorPolicy(risk_threshold=10),
        (ComponentVersion("factory", "1", DIGEST),),
    )
    campaigns.start(contract)
    epoch = EpochContract(contract.campaign_id, 1, ("work-a",), _budget())
    campaigns.launch(epoch, ResourceUsage(work_items=1, cost_units=10))
    server = FactoryServer(
        ("127.0.0.1", 0), _InvestigationStub(tmp_path / "runs"),
        campaign_controller=campaigns,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return campaigns, server, thread, contract.campaign_id


def _request(base, path, payload=None, expected=200):
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(base + path, data=data,
                      headers={"Content-Type": "application/json"})
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as exc:
        assert exc.code == expected, exc.read()
        return json.loads(exc.read() or b"{}")
    with response:
        assert response.status == expected
        return json.loads(response.read())


def test_campaign_api_is_bounded_canonical_and_omits_private_goal(tmp_path):
    campaigns, server, thread, campaign_id = _setup(tmp_path)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        listing = _request(base, "/api/campaigns")
        assert listing["schemaVersion"] == 1
        assert len(listing["campaigns"]) == 1
        public = listing["campaigns"][0]
        detail = _request(base, f"/api/campaigns/{campaign_id}")["campaign"]
        assert public["typedLinks"] == {
            "schemaVersion": 1, "references": [], "totalCount": 0,
            "truncated": False, "sourceTruncated": False,
            "strongestReproducedResult": None,
            "currentResearchFocus": None,
        }
        assert public == detail
        assert public["campaignId"] == campaign_id
        assert public["allowedActions"] == ["pause", "stop"]
        assert public["currentEpoch"] == {
            "epochId": public["currentEpoch"]["epochId"],
            "ordinal": 1,
            "workIds": ["work-a"],
        }
        assert public["budget"]["remaining"]["costUnits"] == 40
        assert public["handoff"] == _request(
            base, f"/api/campaigns/{campaign_id}/handoff"
        )["handoff"]
        encoded = json.dumps(public)
        assert "/Users/private" not in encoded
        assert "s3cr3t" not in encoded
        assert "goal" not in public
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        campaigns.persistence.close()


def test_campaign_controls_bind_operation_payload_and_expected_revision(tmp_path):
    campaigns, server, thread, campaign_id = _setup(tmp_path)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        initial = _request(base, f"/api/campaigns/{campaign_id}")["campaign"]
        pause = {"operationId": "ui-pause-a", "expectedRevision": initial["revision"]}
        suspended = _request(base, f"/api/campaigns/{campaign_id}/pause", pause)["campaign"]
        assert suspended["status"] == "suspended"
        assert suspended["allowedActions"] == ["resume", "stop"]
        assert _request(base, f"/api/campaigns/{campaign_id}/pause", pause)["campaign"] \
            == suspended

        changed_retry = dict(pause, expectedRevision=initial["revision"] + 1)
        assert "conflicting reuse" in _request(
            base, f"/api/campaigns/{campaign_id}/pause", changed_retry, expected=409,
        )["error"]
        assert "conflicting reuse" in _request(
            base, f"/api/campaigns/{campaign_id}/resume",
            {"operationId": "ui-pause-a", "expectedRevision": initial["revision"]},
            expected=409,
        )["error"]
        assert "stale" in _request(
            base, f"/api/campaigns/{campaign_id}/resume",
            {"operationId": "ui-resume-stale", "expectedRevision": initial["revision"]},
            expected=409,
        )["error"]

        resumed = _request(
            base, f"/api/campaigns/{campaign_id}/resume",
            {"operationId": "ui-resume-a", "expectedRevision": suspended["revision"]},
        )["campaign"]
        stopped = _request(
            base, f"/api/campaigns/{campaign_id}/stop",
            {"operationId": "ui-stop-a", "expectedRevision": resumed["revision"],
             "reasonCode": "operator-requested", "evidenceIds": []},
        )["campaign"]
        assert stopped["status"] == "stopped"
        assert stopped["terminal"]["reasonCode"] == "operator-requested"
        assert stopped["terminal"]["evidenceIds"] == ["operator-control:ui-stop-a"]
        assert stopped["allowedActions"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        campaigns.persistence.close()


def test_campaign_api_without_campaign_authority_is_empty_and_read_only(tmp_path):
    server = FactoryServer(("127.0.0.1", 0), _InvestigationStub(tmp_path / "runs"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert _request(base, "/api/campaigns") == {"schemaVersion": 1, "campaigns": []}
        _request(base, "/api/campaigns/unknown", expected=404)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_legacy_transition_payload_and_exact_retry_keep_byte_identity(tmp_path):
    persistence = CampaignPersistence(tmp_path / "legacy.db")
    campaigns = CampaignController(persistence, _Runner(), owner_id="legacy-controller")
    contract = CampaignContract(
        "project-legacy", "Continue a legacy campaign",
        ScopeBinding("scope-a", 1, DIGEST), _budget(), _budget(4, 40),
        CompletionCriteria(1, 0, 0), OperatorPolicy(risk_threshold=10),
        (ComponentVersion("factory", "1", DIGEST),),
    )
    campaigns.start(contract)
    before = persistence.conn.execute(
        "select payload_json from factory_campaign_events where campaign_id=? "
        "and operation_id='controller-start'", (contract.campaign_id,),
    ).fetchone()[0]
    assert "expectedRevision" not in before
    persistence.transition_campaign(
        contract.campaign_id, "running", authority="factory-scheduler",
        operation_id="controller-start",
    )
    after = persistence.conn.execute(
        "select payload_json from factory_campaign_events where campaign_id=? "
        "and operation_id='controller-start'", (contract.campaign_id,),
    ).fetchone()[0]
    assert after == before
    persistence.close()


def test_controller_serializes_scheduler_and_operator_api_reads(tmp_path):
    persistence = CampaignPersistence(tmp_path / "threaded.db")
    campaigns = CampaignController(persistence, _Runner(), owner_id="thread-controller")
    contract = CampaignContract(
        "project-threaded", "Exercise concurrent controller access",
        ScopeBinding("scope-a", 1, DIGEST), _budget(), _budget(4, 40),
        CompletionCriteria(1, 0, 0), OperatorPolicy(risk_threshold=10),
        (ComponentVersion("factory", "1", DIGEST),),
    )
    campaigns.start(contract)
    initial_revision = campaigns.public_state(contract.campaign_id)["revision"]
    errors = []
    barrier = threading.Barrier(9)

    def read_state():
        try:
            barrier.wait()
            campaigns.public_state(contract.campaign_id)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=read_state) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    campaigns.pause(
        contract.campaign_id, operation_id="thread-pause",
        expected_revision=initial_revision,
    )
    for thread in threads:
        thread.join(timeout=2)
    assert errors == []
    assert campaigns.snapshot(contract.campaign_id).status == "suspended"
    persistence.close()


def test_degraded_projection_exposes_no_controls_and_fails_closed(tmp_path):
    campaigns, server, thread, campaign_id = _setup(tmp_path)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with campaigns.persistence.conn:
            campaigns.persistence.conn.execute(
                "update factory_campaigns set revision=revision+10 where campaign_id=?",
                (campaign_id,),
            )
        public = _request(base, f"/api/campaigns/{campaign_id}")["campaign"]
        assert public["health"]["degraded"] is True
        assert public["health"]["problemCount"] == 1
        assert public["health"]["current"] is None
        assert public["health"]["previous"] is None
        assert public["health"]["problemCodes"] == ["campaign-projection-mismatch"]
        assert public["health"]["totalObservations"] == 0
        assert public["allowedActions"] == []
        result = _request(
            base, f"/api/campaigns/{campaign_id}/pause",
            {"operationId": "degraded-pause", "expectedRevision": public["revision"]},
            expected=409,
        )
        assert "degraded" in result["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        campaigns.persistence.close()
