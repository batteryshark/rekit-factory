import pytest

from rekit_factory.mission_routes import mission_control_deep_link, public_route_contract


def test_route_contract_and_delivery_url_share_the_exact_normalized_grammar():
    assert public_route_contract()["queryMarker"] == "mc-v1"
    assert mission_control_deep_link({
        "view": "mission-control", "runId": "run-1", "tab": "findings",
        "entityType": "finding", "entityId": "finding-1",
    }) == ("rekit-factory://mission-control/?mc=mc-v1&tab=outcomes&type=finding&"
           "entity=finding-1&run=run-1")
    assert mission_control_deep_link({
        "view": "mission-control", "tab": "campaigns",
        "entityType": "campaign", "entityId": "campaign-1",
    }) == ("rekit-factory://mission-control/?mc=mc-v1&tab=campaigns&type=campaign&"
           "entity=campaign-1")


@pytest.mark.parametrize("value", [
    {"view": "mission-control", "runId": "run-1", "tab": "decisions",
     "entityType": "finding", "entityId": "finding-1"},
    {"view": "mission-control", "runId": "run-1", "tab": "findings",
     "entityType": "finding", "entityId": "../finding"},
    {"view": "mission-control", "runId": "run-1", "tab": "findings",
     "entityType": "finding", "entityId": "finding-1", "extra": "payload"},
])
def test_delivery_url_rejects_cross_kind_hostile_and_extra_fields(value):
    with pytest.raises(ValueError):
        mission_control_deep_link(value)
