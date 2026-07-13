from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from rekit_factory.notification_outbox import NotificationOutbox
from rekit_factory.notification_policy import notification_candidates
from rekit_factory.notification_preferences import (
    InvalidNotificationPreferences,
    InvalidOutboxRecord,
    NotificationPreferences,
    schedule_is_due,
    schedule_notification,
)
from rekit_factory.outcomes import project_outcomes
from rekit_factory.store import FactoryLedger


class FakeClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, **values):
        self.value += timedelta(**values)


def _preferences(**changes):
    value = {
        "schemaVersion": 1,
        "revision": "preferences-r1",
        "default": {"mode": "immediate"},
        "severity": {"consequential": {"mode": "digest", "atUtcMinute": 480}},
        "projects": {"project-muted": {"mode": "muted"}},
        "campaigns": {"campaign-batch": {"mode": "batched", "intervalMinutes": 30}},
        "quietHours": {"timezone": "UTC", "windows": []},
    }
    value.update(changes)
    return NotificationPreferences.from_dict(value)


def _record(tmp_path, clock, kind="operator-decision.waiting"):
    common = {"workers": (), "work_items": (), "dossiers": ()}
    old = project_outcomes(
        run={"id": "run-1", "status": "running"}, memory={}, pending_questions=(), **common,
    )
    if kind == "operator-decision.waiting":
        new = project_outcomes(
            run={"id": "run-1", "status": "running"}, memory={},
            pending_questions=[{"id": "question-1", "prompt": "secret"}], **common,
        )
    else:
        new = project_outcomes(
            run={"id": "run-1", "status": "running"},
            memory={"findings": {"finding-1": {"id": "finding-1", "status": "reproduced"}}},
            pending_questions=(), **common,
        )
    [candidate] = [item for item in notification_candidates(old, new) if item["kind"] == kind]
    ledger = FactoryLedger(tmp_path / f"{kind}.sqlite")
    box = NotificationOutbox(ledger.conn, clock=clock)
    record = box.get(box.admit([candidate])[0])
    ledger.close()
    return record


def test_precedence_and_all_modes_are_exact(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 7, 12, 34, tzinfo=timezone.utc))
    action = _record(tmp_path, clock)
    consequential = _record(tmp_path, clock, "finding.reproduced")
    preferences = _preferences(
        campaigns={
            "campaign-batch": {"mode": "batched", "intervalMinutes": 30},
            "campaign-escalate": {"mode": "escalation", "afterMinutes": 20},
        },
    )

    immediate = schedule_notification(action, preferences, project_id="project-1", campaign_id="c1")
    assert (immediate["mode"], immediate["ruleSource"], immediate["deliverAt"]) == (
        "immediate", "default", "2026-07-13T07:12:34.000000Z",
    )
    digest = schedule_notification(
        consequential, preferences, project_id="project-1", campaign_id="c1",
    )
    assert digest["deliverAt"] == "2026-07-13T08:00:00.000000Z"
    muted = schedule_notification(
        action, preferences, project_id="project-muted", campaign_id="c1",
    )
    assert (muted["mode"], muted["disposition"], muted["deliverAt"]) == (
        "muted", "muted", None,
    )
    batched = schedule_notification(
        consequential, preferences, project_id="project-muted", campaign_id="campaign-batch",
    )
    assert (batched["ruleSource"], batched["deliverAt"]) == (
        "campaign", "2026-07-13T07:30:00.000000Z",
    )
    escalation = schedule_notification(
        action, preferences, project_id="project-muted", campaign_id="campaign-escalate",
    )
    assert escalation["deliverAt"] == "2026-07-13T07:12:34.000000Z"
    assert escalation["escalateAt"] == "2026-07-13T07:32:34.000000Z"


def test_quiet_hours_are_explicit_utc_and_cross_week_boundaries(tmp_path):
    # Monday 23:30 UTC, inside a Monday window which crosses into Tuesday.
    clock = FakeClock(datetime(2026, 7, 13, 23, 30, tzinfo=timezone.utc))
    record = _record(tmp_path, clock)
    preferences = _preferences(quietHours={
        "timezone": "UTC",
        "windows": [
            {"days": [0, 1, 2, 3, 4], "startMinute": 1320, "endMinute": 420},
        ],
    })
    schedule = schedule_notification(record, preferences, project_id="p1", campaign_id="c1")
    assert schedule["deliverAt"] == "2026-07-14T07:00:00.000000Z"

    with pytest.raises(InvalidNotificationPreferences, match="timezone must be UTC"):
        _preferences(quietHours={"timezone": "America/New_York", "windows": []})


