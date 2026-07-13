from __future__ import annotations

import hashlib
import json
from pathlib import Path

from rekit_factory.control import InvestigationController, RunRequest
from rekit_factory.knowledge import KnowledgeRoot
from rekit_factory.models import (
    DeferredModelToolCall,
    ModelProfile,
    WorkerReport,
    WorkerTurn,
    _durable_message_history,
)
from muster.pydantic_runtime import load_message_history
from pydantic_ai import ModelRequest, ToolReturnPart


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _bundle(root: Path, *, secondary: bool = False) -> Path:
    if secondary:
        _write(root / "index.md", "# Notes\n* [Second](second.md) - Debugger instrumentation field note.\n")
        _write(root / "second.md", """---
type: Producer Defined Note
title: Secondary Debugger Note
---

Debugger instrumentation from a second named root.
""")
        return root
    _write(root / "index.md", """# Knowledge
* [Protection](protections/debugger.md) - Anti-debug and instrumentation guidance.
* [Missing](missing.md) - Deliberately broken index entry.
""")
    _write(root / "protections" / "debugger.md", """---
type: Protection
title: Debugger Rungs
description: Layered anti-debug guidance.
tags: [anti-debug, instrumentation]
---

Debugger behavior is described in bounded layers. This opening paragraph is searchable,
while the implementation detail below is intentionally outside the persisted search snippet.
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
UNIQUE-BODY-MARKER-NEVER-DURABLE.
Follow [inline instrumentation](/techniques/instrumentation.md) or a
[future note](./missing.md).

# Citations

[1] [Research](https://example.test/debugger)
""")
    _write(root / "techniques" / "instrumentation.md", """---
type: Unregistered Producer Technique
title: Inline Instrumentation
---

Install hooks only after unpacking completes.
""")
    return root


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


class _NoopRekit:
    def list_tools(self):
        return []

    def manifest(self, tool_id):  # pragma: no cover - this test requests no Rekit tools
        raise KeyError(tool_id)


class _KnowledgeBackend:
    def __init__(self):
        self.profile = ModelProfile(
            name="knowledge-fixture", provider="test", model="deterministic",
            base_url="https://model.invalid/v1", api_key="not-persisted",
        )
        self.turns = 0
        self.search_roots: set[str] = set()

    async def analyze(self, *, messages_json=None, tool_results=(),
                      knowledge_available=False, **kwargs):
        assert knowledge_available
        self.turns += 1
        if self.turns == 1:
            return WorkerTurn(
                report=None, usage={}, messages_json='[{"turn":1}]',
                deferred_calls=(DeferredModelToolCall(
                    call_id="search", tool_id="knowledge.search",
                    tool_name="knowledge_search", capability="knowledge",
                    query="debugger instrumentation", limit=4,
                ),),
            )
        if self.turns == 2:
            payload = json.loads(tool_results[0].content)
            self.search_roots = {hit["root"] for hit in payload["hits"]}
            return WorkerTurn(
                report=None, usage={}, messages_json='[{"turn":2}]',
                deferred_calls=(DeferredModelToolCall(
                    call_id="get", tool_id="knowledge.get", tool_name="knowledge_get",
                    capability="knowledge", root="primary",
                    concept_id="protections/debugger",
                    rationale="The top result directly addresses anti-debug behavior.",
                ),),
            )
        if self.turns == 3:
            payload = json.loads(tool_results[0].content)
            assert "UNIQUE-BODY-MARKER-NEVER-DURABLE" in payload["body"]
            link = next(item for item in payload["links"]
                        if item["target"] == "/techniques/instrumentation.md")
            return WorkerTurn(
                report=None, usage={}, messages_json='[{"turn":3}]',
                deferred_calls=(DeferredModelToolCall(
                    call_id="follow", tool_id="knowledge.follow",
                    tool_name="knowledge_follow", capability="knowledge", root="primary",
                    source_id="protections/debugger", link_target=link["target"],
                    rationale="Follow the relevant technique disclosed by the selected concept.",
                ),),
            )
        assert "Install hooks" in tool_results[0].content
        return WorkerTurn(
            report=WorkerReport(
                summary="Knowledge retrieval completed", observations=["references selected"],
                next_actions=[], status_update="complete",
            ), usage={}, messages_json='[{"turn":4}]',
        )


