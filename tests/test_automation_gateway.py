from __future__ import annotations

from datetime import datetime, timezone

import pytest

from rekit_factory.automation import (
    AutomationEvent, AutomationGateway, AutomationPrincipal, AutomationTemplate, signature,
)


NOW = datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc)
SECRET = b"a" * 32


class Owner:
    def __init__(self):
        self.launches = {}
        self.cancels = {}
        self.acks = {}
        self.event_values = ()
        self.events_fail = False
        self.crash_after_launch = False

    def launch(self, template, *, operation_id, origin):
        self.launches.setdefault(operation_id, {
            "schemaVersion": 1, "runId": "run-owned", "campaignId": "campaign-owned",
            "status": "queued", "deepLink": "/mission-control?run=run-owned",
            "origin": dict(origin),
        })
        result = self.launches[operation_id]
        if self.crash_after_launch:
            self.crash_after_launch = False
            raise RuntimeError("lost response after canonical commit")
        return result

    def cancel(self, run_id, *, reason_code, operation_id):
        self.cancels.setdefault(operation_id, {
            "schemaVersion": 1, "runId": run_id, "status": "cancel-requested",
            "reasonCode": reason_code,
        })
        return self.cancels[operation_id]

    def status(self, run_id):
        return {"schemaVersion": 1, "runId": run_id, "status": "completed",
                "deepLink": f"/mission-control?run={run_id}"}

    def acknowledge_handoff(self, run_id, handoff_id, *, operation_id):
        self.acks.setdefault(operation_id, {
            "schemaVersion": 1, "runId": run_id, "handoffId": handoff_id,
            "acknowledged": True,
        })
        return self.acks[operation_id]

    def events(self, run_ids):
        if self.events_fail:
            raise RuntimeError("consumer projection unavailable")
        return tuple(item for item in self.event_values if item.run_id in run_ids)

    def dossier(self, run_id, dossier_id):
        return {"schemaVersion": 1, "runId": run_id, "dossierId": dossier_id,
                "verified": True, "deepLink": f"/api/runs/{run_id}/dossiers/{dossier_id}"}


def gateway(path, owner, *, clients=None):
    principal = AutomationPrincipal("scheduler-a", SECRET, 60)
    return AutomationGateway(
        path, owner,
        templates={"approved-fixture": AutomationTemplate(
            "approved-fixture", 3, "target:fixture", "scope:fixture-r7",
        )},
        principals=clients or {principal.client_id: principal},
        clock=lambda: NOW,
    )


def headers(method, path, payload, *, nonce, key="", client="scheduler-a",
            secret=SECRET, timestamp=int(NOW.timestamp())):
    return {
        "X-Factory-Client": client,
        "X-Factory-Timestamp": str(timestamp),
        "X-Factory-Nonce": nonce,
        "Idempotency-Key": key,
        "X-Factory-Signature": signature(
            secret, timestamp=timestamp, nonce=nonce, method=method, path=path,
            idempotency_key=key, payload=payload,
        ),
    }


def launch(gateway, *, nonce="nonce-1", key="launch-1", payload=None):
    path = "/api/automation/v1/launch"
    payload = payload or {"templateId": "approved-fixture", "schedule": {
        "scheduleId": "nightly", "scheduledFor": "2026-07-14T04:00:00Z",
    }}
    return gateway.handle("POST", path, headers(
        "POST", path, payload, nonce=nonce, key=key,
    ), payload)


def test_launch_retry_is_exact_owned_and_does_not_duplicate_canonical_work(tmp_path):
    owner = Owner()
    service = gateway(tmp_path / "automation.db", owner)
    first_status, first = launch(service)
    retry_status, retry = launch(service, nonce="nonce-2")
    assert first_status == retry_status == 202
    assert retry == first
    assert len(owner.launches) == 1
    assert first["runId"] == "run-owned"
    assert first["deepLink"] == "/mission-control?run=run-owned"
    assert first["origin"] == {
        "clientId": "scheduler-a", "kind": "external-scheduler",
        "scheduleId": "nightly", "scheduledFor": "2026-07-14T04:00:00Z",
        "templateId": "approved-fixture", "templateRevision": 3,
    }

    conflict_status, conflict = launch(
        service, nonce="nonce-3", payload={"templateId": "approved-fixture", "schedule": {
            "scheduleId": "weekly", "scheduledFor": "2026-07-14T04:00:00Z",
        }},
    )
    assert conflict_status == 409 and conflict["error"] == "idempotency-conflict"


def test_restart_after_lost_launch_response_recovers_same_owner_operation(tmp_path):
    path = tmp_path / "automation.db"
    owner = Owner()
    owner.crash_after_launch = True
    first = gateway(path, owner)
    status, response = launch(first)
    assert status == 503 and response["error"] == "canonical-owner-unavailable"
    assert len(owner.launches) == 1
    first.close()

    restarted = gateway(path, owner)
    status, recovered = launch(restarted, nonce="nonce-after-restart")
    assert status == 202 and recovered["runId"] == "run-owned"
    assert len(owner.launches) == 1  # operation identity maps to the original canonical run
    assert restarted.conn.execute(
        "select status from factory_automation_commands"
    ).fetchone()[0] == "completed"


@pytest.mark.parametrize("hostile", (
    {"templateId": "approved-fixture", "target": "/Users/private", "schedule": {}},
    {"templateId": "approved-fixture", "scope": {"actions": ["expand_scope"]},
     "schedule": {}},
    {"templateId": "approved-fixture", "providerToken": "secret", "schedule": {}},
    {"templateId": "approved-fixture", "answer": "approve", "schedule": {}},
))
def test_hostile_launch_fields_cannot_supply_paths_scope_credentials_or_answers(
        tmp_path, hostile):
    service = gateway(tmp_path / "automation.db", Owner())
    status, _response = launch(service, payload=hostile)
    assert status in {400, 403}
    assert service.conn.execute(
        "select count(*) from factory_automation_runs"
    ).fetchone()[0] == 0


