from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rekit_factory.api import FactoryServer
from rekit_factory.campaign_contracts import (
    CampaignContract, CompletionCriteria, ComponentVersion, OperatorPolicy,
    ResourceBudget, ResourceLimit, ScopeBinding,
)
from rekit_factory.campaign_controller import CampaignController
from rekit_factory.campaign_persistence import CampaignPersistence


DIGEST = "a" * 64


def _limit(value: int, unit: str) -> ResourceLimit:
    return ResourceLimit(value, unit)


def _budget(work: int = 4, cost: int = 40) -> ResourceBudget:
    return ResourceBudget(
        _limit(work, "items"), _limit(2, "workers"), _limit(2, "attempts"),
        _limit(1000, "tokens"), _limit(1000, "tokens"),
        _limit(cost, "cost-units"), _limit(600, "seconds"), _limit(20, "calls"),
        _limit(0, "calls"), _limit(10_000, "bytes"),
    )


def _contract(project: str, goal: str = "bounded campaign") -> CampaignContract:
    suffix = project[-1]
    return CampaignContract(
        project, goal, ScopeBinding(f"scope-{suffix}", 1, suffix * 64),
        _budget(2, 20), _budget(), CompletionCriteria(100, 0, 0),
        OperatorPolicy(risk_threshold=10),
        (ComponentVersion("factory", "1", DIGEST),),
    )


class _UnusedRunner:
    def run(self, _request):
        raise AssertionError("operator API must not launch campaign work")