class _LineageBackend(_KnowledgeBackend):
    def __init__(self, mode: str):
        super().__init__()
        self.mode = mode

    async def analyze(self, *, messages_json=None, tool_results=(),
                      knowledge_available=False, **kwargs):
        assert knowledge_available
        self.turns += 1
        if self.turns == 1 and self.mode == "get":
            return WorkerTurn(
                report=None, usage={}, messages_json='[{"turn":1}]',
                deferred_calls=(DeferredModelToolCall(
                    call_id="guessed-get", tool_id="knowledge.get",
                    tool_name="knowledge_get", capability="knowledge", root="primary",
                    concept_id="protections/debugger", rationale="Guessed without search.",
                ),),
            )
        if self.turns == 1:
            return WorkerTurn(
                report=None, usage={}, messages_json='[{"turn":1}]',
                deferred_calls=(DeferredModelToolCall(
                    call_id="search", tool_id="knowledge.search",
                    tool_name="knowledge_search", capability="knowledge",
                    query="debugger instrumentation", limit=4,
                ),),
            )
        if self.turns == 2 and self.mode == "follow":
            assert not tool_results[0].denied
            return WorkerTurn(
                report=None, usage={}, messages_json='[{"turn":2}]',
                deferred_calls=(DeferredModelToolCall(
                    call_id="guessed-follow", tool_id="knowledge.follow",
                    tool_name="knowledge_follow", capability="knowledge", root="primary",
                    source_id="protections/debugger",
                    link_target="/techniques/instrumentation.md",
                    rationale="Tried to follow before selecting the source.",
                ),),
            )
        assert tool_results[0].denied
        return WorkerTurn(
            report=WorkerReport(
                summary="Lineage rejection observed", observations=[tool_results[0].content],
                next_actions=[], status_update="complete",
            ), usage={}, messages_json='[{"complete":true}]',
        )


class _MutationBackend(_KnowledgeBackend):
    def __init__(self, source: Path, mode: str):
        super().__init__()
        self.source = source
        self.mode = mode

    async def analyze(self, *, tool_results=(), knowledge_available=False, **kwargs):
        assert knowledge_available
        self.turns += 1
        if self.turns == 1:
            return WorkerTurn(
                report=None, usage={}, messages_json='[{"turn":1}]',
                deferred_calls=(DeferredModelToolCall(
                    call_id="search", tool_id="knowledge.search",
                    tool_name="knowledge_search", capability="knowledge",
                    query="debugger instrumentation", limit=4,
                ),),
            )
        if self.turns == 2:
            assert not tool_results[0].denied
            if self.mode == "get":
                self.source.write_text(
                    self.source.read_text(encoding="utf-8") + "\nChanged after search.\n",
                    encoding="utf-8",
                )
            return WorkerTurn(
                report=None, usage={}, messages_json='[{"turn":2}]',
                deferred_calls=(DeferredModelToolCall(
                    call_id="get", tool_id="knowledge.get", tool_name="knowledge_get",
                    capability="knowledge", root="primary",
                    concept_id="protections/debugger", rationale="Open the disclosed result.",
                ),),
            )
        if self.turns == 3 and self.mode == "follow":
            assert not tool_results[0].denied
            self.source.write_text(
                self.source.read_text(encoding="utf-8") + "\nChanged after selection.\n",
                encoding="utf-8",
            )
            return WorkerTurn(
                report=None, usage={}, messages_json='[{"turn":3}]',
                deferred_calls=(DeferredModelToolCall(
                    call_id="follow", tool_id="knowledge.follow",
                    tool_name="knowledge_follow", capability="knowledge", root="primary",
                    source_id="protections/debugger",
                    link_target="/techniques/instrumentation.md",
                    rationale="Follow a link from the selected source.",
                ),),
            )
        assert tool_results[0].denied
        return WorkerTurn(
            report=WorkerReport(
                summary="Changed knowledge was rejected", observations=[tool_results[0].content],
                next_actions=[], status_update="complete",
            ), usage={}, messages_json='[{"complete":true}]',
        )


