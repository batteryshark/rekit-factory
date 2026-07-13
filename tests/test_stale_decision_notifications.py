from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from rekit_factory.notification_outbox import NotificationOutbox
from rekit_factory.notification_configuration import NotificationConfigurationStore
from rekit_factory.notification_delivery import (
    InvalidDeliveryConfiguration,
    WebhookChannel,
    build_webhook_request,
    delivery_preview,
)
from rekit_factory.notification_preferences import NotificationPreferences
from rekit_factory.notification_policy import stale_operator_decision_candidate
from rekit_factory.notification_supervisor import NotificationDeliverySupervisor
from rekit_factory.store import FactoryLedger
from rekit_factory.cli import parser


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _ledger(path, clock: FakeClock) -> FactoryLedger:
    ledger = FactoryLedger(path)
    ledger.ask_question(
        "run-1", qid="question-1", node="permission", kind="permission",
        prompt="private question prose", options=["yes", "no"],
    )
    ledger.conn.execute(
        "update questions set created_at=? where id='question-1'",
        (clock().isoformat().replace("+00:00", "Z"),),
    )
    ledger.conn.commit()
    return ledger


def test_fake_clock_threshold_replay_and_restart_have_one_stable_identity(tmp_path):
    path = tmp_path / "run.db"
    clock = FakeClock()
    first = _ledger(path, clock)

    assert first.reconcile_stale_operator_decisions(
        "run-1", threshold_seconds=60, clock=clock,
    ) == []
    clock.advance(59)
    assert first.reconcile_stale_operator_decisions(
        "run-1", threshold_seconds=60, clock=clock,
    ) == []
    clock.advance(1)
    ids = first.reconcile_stale_operator_decisions(
        "run-1", threshold_seconds=60, clock=clock,
    )
    assert len(ids) == 1
    record = NotificationOutbox(first.conn, clock=clock).get(ids[0])
    assert record["payload"]["kind"] == "operator-decision.stale"
    assert record["payload"]["thresholdSeconds"] == 60
    assert record["payload"]["deepLink"]["entityId"] == "question-1"
    assert "private question prose" not in str(record)
    assert first.reconcile_stale_operator_decisions(
        "run-1", threshold_seconds=60, clock=clock,
    ) == ids
    first.close()

    restarted = FactoryLedger(path)
    clock.advance(86_400)
    assert restarted.reconcile_stale_operator_decisions(
        "run-1", threshold_seconds=60, clock=clock,
    ) == ids
    assert restarted.conn.execute(
        "select count(*) from factory_notification_outbox"
    ).fetchone()[0] == 1
    restarted.close()


def test_resolution_and_configuration_change_supersede_pending_work(tmp_path):
    clock = FakeClock()
    ledger = _ledger(tmp_path / "run.db", clock)
    clock.advance(10)
    first_id = ledger.reconcile_stale_operator_decisions(
        "run-1", threshold_seconds=10, clock=clock,
    )[0]

    # A longer explicit policy revision means the prior invitation is no longer actionable.
    assert ledger.reconcile_stale_operator_decisions(
        "run-1", threshold_seconds=20, clock=clock,
    ) == []
    assert NotificationOutbox(ledger.conn, clock=clock).get(first_id)["status"] == "superseded"
    clock.advance(10)
    second_id = ledger.reconcile_stale_operator_decisions(
        "run-1", threshold_seconds=20, clock=clock,
    )[0]
    assert second_id != first_id

    ledger.record_answer("run-1", "question-1", "yes")
    assert ledger.reconcile_stale_operator_decisions(
        "run-1", threshold_seconds=20, clock=clock,
    ) == []
    assert NotificationOutbox(ledger.conn, clock=clock).get(second_id)["status"] == "superseded"
    ledger.close()


