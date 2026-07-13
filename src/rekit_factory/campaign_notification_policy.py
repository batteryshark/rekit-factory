"""Pure exception-only candidates from canonical CampaignController public state.

The campaign controller and run outcome projection are separate authorities.  This module
therefore accepts only the bounded public campaign read model and never invents campaign facts
from a run projection.  Persistence and observation wiring remain outbox concerns.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping


POLICY_VERSION = "factory-campaign-notification-policy/v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TERMINAL = frozenset({"completed", "exhausted", "blocked", "stopped", "policy-stopped", "failed"})
_RESOURCES = frozenset({
    "workItems", "retries", "inputTokens", "outputTokens", "costUnits", "wallSeconds",
    "toolCalls", "networkCalls", "artifactBytes",
})


class InvalidCampaignNotificationState(ValueError):
    """The campaign observation or policy is not safe enough to notify from."""


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _safe_id(value: Any, field: str) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise InvalidCampaignNotificationState(f"{field} must be a safe stable identifier")
    return value


def _thresholds(value: Mapping[str, Any]) -> dict[str, tuple[int, ...]]:
    if type(value) is not dict or not value or set(value) - _RESOURCES:
        raise InvalidCampaignNotificationState("budget thresholds must name supported resources")
    result: dict[str, tuple[int, ...]] = {}
    for resource, raw in value.items():
        if (type(raw) is not list or not raw or len(raw) > 8
                or any(type(item) is not int or not 1 <= item <= 10_000 for item in raw)
                or raw != sorted(set(raw))):
            raise InvalidCampaignNotificationState("budget thresholds must be sorted basis points")
        result[resource] = tuple(raw)
    return dict(sorted(result.items()))


def _state(value: Mapping[str, Any]) -> dict[str, Any]:
    if type(value) is not dict or value.get("schemaVersion") != 1:
        raise InvalidCampaignNotificationState("campaign state must be the v1 public projection")
    campaign_id = _safe_id(value.get("campaignId"), "campaignId")
    project_id = _safe_id(value.get("projectId"), "projectId")
    revision, status = value.get("revision"), value.get("status")
    if type(revision) is not int or revision < 1 or type(status) is not str:
        raise InvalidCampaignNotificationState("campaign revision or status is invalid")
    health = value.get("health")
    if type(health) is not dict or health.get("degraded") is not False:
        raise InvalidCampaignNotificationState("degraded campaign state cannot notify")
    usage, budget = value.get("cumulativeUsage"), value.get("budget")
    cumulative = budget.get("cumulative") if type(budget) is dict else None
    if type(usage) is not dict or type(cumulative) is not dict:
        raise InvalidCampaignNotificationState("campaign budget state is missing")
    ratios: dict[str, int] = {}
    for resource in _RESOURCES:
        if resource not in usage or resource not in cumulative:
            continue
        amount, limit = usage[resource], cumulative[resource]
        ceiling = limit.get("value") if type(limit) is dict else None
        if type(amount) is not int or amount < 0 or type(ceiling) is not int or ceiling < 0:
            raise InvalidCampaignNotificationState("campaign budget state is invalid")
        ratios[resource] = 10_000 if ceiling == 0 and amount else (
            0 if ceiling == 0 else min(10_000, amount * 10_000 // ceiling)
        )
    terminal = value.get("terminal")
    if terminal is not None and (type(terminal) is not dict
                                 or terminal.get("status") != status
                                 or type(terminal.get("reasonCode")) is not str):
        raise InvalidCampaignNotificationState("campaign terminal authority is inconsistent")
    return {"campaignId": campaign_id, "projectId": project_id, "revision": revision,
            "status": status, "ratios": ratios, "terminal": terminal}


def _candidate(campaign_id: str, kind: str, severity: str, message: str, marker: str) -> dict[str, Any]:
    identity = {"policyVersion": POLICY_VERSION, "campaignId": campaign_id,
                "transition": kind, "marker": marker}
    dedupe = "sha256:" + hashlib.sha256(_canonical(identity).encode()).hexdigest()
    return {"schemaVersion": 1, "policyVersion": POLICY_VERSION, "dedupeKey": dedupe,
            "kind": kind, "severity": severity, "campaignId": campaign_id,
            "transitionMarker": marker,
            "entity": {"entityType": "campaign", "entityId": campaign_id},
            "message": message,
            "deepLink": {"view": "mission-control", "tab": "campaigns",
                         "entityType": "campaign", "entityId": campaign_id}}


def campaign_notification_candidates(
    old: Mapping[str, Any] | None, new: Mapping[str, Any], *,
    budget_thresholds: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return stable budget and terminal candidates; hydration and chatter are silent."""
    current = _state(new)
    configured = _thresholds(budget_thresholds)
    if old is None:
        return []
    previous = _state(old)
    if current["campaignId"] != previous["campaignId"] \
            or current["projectId"] != previous["projectId"] \
            or current["revision"] < previous["revision"]:
        raise InvalidCampaignNotificationState("campaign observation identity regressed")
    candidates: list[dict[str, Any]] = []
    for resource, values in configured.items():
        before, after = previous["ratios"].get(resource), current["ratios"].get(resource)
        if before is None or after is None or after < before:
            continue
        for threshold in values:
            if before < threshold <= after:
                candidates.append(_candidate(
                    current["campaignId"], "campaign.budget-threshold", "consequential",
                    "A configured campaign budget threshold was crossed.",
                    f"{resource}:{threshold}",
                ))
    if current["status"] in _TERMINAL and previous["status"] not in _TERMINAL:
        terminal = current["terminal"]
        reason = terminal["reasonCode"] if terminal else current["status"]
        infrastructure = current["status"] == "failed" and reason == "infrastructure-failure"
        candidates.append(_candidate(
            current["campaignId"],
            "campaign.infrastructure-action" if infrastructure else "campaign.terminal",
            "action-required" if infrastructure else "consequential",
            ("A campaign infrastructure failure requires operator attention."
             if infrastructure else "A campaign reached a terminal outcome."),
            f"{current['status']}:{reason}",
        ))
    return sorted(candidates, key=lambda item: (item["kind"], item["dedupeKey"]))
