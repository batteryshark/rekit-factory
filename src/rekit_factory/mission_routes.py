"""One bounded Mission Control URL route contract shared by API and delivery."""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlencode
import re


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ROUTES = (
    {"entityType": "campaign", "surface": "campaigns", "requiresRun": False},
    {"entityType": "finding", "surface": "outcomes", "requiresRun": True},
    {"entityType": "operator-decision", "surface": "decisions", "requiresRun": True},
    {"entityType": "proof-bundle", "surface": "dossiers", "requiresRun": True},
)


def public_route_contract() -> dict[str, Any]:
    return {"schemaVersion": 1, "queryMarker": "mc-v1", "maxLength": 512,
            "routes": [dict(route) for route in ROUTES]}


def mission_control_deep_link(link: Mapping[str, Any]) -> str:
    """Return the exact custom-scheme URL for one already canonical delivery link."""
    if type(link) is not dict:
        raise ValueError("Mission Control deep link must be an object")
    entity_type, entity_id = link.get("entityType"), link.get("entityId")
    if not isinstance(entity_id, str) or _SAFE_ID.fullmatch(entity_id) is None:
        raise ValueError("Mission Control deep link entity is invalid")
    wire = {
        "campaign": ("campaigns", "campaigns", False),
        "finding": ("findings", "outcomes", True),
        "operator-decision": ("decisions", "decisions", True),
        "proof-bundle": ("dossiers", "dossiers", True),
    }.get(entity_type)
    if wire is None:
        raise ValueError("Mission Control deep link entity type is unsupported")
    wire_tab, surface, requires_run = wire
    expected = {"view", "tab", "entityType", "entityId"} | ({"runId"} if requires_run else set())
    run_id = link.get("runId")
    if (set(link) != expected or link.get("view") != "mission-control"
            or link.get("tab") != wire_tab
            or (requires_run and (not isinstance(run_id, str)
                                  or _SAFE_ID.fullmatch(run_id) is None))):
        raise ValueError("Mission Control deep link is not canonical")
    values = {"mc": "mc-v1", "tab": surface, "type": entity_type, "entity": entity_id}
    if requires_run:
        values["run"] = run_id
    result = "rekit-factory://mission-control/?" + urlencode(values)
    if len(result.partition("?")[2]) + 1 > 512:
        raise ValueError("Mission Control deep link is too long")
    return result
