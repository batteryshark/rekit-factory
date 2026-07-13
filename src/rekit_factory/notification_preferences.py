"""Deterministic notification preference selection and UTC scheduling.

This module is deliberately pure: it does not mutate the outbox, deliver messages, or
interpret credentials.  A verified immutable outbox record and a canonical preference
document produce the same content-addressed schedule before and after a restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Mapping

from rekit_factory.notification_outbox import InvalidNotificationCandidate, _payload
from rekit_factory.notification_policy import (
    CANDIDATE_SCHEMA_VERSION,
    STALE_DECISION_POLICY_VERSION,
)
from rekit_factory.campaign_notification_policy import POLICY_VERSION as CAMPAIGN_POLICY_VERSION


PREFERENCES_SCHEMA_VERSION = 1
SCHEDULE_SCHEMA_VERSION = 1
MAX_SCOPED_RULES = 64
MAX_QUIET_WINDOWS = 32
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MODES = frozenset({"immediate", "batched", "digest", "muted", "escalation"})
_SEVERITIES = frozenset({"action-required", "consequential"})
_RULE_SOURCES = frozenset({"default", "severity", "project", "campaign"})
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class InvalidNotificationPreferences(ValueError):
    """The preference document is unsupported, ambiguous, or unbounded."""


class InvalidOutboxRecord(ValueError):
    """The scheduling input is not an intact public outbox record."""


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_id(value: Any, field: str) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise InvalidNotificationPreferences(f"{field} must be a safe stable identifier")
    return value


def _timestamp(value: datetime, field: str = "clock") -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any, field: str) -> datetime:
    if type(value) is not str or len(value) > 40 or not value.endswith("Z"):
        raise InvalidOutboxRecord(f"{field} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise InvalidOutboxRecord(f"{field} must be a canonical UTC timestamp") from exc
    if _timestamp(parsed, field) != value:
        raise InvalidOutboxRecord(f"{field} must be a canonical UTC timestamp")
    return parsed


def _rule(value: Any, field: str) -> dict[str, Any]:
    if type(value) is not dict or value.get("mode") not in _MODES:
        raise InvalidNotificationPreferences(f"{field} must be a supported rule")
    mode = value["mode"]
    parameter = {
        "batched": ("intervalMinutes", 1, 1440),
        "digest": ("atUtcMinute", 0, 1439),
        "escalation": ("afterMinutes", 1, 10080),
    }.get(mode)
    expected = {"mode"} if parameter is None else {"mode", parameter[0]}
    if set(value) != expected:
        raise InvalidNotificationPreferences(f"{field} has unsupported fields")
    result = {"mode": mode}
    if parameter is not None:
        name, minimum, maximum = parameter
        number = value[name]
        if type(number) is not int or not minimum <= number <= maximum:
            raise InvalidNotificationPreferences(
                f"{field}.{name} must be between {minimum} and {maximum}"
            )
        result[name] = number
    return result


def _rule_map(value: Any, field: str, *, allowed_keys: frozenset[str] | None = None) -> dict[str, Any]:
    if type(value) is not dict or len(value) > MAX_SCOPED_RULES:
        raise InvalidNotificationPreferences(f"{field} must be a bounded object")
    result: dict[str, Any] = {}
    for key, rule in value.items():
        stable = _safe_id(key, f"{field} key")
        if allowed_keys is not None and stable not in allowed_keys:
            raise InvalidNotificationPreferences(f"{field} has unsupported severity")
        result[stable] = _rule(rule, f"{field}.{stable}")
    return dict(sorted(result.items()))


def _quiet_hours(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {"timezone", "windows"}:
        raise InvalidNotificationPreferences("quietHours must contain timezone and windows")
    if value["timezone"] != "UTC":
        raise InvalidNotificationPreferences("quietHours timezone must be UTC")
    windows = value["windows"]
    if type(windows) is not list or len(windows) > MAX_QUIET_WINDOWS:
        raise InvalidNotificationPreferences("quietHours.windows must be a bounded list")
    canonical: list[dict[str, Any]] = []
    occupied: set[int] = set()
    for index, window in enumerate(windows):
        if type(window) is not dict or set(window) != {"days", "startMinute", "endMinute"}:
            raise InvalidNotificationPreferences(f"quietHours.windows[{index}] is invalid")
        days = window["days"]
        start, end = window["startMinute"], window["endMinute"]
        if (type(days) is not list or not 1 <= len(days) <= 7
                or any(type(day) is not int or not 0 <= day <= 6 for day in days)
                or len(set(days)) != len(days)):
            raise InvalidNotificationPreferences(f"quietHours.windows[{index}].days is invalid")
        if (type(start) is not int or type(end) is not int
                or not 0 <= start <= 1439 or not 0 <= end <= 1439 or start == end):
            raise InvalidNotificationPreferences(f"quietHours.windows[{index}] minutes are invalid")
        # Minute-level occupancy makes overlap rejection exact for the contract's resolution.
        for day in days:
            duration = (end - start) % 1440
            for offset in range(duration):
                minute = (day * 1440 + start + offset) % (7 * 1440)
                if minute in occupied:
                    raise InvalidNotificationPreferences("quietHours windows overlap")
                occupied.add(minute)
        canonical.append({"days": sorted(days), "startMinute": start, "endMinute": end})
    canonical.sort(key=lambda item: (item["days"], item["startMinute"], item["endMinute"]))
    return {"timezone": "UTC", "windows": canonical}


@dataclass(frozen=True)
class NotificationPreferences:
    """Validated canonical preferences with a content-bound identity."""

    _canonical_document: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NotificationPreferences":
        if type(value) is not dict or set(value) != {
            "schemaVersion", "revision", "default", "severity", "projects", "campaigns",
            "quietHours",
        }:
            raise InvalidNotificationPreferences("preference document has unsupported fields")
        if value["schemaVersion"] != PREFERENCES_SCHEMA_VERSION:
            raise InvalidNotificationPreferences("preference schema version is unsupported")
        document = {
            "schemaVersion": PREFERENCES_SCHEMA_VERSION,
            "revision": _safe_id(value["revision"], "revision"),
            "default": _rule(value["default"], "default"),
            "severity": _rule_map(value["severity"], "severity", allowed_keys=_SEVERITIES),
            "projects": _rule_map(value["projects"], "projects"),
            "campaigns": _rule_map(value["campaigns"], "campaigns"),
            "quietHours": _quiet_hours(value["quietHours"]),
        }
        return cls(_canonical_document=_canonical(document))

    @classmethod
    def from_json(cls, value: str) -> "NotificationPreferences":
        if type(value) is not str or len(value.encode("utf-8")) > 65536:
            raise InvalidNotificationPreferences("preference JSON is too large")
        try:
            def exact_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, item in pairs:
                    if key in result:
                        raise InvalidNotificationPreferences(
                            "preference JSON contains duplicate object keys"
                        )
                    result[key] = item
                return result

            decoded = json.loads(value, object_pairs_hook=exact_object)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise InvalidNotificationPreferences("preference JSON is invalid") from exc
        return cls.from_dict(decoded)

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self.canonical_json)

    @property
    def document(self) -> dict[str, Any]:
        # Return a detached value so callers cannot mutate the validated policy in place.
        return self.to_dict()

    @property
    def canonical_json(self) -> str:
        return self._canonical_document

    @property
    def identity(self) -> str:
        return "sha256:" + _sha256(self.canonical_json)


def _validated_record(record: Mapping[str, Any]) -> tuple[str, str, datetime]:
    if type(record) is not dict:
        raise InvalidOutboxRecord("outbox record must be an object")
    required = {"id", "status", "createdAt", "payload"}
    if not required <= set(record):
        raise InvalidOutboxRecord("outbox record is incomplete")
    notification_id = record["id"]
    if type(notification_id) is not str or not notification_id.startswith("notification-") \
            or len(notification_id) != 77:
        raise InvalidOutboxRecord("outbox record id is invalid")
    if record["status"] not in {"queued", "failed"}:
        raise InvalidOutboxRecord("only pending outbox records may be scheduled")
    payload = record["payload"]
    try:
        if payload.get("policyVersion") == CAMPAIGN_POLICY_VERSION:
            campaign_id = payload["deepLink"]["entityId"]
            candidate = {
                "schemaVersion": 1, "policyVersion": payload["policyVersion"],
                "dedupeKey": payload["dedupeKey"], "kind": payload["kind"],
                "severity": payload["severity"], "campaignId": campaign_id,
                "transitionMarker": payload["transitionMarker"],
                "entity": {"entityType": "campaign", "entityId": campaign_id},
                "message": payload["message"], "deepLink": payload["deepLink"],
            }
        elif payload.get("policyVersion") == STALE_DECISION_POLICY_VERSION:
            candidate = {
                "schemaVersion": CANDIDATE_SCHEMA_VERSION,
                "policyVersion": payload["policyVersion"],
                "policyRevision": payload["policyRevision"],
                "thresholdSeconds": payload["thresholdSeconds"],
                "dedupeKey": payload["dedupeKey"], "kind": payload["kind"],
                "severity": payload["severity"], "runId": payload["deepLink"]["runId"],
                "entity": {"entityType": payload["deepLink"]["entityType"],
                           "entityId": payload["deepLink"]["entityId"]},
                "message": payload["message"],
            }
        else:
            source_run_id = payload.get("sourceRunId", payload["deepLink"]["runId"])
            candidate = {
                "schemaVersion": CANDIDATE_SCHEMA_VERSION,
                "policyVersion": payload["policyVersion"],
                "dedupeKey": payload["dedupeKey"],
                "kind": payload["kind"],
                "severity": payload["severity"],
                "runId": source_run_id,
                "entity": {
                    "entityType": payload["deepLink"]["entityType"],
                    "entityId": payload["deepLink"]["entityId"],
                },
                "message": payload["message"],
            }
            if payload["deepLink"]["runId"] != source_run_id:
                candidate["deepLinkRunId"] = payload["deepLink"]["runId"]
        canonical_payload = _payload(candidate)
    except (KeyError, TypeError, InvalidNotificationCandidate) as exc:
        raise InvalidOutboxRecord("outbox payload is invalid") from exc
    if payload != canonical_payload:
        raise InvalidOutboxRecord("outbox payload is not canonical")
    expected_id = "notification-" + payload["dedupeKey"].removeprefix("sha256:")
    if notification_id != expected_id:
        raise InvalidOutboxRecord("outbox id conflicts with payload")
    return notification_id, payload["severity"], _parse_timestamp(record["createdAt"], "createdAt")


def _quiet_end(value: datetime, quiet: Mapping[str, Any]) -> datetime | None:
    week_start = value - timedelta(
        days=value.weekday(), hours=value.hour, minutes=value.minute,
        seconds=value.second, microseconds=value.microsecond,
    )
    for window in quiet["windows"]:
        duration = (window["endMinute"] - window["startMinute"]) % 1440
        for day in window["days"]:
            start = week_start + timedelta(days=day, minutes=window["startMinute"])
            for shifted in (start - timedelta(days=7), start, start + timedelta(days=7)):
                end = shifted + timedelta(minutes=duration)
                if shifted <= value < end:
                    return end
    return None


def _after_quiet(value: datetime, quiet: Mapping[str, Any]) -> datetime:
    # Overlap is rejected, but adjacent windows can form a chain. The bounded window count
    # bounds this loop and lets the scheduler fail closed if a corrupted object is supplied.
    for _ in range(MAX_QUIET_WINDOWS + 1):
        end = _quiet_end(value, quiet)
        if end is None:
            return value
        value = end
    raise InvalidNotificationPreferences("quietHours cannot produce a finite delivery time")


def _schedule_time(created: datetime, rule: Mapping[str, Any]) -> datetime | None:
    mode = rule["mode"]
    if mode == "muted":
        return None
    if mode in {"immediate", "escalation"}:
        return created
    if mode == "batched":
        seconds = rule["intervalMinutes"] * 60
        epoch = int(created.timestamp())
        return datetime.fromtimestamp(((epoch // seconds) + 1) * seconds, timezone.utc)
    target = created.replace(
        hour=rule["atUtcMinute"] // 60, minute=rule["atUtcMinute"] % 60,
        second=0, microsecond=0,
    )
    return target if target > created else target + timedelta(days=1)


def schedule_notification(
    record: Mapping[str, Any], preferences: NotificationPreferences, *,
    project_id: str, campaign_id: str,
) -> dict[str, Any]:
    """Return a stable delivery/mute schedule for one pending immutable outbox record."""
    if not isinstance(preferences, NotificationPreferences):
        raise TypeError("preferences must be NotificationPreferences")
    # Reparse even instances created outside ``from_dict`` so the scheduler never trusts an
    # unvalidated dataclass constructor call.
    preferences = NotificationPreferences.from_json(preferences.canonical_json)
    project_id = _safe_id(project_id, "projectId")
    campaign_id = _safe_id(campaign_id, "campaignId")
    notification_id, severity, created = _validated_record(record)
    document = preferences.document
    if campaign_id in document["campaigns"]:
        rule, source = document["campaigns"][campaign_id], "campaign"
    elif project_id in document["projects"]:
        rule, source = document["projects"][project_id], "project"
    elif severity in document["severity"]:
        rule, source = document["severity"][severity], "severity"
    else:
        rule, source = document["default"], "default"
    mode = rule["mode"]
    deliver = _schedule_time(created, rule)
    if deliver is not None:
        deliver = _after_quiet(deliver, document["quietHours"])
    escalation = None
    if mode == "escalation":
        escalation = _after_quiet(
            created + timedelta(minutes=rule["afterMinutes"]), document["quietHours"],
        )
    body: dict[str, Any] = {
        "schemaVersion": SCHEDULE_SCHEMA_VERSION,
        "preferencesId": preferences.identity,
        "preferencesRevision": document["revision"],
        "notificationId": notification_id,
        "projectId": project_id,
        "campaignId": campaign_id,
        "severity": severity,
        "mode": mode,
        "ruleSource": source,
        "disposition": "muted" if deliver is None else "scheduled",
        "deliverAt": None if deliver is None else _timestamp(deliver),
        "escalateAt": None if escalation is None else _timestamp(escalation),
    }
    body["scheduleId"] = "sha256:" + _sha256(_canonical(body))
    return body


def schedule_is_due(schedule: Mapping[str, Any], now: datetime, *, escalation: bool = False) -> bool:
    """Compare a canonical schedule with an injected clock without changing its identity."""
    exact = {
        "schemaVersion", "preferencesId", "preferencesRevision", "notificationId",
        "projectId", "campaignId", "severity", "mode", "ruleSource", "disposition",
        "deliverAt", "escalateAt", "scheduleId",
    }
    if (type(schedule) is not dict or set(schedule) != exact
            or schedule.get("schemaVersion") != SCHEDULE_SCHEMA_VERSION):
        raise ValueError("schedule is unsupported")
    expected = dict(schedule)
    schedule_id = expected.pop("scheduleId", None)
    if (type(schedule_id) is not str or _SHA256.fullmatch(schedule_id) is None
            or schedule_id != "sha256:" + _sha256(_canonical(expected))):
        raise ValueError("schedule identity is invalid")
    if (type(schedule["preferencesId"]) is not str
            or _SHA256.fullmatch(schedule["preferencesId"]) is None):
        raise ValueError("schedule preferences identity is invalid")
    for field in ("preferencesRevision", "notificationId", "projectId", "campaignId"):
        if type(schedule[field]) is not str or _SAFE_ID.fullmatch(schedule[field]) is None:
            raise ValueError(f"schedule {field} is invalid")
    if (not schedule["notificationId"].startswith("notification-")
            or len(schedule["notificationId"]) != 77):
        raise ValueError("schedule notification identity is invalid")
    if schedule["severity"] not in _SEVERITIES or schedule["mode"] not in _MODES \
            or schedule["ruleSource"] not in _RULE_SOURCES:
        raise ValueError("schedule policy selection is invalid")
    deliver_at, escalate_at = schedule["deliverAt"], schedule["escalateAt"]
    if schedule["mode"] == "muted":
        valid_semantics = (
            schedule["disposition"] == "muted" and deliver_at is None and escalate_at is None
        )
    elif schedule["mode"] == "escalation":
        valid_semantics = (
            schedule["disposition"] == "scheduled"
            and deliver_at is not None and escalate_at is not None
        )
    else:
        valid_semantics = (
            schedule["disposition"] == "scheduled"
            and deliver_at is not None and escalate_at is None
        )
    if not valid_semantics:
        raise ValueError("schedule mode semantics are invalid")
    deliver_time = None if deliver_at is None else _parse_timestamp(deliver_at, "deliverAt")
    escalation_time = (
        None if escalate_at is None else _parse_timestamp(escalate_at, "escalateAt")
    )
    if escalation_time is not None and deliver_time is not None and escalation_time < deliver_time:
        raise ValueError("schedule escalation precedes delivery")
    if type(escalation) is not bool:
        raise TypeError("escalation must be a boolean")
    field = "escalateAt" if escalation else "deliverAt"
    target = escalation_time if escalation else deliver_time
    if target is None:
        return False
    return target <= datetime.fromisoformat(_timestamp(now)[:-1] + "+00:00")
