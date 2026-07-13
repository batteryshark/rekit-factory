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
from typing import Any, Callable, Literal

from muster import utcnow


SCHEMA_VERSION = 1
KNOWN_TYPES = frozenset({
    "goal_set", "workstream_upserted", "attempt_recorded", "decision_recorded",
    "research_question_upserted", "theory_upserted", "next_action_upserted",
    "session_compacted",
    "hypothesis_upserted", "hypothesis_test_upserted", "hypothesis_observation_recorded",
})
ENTITY_TYPES = KNOWN_TYPES - {"session_compacted"}
REFERENCE_KINDS = frozenset({
    "memory-event", "ledger-event", "artifact", "run-event", "question",
    "capability-lead", "external",
})
MAX_EVENT_BYTES = 32_768


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
    compaction: dict[str, Any] | None = None
    degraded: bool = False
    diagnostics: list[str] = field(default_factory=list)
    missing_references: list[dict[str, str]] = field(default_factory=list)

    def deterministic_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for name in ("workstreams", "attempts", "decisions", "questions",
                     "theories", "next_actions", "hypotheses", "hypothesis_tests",
                     "hypothesis_observations"):
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
            for event in events:
                if event.action_id == action_id:
                    if event.type != action.type or event.payload != payload:
                        raise ValueError(f"action id {action_id!r} already has different content")
                    return event
            seq = events[-1].seq + 1 if events else 1
            event = MemoryEvent(
                version=SCHEMA_VERSION, seq=seq, id=f"mem-{seq:08d}",
                action_id=action_id, type=action.type, ts=utcnow(), payload=payload,
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
        item = {**event.payload, "_eventSeq": event.seq, "_eventId": event.id}
        if event.type == "goal_set":
            memory.goals.append(item)
        elif event.type == "session_compacted":
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
            }[event.type]
            collection[item["id"]] = item
        if reference_exists:
            for reference in _references(event.payload):
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
    }[action.type]
    missing = [name for name in required if name not in payload]
    if missing:
        raise ValueError(f"{action.type} missing fields: {', '.join(missing)}")
    for name in required:
        if name in {"alternatives", "unknowns", "references", "observations"}:
            if not isinstance(payload[name], list):
                raise ValueError(f"{action.type}.{name} must be a list")
        elif name == "stopCondition":
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