def test_fault_rollback_and_invalid_clock_never_guess_elapsed_time(tmp_path):
    clock = FakeClock()
    ledger = _ledger(tmp_path / "run.db", clock)
    clock.advance(30)

    def fail(boundary: str) -> None:
        if boundary == "stale-decisions-admitted":
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError, match="crash"):
        ledger.reconcile_stale_operator_decisions(
            "run-1", threshold_seconds=30, clock=clock, failure_injector=fail,
        )
    assert ledger.conn.execute(
        "select count(*) from factory_notification_outbox"
    ).fetchone()[0] == 0

    with pytest.raises(ValueError, match="timezone-aware"):
        ledger.reconcile_stale_operator_decisions(
            "run-1", threshold_seconds=30,
            clock=lambda: datetime(2026, 7, 14, 2, 0),
        )
    with pytest.raises(ValueError, match="threshold"):
        ledger.reconcile_stale_operator_decisions(
            "run-1", threshold_seconds=0, clock=clock,
        )
    ledger.close()


def test_explicit_configuration_and_scheduler_use_the_stable_stale_record(tmp_path):
    clock = FakeClock()
    configuration = NotificationConfigurationStore(
        tmp_path / "configuration.db", stale_operator_decision_after_seconds=15,
    )
    assert configuration.stale_operator_decision_after_seconds == 15
    with pytest.raises(ValueError, match="threshold"):
        NotificationConfigurationStore(
            tmp_path / "invalid.db", stale_operator_decision_after_seconds=0,
        )

    ledger = _ledger(tmp_path / "run.db", clock)
    clock.advance(15)
    notification_id = ledger.reconcile_stale_operator_decisions(
        "run-1",
        threshold_seconds=configuration.stale_operator_decision_after_seconds,
        clock=clock,
    )[0]
    preference = NotificationPreferences.from_dict({
        "schemaVersion": 1, "revision": "stale-test-v1",
        "default": {"mode": "immediate"}, "severity": {}, "projects": {},
        "campaigns": {}, "quietHours": {"timezone": "UTC", "windows": []},
    })
    supervisor = NotificationDeliverySupervisor(ledger.conn, clock=clock)
    schedule = supervisor.schedule(
        notification_id, preference, project_id="project-1", campaign_id="no-campaign",
        channel_refs=("desktop-primary",),
    )
    assert schedule["notificationId"] == notification_id
    ledger.close()


def test_stale_delivery_preview_webhook_and_forgery_validation_are_exact():
    candidate = stale_operator_decision_candidate(
        run_id="run-1", question_id="question-1", threshold_seconds=900,
    )
    record = {
        "id": "notification-" + candidate["dedupeKey"].removeprefix("sha256:"),
        "payload": {
            "schemaVersion": 1, "policyVersion": candidate["policyVersion"],
            "policyRevision": candidate["policyRevision"],
            "thresholdSeconds": candidate["thresholdSeconds"],
            "dedupeKey": candidate["dedupeKey"], "kind": candidate["kind"],
            "severity": candidate["severity"], "message": candidate["message"],
            "deepLink": {
                "view": "mission-control", "runId": "run-1", "tab": "decisions",
                "entityType": "operator-decision", "entityId": "question-1",
            },
        },
    }
    preview = delivery_preview(record)
    assert preview["title"] == "Operator decision overdue"
    assert preview["message"] == candidate["message"]
    body = build_webhook_request(
        WebhookChannel("stale-hook", "https://example.test/stale", "credential:stale"),
        record,
    ).body.decode()
    assert candidate["message"] in body
    assert "thresholdSeconds" not in body

    for field, value in (
        ("policyRevision", "sha256:" + "0" * 64),
        ("dedupeKey", "sha256:" + "0" * 64),
        ("message", "private prompt"),
    ):
        forged = {**record, "payload": {**record["payload"], field: value}}
        with pytest.raises(InvalidDeliveryConfiguration):
            delivery_preview(forged)
    forged_link = {
        **record,
        "payload": {**record["payload"], "deepLink": {
            **record["payload"]["deepLink"], "tab": "findings",
        }},
    }
    with pytest.raises(InvalidDeliveryConfiguration):
        delivery_preview(forged_link)


def test_cli_stale_threshold_is_explicit_bounded_and_disabled_by_default():
    disabled = parser().parse_args(["serve"])
    assert disabled.stale_operator_decision_after_seconds is None
    configured = parser().parse_args([
        "serve", "--stale-operator-decision-after-seconds", "3600",
    ])
    assert configured.stale_operator_decision_after_seconds == 3600
    with pytest.raises(SystemExit):
        parser().parse_args([
            "serve", "--stale-operator-decision-after-seconds", "0",
        ])
