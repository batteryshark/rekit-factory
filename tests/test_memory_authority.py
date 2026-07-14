from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from rekit_factory.memory import (
    MemoryAction, MemoryOperationConflict, ProjectMemoryLog,
)
from rekit_factory.memory_authority import (
    apply_memory_operation, entity_sha256, public_memory_authority,
)


def _finding(identifier: str, status: str) -> dict:
    return {
        "id": identifier, "hypothesisId": "hypothesis-1", "scope": "bounded",
        "observations": ["observed"], "affectedComponent": "parser",
        "impactClaim": "bounded impact", "assumptions": ["fixture"],
        "knownUncertainty": "none", "findingType": "defect",
        "consequence": "medium", "confidence": .8,
        "proofPolicy": {"schema_version": 1}, "recipe": {"id": "recipe-1"},
        "status": status, "originWorkerId": "worker-1",
        "originSessionId": "session-1", "originModelProfile": "fixture",
        "references": [{"kind": "artifact", "id": "artifact-1"}],
    }


def _seed(tmp_path, *, finding_status: str = "reproduced") -> ProjectMemoryLog:
    log = ProjectMemoryLog(tmp_path / "project")
    log.append(MemoryAction("workstream_upserted", {
        "id": "workstream-1", "title": "Bad branch", "status": "active",
        "goal": "test one branch", "references": [],
    }))
    log.append(MemoryAction("finding_upserted", _finding("finding-1", finding_status)))
    return log


def _request(log: ProjectMemoryLog, action: str, entity_id: str, rationale: str):
    memory = log.replay()
    entity = (memory.workstreams if action == "workstream-stop" else memory.findings)[entity_id]
    return dict(action=action, entity_id=entity_id, expected_revision=memory.last_seq,
                expected_entity_sha256=entity_sha256(entity),
                expected_project_id=log.project_dir.name, rationale=rationale)


def test_workstream_stop_is_one_fsynced_revision_and_exact_restart_replay(tmp_path):
    log = _seed(tmp_path)
    request = _request(log, "workstream-stop", "workstream-1", "No useful novelty")
    first = apply_memory_operation(log, **request)
    restarted = ProjectMemoryLog(log.project_dir)
    replay = apply_memory_operation(restarted, **request)
    assert replay == first
    assert first["revision"] == request["expected_revision"] + 1
    memory = restarted.replay()
    assert memory.last_seq == first["revision"]
    assert memory.workstreams["workstream-1"]["status"] == "rejected"
    assert memory.workstreams["workstream-1"]["stopReason"] == "No useful novelty"
    events = log.path.read_text().splitlines()
    assert len(events) == 3, "the complete mutation is one append-only event"


def test_finding_rejection_decision_transition_and_state_are_one_revision(tmp_path):
    log = _seed(tmp_path)
    request = _request(log, "finding-reject", "finding-1", "Contradictory evidence")
    result = apply_memory_operation(log, **request)
    memory = log.replay()
    decision = next(iter(memory.finding_operator_decisions.values()))
    transition = next(iter(memory.finding_transitions.values()))
    finding = memory.findings["finding-1"]
    assert {decision["_eventSeq"], transition["_eventSeq"], finding["_eventSeq"]} \
        == {result["revision"]}
    assert decision["decision"] == "rejected"
    assert transition["toStatus"] == finding["status"] == "rejected"
    assert not any(item["entityId"] == "finding-1"
                   for item in result["authority"]["operations"])


def test_only_technically_reproduced_finding_publishes_acceptance(tmp_path):
    candidate = _seed(tmp_path / "candidate", finding_status="candidate")
    published = public_memory_authority(candidate.replay(), candidate.project_dir.name)["operations"]
    assert not any(item["action"] == "finding-accept" for item in published)
    with pytest.raises(ValueError, match="technically reproduced"):
        apply_memory_operation(
            candidate, **_request(candidate, "finding-accept", "finding-1", "Accept"),
        )
    reproduced = _seed(tmp_path / "reproduced")
    request = _request(reproduced, "finding-accept", "finding-1", "Proof reviewed")
    result = apply_memory_operation(reproduced, **request)
    assert next(iter(reproduced.replay().finding_operator_decisions.values()))["decision"] \
        == "accepted"
    assert not any(item["entityId"] == "finding-1"
                   for item in result["authority"]["operations"])


def test_concurrent_exact_replays_converge_and_changed_requests_fail_stale(tmp_path):
    log = _seed(tmp_path)
    request = _request(log, "workstream-stop", "workstream-1", "Stop exact branch")
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _index: apply_memory_operation(log, **request), range(24)))
    assert len({item["operationId"] for item in results}) == 1
    assert len({item["eventId"] for item in results}) == 1
    changed = {**request, "rationale": "Different decision"}
    with pytest.raises(MemoryOperationConflict, match="revision is stale"):
        apply_memory_operation(log, **changed)


def test_stale_entity_revision_and_degraded_memory_fail_closed(tmp_path):
    log = _seed(tmp_path)
    request = _request(log, "finding-reject", "finding-1", "Reject")
    log.append(MemoryAction("decision_recorded", {
        "id": "decision-race", "choice": "other", "rationale": "race",
        "alternatives": [], "references": [],
    }))
    with pytest.raises(MemoryOperationConflict, match="revision is stale"):
        apply_memory_operation(log, **request)
    authority = public_memory_authority(log.replay(), log.project_dir.name)
    stale = {**request, "expected_revision": authority["revision"],
             "expected_entity_sha256": "0" * 64}
    with pytest.raises(ValueError, match="content is stale"):
        apply_memory_operation(log, **stale)
    with log.path.open("ab") as stream:
        stream.write(b"not-json\n")
    degraded = public_memory_authority(log.replay(), log.project_dir.name)
    assert degraded["degraded"] is True and degraded["operations"] == []
    current = {**stale, "expected_revision": degraded["revision"]}
    with pytest.raises(MemoryOperationConflict, match="degraded"):
        apply_memory_operation(log, **current)


def test_malformed_atomic_event_degrades_without_applying_partial_actions(tmp_path):
    log = _seed(tmp_path)
    malformed = {"version": 1, "seq": 3, "id": "mem-00000003",
                 "action_id": "memory-operation-" + "a" * 64,
                 "type": "operator_mutation_applied", "ts": "2026-01-01T00:00:00Z",
                 "payload": {"operationId": "memory-operation-" + "a" * 64,
                             "expectedRevision": 2, "request": {},
                             "actions": [{"type": "workstream_upserted",
                                          "payload": {"id": "workstream-1"}}]}}
    with log.path.open("ab") as stream:
        stream.write(json.dumps(malformed).encode() + b"\n")
    memory = log.replay()
    assert memory.degraded is True
    assert memory.workstreams["workstream-1"]["status"] == "active"
    assert any("invalid operator mutation" in item for item in memory.diagnostics)