def test_deferred_knowledge_round_trip_is_durable_bounded_and_restart_safe(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    _write(target / "sample.txt", "authorized fixture")
    primary = _bundle(tmp_path / "primary")
    secondary = _bundle(tmp_path / "secondary", secondary=True)
    before = {"primary": _tree_digest(primary), "secondary": _tree_digest(secondary)}
    backend = _KnowledgeBackend()
    roots = (
        KnowledgeRoot.at(primary, name="primary"),
        KnowledgeRoot.at(secondary, name="secondary"),
    )
    controller = InvestigationController(
        storage_root=tmp_path / "runs", rekit=_NoopRekit(), workers=backend,
        knowledge_roots=roots,
    )

    result = controller.run(RunRequest(
        target, "Use relevant project knowledge", worker_roles=("analyst",),
    ))

    assert result["run"]["status"] == "completed"
    assert backend.turns == 4
    assert backend.search_roots == {"primary", "secondary"}
    assert len(result["knowledgeReferences"]) == 2
    source = next(item for item in result["knowledgeReferences"]
                  if item["conceptId"] == "protections/debugger")
    assert source["root"] == "primary"
    assert source["citations"] == ["https://example.test/debugger"]
    assert source["provenance"]["operation"] == "get"
    assert len(source["contentHash"]) == 64
    assert "body" not in source

    run_dir = Path(result["run"]["run_dir"])
    durable_bytes = b"".join(path.read_bytes() for path in run_dir.rglob("*") if path.is_file())
    assert b"UNIQUE-BODY-MARKER-NEVER-DURABLE" not in durable_bytes

    restarted = InvestigationController(
        storage_root=tmp_path / "runs", rekit=_NoopRekit(), workers=_KnowledgeBackend(),
        knowledge_roots=roots,
    )
    restarted_snapshot = restarted.snapshot(run_dir)
    assert len(restarted_snapshot["knowledgeReferences"]) == 2
    assert before == {"primary": _tree_digest(primary), "secondary": _tree_digest(secondary)}


def test_provider_message_shape_replaces_knowledge_body_with_compact_reference():
    content = json.dumps({
        "operation": "get", "root": "primary", "conceptId": "protections/debugger",
        "title": "Debugger Rungs", "contentHash": "a" * 64,
        "body": "UNIQUE-BODY-MARKER-NEVER-DURABLE",
    })
    message = ModelRequest(parts=[ToolReturnPart(
        tool_name="knowledge_get", content=content, tool_call_id="knowledge-call-1",
    )])

    serialized = _durable_message_history([message])
    restored = load_message_history(serialized)
    part = restored[0].parts[0]

    assert "UNIQUE-BODY-MARKER-NEVER-DURABLE" not in serialized
    assert part.tool_call_id == "knowledge-call-1"
    assert part.tool_name == "knowledge_get"
    assert json.loads(part.content) == {
        "operation": "get", "root": "primary", "conceptId": "protections/debugger",
        "title": "Debugger Rungs", "contentHash": "a" * 64,
    }


def test_get_and_follow_require_worker_progressive_disclosure_lineage(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    _write(target / "fixture.txt", "authorized")
    primary = _bundle(tmp_path / "primary")
    roots = (KnowledgeRoot.at(primary, name="primary"),)

    for mode, expected in (
        ("get", "not disclosed by this worker's prior search"),
        ("follow", "source was not selected by this worker"),
    ):
        backend = _LineageBackend(mode)
        controller = InvestigationController(
            storage_root=tmp_path / f"runs-{mode}", rekit=_NoopRekit(), workers=backend,
            knowledge_roots=roots,
        )
        result = controller.run(RunRequest(
            target, f"Reject guessed knowledge {mode}", worker_roles=("analyst",),
        ))
        failed = [item for item in result["workItems"]
                  if item["operation"] == "model-knowledge" and item["status"] == "failed"]
        assert len(failed) == 1
        assert expected in failed[0]["error"]
        assert result["knowledgeReferences"] == []


def test_get_and_follow_bind_the_exact_disclosed_content_hash(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    _write(target / "fixture.txt", "authorized")

    for mode, expected in (
        ("get", "changed after search"),
        ("follow", "changed after selection"),
    ):
        primary = _bundle(tmp_path / f"primary-{mode}")
        backend = _MutationBackend(primary / "protections" / "debugger.md", mode)
        controller = InvestigationController(
            storage_root=tmp_path / f"runs-mutated-{mode}",
            rekit=_NoopRekit(), workers=backend,
            knowledge_roots=(KnowledgeRoot.at(primary, name="primary"),),
        )
        result = controller.run(RunRequest(
            target, f"Reject changed knowledge during {mode}", worker_roles=("analyst",),
        ))
        failed = [item for item in result["workItems"]
                  if item["operation"] == "model-knowledge" and item["status"] == "failed"]
        assert len(failed) == 1
        assert expected in failed[0]["error"]
