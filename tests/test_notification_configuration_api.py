from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rekit_factory.api import FactoryServer
from rekit_factory.notification_configuration import NotificationConfigurationStore
from rekit_factory.notification_delivery import DesktopChannel
from rekit_factory.notification_outbox import NotificationOutbox
from rekit_factory.notification_policy import notification_candidates
from rekit_factory.outcomes import project_outcomes
from rekit_factory.store import FactoryLedger


class _Controller:
    def __init__(self, storage_root):
        self.storage_root = storage_root


class _DesktopTransport:
    def __init__(self):
        self.calls = []

    def notify(self, **kwargs):
        self.calls.append(kwargs)


def _request(base, path, payload=None, expected=200):
    request = Request(
        base + path, data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as exc:
        assert exc.code == expected
        return json.loads(exc.read())
    with response:
        assert response.status == expected
        return json.loads(response.read())


def _candidate():
    common = {"workers": (), "work_items": (), "memory": {}, "dossiers": ()}
    old = project_outcomes(
        run={"id": "run-1", "status": "running"}, pending_questions=(), **common,
    )
    new = project_outcomes(
        run={"id": "run-1", "status": "running"},
        pending_questions=[{"id": "question-1", "prompt": "TOKEN=never-render"}], **common,
    )
    return notification_candidates(old, new)[0]


def test_configuration_preview_and_fixed_test_commands_are_redacted_and_exact(tmp_path):
    storage = tmp_path / "storage"
    run_dir = storage / "projects" / "project-1" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"runId": "run-1"}))
    with FactoryLedger(run_dir / "run.db") as ledger:
        notification_id = NotificationOutbox(ledger.conn).admit([_candidate()])[0]
    configuration = NotificationConfigurationStore(
        storage / ".factory" / "test-notifications.sqlite3",
        channels={
            "desktop-main": DesktopChannel("desktop-main"),
            "desktop-unselected": DesktopChannel("desktop-unselected"),
        },
    )
    transport = _DesktopTransport()
    server = FactoryServer(
        ("127.0.0.1", 0), _Controller(storage),
        notification_configuration=configuration,
        notification_desktop_transport=transport,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        projected = _request(base, "/api/notification-configuration")["configuration"]
        assert projected["channels"] == [
            {"ref": "desktop-main", "kind": "desktop"},
            {"ref": "desktop-unselected", "kind": "desktop"},
        ]
        assert projected["channelRefs"] == ["desktop-main"]
        preview = _request(
            base, f"/api/runs/run-1/notifications/{notification_id}/preview"
        )["preview"]
        assert preview["message"] == "Operator decision is waiting in Mission Control."
        assert "never-render" not in json.dumps(preview)

        hostile = _request(base, "/api/notification-configuration", {
            "expectedRevision": projected["revision"],
            "preferencePresetId": "muted", "channelRefs": ["desktop-main"],
            "endpoint": "https://private.example/TOKEN=do-not-echo",
        }, 400)
        assert "private.example" not in json.dumps(hostile)
        assert "do-not-echo" not in json.dumps(hostile)

        result = _request(
            base, "/api/notification-configuration/channels/desktop-main/test",
            {"expectedRevision": projected["revision"], "testId": "operation-1"},
        )
        assert result["sent"] is True
        assert result["preview"]["message"] == (
            "Test notification from Mission Control. No investigation content is included."
        )
        assert transport.calls == [{
            "title": "Rekit Factory test",
            "message": "Test notification from Mission Control. No investigation content is included.",
            "deep_link": "rekit-factory://mission-control",
            "idempotency_key": result["preview"]["idempotencyKey"],
        }]

        # A server-declared channel can be tested before enabling it. The browser still sends
        # only its opaque ref, the current configuration revision, and a bounded test identity.
        unselected = _request(
            base, "/api/notification-configuration/channels/desktop-unselected/test",
            {"expectedRevision": projected["revision"], "testId": "operation-2"},
        )
        assert unselected["sent"] is True
        assert unselected["channelRef"] == "desktop-unselected"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
