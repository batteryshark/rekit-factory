from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rekit_factory.api import FactoryServer
from rekit_factory.notification_outbox import NotificationOutbox
from rekit_factory.notification_policy import notification_candidates
from rekit_factory.outcomes import project_outcomes
from rekit_factory.store import FactoryLedger


class _Controller:
    def __init__(self, storage_root):
        self.storage_root = storage_root


def _candidate():
    common = {"workers": (), "work_items": (), "memory": {}, "dossiers": ()}
    old = project_outcomes(
        run={"id": "run-1", "status": "running"}, pending_questions=(), **common,
    )
    new = project_outcomes(
        run={"id": "run-1", "status": "running"},
        pending_questions=[{
            "id": "question-1",
            "prompt": "/Users/private/target TOKEN=never-render-this",
        }],
        **common,
    )
    return notification_candidates(old, new)[0]


def _request(base, path, payload=None, expected=200):
    request = Request(
        base + path,
        data=None if payload is None else json.dumps(payload).encode(),
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


def _setup(tmp_path):
    storage = tmp_path / "storage"
    run_dir = storage / "projects" / "project-1" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "runId": "run-1", "creationComplete": True,
    }))
    with FactoryLedger(run_dir / "run.db") as ledger:
        box = NotificationOutbox(ledger.conn)
        notification_id = box.admit([_candidate()])[0]
        claim = box.claim_due("test-sender")[0]
        box.record_sent(notification_id, claim["leaseToken"])
    server = FactoryServer(("127.0.0.1", 0), _Controller(storage))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return run_dir, notification_id, server, thread


def test_notification_projection_is_bounded_redacted_and_exactly_linked(tmp_path):
    _, notification_id, server, thread = _setup(tmp_path)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        result = _request(base, "/api/runs/run-1/notifications")
        assert result["schemaVersion"] == 1
        assert result["runId"] == "run-1"
        assert len(result["notifications"]) == 1
        notification = result["notifications"][0]
        assert notification["id"] == notification_id
        assert notification["status"] == "sent"
        assert notification["revision"].startswith("sha256:")
        assert notification["payload"]["deepLink"] == {
            "view": "mission-control", "runId": "run-1", "tab": "decisions",
            "entityType": "operator-decision", "entityId": "question-1",
        }
        encoded = json.dumps(result)
        assert "/Users/private" not in encoded
        assert "never-render-this" not in encoded
        assert "lease" not in encoded.lower()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_acknowledgement_is_revision_bound_exact_and_replay_safe(tmp_path):
    _, notification_id, server, thread = _setup(tmp_path)
    base = f"http://127.0.0.1:{server.server_port}"
    path = f"/api/runs/run-1/notifications/{notification_id}/acknowledge"
    try:
        current = _request(base, "/api/runs/run-1/notifications")["notifications"][0]
        stale = _request(base, path, {"expectedRevision": "sha256:" + "0" * 64}, 409)
        assert stale == {"error": "notification revision is stale"}

        hostile = _request(base, path, {
            "expectedRevision": current["revision"],
            "message": "TOKEN=must-not-cross-boundary",
        }, 400)
        assert "must-not-cross-boundary" not in json.dumps(hostile)

        first = _request(base, path, {"expectedRevision": current["revision"]})
        replay = _request(base, path, {"expectedRevision": current["revision"]})
        assert first == replay
        assert first["notification"]["status"] == "acknowledged"
        assert first["notification"]["acknowledgedAt"] is not None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
