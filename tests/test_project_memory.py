from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile

import pytest

from rekit_factory.memory import (
    MemoryAction,
    ProjectMemoryLog,
    export_markdown,
    memory_context,
    render_markdown,
)


def action(kind, **payload):
    return MemoryAction(kind, payload)


def seed(log: ProjectMemoryLog):
    goal = log.append(action(
        "goal_set", text="Recover the validation boundary", reason="operator request",
        scope="target", references=[{"kind": "run-event", "id": "run-1:created"}],
    ))
    log.append(action(
        "workstream_upserted", id="ws-validation", title="Validation boundary",
        status="active", goal="Locate and explain validation", nextAction="Try static trace",
        stopCondition="A test reproduces the decision",
        references=[{"kind": "memory-event", "id": str(goal.seq)}],
    ))
    log.append(action(
        "attempt_recorded", id="attempt-symbolic", workstreamId="ws-validation",
        intent="Trace symbolically", method="symbolic execution", status="failed",
        result="State explosion before validator", followUp="Use a bounded static slice",
        references=[{"kind": "artifact", "id": "sha256:failure-log"}],
    ))
    log.append(action(
        "research_question_upserted", id="q-checksum", question="Is the checksum salted?",
        status="open", impact="Changes reproduction", evidenceNeeded="Known input/output pair",
        references=[{"kind": "run-event", "id": "run-1:worker-2"}],
    ))
    log.append(action(
        "theory_upserted", id="theory-table", claim="Validation uses a lookup table",
        status="testing", confidence=0.6, validationStep="Inspect indexed reads",
        references=[{"kind": "artifact", "id": "sha256:disassembly"}],
    ))
    log.append(action(
        "next_action_upserted", id="next-static", text="Build bounded static slice",
        priority=90, status="pending", blockers=[], workstreamId="ws-validation",
        references=[{"kind": "memory-event", "id": "3"}],
    ))
    log.append(action(
        "decision_recorded", id="decision-no-emulation", choice="Prefer static slice",
        rationale="Symbolic execution already exhausted the state budget",
        alternatives=["retry symbolic execution", "dynamic emulation"],
        reconsiderWhen="A smaller state model is available",
        references=[{"kind": "memory-event", "id": "3"}],
    ))
    log.append(action(
        "session_compacted", summary="Validation work narrowed to a static slice.",
        unknowns=["Checksum salt remains unknown"],
        references=[{"kind": "memory-event", "id": "3"}],
    ))


def test_replay_is_byte_equivalent_and_duplicate_actions_are_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        log = ProjectMemoryLog(tmp)
        first = action("goal_set", text="Understand target", reason="start", scope="target")
        event = log.append(first)
        assert log.append(first) == event
        before = log.replay().canonical_json()
        after = ProjectMemoryLog(tmp).replay().canonical_json()
        assert before == after
        assert len(Path(tmp, "memory.jsonl").read_text().splitlines()) == 1


def test_concurrent_writers_get_monotonic_unique_sequence_numbers():
    with tempfile.TemporaryDirectory() as tmp:
        log = ProjectMemoryLog(tmp)
        actions = [action(
            "next_action_upserted", id=f"next-{index}", text=f"Inspect item {index}",
            priority=index, status="pending", blockers=[],
            references=[{"kind": "external", "id": f"fixture:{index}"}],
        ) for index in range(20)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            events = list(pool.map(log.append, actions))
        assert sorted(event.seq for event in events) == list(range(1, 21))
        assert log.replay().last_seq == 20


def test_bounded_context_resumes_next_action_and_preserves_relevant_failure():
    with tempfile.TemporaryDirectory() as tmp:
        log = ProjectMemoryLog(tmp)
        seed(log)
        for index in range(80):
            log.append(action(
                "attempt_recorded", id=f"noise-{index}", intent="Explore", method=f"method {index}",
                status="completed", result="Not relevant",
                references=[{"kind": "artifact", "id": f"sha256:{index:064x}"}],
            ))
        context = memory_context(log.replay(), max_chars=900)
        assert len(context) <= 900
        assert "Build bounded static slice" in context
        assert "symbolic execution" in context
        assert "Checksum salt remains unknown" in context
        assert "memory-event:" in context
        assert "Not relevant" not in context


def test_markdown_is_deterministic_and_cannot_mutate_runtime_stream():
    with tempfile.TemporaryDirectory() as tmp:
        log = ProjectMemoryLog(Path(tmp) / "project")
        seed(log)
        memory = log.replay()
        first = render_markdown(memory)
        output = export_markdown(memory, Path(tmp) / "export" / "README.md")
        assert output.read_text() == first == render_markdown(log.replay())
        stream_before = log.path.read_bytes()
        output.write_text("# edited\n", encoding="utf-8")
        assert log.path.read_bytes() == stream_before
        assert log.replay().canonical_json() == memory.canonical_json()


def test_corrupt_tail_unknown_event_and_missing_reference_degrade_explicitly():
    with tempfile.TemporaryDirectory() as tmp:
        log = ProjectMemoryLog(tmp)
        log.append(action(
            "goal_set", text="Understand target", reason="start", scope="target",
            references=[{"kind": "artifact", "id": "missing-sha"}],
        ))
        unknown = {
            "version": 2, "seq": 2, "id": "mem-00000002", "action_id": "future-1",
            "type": "future_insight", "ts": "2030-01-01T00:00:00Z", "payload": {"claim": "x"},
        }
        with log.path.open("ab") as stream:
            stream.write(json.dumps(unknown).encode() + b"\n{broken-tail")
        memory = log.replay(reference_exists=lambda _reference: False)
        assert memory.degraded
        assert any("unknown event" in item for item in memory.diagnostics)
        assert any("corrupt event" in item for item in memory.diagnostics)
        assert memory.missing_references == [{"kind": "artifact", "id": "missing-sha"}]
        assert "invented" not in memory.canonical_json()


def test_validation_rejects_arbitrary_events_conflicts_and_bulky_payloads():
    with tempfile.TemporaryDirectory() as tmp:
        log = ProjectMemoryLog(tmp)
        with pytest.raises(ValueError, match="unsupported"):
            log.append(MemoryAction("raw_model_thought", {"text": "secret"}))  # type: ignore[arg-type]
        log.append(MemoryAction(
            "goal_set", {"text": "one", "reason": "x", "scope": "target"}, "same-id"
        ))
        with pytest.raises(ValueError, match="different content"):
            log.append(MemoryAction(
                "goal_set", {"text": "two", "reason": "x", "scope": "target"}, "same-id"
            ))
        with pytest.raises(ValueError, match="too large"):
            log.append(action(
                "attempt_recorded", id="huge", intent="copy output", method="bad",
                status="failed", result="x" * 40_000,
            ))
        with pytest.raises(ValueError, match="invalid theory"):
            log.append(action(
                "theory_upserted", id="theory", claim="A claim", status="certain",
                confidence=1, references=[],
            ))
        with pytest.raises(ValueError, match="cite retained evidence"):
            log.append(action(
                "session_compacted", summary="summary", unknowns=["still unknown"],
                references=[],
            ))