def _request(url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def _running_server(tmp_path, campaign_controller):
    # Campaign routes do not depend on the investigation controller, but FactoryServer
    # intentionally owns the shared loopback transport and supervisor lifecycle.
    factory = SimpleNamespace(storage_root=tmp_path)
    server = FactoryServer(
        ("127.0.0.1", 0), factory, campaign_controller=campaign_controller,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def test_campaign_api_exact_retries_stale_races_and_cross_campaign_isolation(tmp_path):
    persistence = CampaignPersistence(tmp_path / "campaigns.db")
    controller = CampaignController(persistence, _UnusedRunner(), owner_id="api-test")
    first, second = _contract("project-a"), _contract("project-b")
    first_start = controller.start(first)
    controller.start(second)
    server, thread, base = _running_server(tmp_path, controller)
    try:
        pause = {
            "operationId": "ui-pause-exact", "expectedRevision": first_start.status and 2,
        }
        url = f"{base}/api/campaigns/{first.campaign_id}/pause"
        # Repeated clicks and reconnect replay of the exact request are one transition.
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _index: _request(url, pause), range(8)))
        assert {status for status, _body in responses} == {200}
        assert {body["campaign"]["revision"] for _status, body in responses} == {3}
        assert persistence.campaign(first.campaign_id).revision == 3

        # Reusing the operation identity with changed content and using a fresh operation
        # against the stale browser revision both fail without another transition.
        changed = {"operationId": "ui-pause-exact", "expectedRevision": 3}
        assert _request(url, changed)[0] == 409
        stale = {"operationId": "ui-pause-stale-tab", "expectedRevision": 2}
        assert _request(url, stale)[0] == 409
        assert persistence.campaign(first.campaign_id).revision == 3

        # A request scoped to campaign A never changes campaign B.
        assert persistence.campaign(second.campaign_id).revision == 2
        status, body = _request(f"{base}/api/campaigns/{second.campaign_id}")
        assert status == 200 and body["campaign"]["status"] == "running"
        assert body["campaign"]["revision"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        persistence.close()


def test_campaign_api_public_projection_is_bounded_private_and_campaign_local(tmp_path):
    persistence = CampaignPersistence(tmp_path / "campaigns.db")
    controller = CampaignController(persistence, _UnusedRunner(), owner_id="api-test")
    private = "/Users/operator/private/target SECRET_TOKEN raw transcript"
    first, second = _contract("project-a", private), _contract("project-b")
    controller.start(first)
    controller.start(second)
    # Seed a deliberately large canonical history in each campaign. The public handoff must
    # retain counts while exposing only the bounded stable-ID tail for the selected campaign.
    with persistence.conn:
        for campaign, prefix in ((first, "a"), (second, "b")):
            for index in range(80):
                persistence.conn.execute(
                    "insert into factory_campaign_controller_epochs "
                    "(campaign_id,epoch_id,owner_id,lease_id,reservation_id,reservation_json,"
                    "execution_json,factory_run_id) values (?,?,?,?,?,?,?,?)",
                    (campaign.campaign_id, f"epoch-{prefix}-{index}", "owner",
                     f"lease-{prefix}-{index}", f"reserve-{prefix}-{index}", "{}",
                     json.dumps({"evidenceIds": [f"evidence-{prefix}-{index}"]}),
                     f"run-{prefix}-{index}"),
                )
    server, thread, base = _running_server(tmp_path, controller)
    try:
        status, listing = _request(f"{base}/api/campaigns")
        assert status == 200 and listing["schemaVersion"] == 1
        encoded = json.dumps(listing, sort_keys=True)
        for forbidden in (private, "/Users/", "SECRET_TOKEN", "raw transcript", "goal"):
            assert forbidden not in encoded
        by_id = {item["campaignId"]: item for item in listing["campaigns"]}
        assert set(by_id) == {first.campaign_id, second.campaign_id}
        first_public = by_id[first.campaign_id]
        handoff = first_public["handoff"]
        assert handoff["evidenceCount"] == 80 and handoff["factoryRunCount"] == 80
        assert handoff["truncated"] is True
        assert len(handoff["evidenceIds"]) == len(handoff["factoryRunIds"]) == 32
        assert all("-a-" in value for value in handoff["evidenceIds"])
        assert all("-a-" in value for value in handoff["factoryRunIds"])
        assert not ({"events", "paths", "transcripts", "artifacts"} & set(first_public))

        status, detail = _request(f"{base}/api/campaigns/{first.campaign_id}")
        detail_campaign = dict(detail["campaign"])
        typed_links = detail_campaign.pop("typedLinks")
        assert status == 200 and detail_campaign == first_public
        assert typed_links["references"] == []
        status, missing = _request(f"{base}/api/campaigns/campaign-missing")
        assert status == 404 and "does not exist" in missing["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        persistence.close()


def test_campaign_stop_exact_content_cannot_be_forged_or_replayed_differently(tmp_path):
    persistence = CampaignPersistence(tmp_path / "campaigns.db")
    controller = CampaignController(persistence, _UnusedRunner(), owner_id="api-test")
    campaign = _contract("project-a")
    controller.start(campaign)
    server, thread, base = _running_server(tmp_path, controller)
    try:
        url = f"{base}/api/campaigns/{campaign.campaign_id}/stop"
        exact = {
            "operationId": "decision-stop-exact", "expectedRevision": 2,
            "reasonCode": "operator-requested", "evidenceIds": ["decision-1"],
        }
        first = _request(url, exact)
        retry = _request(url, exact)
        assert first[0] == retry[0] == 200
        assert first[1]["campaign"]["revision"] == retry[1]["campaign"]["revision"] == 3
        forged = {**exact, "reasonCode": "changed-after-signing"}
        assert _request(url, forged)[0] == 409
        assert persistence.campaign(campaign.campaign_id).terminal.reason_code \
            == "operator-requested"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        persistence.close()


def test_degraded_campaign_health_is_bounded_and_fails_control_authority_closed(tmp_path):
    persistence = CampaignPersistence(tmp_path / "campaigns.db")
    controller = CampaignController(persistence, _UnusedRunner(), owner_id="api-test")
    campaign = _contract("project-a")
    controller.start(campaign)
    # Simulate a valid-shaped live projection that no longer agrees with canonical replay.
    usage = controller.public_state(campaign.campaign_id)["cumulativeUsage"]
    usage["costUnits"] = 1
    with persistence.conn:
        persistence.conn.execute(
            "update factory_campaigns set cumulative_usage_json=? where campaign_id=?",
            (json.dumps(usage, sort_keys=True), campaign.campaign_id),
        )
    public = controller.public_state(campaign.campaign_id)
    health = public["health"]
    assert set(health) == {
        "current", "degraded", "previous", "problemCodes", "problemCount",
        "problemsTruncated", "totalObservations",
    }
    assert health["degraded"] is True
    assert health["current"] is health["previous"] is None
    assert health["problemCount"] >= 1
    assert health["problemCodes"]
    assert len(health["problemCodes"]) <= 16
    assert health["totalObservations"] == 0
    assert type(health["problemsTruncated"]) is bool
    assert public["allowedActions"] == []
    persistence.close()
