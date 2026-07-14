from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import socket

import pytest

from rekit_factory.notification_delivery import (
    DesktopChannel, WebhookChannel, WebhookRequest, delivery_preview,
)
from rekit_factory.notification_outbox import NotificationOutbox
from rekit_factory.notification_outbox import NotificationStateConflict
from rekit_factory.notification_policy import FindingNotificationPolicy, notification_candidates
from rekit_factory.notification_preferences import NotificationPreferences
from rekit_factory.notification_supervisor import (
    NotificationDeliverySupervisor,
    SafeHttpsWebhookTransport,
)
from rekit_factory.outcomes import project_outcomes
from rekit_factory.store import FactoryLedger


class FakeClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, **changes):
        self.value += timedelta(**changes)


def _preferences(*, mode="immediate"):
    default = {"mode": mode}
    if mode == "batched":
        default["intervalMinutes"] = 30
    elif mode == "escalation":
        default["afterMinutes"] = 20
    return NotificationPreferences.from_dict({
        "schemaVersion": 1, "revision": "r1", "default": default,
        "severity": {}, "projects": {}, "campaigns": {},
        "quietHours": {"timezone": "UTC", "windows": []},
    })


def _admit(ledger, clock):
    common = {"workers": (), "work_items": (), "dossiers": ()}
    old = project_outcomes(
        run={"id": "run-1", "status": "running"}, memory={},
        pending_questions=(), **common,
    )
    new = project_outcomes(
        run={"id": "run-1", "status": "running"}, memory={},
        pending_questions=[{"id": "question-1", "prompt": "private"}], **common,
    )
    [candidate] = notification_candidates(old, new)
    return NotificationOutbox(ledger.conn, clock=clock).admit([candidate])[0]


def test_cross_run_campaign_proof_schedules_by_source_and_delivers_exact_link(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc))
    ledger = FactoryLedger(tmp_path / "run.db")
    common = {"workers": (), "work_items": (), "dossiers": (), "pending_questions": ()}
    old = project_outcomes(
        run={"id": "run-source", "status": "running"},
        memory={"findings": {"finding-a": {"id": "finding-a", "status": "candidate"}}},
        **common,
    )
    new = project_outcomes(
        run={"id": "run-source", "status": "running"},
        memory={"findings": {"finding-a": {"id": "finding-a", "status": "reproduced"}}},
        **common,
    )
    [candidate] = notification_candidates(
        old, new,
        proof_resolver=lambda _run, _finding: ("run-proof", "dossier-exact"),
    )
    [notification_id] = NotificationOutbox(ledger.conn, clock=clock).admit([candidate])
    supervisor = NotificationDeliverySupervisor(ledger.conn, clock=clock)
    schedules = supervisor.schedule_unscheduled(
        _preferences(), project_id="project-a", campaign_id="campaign-a",
        channel_refs=["desktop-local"], routing_id="run-source",
    )
    assert len(schedules) == 1 and schedules[0]["notificationId"] == notification_id
    [work] = supervisor.claim_due("sender")
    assert delivery_preview(work["record"])["deepLink"] == {
        "view": "mission-control", "runId": "run-proof", "tab": "dossiers",
        "entityType": "proof-bundle", "entityId": "dossier-exact",
    }
    ledger.close()


