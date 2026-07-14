"""Exact, atomic operator authority over canonical project memory."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from rekit_factory.memory import MemoryAction, ProjectMemory, ProjectMemoryLog


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ACTIONS = frozenset({"workstream-stop", "finding-accept", "finding-reject"})
MAX_ELIGIBLE_ENTITIES = 64


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def entity_sha256(entity: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(entity)).hexdigest()


def _latest_finding_decisions(memory: ProjectMemory) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for decision in sorted(memory.finding_operator_decisions.values(),
                           key=lambda item: (item.get("_eventSeq", 0),
                                             item.get("_operationIndex", 0))):
        latest[decision["findingId"]] = decision
    return latest


def public_memory_authority(memory: ProjectMemory, project_id: str) -> dict[str, Any]:
    """Publish bounded eligible mutations; never infer authority in the browser."""
    if not isinstance(project_id, str) or _ID.fullmatch(project_id) is None:
        raise ValueError("project-memory project identity is invalid")
    if memory.degraded:
        return {"schemaVersion": 1, "projectId": project_id,
                "revision": memory.last_seq, "degraded": True,
                "operations": [], "totalCount": 0, "truncated": False}
    decisions = _latest_finding_decisions(memory)
    operations: list[dict[str, Any]] = []
    for identifier in sorted(memory.workstreams):
        item = memory.workstreams[identifier]
        if item.get("status") in {"candidate", "active", "paused"}:
            operations.append({"action": "workstream-stop", "entityType": "workstream",
                               "entityId": identifier,
                               "expectedEntitySha256": entity_sha256(item)})
    for identifier in sorted(memory.findings):
        item = memory.findings[identifier]
        latest = decisions.get(identifier)
        terminal_decision = latest and latest.get("decision") in {"accepted", "rejected"}
        if item.get("status") == "reproduced" and not terminal_decision:
            operations.append({"action": "finding-accept", "entityType": "finding",
                               "entityId": identifier,
                               "expectedEntitySha256": entity_sha256(item)})
        if item.get("status") not in {"rejected", "withdrawn"} and not terminal_decision:
            operations.append({"action": "finding-reject", "entityType": "finding",
                               "entityId": identifier,
                               "expectedEntitySha256": entity_sha256(item)})
    total = len(operations)
    return {"schemaVersion": 1, "projectId": project_id,
            "revision": memory.last_seq, "degraded": False,
            "operations": operations[:MAX_ELIGIBLE_ENTITIES], "totalCount": total,
            "truncated": total > MAX_ELIGIBLE_ENTITIES}


def apply_memory_operation(
    log: ProjectMemoryLog, *, action: str, entity_id: str, expected_revision: int,
    expected_entity_sha256: str, expected_project_id: str, rationale: str,
) -> dict[str, Any]:
    if action not in _ACTIONS:
        raise ValueError("unsupported project-memory operation")
    if not isinstance(entity_id, str) or _ID.fullmatch(entity_id) is None:
        raise ValueError("project-memory entity identity is invalid")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("expected memory revision must be a non-negative integer")
    if (not isinstance(expected_entity_sha256, str)
            or _DIGEST.fullmatch(expected_entity_sha256) is None):
        raise ValueError("expected entity digest is invalid")
    if (not isinstance(expected_project_id, str) or _ID.fullmatch(expected_project_id) is None
            or expected_project_id != log.project_dir.name):
        raise ValueError("project-memory project authority does not match the run")
    if (not isinstance(rationale, str) or rationale != rationale.strip()
            or not 1 <= len(rationale) <= 2_000):
        raise ValueError("operator rationale must be 1..2000 trimmed characters")
    request = {"action": action, "entityId": entity_id,
               "expectedProjectId": expected_project_id,
               "expectedRevision": expected_revision,
               "expectedEntitySha256": expected_entity_sha256,
               "rationale": rationale}
    operation_id = "memory-operation-" + hashlib.sha256(_canonical(request)).hexdigest()

    def build(memory: ProjectMemory) -> list[MemoryAction]:
        if action == "workstream-stop":
            current = memory.workstreams.get(entity_id)
            if current is None:
                raise KeyError(entity_id)
            if entity_sha256(current) != expected_entity_sha256:
                raise ValueError("workstream content is stale")
            if current.get("status") not in {"candidate", "active", "paused"}:
                raise ValueError("workstream is not eligible for operator stop")
            payload = {key: value for key, value in current.items()
                       if not key.startswith("_")}
            payload.update({"status": "rejected", "stopReason": rationale,
                            "stoppedBy": "operator", "stopOperationId": operation_id})
            return [MemoryAction("workstream_upserted", payload)]

        current = memory.findings.get(entity_id)
        if current is None:
            raise KeyError(entity_id)
        if entity_sha256(current) != expected_entity_sha256:
            raise ValueError("finding content is stale")
        latest = _latest_finding_decisions(memory).get(entity_id)
        if latest and latest.get("decision") in {"accepted", "rejected"}:
            raise ValueError("finding already has a terminal operator decision")
        decision = "accepted" if action == "finding-accept" else "rejected"
        if decision == "accepted" and current.get("status") != "reproduced":
            raise ValueError("operator acceptance requires a technically reproduced finding")
        if decision == "rejected" and current.get("status") in {"rejected", "withdrawn"}:
            raise ValueError("finding is not eligible for operator rejection")
        references = [{"kind": "memory-event", "id": current["_eventId"]}]
        decision_id = "finding-decision-" + hashlib.sha256(_canonical({
            "operationId": operation_id, "findingId": entity_id,
            "decision": decision, "rationale": rationale,
        })).hexdigest()[:20]
        actions = [MemoryAction("finding_operator_decision_recorded", {
            "id": decision_id, "findingId": entity_id, "decision": decision,
            "rationale": rationale, "unmetCriteria": [], "references": references,
        })]
        if decision == "rejected":
            transition_id = "finding-transition-" + hashlib.sha256(_canonical({
                "operationId": operation_id, "findingId": entity_id,
                "fromStatus": current["status"], "toStatus": "rejected",
            })).hexdigest()[:20]
            actions.append(MemoryAction("finding_transition_recorded", {
                "id": transition_id, "findingId": entity_id,
                "fromStatus": current["status"], "toStatus": "rejected",
                "reason": rationale, "previousConfidence": current["confidence"],
                "nextConfidence": current["confidence"],
                "previousConsequence": current["consequence"],
                "nextConsequence": current["consequence"], "references": references,
            }))
            updated = {key: value for key, value in current.items()
                       if not key.startswith("_")}
            updated.update({"status": "rejected", "lastTransitionId": transition_id})
            actions.append(MemoryAction("finding_upserted", updated))
        return actions

    event = log.apply_operation(
        operation_id=operation_id, expected_revision=expected_revision,
        request=request, build_actions=build,
    )
    memory = log.replay()
    return {"operationId": operation_id, "eventId": event.id,
            "revision": event.seq,
            "authority": public_memory_authority(memory, expected_project_id)}
