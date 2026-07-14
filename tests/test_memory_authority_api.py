from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rekit_factory.api import FactoryServer
from rekit_factory.memory import ProjectMemoryLog
from rekit_factory.memory_authority import apply_memory_operation


class _Controller:
    def __init__(self, storage_root, log):
        self.storage_root = storage_root
        self.log = log

    def mutate_project_memory(self, _run_dir, **values):
        return apply_memory_operation(self.log, **values)


def _request(base, payload, expected=200):
    request = Request(
        base + "/api/runs/run-1/memory-operations",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as exc:
        assert exc.code == expected
        return json.loads(exc.read())
    with response:
        assert response.status == expected
        return json.loads(response.read())


def test_memory_operation_api_is_strict_and_exact_replay_safe(tmp_path):
    storage = tmp_path / "storage"
    run_dir = storage / "projects" / "project-1" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "runId": "run-1", "creationComplete": True,
    }))
    log = ProjectMemoryLog(run_dir.parents[1])
    from rekit_factory.memory import MemoryAction
    from rekit_factory.memory_authority import entity_sha256
    log.append(MemoryAction("workstream_upserted", {
        "id": "workstream-1", "title": "Branch", "status": "active",
        "goal": "bounded", "references": [],
    }))
    current = log.replay().workstreams["workstream-1"]
    payload = {"action": "workstream-stop", "entityId": "workstream-1",
               "expectedProjectId": "project-1",
               "expectedRevision": 1, "expectedEntitySha256": entity_sha256(current),
               "rationale": "No remaining information gain"}
    server = FactoryServer(("127.0.0.1", 0), _Controller(storage, log))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        first = _request(base, payload)
        replay = _request(base, payload)
        assert first == replay
        assert first["revision"] == 2
        assert log.replay().workstreams["workstream-1"]["status"] == "rejected"
        assert _request(base, {**payload, "approved": True}, 400)["error"].startswith(
            "ValueError: project-memory operation body"
        )
        stale = {**payload, "rationale": "Changed content"}
        assert "revision is stale" in _request(base, stale, 409)["error"]
        other_project = {**payload, "expectedProjectId": "project-2"}
        assert "does not match" in _request(base, other_project, 400)["error"]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
