from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from rekit_factory.control import (
    InvestigationController,
    RunRequest,
    WORKER_SEMANTIC_BOUNDARIES,
    WorkerSemanticInterruption,
)
from rekit_factory.memory import MemoryAction
from rekit_factory.memory import EvidenceRef
from rekit_factory.hypotheses import DiscriminatingTestProposal, HypothesisProposal
from rekit_factory.models import ModelProfile, WorkerReport
from rekit_factory.store import FactoryLedger
from muster import resolve_run_dir


class NoopRekit:
    pass


class SemanticBackend:
    def __init__(self):
        self.profile = ModelProfile(
            name="semantic-fixture", provider="test", model="fixture",
            base_url="https://invalid.test", api_key="secret",
        )
        self.calls: list[str] = []

    async def analyze(self, *, role, **kwargs):
        self.calls.append(role)
        proposals = []
        next_actions = []
        if role == "recon":
            proposals = [MemoryAction(
                "attempt_recorded",
                {
                    "id": "attempt-worker-semantic",
                    "intent": "Map the durable worker commit",
                    "method": "bounded fixture review",
                    "status": "failed",
                    "result": "Crash boundary observed",
                    "followUp": "Replay the staged report",
                    "references": [{"kind": "artifact", "id": "sha256:fixture"}],
                },
                action_id="worker-semantic-attempt",
            )]
            next_actions = [
                "[follow-up:format-specialist] Verify the staged report projection"
            ]
            hypotheses = [HypothesisProposal(
                id="h-worker-semantic",
                claim="The staged report is replayed without model reinvocation",
                scope="target",
                expected_observation="One model call produces one semantic projection",
                falsifier="A restart invokes the originating model twice",
                confidence=.8,
                references=[EvidenceRef("artifact", "sha256:fixture")],
                proposed_test=DiscriminatingTestProposal(
                    id="test-worker-semantic",
                    objective="Restart after the semantic boundary",
                    method="deterministic fault injection",
                    expected_observation="The staged report is replayed",
                    falsifying_observation="The model is invoked again",
                    information_gain=90, risk=0, cost_units=5,
                ),
            )]
        else:
            hypotheses = []
        return WorkerReport(
            summary=f"{role} complete", observations=["durable report"],
            next_actions=next_actions, status_update="complete",
            proposed_memory_actions=proposals,
            proposed_hypotheses=hypotheses,
        ), {"inputTokens": 1, "outputTokens": 1}


class FailOnce:
    def __init__(self, boundary: str):
        self.boundary = boundary
        self.fired = False

    def __call__(self, boundary: str) -> None:
        if boundary == self.boundary and not self.fired:
            self.fired = True
            raise WorkerSemanticInterruption(f"crash after {boundary}")


def target(tmp_path: Path) -> Path:
    result = tmp_path / "target"
    result.mkdir()
    (result / "sample.txt").write_text("fixture", encoding="utf-8")
    return result


def request(run_target: Path) -> RunRequest:
    return RunRequest(
        run_target, "Exercise worker semantic recovery", worker_roles=("recon",),
        concurrency=1, retries_per_worker=0, cost_units=30, max_workers=3,
    )


@pytest.mark.parametrize("boundary", WORKER_SEMANTIC_BOUNDARIES)
def test_worker_semantic_restart_converges_without_model_or_projection_duplicates(
        tmp_path, boundary):
    storage = tmp_path / "runs"
    run_target = target(tmp_path)
    backend = SemanticBackend()
    interrupted = InvestigationController(
        storage_root=storage, rekit=NoopRekit(), workers=backend,
        worker_semantic_fault_injector=FailOnce(boundary),
    )
    run_dir = interrupted.create(request(run_target))
    with pytest.raises(WorkerSemanticInterruption, match=boundary):
        asyncio.run(interrupted.drive(run_dir))

    resumed = InvestigationController(
        storage_root=storage, rekit=NoopRekit(), workers=backend,
    )
    result = asyncio.run(resumed.drive(run_dir))
    assert result["run"]["status"] == "completed"
    assert backend.calls.count("recon") == 1
    assert backend.calls.count("format-specialist") == 1
    assert backend.calls.count("hypothesis-test:h-worker-semantic") == 1
    assert len(result["workers"]) == 3
    assert len(result["workItems"]) == 3
    assert list(result["memory"]["attempts"]) == ["attempt-worker-semantic"]

    events = result["events"]
    assert len([event for event in events if event["kind"] == "hypothesis.activity"]) == 1
    assert len([event for event in events
                if event["kind"] == "strategy.follow_up_enqueued"]) == 1
    assert len([event for event in events if event["kind"] == "worker.completed"]) == 3
    assert len(result["modelCalls"]) == 3
    assert not [artifact for artifact in result["artifacts"]
                if artifact["kind"] == "worker-report"]
    recon_item = next(item for item in result["workItems"]
                      if item["payload"]["role"] == "recon")
    assert recon_item["result"]["summary"] == "recon complete"

    paths = resolve_run_dir(run_dir)
    with FactoryLedger(paths.db_path) as ledger:
        rows = ledger.conn.execute(
            "select status from factory_worker_semantic_commits order by created_at"
        ).fetchall()
        assert [row["status"] for row in rows] == ["complete", "complete", "complete"]


def test_staged_worker_report_tamper_fails_closed_without_reinvocation(tmp_path):
    storage = tmp_path / "runs"
    run_target = target(tmp_path)
    backend = SemanticBackend()
    controller = InvestigationController(
        storage_root=storage, rekit=NoopRekit(), workers=backend,
        worker_semantic_fault_injector=FailOnce("report-staged"),
    )
    run_dir = controller.create(request(run_target))
    with pytest.raises(WorkerSemanticInterruption):
        asyncio.run(controller.drive(run_dir))

    paths = resolve_run_dir(run_dir)
    with FactoryLedger(paths.db_path) as ledger:
        row = ledger.conn.execute(
            "select commit_key,report_json from factory_worker_semantic_commits"
        ).fetchone()
        report = json.loads(row["report_json"])
        report["summary"] = "tampered"
        ledger.conn.execute(
            "update factory_worker_semantic_commits set report_json=? where commit_key=?",
            (json.dumps(report, sort_keys=True, separators=(",", ":")), row["commit_key"]),
        )
        ledger.conn.commit()

    resumed = InvestigationController(
        storage_root=storage, rekit=NoopRekit(), workers=backend,
    )
    result = asyncio.run(resumed.drive(run_dir))
    assert result["run"]["status"] == "failed"
    failed = next(item for item in result["workItems"]
                  if item["payload"]["role"] == "recon")
    assert "integrity verification" in failed["error"]
    assert backend.calls == ["recon"]
