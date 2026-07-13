from __future__ import annotations

from pathlib import Path
import tempfile

from rekit_factory.control import InvestigationController, RunRequest
from rekit_factory.memory import MemoryAction
from rekit_factory.models import ModelProfile, WorkerReport


class NoopRekit:
    pass


class MemoryBackend:
    def __init__(self, *, propose=True):
        self.profile = ModelProfile(
            name="memory-fixture", provider="test", model="fixture",
            base_url="https://invalid.test", api_key="secret",
        )
        self.propose = propose
        self.contexts: list[str] = []

    async def analyze(self, *, role, tool_context, **kwargs):
        self.contexts.append(tool_context)
        proposals = []
        if self.propose:
            proposals.append(MemoryAction(
                "attempt_recorded",
                {
                    "id": "attempt-symbolic",
                    "intent": "Trace validator",
                    "method": "symbolic execution",
                    "status": "failed",
                    "result": "State explosion before validator",
                    "followUp": "Build a bounded static slice",
                    "references": [{"kind": "artifact", "id": "sha256:failure-log"}],
                },
                action_id="fixture-symbolic-failure",
            ))
        return WorkerReport(
            summary=f"{role} complete", observations=[], next_actions=[],
            status_update="complete", proposed_memory_actions=proposals,
        ), {"inputTokens": 1, "outputTokens": 1}


class InvalidMemoryBackend(MemoryBackend):
    async def analyze(self, *, role, tool_context, **kwargs):
        self.contexts.append(tool_context)
        return WorkerReport(
            summary="complete", status_update="complete",
            proposed_memory_actions=[MemoryAction(
                "attempt_recorded",
                {"id": "invalid", "intent": "x", "method": "x",
                 "status": "failed", "result": "uncited"},
            )],
        ), {}


def _target(root: str) -> Path:
    target = Path(root) / "target"
    target.mkdir()
    (target / "sample.txt").write_text("fixture", encoding="utf-8")
    return target


def test_completed_worker_actions_feed_later_worker_and_snapshot_projection():
    with tempfile.TemporaryDirectory() as tmp:
        backend = MemoryBackend()
        controller = InvestigationController(
            storage_root=Path(tmp) / "runs", rekit=NoopRekit(), workers=backend,
        )
        result = controller.run(RunRequest(
            _target(tmp), "Recover validator", worker_roles=("first", "second"),
            concurrency=1,
        ))

        assert "symbolic execution" not in backend.contexts[0]
        assert "symbolic execution" in backend.contexts[1]
        assert "State explosion before validator" in backend.contexts[1]
        assert result["memory"]["attempts"]["attempt-symbolic"]["status"] == "failed"
        assert "symbolic execution" in result["memoryContext"]
        # The identical action proposed by both workers is one event.
        assert len(result["memory"]["attempts"]) == 1
        assert result["memory"]["last_seq"] == 2  # run goal + failed attempt


def test_new_controller_and_run_resume_project_memory_without_transcript():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp) / "runs"
        target = _target(tmp)
        first = InvestigationController(
            storage_root=storage, rekit=NoopRekit(), workers=MemoryBackend(),
        )
        first.run(RunRequest(target, "Initial investigation", worker_roles=("first",)))

        resumed_backend = MemoryBackend(propose=False)
        resumed = InvestigationController(
            storage_root=storage, rekit=NoopRekit(), workers=resumed_backend,
        )
        result = resumed.run(RunRequest(
            target, "Continue with the next method", worker_roles=("second-harness",),
        ))
        assert "symbolic execution" in resumed_backend.contexts[0]
        assert "Build a bounded static slice" in resumed_backend.contexts[0]
        assert len(result["memory"]["goals"]) == 2
        assert result["memory"]["attempts"]["attempt-symbolic"]["status"] == "failed"


def test_uncited_or_invalid_model_actions_are_rejected_without_failing_report():
    with tempfile.TemporaryDirectory() as tmp:
        controller = InvestigationController(
            storage_root=Path(tmp) / "runs", rekit=NoopRekit(),
            workers=InvalidMemoryBackend(),
        )
        result = controller.run(RunRequest(
            _target(tmp), "Reject arbitrary memory", worker_roles=("worker",),
        ))
        assert result["run"]["status"] == "completed"
        assert result["memory"]["attempts"] == {}
        completed = [event for event in result["events"] if event["kind"] == "worker.completed"]
        assert completed[0]["payload"]["memoryActionRejectedCount"] == 1