def test_auth_replay_expiry_rate_and_failures_are_audited(tmp_path):
    owner = Owner()
    principal = AutomationPrincipal("scheduler-a", SECRET, 2)
    service = gateway(tmp_path / "automation.db", owner, clients={"scheduler-a": principal})
    assert launch(service)[0] == 202

    payload = {}
    path = "/api/automation/v1/runs/run-owned/status"
    valid = headers("GET", path, payload, nonce="same-nonce")
    assert service.handle("GET", path, valid, payload)[0] == 200
    assert service.handle("GET", path, valid, payload) == (
        429, {"error": "rate-limit-exceeded", "schemaVersion": 1},
    )

    bad = headers("GET", path, payload, nonce="bad-signature")
    bad["X-Factory-Signature"] = "0" * 64
    assert service.handle("GET", path, bad, payload)[0] == 401
    expired = headers("GET", path, payload, nonce="expired", timestamp=1)
    assert service.handle("GET", path, expired, payload)[0] == 401
    outcomes = {row[0] for row in service.conn.execute(
        "select outcome from factory_automation_audit"
    )}
    assert {"accepted", "rate-rejected", "signature-rejected",
            "timestamp-rejected"}.issubset(outcomes)
    database = (tmp_path / "automation.db").read_bytes()
    assert SECRET not in database and b"/Users/private" not in database


def test_nonce_replay_is_rejected_before_any_second_owner_read(tmp_path):
    service = gateway(tmp_path / "automation.db", Owner())
    assert launch(service)[0] == 202
    path = "/api/automation/v1/runs/run-owned/status"
    signed = headers("GET", path, {}, nonce="replayed-status")
    assert service.handle("GET", path, signed, {})[0] == 200
    status, response = service.handle("GET", path, signed, {})
    assert status == 409 and response["error"] == "request-replayed"
    assert "replay-rejected" in {row[0] for row in service.conn.execute(
        "select outcome from factory_automation_audit"
    )}


def test_cursor_feed_is_durable_logically_once_and_survives_projection_outage(tmp_path):
    owner = Owner()
    path = tmp_path / "automation.db"
    service = gateway(path, owner)
    assert launch(service)[0] == 202
    owner.event_values = (
        AutomationEvent("notification-1", "run-owned", "operator-decision.required",
                        {"title": "Direction required", "deepLink": "/mission-control"}),
        AutomationEvent("dossier-1", "run-owned", "proof.available",
                        {"dossierId": "dossier-1", "verified": True}),
    )
    feed_path = "/api/automation/v1/events?after=0&limit=1"
    first = service.handle("GET", feed_path, headers(
        "GET", feed_path, {}, nonce="feed-1",
    ), {})[1]
    assert [item["eventId"] for item in first["events"]] == ["notification-1"]
    cursor = first["nextCursor"]
    service.close()

    restarted = gateway(path, owner)
    second_path = f"/api/automation/v1/events?after={cursor}&limit=10"
    second = restarted.handle("GET", second_path, headers(
        "GET", second_path, {}, nonce="feed-2",
    ), {})[1]
    assert [item["eventId"] for item in second["events"]] == ["dossier-1"]
    owner.events_fail = True
    replay_path = "/api/automation/v1/events?after=0&limit=10"
    replay = restarted.handle("GET", replay_path, headers(
        "GET", replay_path, {}, nonce="feed-3",
    ), {})[1]
    assert [item["eventId"] for item in replay["events"]] == [
        "notification-1", "dossier-1",
    ]
    assert restarted.conn.execute(
        "select count(*) from factory_automation_events"
    ).fetchone()[0] == 2


def test_owned_status_cancel_handoff_and_verified_dossier_routes_are_bounded(tmp_path):
    owner = Owner()
    service = gateway(tmp_path / "automation.db", owner)
    assert launch(service)[0] == 202

    status_path = "/api/automation/v1/runs/run-owned/status"
    assert service.handle("GET", status_path, headers(
        "GET", status_path, {}, nonce="status-1",
    ), {})[1]["status"] == "completed"
    cancel_path = "/api/automation/v1/runs/run-owned/cancel"
    cancel_body = {"reasonCode": "schedule-superseded"}
    assert service.handle("POST", cancel_path, headers(
        "POST", cancel_path, cancel_body, nonce="cancel-1", key="cancel-1",
    ), cancel_body)[0] == 202
    ack_path = "/api/automation/v1/runs/run-owned/acknowledge-handoff"
    ack_body = {"handoffId": "handoff-1"}
    assert service.handle("POST", ack_path, headers(
        "POST", ack_path, ack_body, nonce="ack-1", key="ack-1",
    ), ack_body)[1]["acknowledged"] is True
    dossier_path = "/api/automation/v1/runs/run-owned/dossiers/dossier-1"
    assert service.handle("GET", dossier_path, headers(
        "GET", dossier_path, {}, nonce="dossier-1",
    ), {})[1]["verified"] is True

    answer_path = "/api/automation/v1/runs/run-owned/answers"
    answer = {"questionId": "question-1", "answer": "approve"}
    assert service.handle("POST", answer_path, headers(
        "POST", answer_path, answer, nonce="answer-1", key="answer-1",
    ), answer)[0] == 404