def test_schedule_is_restart_stable_and_clock_does_not_change_identity(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 7, 12, 34, tzinfo=timezone.utc))
    record = _record(tmp_path, clock)
    encoded = _preferences().canonical_json
    first = schedule_notification(
        record, NotificationPreferences.from_json(encoded), project_id="p1", campaign_id="c1",
    )
    clock.advance(days=4)
    restarted = schedule_notification(
        record, NotificationPreferences.from_json(encoded), project_id="p1", campaign_id="c1",
    )
    assert restarted == first
    assert schedule_is_due(first, datetime(2026, 7, 13, 7, 12, 33, tzinfo=timezone.utc)) is False
    assert schedule_is_due(first, clock()) is True


def test_canonical_serialization_and_identity_ignore_input_order():
    first = _preferences()
    unordered = json.loads(first.canonical_json)
    unordered["campaigns"] = dict(reversed(list(unordered["campaigns"].items())))
    second = NotificationPreferences.from_dict(unordered)
    assert second.canonical_json == first.canonical_json
    assert second.identity == first.identity
    assert second.identity.startswith("sha256:")
    detached = second.document
    detached["default"]["mode"] = "muted"
    assert second.canonical_json == first.canonical_json

    duplicate = first.canonical_json.replace(
        '"schemaVersion":1', '"schemaVersion":1,"schemaVersion":1', 1,
    )
    with pytest.raises(InvalidNotificationPreferences, match="duplicate"):
        NotificationPreferences.from_json(duplicate)


@pytest.mark.parametrize("mutation,match", [
    (lambda value: value.update(extra=True), "unsupported fields"),
    (lambda value: value.update(default={"mode": "batched", "intervalMinutes": 0}), "between"),
    (lambda value: value.update(severity={"private": {"mode": "muted"}}), "severity"),
    (lambda value: value.update(projects={"/private/path": {"mode": "muted"}}), "safe stable"),
    (lambda value: value.update(quietHours={
        "timezone": "UTC", "windows": [
            {"days": [0], "startMinute": 60, "endMinute": 180},
            {"days": [0], "startMinute": 120, "endMinute": 240},
        ],
    }), "overlap"),
])
def test_invalid_or_ambiguous_preferences_fail_closed(mutation, match):
    value = _preferences().to_dict()
    mutation(value)
    with pytest.raises(InvalidNotificationPreferences, match=match):
        NotificationPreferences.from_dict(value)


def test_forged_nonpending_or_context_inputs_fail_closed(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc))
    record = _record(tmp_path, clock)
    forged = dict(record, id="notification-" + "0" * 64)
    with pytest.raises(InvalidOutboxRecord, match="conflicts"):
        schedule_notification(forged, _preferences(), project_id="p1", campaign_id="c1")
    sent = dict(record, status="sent")
    with pytest.raises(InvalidOutboxRecord, match="pending"):
        schedule_notification(sent, _preferences(), project_id="p1", campaign_id="c1")
    with pytest.raises(InvalidNotificationPreferences, match="safe stable"):
        schedule_notification(record, _preferences(), project_id="/secret", campaign_id="c1")


def test_escalation_and_delivery_each_respect_quiet_hours(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 21, 50, tzinfo=timezone.utc))
    record = _record(tmp_path, clock)
    preferences = _preferences(
        campaigns={"c1": {"mode": "escalation", "afterMinutes": 30}},
        quietHours={"timezone": "UTC", "windows": [
            {"days": [0], "startMinute": 1320, "endMinute": 420},
        ]},
    )
    schedule = schedule_notification(record, preferences, project_id="p1", campaign_id="c1")
    assert schedule["deliverAt"] == "2026-07-13T21:50:00.000000Z"
    assert schedule["escalateAt"] == "2026-07-14T07:00:00.000000Z"
    assert schedule_is_due(schedule, datetime(2026, 7, 14, 6, 59, tzinfo=timezone.utc), escalation=True) is False
    assert schedule_is_due(schedule, datetime(2026, 7, 14, 7, 0, tzinfo=timezone.utc), escalation=True) is True


@pytest.mark.parametrize("changes,match", [
    ({"preferencesId": "sha256:" + "z" * 64}, "preferences identity"),
    ({"preferencesRevision": "/private"}, "preferencesRevision"),
    ({"notificationId": "notification-" + "a" * 63 + "/"}, "notificationId"),
    ({"severity": "critical"}, "policy selection"),
    ({"mode": "private"}, "policy selection"),
    ({"ruleSource": "guessed"}, "policy selection"),
    ({"disposition": "muted", "deliverAt": None}, "mode semantics"),
    ({"escalateAt": "2026-07-13T12:00:00.000000Z"}, "mode semantics"),
    ({"deliverAt": "2026-07-13 12:00:00Z"}, "canonical UTC"),
])
def test_rehashed_hostile_schedules_fail_semantic_validation(tmp_path, changes, match):
    clock = FakeClock(datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc))
    schedule = schedule_notification(
        _record(tmp_path, clock), _preferences(), project_id="p1", campaign_id="c1",
    )
    schedule.update(changes)
    identity = dict(schedule)
    identity.pop("scheduleId")
    canonical = json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    schedule["scheduleId"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    with pytest.raises(ValueError, match=match):
        schedule_is_due(schedule, clock())