def test_configured_finding_stage_reaches_provider_once_with_exact_proof_link(tmp_path):
    """Exercise canonical fold through the actual provider-visible webhook boundary."""
    clock = FakeClock(datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc))
    ledger = FactoryLedger(tmp_path / "run.db")
    common = {"workers": (), "work_items": (), "pending_questions": ()}
    candidate = project_outcomes(
        run={"id": "run-1", "status": "running"},
        memory={"findings": {"finding-1": {"id": "finding-1", "status": "candidate"}}},
        dossiers=(), **common,
    )
    reproduced = project_outcomes(
        run={"id": "run-1", "status": "running"},
        memory={"findings": {"finding-1": {"id": "finding-1", "status": "reproduced"}}},
        dossiers=[{"id": "dossier-1", "findingId": "finding-1",
                   "verificationStatus": "published"}], **common,
    )
    accepted = project_outcomes(
        run={"id": "run-1", "status": "running"},
        memory={
            "findings": {"finding-1": {"id": "finding-1", "status": "reproduced"}},
            "finding_operator_decisions": {
                "decision-1": {"id": "decision-1", "findingId": "finding-1",
                               "decision": "accepted", "_eventSeq": 1},
            },
        },
        dossiers=[{"id": "dossier-1", "findingId": "finding-1",
                   "verificationStatus": "published"}], **common,
    )
    policy = FindingNotificationPolicy.for_stage("accepted")
    assert ledger.admit_notification_projection(
        "run-1", candidate, finding_policy=policy,
    ) == []
    assert ledger.admit_notification_projection(
        "run-1", reproduced, finding_policy=policy,
    ) == []
    [notification_id] = ledger.admit_notification_projection(
        "run-1", accepted, finding_policy=policy,
    )

    # Projection admission uses the ledger's production clock. Align the injected
    # delivery clock with that durable timestamp so this boundary test does not
    # become date- or wall-clock-dependent.
    admitted = NotificationOutbox(ledger.conn).get(notification_id)
    assert admitted is not None
    clock.value = datetime.fromisoformat(admitted["createdAt"].replace("Z", "+00:00"))

    supervisor = NotificationDeliverySupervisor(ledger.conn, clock=clock)
    supervisor.schedule(
        notification_id, _preferences(), project_id="project-1", campaign_id="campaign-1",
        channel_refs=["webhook-primary"],
    )
    channel = WebhookChannel(
        "webhook-primary", "https://hooks.example.test/factory", "credential:primary",
    )

    class Resolver:
        def resolve(self, ref):
            assert ref == "credential:primary"
            return "secret-token"

    class Transport:
        def __init__(self):
            self.bodies = []

        def send(self, request, *, bearer_token):
            assert bearer_token == "secret-token"
            self.bodies.append(json.loads(request.body))

    transport = Transport()
    result = supervisor.run_once(
        "sender", channels={"webhook-primary": channel}, webhook_transport=transport,
        credential_resolver=Resolver(),
    )
    assert result == [{"deliveryId": result[0]["deliveryId"], "sent": True,
                       "errorCode": None}]
    assert len(transport.bodies) == 1
    assert transport.bodies[0]["kind"] == "finding.accepted"
    assert transport.bodies[0]["deepLink"] == {
        "view": "mission-control", "runId": "run-1", "tab": "dossiers",
        "entityType": "proof-bundle", "entityId": "dossier-1",
    }
    assert transport.bodies[0]["deepLinkUrl"] == (
        "rekit-factory://mission-control/?mc=mc-v1&tab=dossiers&"
        "type=proof-bundle&entity=dossier-1&run=run-1"
    )

    # Reconnect/restart observes the same canonical state and cannot create a second
    # provider call for the already delivered configured-stage transition.
    assert ledger.admit_notification_projection(
        "run-1", accepted, finding_policy=policy,
    ) == []
    restarted = NotificationDeliverySupervisor(ledger.conn, clock=clock)
    assert restarted.run_once(
        "sender-restarted", channels={"webhook-primary": channel},
        webhook_transport=transport, credential_resolver=Resolver(),
    ) == []
    assert len(transport.bodies) == 1
    ledger.close()


class RecordingDesktop:
    def __init__(self, *, ambiguous=False):
        self.calls = []
        self.ambiguous = ambiguous

    def notify(self, **kwargs):
        self.calls.append(kwargs)
        if self.ambiguous:
            raise TimeoutError("receiver outcome is unknown and contains hostile prose")


