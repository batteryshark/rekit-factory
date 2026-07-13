from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from rekit_factory.api import NotificationDeliveryWorker
from rekit_factory.control import InvestigationController, RunRequest
from rekit_factory.notification_configuration import NotificationConfigurationStore
from rekit_factory.notification_delivery import DesktopChannel
from rekit_factory.notification_supervisor import NotificationDeliverySupervisor
from rekit_factory.store import FactoryLedger
from tests.test_control_plane import FakeBackend, FakeRekit, authorized_dynamic_scope
from tests.test_campaign_notification_admission import _finish, _setup


class _DesktopTransport:
    def __init__(self):
        self.calls = []

    def notify(self, **kwargs):
        self.calls.append(kwargs)


def _target(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    (target / "main.py").write_text("def run(value):\n    return value + 1\n", encoding="utf-8")
    return target


def test_new_run_notification_uses_selected_external_configuration_once(tmp_path):
    target = _target(tmp_path)
    configuration = NotificationConfigurationStore(
        tmp_path / "configuration.sqlite3",
        channels={"desktop-selected": DesktopChannel("desktop-selected")},
    )
    controller = InvestigationController(
        storage_root=tmp_path / "runs", rekit=FakeRekit(risky=True), workers=FakeBackend(),
        notification_configuration=configuration,
    )
    run_dir = controller.create(RunRequest(
        target=target, goal="Reach one exact permission transition",
        tools=("exec-observe",), worker_roles=("analyst",),
        scope=authorized_dynamic_scope(target),
    ))
    controller.snapshot(run_dir)
    waiting = asyncio.run(controller.drive(run_dir))
    assert waiting["run"]["status"] == "needs_input"
    controller.snapshot(run_dir)

    with FactoryLedger(Path(run_dir) / "run.db") as ledger:
        schedules = ledger.conn.execute(
            "select notification_id from factory_notification_schedules"
        ).fetchall()
        deliveries = ledger.conn.execute(
            "select notification_id,channel_ref from factory_notification_deliveries"
        ).fetchall()
    assert len(schedules) == 1
    assert [tuple(row) for row in deliveries] == [
        (schedules[0]["notification_id"], "desktop-selected"),
    ]


def test_schedule_failure_is_downstream_of_committed_run_progress(tmp_path):
    target = _target(tmp_path)
    controller = InvestigationController(
        storage_root=tmp_path / "runs", rekit=FakeRekit(risky=True), workers=FakeBackend(),
    )
    run_dir = controller.create(RunRequest(
        target=target, goal="Commit progress despite schedule storage failure",
        tools=("exec-observe",), worker_roles=("analyst",),
        scope=authorized_dynamic_scope(target),
    ))
    controller.snapshot(run_dir)
    with patch.object(
        NotificationDeliverySupervisor, "schedule",
        side_effect=sqlite3.OperationalError("schedule table unavailable"),
    ):
        waiting = asyncio.run(controller.drive(run_dir))
    assert waiting["run"]["status"] == "needs_input"
    assert len(waiting["pendingQuestions"]) == 1
    with FactoryLedger(Path(run_dir) / "run.db") as ledger:
        assert ledger.conn.execute(
            "select count(*) from factory_notification_outbox"
        ).fetchone()[0] == 1
        assert ledger.conn.execute(
            "select count(*) from factory_notification_schedules"
        ).fetchone()[0] == 0
    # The baseline already advanced, so restart/hydration must reconcile the intact
    # pending outbox record rather than relying on the transition to be admitted again.
    controller.snapshot(run_dir)
    with FactoryLedger(Path(run_dir) / "run.db") as ledger:
        assert ledger.conn.execute(
            "select count(*) from factory_notification_schedules"
        ).fetchone()[0] == 1


def test_live_worker_delivers_selected_run_channel_and_is_stoppable(tmp_path):
    target = _target(tmp_path)
    configuration = NotificationConfigurationStore(
        tmp_path / "configuration.sqlite3",
        channels={"desktop-selected": DesktopChannel("desktop-selected")},
    )
    controller = InvestigationController(
        storage_root=tmp_path / "runs", rekit=FakeRekit(risky=True), workers=FakeBackend(),
        notification_configuration=configuration,
    )
    run_dir = controller.create(RunRequest(
        target=target, goal="Deliver one selected notification",
        tools=("exec-observe",), worker_roles=("analyst",),
        scope=authorized_dynamic_scope(target),
    ))
    controller.snapshot(run_dir)
    asyncio.run(controller.drive(run_dir))
    transport = _DesktopTransport()
    server = SimpleNamespace(
        storage_root=controller.storage_root, campaign_controller=None,
        notification_configuration=configuration,
        notification_desktop_transport=transport,
        notification_webhook_transport=None, notification_credential_resolver=None,
        instance_id="test-instance",
    )
    broken = controller.storage_root / "projects" / "broken" / "runs" / "broken"
    broken.mkdir(parents=True)
    (broken / "run.json").write_text(
        json.dumps({"creationComplete": True}), encoding="utf-8",
    )
    sqlite3.connect(broken / "run.db").close()
    worker = NotificationDeliveryWorker(server, autostart=False)
    results = worker.run_cycle()
    assert len(results) == len(transport.calls) == 1
    assert results[0]["sent"] is True
    assert transport.calls[0]["idempotency_key"].startswith("sha256:")
    assert transport.calls[0]["message"] == (
        "Operator decision is waiting in Mission Control."
    )
    # Re-observing the same committed decision and restarting the sender are both
    # silent.  This proves the provider-visible boundary, rather than merely the
    # outbox or schedule counts, remains exactly once across reconnect/restart.
    controller.snapshot(run_dir)
    restarted = NotificationDeliveryWorker(server, autostart=False)
    assert restarted.run_cycle() == []
    restarted.close()
    assert len(transport.calls) == 1
    with FactoryLedger(Path(run_dir) / "run.db") as ledger:
        assert ledger.conn.execute(
            "select status from factory_notification_deliveries"
        ).fetchone()[0] == "sent"
    worker.close()
    assert not worker.thread.is_alive()


def test_live_worker_scans_campaign_db_and_skips_disabled_desktop(tmp_path):
    campaign, persistence, contract = _setup(tmp_path / "campaigns.db")
    _finish(persistence, contract)
    campaign.public_state(contract.campaign_id)
    configuration = campaign.notification_configuration
    disabled_server = SimpleNamespace(
        storage_root=tmp_path / "runs", campaign_controller=campaign,
        notification_configuration=configuration, notification_desktop_transport=None,
        notification_webhook_transport=None, notification_credential_resolver=None,
        instance_id="disabled-instance",
    )
    disabled = NotificationDeliveryWorker(disabled_server, autostart=False)
    assert disabled.run_cycle() == []
    assert persistence.conn.execute(
        "select status from factory_notification_deliveries"
    ).fetchone()[0] == "queued"

    transport = _DesktopTransport()
    disabled_server.notification_desktop_transport = transport
    assert NotificationDeliveryWorker(disabled_server, autostart=False).run_cycle()[0]["sent"]
    assert len(transport.calls) == 1
    running = NotificationDeliveryWorker(
        disabled_server, poll_seconds=0.05, autostart=True,
    )
    running.close()
    assert not running.thread.is_alive()


def test_live_worker_rotates_its_bounded_ledger_window(tmp_path):
    server = SimpleNamespace()
    worker = NotificationDeliveryWorker(
        server, ledger_limit=1, autostart=False,
    )
    paths = [tmp_path / name for name in ("a.db", "b.db", "c.db")]
    assert worker._fair_batch(paths) == [paths[0]]
    assert worker._fair_batch(paths) == [paths[1]]
    assert worker._fair_batch(paths) == [paths[2]]
