"""Versioned, append-only project reasoning memory.

Memory points at operational evidence; it does not duplicate runs, artifacts, tool output,
or human questions.  The JSONL stream is the sole writable source.  Snapshots, bounded
resume context, and Markdown are deterministic projections.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Literal

from muster import utcnow


SCHEMA_VERSION = 1
KNOWN_TYPES = frozenset({
    "goal_set", "workstream_upserted", "attempt_recorded", "decision_recorded",
    "research_question_upserted", "theory_upserted", "next_action_upserted",
    "session_compacted",
    "hypothesis_upserted", "hypothesis_test_upserted", "hypothesis_observation_recorded",
    "finding_upserted", "finding_attempt_recorded", "finding_transition_recorded",
    "finding_operator_decision_recorded",
    "operator_mutation_applied",
})
ENTITY_TYPES = KNOWN_TYPES - {"session_compacted", "operator_mutation_applied"}
REFERENCE_KINDS = frozenset({
    "memory-event", "ledger-event", "artifact", "run-event", "question",
    "capability-lead", "external",
})
MAX_EVENT_BYTES = 32_768


class MemoryOperationConflict(ValueError):
    """An exact operator mutation conflicts with durable project-memory state."""


@dataclass(frozen=True)
class EvidenceRef:
    kind: str
    id: str


@dataclass(frozen=True)
class MemoryAction:
    type: Literal[
        "goal_set", "workstream_upserted", "attempt_recorded", "decision_recorded",
        "research_question_upserted", "theory_upserted", "next_action_upserted",
        "session_compacted",
        "hypothesis_upserted", "hypothesis_test_upserted",
        "hypothesis_observation_recorded",
        "finding_upserted", "finding_attempt_recorded", "finding_transition_recorded",
        "finding_operator_decision_recorded",
        "operator_mutation_applied",
    ]
    payload: dict[str, Any]
    action_id: str | None = None


@dataclass(frozen=True)
class MemoryEvent:
    version: int
    seq: int
    id: str
    action_id: str
    type: str
    ts: str
    payload: dict[str, Any]


@dataclass
class ProjectMemory:
    version: int = SCHEMA_VERSION
    last_seq: int = 0
    goals: list[dict[str, Any]] = field(default_factory=list)
    workstreams: dict[str, dict[str, Any]] = field(default_factory=dict)
    attempts: dict[str, dict[str, Any]] = field(default_factory=dict)
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    questions: dict[str, dict[str, Any]] = field(default_factory=dict)
    theories: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    hypotheses: dict[str, dict[str, Any]] = field(default_factory=dict)
    hypothesis_tests: dict[str, dict[str, Any]] = field(default_factory=dict)
    hypothesis_observations: dict[str, dict[str, Any]] = field(default_factory=dict)
    findings: dict[str, dict[str, Any]] = field(default_factory=dict)
    finding_attempts: dict[str, dict[str, Any]] = field(default_factory=dict)
    finding_transitions: dict[str, dict[str, Any]] = field(default_factory=dict)
    finding_operator_decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    compaction: dict[str, Any] | None = None
    degraded: bool = False
    diagnostics: list[str] = field(default_factory=list)
    missing_references: list[dict[str, str]] = field(default_factory=list)

    def deterministic_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for name in ("workstreams", "attempts", "decisions", "questions",
                     "theories", "next_actions", "hypotheses", "hypothesis_tests",
                     "hypothesis_observations", "findings", "finding_attempts",
                     "finding_transitions", "finding_operator_decisions"):
            value[name] = {key: value[name][key] for key in sorted(value[name])}
        value["missing_references"] = sorted(
            value["missing_references"], key=lambda item: (item["kind"], item["id"])
        )
        return value

    def canonical_json(self) -> str:
        return json.dumps(self.deterministic_dict(), sort_keys=True, separators=(",", ":"))


class ProjectMemoryLog:
    def __init__(self, project_dir: str | Path):
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.path = self.project_dir / "memory.jsonl"
        self.lock_path = self.project_dir / ".memory.lock"

    def append(self, action: MemoryAction) -> MemoryEvent:
        payload = _validate_action(action)
        action_id = action.action_id or _stable_id("action", action.type, payload)
        self.project_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            events, _ = _read_events(self.path)
            return self._append_locked(events, action.type, payload, action_id)

    def apply_operation(
        self, *, operation_id: str, expected_revision: int, request: dict[str, Any],
        build_actions: Callable[[ProjectMemory], list[MemoryAction]],
    ) -> MemoryEvent:
        """Validate and append one atomic operator mutation at an exact project revision.

        The resulting domain actions live inside one fsynced memory event. Exact replay returns
        that event even after the project revision advances; conflicting operation reuse and
        partial multi-event effects are impossible.
        """
        if not isinstance(operation_id, str) or re.fullmatch(
                r"memory-operation-[0-9a-f]{64}", operation_id) is None:
            raise ValueError("memory operation identity is invalid")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected memory revision must be a non-negative integer")
        normalized_request = json.loads(json.dumps(request, sort_keys=True))
        self.project_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            events, diagnostics = _read_events(self.path)
            for event in events:
                if event.action_id != operation_id:
                    continue
                if (event.type != "operator_mutation_applied"
                        or event.payload.get("expectedRevision") != expected_revision
                        or event.payload.get("request") != normalized_request):
                    raise MemoryOperationConflict(
                        "memory operation identity already has different content"
                    )
                return event
            if diagnostics:
                raise MemoryOperationConflict(
                    "degraded project memory cannot accept operator mutations"
                )
            memory = fold_memory(events)
            if memory.degraded:
                raise MemoryOperationConflict(
                    "degraded project memory cannot accept operator mutations"
                )
            if memory.last_seq != expected_revision:
                raise MemoryOperationConflict("project memory revision is stale")
            actions = build_actions(memory)
            if not isinstance(actions, list) or not 1 <= len(actions) <= 4:
                raise ValueError("memory operation must produce one to four bounded actions")
            encoded_actions = []
            for action in actions:
                if not isinstance(action, MemoryAction) or action.type == "operator_mutation_applied":
                    raise ValueError("memory operation produced an invalid nested action")
                encoded_actions.append({"type": action.type, "payload": _validate_action(action)})
            payload = _validate_action(MemoryAction("operator_mutation_applied", {
                "operationId": operation_id, "expectedRevision": expected_revision,
                "request": normalized_request, "actions": encoded_actions,
            }))
            return self._append_locked(
                events, "operator_mutation_applied", payload, operation_id,
            )

    def _append_locked(self, events: list[MemoryEvent], event_type: str,
                       payload: dict[str, Any], action_id: str) -> MemoryEvent:
        for event in events:
            if event.action_id == action_id:
                if event.type != event_type or event.payload != payload:
                    raise ValueError(f"action id {action_id!r} already has different content")
                return event
        seq = events[-1].seq + 1 if events else 1
        event = MemoryEvent(
            version=SCHEMA_VERSION, seq=seq, id=f"mem-{seq:08d}",
            action_id=action_id, type=event_type, ts=utcnow(), payload=payload,
        )
        encoded = json.dumps(asdict(event), sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("memory event is too large; store bulky evidence as an artifact reference")
        with self.path.open("ab") as stream:
            stream.write(encoded.encode("utf-8") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        return event

    def replay(self, *, reference_exists: Callable[[EvidenceRef], bool] | None = None
               ) -> ProjectMemory:
        events, diagnostics = _read_events(self.path)
        return fold_memory(events, diagnostics=diagnostics, reference_exists=reference_exists)


def fold_memory(events: list[MemoryEvent], *, diagnostics: list[str] | None = None,
                reference_exists: Callable[[EvidenceRef], bool] | None = None
                ) -> ProjectMemory:
    memory = ProjectMemory(diagnostics=list(diagnostics or []))
    memory.degraded = bool(memory.diagnostics)
    expected = 1
    seen_actions: dict[str, MemoryEvent] = {}
    for event in events:
        if event.seq != expected:
            memory.degraded = True
            memory.diagnostics.append(
                f"sequence discontinuity: expected {expected}, found {event.seq}"
            )
            expected = event.seq
        if event.id != f"mem-{event.seq:08d}":
            memory.degraded = True
            memory.diagnostics.append(f"noncanonical event id at sequence {event.seq}")
        expected += 1
        memory.last_seq = max(memory.last_seq, event.seq)
        previous = seen_actions.get(event.action_id)
        if previous is not None:
            if previous.type != event.type or previous.payload != event.payload:
                memory.degraded = True
                memory.diagnostics.append(f"conflicting duplicate action {event.action_id}")
            continue
        seen_actions[event.action_id] = event
        if event.version > SCHEMA_VERSION or event.type not in KNOWN_TYPES:
            memory.degraded = True
            memory.diagnostics.append(
                f"unknown event skipped at sequence {event.seq}: v{event.version} {event.type}"
            )
            continue
        if event.type == "operator_mutation_applied":
            try:
                operation_payload = _validate_action(MemoryAction(event.type, event.payload))
            except (KeyError, TypeError, ValueError) as exc:
                memory.degraded = True
                memory.diagnostics.append(
                    f"invalid operator mutation at sequence {event.seq}: {type(exc).__name__}"
                )
                continue
            actions = operation_payload["actions"]
        else:
            actions = [{"type": event.type, "payload": event.payload}]
        for operation_index, action in enumerate(actions):
            action_type, action_payload = action["type"], action["payload"]
            item = {**action_payload, "_eventSeq": event.seq, "_eventId": event.id}
            if event.type == "operator_mutation_applied":
                item["_operationId"] = event.action_id
                item["_operationIndex"] = operation_index
            if action_type == "goal_set":
                memory.goals.append(item)
            elif action_type == "session_compacted":
                memory.compaction = item
            else:
                collection = {
                "workstream_upserted": memory.workstreams,
                "attempt_recorded": memory.attempts,
                "decision_recorded": memory.decisions,
                "research_question_upserted": memory.questions,
                "theory_upserted": memory.theories,
                "next_action_upserted": memory.next_actions,
                "hypothesis_upserted": memory.hypotheses,
                "hypothesis_test_upserted": memory.hypothesis_tests,
                "hypothesis_observation_recorded": memory.hypothesis_observations,
                "finding_upserted": memory.findings,
                "finding_attempt_recorded": memory.finding_attempts,
                "finding_transition_recorded": memory.finding_transitions,
                "finding_operator_decision_recorded": memory.finding_operator_decisions,
                }[action_type]
                collection[item["id"]] = item
        if reference_exists:
            reference_payloads = ([action["payload"] for action in actions]
                                  if event.type == "operator_mutation_applied"
                                  else [event.payload])
            for reference in (reference for payload in reference_payloads
                              for reference in _references(payload)):
                if not reference_exists(reference):
                    missing = asdict(reference)
                    if missing not in memory.missing_references:
                        memory.missing_references.append(missing)
                    memory.degraded = True
                    memory.diagnostics.append(
                        f"missing optional reference {reference.kind}:{reference.id}"
                    )
    return memory


def memory_context(memory: ProjectMemory, *, max_chars: int = 8_000) -> str:
    if max_chars < 256:
        raise ValueError("max_chars must be at least 256")
    sections: list[tuple[int, str]] = []
    if memory.goals:
        goal = memory.goals[-1]
        sections.append((1000, _line("GOAL", goal.get("text", ""), goal)))
    for item in memory.workstreams.values():
        if item.get("status") in {"active", "paused"}:
            text = f"{item.get('title')}: {item.get('goal')} | next={item.get('nextAction')}"
            if item.get("stopCondition"):
                text += f" | stop={item['stopCondition']}"
            sections.append((900, _line("WORK", text, item)))
    for item in memory.attempts.values():
        if item.get("status") in {"failed", "blocked", "inconclusive"}:
            text = f"{item.get('method')}: {item.get('result')}"
            if item.get("followUp"):
                text += f" | follow-up={item['followUp']}"
            sections.append((850, _line("PRIOR NEGATIVE", text, item)))
    for item in memory.questions.values():
        if item.get("status") == "open":
            sections.append((800, _line("OPEN QUESTION", item.get("question", ""), item)))
    for item in memory.theories.values():
        if item.get("status") in {"supported", "testing"}:
            text = f"{item.get('claim')} | confidence={item.get('confidence')}"
            sections.append((750, _line("THEORY", text, item)))
    for item in memory.hypotheses.values():
        status = item.get("status")
        text = (
            f"{item.get('claim')} | status={status} | expected={item.get('expectedObservation')} "
            f"| falsifier={item.get('falsifier')}"
        )
        if status in {"disproved", "contradicted", "blocked"}:
            sections.append((875, _line("HYPOTHESIS NEGATIVE", text, item)))
        elif status in {"proposed", "queued", "testing", "supported", "reproduced"}:
            sections.append((825, _line("HYPOTHESIS", text, item)))
    for item in memory.hypothesis_observations.values():
        text = (
            f"{item.get('hypothesisId')} outcome={item.get('outcome')}: "
            f"{'; '.join(item.get('observations', []))}"
        )
        sections.append((860, _line("HYPOTHESIS EVIDENCE", text, item)))
    for item in memory.findings.values():
        status = item.get("status")
        text = f"{item.get('affectedComponent')}: {item.get('impactClaim')} | status={status}"
        label = "VALIDATED FINDING" if status == "reproduced" else "FINDING CANDIDATE"
        sections.append((880 if status == "reproduced" else 820, _line(label, text, item)))
    for item in memory.finding_attempts.values():
        if item.get("outcome") in {"negative", "flaky", "contradictory"}:
            text = f"{item.get('findingId')} outcome={item.get('outcome')}: " \
                   f"{'; '.join(item.get('observations', []))}"
            sections.append((890, _line("REPRODUCTION NEGATIVE", text, item)))
    for item in memory.next_actions.values():
        if item.get("status") == "pending" and not item.get("blockers"):
            sections.append((700 + int(item.get("priority", 0)),
                             _line("NEXT", item.get("text", ""), item)))
    if memory.compaction:
        unknowns = memory.compaction.get("unknowns") or []
        if unknowns:
            sections.append((950, _line("UNRESOLVED", "; ".join(unknowns), memory.compaction)))
    if memory.degraded:
        sections.append((1100, "DEGRADED: " + "; ".join(memory.diagnostics[:4])))
    output: list[str] = []
    used = 0
    for _, line in sorted(sections, key=lambda value: (-value[0], value[1])):
        addition = line + "\n"
        if used + len(addition) > max_chars:
            continue
        output.append(line)
        used += len(addition)
    return "\n".join(output)


def render_markdown(memory: ProjectMemory) -> str:
    lines = ["# Project memory", "", f"Schema version: {memory.version}",
             f"Last sequence: {memory.last_seq}", ""]
    if memory.degraded:
        lines += ["> **Degraded:** " + "; ".join(memory.diagnostics), ""]
    groups = (
        ("Goals", memory.goals, "text"),
        ("Workstreams", memory.workstreams.values(), "title"),
        ("Attempts", memory.attempts.values(), "method"),
        ("Decisions", memory.decisions.values(), "choice"),
        ("Research questions", memory.questions.values(), "question"),
        ("Theories", memory.theories.values(), "claim"),
        ("Next actions", memory.next_actions.values(), "text"),
        ("Hypotheses", memory.hypotheses.values(), "claim"),
        ("Hypothesis tests", memory.hypothesis_tests.values(), "objective"),
        ("Hypothesis observations", memory.hypothesis_observations.values(), "reason"),
        ("Findings", memory.findings.values(), "impactClaim"),
        ("Finding reproduction attempts", memory.finding_attempts.values(), "outcome"),
        ("Finding transitions", memory.finding_transitions.values(), "reason"),
        ("Finding operator decisions", memory.finding_operator_decisions.values(), "rationale"),
    )
    for title, values, label in groups:
        lines += [f"## {title}", ""]
        ordered = sorted(values, key=lambda item: (item.get("_eventSeq", 0), item.get("id", "")))
        if not ordered:
            lines += ["_None._", ""]
            continue
        for item in ordered:
            lines.append(f"- {item.get(label, '')}  ")
            lines.append(f"  Status: {item.get('status', 'recorded')} · memory-event:{item['_eventSeq']}")
            refs = _references(item)
            if refs:
                lines.append("  References: " + ", ".join(f"{ref.kind}:{ref.id}" for ref in refs))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_markdown(memory: ProjectMemory, output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(memory), encoding="utf-8")
    return path


def _read_events(path: Path) -> tuple[list[MemoryEvent], list[str]]:
    if not path.exists():
        return [], []
    events, diagnostics = [], []
    for line_number, raw in enumerate(path.read_bytes().splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
            events.append(MemoryEvent(**value))
        except Exception as exc:
            diagnostics.append(f"corrupt event at line {line_number}: {type(exc).__name__}")
    return events, diagnostics


def _validate_action(action: MemoryAction) -> dict[str, Any]:
    if action.type not in KNOWN_TYPES:
        raise ValueError(f"unsupported memory action type {action.type!r}")
    if not isinstance(action.payload, dict):
        raise ValueError("memory action payload must be an object")
    payload = json.loads(json.dumps(action.payload, sort_keys=True))
    if action.type == "operator_mutation_applied":
        if set(payload) != {"operationId", "expectedRevision", "request", "actions"}:
            raise ValueError("operator mutation must contain only exact operation fields")
        if (not isinstance(payload["operationId"], str)
                or re.fullmatch(r"memory-operation-[0-9a-f]{64}", payload["operationId"]) is None
                or type(payload["expectedRevision"]) is not int
                or payload["expectedRevision"] < 0
                or type(payload["request"]) is not dict
                or type(payload["actions"]) is not list
                or not 1 <= len(payload["actions"]) <= 4):
            raise ValueError("operator mutation fields are invalid")
        for nested in payload["actions"]:
            if type(nested) is not dict or set(nested) != {"type", "payload"}:
                raise ValueError("operator mutation nested action is invalid")
            if nested["type"] == "operator_mutation_applied":
                raise ValueError("operator mutations cannot nest")
            _validate_action(MemoryAction(nested["type"], nested["payload"]))
        return payload
    required = {
        "goal_set": ("text",), "workstream_upserted": ("id", "title", "status", "goal"),
        "attempt_recorded": ("id", "intent", "method", "status", "result"),
        "decision_recorded": ("id", "choice", "rationale", "alternatives"),
        "research_question_upserted": ("id", "question", "status"),
        "theory_upserted": ("id", "claim", "status", "confidence"),
        "next_action_upserted": ("id", "text", "priority", "status"),
        "session_compacted": ("summary", "unknowns", "references"),
        "hypothesis_upserted": (
            "id", "claim", "scope", "expectedObservation", "falsifier", "confidence",
            "status", "stopCondition", "semanticKey", "references",
        ),
        "hypothesis_test_upserted": (
            "id", "hypothesisId", "objective", "scope", "method", "expected_observation",
            "falsifying_observation", "information_gain", "risk", "cost_units", "status",
            "attempts", "references",
        ),
        "hypothesis_observation_recorded": (
            "id", "hypothesisId", "testId", "outcome", "observations", "reason", "references",
        ),
        "finding_upserted": (
            "id", "hypothesisId", "scope", "observations", "affectedComponent",
            "impactClaim", "assumptions", "knownUncertainty", "findingType",
            "consequence", "proofPolicy", "recipe", "status", "originWorkerId",
            "originSessionId", "references",
        ),
        "finding_attempt_recorded": (
            "id", "findingId", "recipeId", "outcome", "workerId", "sessionId",
            "environment", "observations", "environmentalDifferences", "references",
        ),
        "finding_transition_recorded": (
            "id", "findingId", "fromStatus", "toStatus", "reason", "references",
        ),
        "finding_operator_decision_recorded": (
            "id", "findingId", "decision", "rationale", "unmetCriteria", "references",
        ),
    }[action.type]
    missing = [name for name in required if name not in payload]
    if missing:
        raise ValueError(f"{action.type} missing fields: {', '.join(missing)}")
    for name in required:
        if name in {"alternatives", "unknowns", "references", "observations",
                    "assumptions", "environmentalDifferences", "unmetCriteria"}:
            if not isinstance(payload[name], list):
                raise ValueError(f"{action.type}.{name} must be a list")
        elif name in {"stopCondition", "proofPolicy", "recipe", "environment"}:
            if not isinstance(payload[name], dict):
                raise ValueError("hypothesis stopCondition must be an object")
        elif name == "priority":
            if isinstance(payload[name], bool) or not isinstance(payload[name], int):
                raise ValueError("next_action_upserted.priority must be an integer")
        elif not isinstance(payload[name], (str, int, float)) or str(payload[name]).strip() == "":
            raise ValueError(f"{action.type}.{name} must not be empty")
    if action.type in ENTITY_TYPES and action.type != "goal_set":
        _stable_entity_id(payload["id"])
    if action.type == "goal_set":
        payload.setdefault("id", _stable_id("goal", payload["text"]))
    statuses = {
        "workstream_upserted": {"candidate", "active", "paused", "rejected", "completed"},
        "attempt_recorded": {"planned", "running", "completed", "failed", "blocked", "inconclusive"},
        "research_question_upserted": {"open", "answered", "closed"},
        "theory_upserted": {"proposed", "testing", "supported", "rejected", "disproven"},
        "next_action_upserted": {"pending", "running", "blocked", "completed", "cancelled"},
        "hypothesis_upserted": {
            "proposed", "queued", "testing", "supported", "contradicted", "disproved",
            "reproduced", "retired", "blocked",
        },
        "hypothesis_test_upserted": {"proposed", "queued", "leased", "completed", "blocked"},
        "finding_upserted": {
            "lead", "candidate", "demonstrated", "reproduction-pending", "reproduced",
            "rejected", "withdrawn", "inconclusive",
        },
    }
    if action.type in statuses and payload["status"] not in statuses[action.type]:
        raise ValueError(f"invalid {action.type} status {payload['status']!r}")
    if action.type == "theory_upserted" and isinstance(payload["confidence"], (int, float)):
        if not 0 <= payload["confidence"] <= 1:
            raise ValueError("theory confidence must be between 0 and 1")
    if action.type == "next_action_upserted":
        blockers = payload.get("blockers", [])
        if not isinstance(blockers, list) or not all(isinstance(item, str) for item in blockers):
            raise ValueError("next action blockers must be a list of stable ids")
    raw_references = payload.get("references", [])
    if not isinstance(raw_references, list) or any(
        not isinstance(item, dict) or set(item) != {"kind", "id"}
        for item in raw_references
    ):
        raise ValueError("references must contain only {kind, id} objects")
    if action.type == "session_compacted":
        if not raw_references:
            raise ValueError("session compaction must cite retained evidence")
        if not all(isinstance(item, str) and item.strip() for item in payload["unknowns"]):
            raise ValueError("session compaction unknowns must be non-empty strings")
    for reference in _references(payload):
        if reference.kind not in REFERENCE_KINDS or not reference.id.strip():
            raise ValueError("invalid evidence reference")
    encoded = json.dumps(payload, sort_keys=True)
    if len(encoded.encode()) > MAX_EVENT_BYTES - 512:
        raise ValueError("memory payload is too large; use evidence references")
    return payload


def _references(payload: dict[str, Any]) -> list[EvidenceRef]:
    result = []
    for item in payload.get("references", []):
        if isinstance(item, dict) and set(item) >= {"kind", "id"}:
            result.append(EvidenceRef(str(item["kind"]), str(item["id"])))
    return result


def _line(label: str, text: str, item: dict[str, Any]) -> str:
    refs = [f"{ref.kind}:{ref.id}" for ref in _references(item)]
    refs.append(f"memory-event:{item['_eventSeq']}")
    return f"{label}: {' '.join(str(text).split())} [{', '.join(refs)}]"


def _stable_entity_id(value: Any) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("memory entity id must be a non-empty string up to 128 characters")


def _stable_id(kind: str, *parts: Any) -> str:
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return f"{kind}-{hashlib.sha256(encoded.encode()).hexdigest()[:16]}"