def test_batch_schedule_and_channel_disposition_survive_restart(tmp_path):
    path = tmp_path / "run.db"
    clock = FakeClock(datetime(2026, 7, 13, 7, 12, 34, tzinfo=timezone.utc))
    first = FactoryLedger(path)
    notification_id = _admit(first, clock)
    supervisor = NotificationDeliverySupervisor(first.conn, clock=clock)
    schedule = supervisor.schedule(
        notification_id, _preferences(mode="batched"), project_id="project-1",
        campaign_id="campaign-1", channel_refs=["desktop-local"],
    )
    assert schedule["deliverAt"] == "2026-07-13T07:30:00.000000Z"
    assert supervisor.schedule(
        notification_id, _preferences(mode="batched"), project_id="project-1",
        campaign_id="campaign-1", channel_refs=["desktop-local"],
    ) == schedule
    with pytest.raises(NotificationStateConflict, match="channel references"):
        supervisor.schedule(
            notification_id, _preferences(mode="batched"), project_id="project-1",
            campaign_id="campaign-1", channel_refs=["desktop-other"],
        )
    assert supervisor.claim_due("sender") == []
    first.close()

    clock.advance(minutes=17, seconds=26)
    reopened = FactoryLedger(path)
    restarted = NotificationDeliverySupervisor(reopened.conn, clock=clock)
    [work] = restarted.claim_due("sender")
    assert work["notificationId"] == notification_id
    assert work["schedule"] == schedule
    stored = reopened.conn.execute(
        "select preferences_json,schedule_json from factory_notification_schedules",
    ).fetchone()
    assert json.loads(stored["preferences_json"])["revision"] == "r1"
    assert "endpoint" not in stored["schedule_json"] + stored["preferences_json"]
    reopened.close()


def test_ambiguous_receiver_retries_with_one_provider_idempotency_key(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc))
    ledger = FactoryLedger(tmp_path / "run.db")
    notification_id = _admit(ledger, clock)
    supervisor = NotificationDeliverySupervisor(
        ledger.conn, clock=clock, max_attempts=2, base_backoff_seconds=10,
    )
    supervisor.schedule(
        notification_id, _preferences(), project_id="p1", campaign_id="c1",
        channel_refs=["desktop-local"],
    )
    transport = RecordingDesktop(ambiguous=True)
    channels = {"desktop-local": DesktopChannel("desktop-local")}

    assert supervisor.run_once(
        "sender", channels=channels, desktop_transport=transport,
    )[0]["errorCode"] == "transport-failed"
    clock.advance(seconds=10)
    supervisor.run_once("sender", channels=channels, desktop_transport=transport)
    state = supervisor.get_deliveries(notification_id)
    assert state[0]["status"] == "failed"
    assert state[0]["attemptCount"] == 2
    assert state[0]["nextAttemptAt"] is None
    assert len(transport.calls) == 2
    assert len({call["idempotency_key"] for call in transport.calls}) == 1
    assert NotificationOutbox(ledger.conn, clock=clock).get(notification_id)["status"] == "queued"
    ledger.close()


def test_escalation_is_durable_and_self_resolution_supersedes_pending_phase(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc))
    ledger = FactoryLedger(tmp_path / "run.db")
    notification_id = _admit(ledger, clock)
    supervisor = NotificationDeliverySupervisor(ledger.conn, clock=clock)
    supervisor.schedule(
        notification_id, _preferences(mode="escalation"), project_id="p1", campaign_id="c1",
        channel_refs=["desktop-local"],
    )
    [initial] = supervisor.claim_due("sender")
    supervisor.record_sent(initial["deliveryId"], initial["leaseToken"])
    supervisor.supersede(notification_id)
    assert [(item["phase"], item["status"]) for item in supervisor.get_deliveries(notification_id)] == [
        ("delivery", "sent"), ("escalation", "superseded"),
    ]
    clock.advance(minutes=20)
    assert supervisor.claim_due("sender") == []
    ledger.close()


def test_canonical_self_resolution_transactionally_cancels_scheduled_delivery(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc))
    ledger = FactoryLedger(tmp_path / "run.db")
    common = {"workers": (), "work_items": (), "dossiers": (), "memory": {}}
    empty = project_outcomes(
        run={"id": "run-1", "status": "running"}, pending_questions=(), **common,
    )
    waiting = project_outcomes(
        run={"id": "run-1", "status": "running"},
        pending_questions=[{"id": "question-1", "prompt": "private"}], **common,
    )
    ledger.admit_notification_projection("run-1", empty)
    [notification_id] = ledger.admit_notification_projection("run-1", waiting)
    supervisor = NotificationDeliverySupervisor(ledger.conn, clock=clock)
    supervisor.schedule(
        notification_id, _preferences(mode="batched"), project_id="p1", campaign_id="c1",
        channel_refs=["desktop-local"],
    )

    assert ledger.admit_notification_projection("run-1", empty) == []
    assert NotificationOutbox(ledger.conn, clock=clock).get(notification_id)["status"] \
        == "superseded"
    [delivery] = supervisor.get_deliveries(notification_id)
    assert delivery["status"] == "superseded"
    assert delivery["supersededAt"] is not None
    clock.advance(minutes=30)
    assert supervisor.claim_due("sender") == []
    ledger.close()


def test_corrupt_durable_schedule_fails_closed_before_a_lease_is_issued(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc))
    ledger = FactoryLedger(tmp_path / "run.db")
    notification_id = _admit(ledger, clock)
    supervisor = NotificationDeliverySupervisor(ledger.conn, clock=clock)
    supervisor.schedule(
        notification_id, _preferences(), project_id="p1", campaign_id="c1",
        channel_refs=["desktop-local"],
    )
    ledger.conn.execute(
        "update factory_notification_schedules set schedule_json='{}' where notification_id=?",
        (notification_id,),
    )
    ledger.conn.commit()
    with pytest.raises(ValueError, match="integrity"):
        supervisor.claim_due("sender")
    assert supervisor.get_deliveries(notification_id)[0]["attemptCount"] == 0
    ledger.close()


def test_webhook_configuration_is_resolved_only_at_send_time(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc))
    ledger = FactoryLedger(tmp_path / "run.db")
    notification_id = _admit(ledger, clock)
    supervisor = NotificationDeliverySupervisor(ledger.conn, clock=clock)
    supervisor.schedule(
        notification_id, _preferences(), project_id="p1", campaign_id="c1",
        channel_refs=["webhook-primary"],
    )
    database_text = " ".join(
        str(item) for row in ledger.conn.execute(
            "select * from factory_notification_deliveries",
        ).fetchall() for item in row
    )
    assert "https://" not in database_text
    assert "credential:" not in database_text
    channel = WebhookChannel(
        "webhook-primary", "https://hooks.example.test/factory", "credential:primary",
    )

    class Resolver:
        def resolve(self, ref):
            assert ref == "credential:primary"
            return "secret-token"

    class Transport:
        def __init__(self):
            self.keys = []

        def send(self, request, *, bearer_token):
            assert bearer_token == "secret-token"
            self.keys.append(request.headers["Idempotency-Key"])

    transport = Transport()
    assert supervisor.run_once(
        "sender", channels={"webhook-primary": channel}, webhook_transport=transport,
        credential_resolver=Resolver(),
    )[0]["sent"] is True
    assert len(transport.keys) == 1
    ledger.close()


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.4", "169.254.169.254", "::1"])
def test_concrete_https_transport_rejects_nonpublic_destinations_without_connecting(address):
    def resolver(*args, **kwargs):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, 443))]

    request = WebhookRequest(
        channel_id="webhook-primary", url="https://example.test/hook", method="POST",
        headers={
            "Content-Type": "application/json", "Idempotency-Key": "sha256:" + "a" * 64,
            "User-Agent": "rekit-factory-notifications/1",
        }, body=b"{}",
    )
    with pytest.raises(RuntimeError, match="address-rejected"):
        SafeHttpsWebhookTransport(resolver=resolver).send(request, bearer_token="secret")
