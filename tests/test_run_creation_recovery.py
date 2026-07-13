from __future__ import annotations

import json
from pathlib import Path

import pytest

from rekit_factory.api import _run_dirs
from rekit_factory.control import InvestigationController, RunRequest
from rekit_factory.models import ModelProfile, WorkerReport
from rekit_factory.scope import AuthorizedScope, ScopeApproval, ScopeEnvelope, TargetGrant
from rekit_factory.store import FactoryLedger
from muster import resolve_run_dir


class NoopRekit:
    pass


class CreationBackend:
    def __init__(self):
        self.profile = ModelProfile(
            name="creation-fixture", provider="test", model="fixture",
            base_url="https://invalid.test", api_key="secret",
        )

    async def analyze(self, *, role, **kwargs):
        return WorkerReport(
            summary=f"{role} complete", observations=[], next_actions=[],
            status_update="complete",
        ), {}


class InjectedCreationFailure(RuntimeError):
    pass


class FailBoundaryOnce:
    def __init__(self, prefix: str):
        self.prefix = prefix
        self.fired = False

    def __call__(self, boundary: str) -> None:
        if not self.fired and boundary.startswith(self.prefix):
            self.fired = True
            raise InjectedCreationFailure(boundary)


def target(tmp_path: Path) -> Path:
    result = tmp_path / "target"
    result.mkdir()
    (result / "sample.txt").write_text("fixture", encoding="utf-8")
    return result


def request_for(path: Path) -> RunRequest:
    envelope = ScopeEnvelope(
        scope_id="scope-creation-recovery", revision=1,
        valid_from="2026-07-01T00:00:00Z", valid_until="2026-08-01T00:00:00Z",
        targets=(TargetGrant.from_path(path),),
    )
    scope = AuthorizedScope(envelope, ScopeApproval(
        scope_id=envelope.scope_id, revision=envelope.revision,
        content_digest=envelope.content_digest, approved_by="test-operator",
        approved_at="2026-07-01T00:00:00Z", expires_at="2026-08-01T00:00:00Z",
        rationale="Deterministic run creation recovery fixture",
    ))
    return RunRequest(
        path, "Recover creation", strategy="recon-then-analysis",
        concurrency=2, max_workers=4, cost_units=40, scope=scope,
    )


@pytest.mark.parametrize("boundary", [
    "run-authority", "scope-authority", "campaign-authority", "run-row",
    "run-created-event", "project-memory", "worker-row", "worker-work",
    "creation-complete",
])
def test_creation_boundary_retry_converges_to_one_resumable_graph(tmp_path, boundary):
    storage = tmp_path / "runs"
    run_target = target(tmp_path)
    failure = FailBoundaryOnce(boundary)
    interrupted = InvestigationController(
        storage_root=storage, rekit=NoopRekit(), workers=CreationBackend(),
        creation_fault_injector=failure,
    )
    with pytest.raises(InjectedCreationFailure, match=boundary):
        interrupted.create(request_for(run_target))

    partial = list(storage.glob("projects/*/runs/*/run.json"))
    assert len(partial) == 1
    partial_meta = json.loads(partial[0].read_text(encoding="utf-8"))
    assert _run_dirs(storage) == ([] if not partial_meta["creationComplete"] else [partial[0].parent])

    resumed = InvestigationController(
        storage_root=storage, rekit=NoopRekit(), workers=CreationBackend(),
    )
    run_dir = resumed.reconcile_run_creation(partial[0].parent, request_for(run_target))
    snapshot = resumed.snapshot(run_dir)
    assert snapshot["meta"]["creationComplete"] is True
    assert len(snapshot["workers"]) == 2
    assert len(snapshot["workItems"]) == 2
    assert len([event for event in snapshot["events"] if event["kind"] == "run.created"]) == 1
    assert snapshot["memory"]["last_seq"] == 1

    paths = resolve_run_dir(run_dir)
    with FactoryLedger(paths.db_path) as ledger:
        ledger.set_run_status(paths.run_id, "completed")
    assert resumed.reconcile_run_creation(run_dir, request_for(run_target)) == run_dir
    with FactoryLedger(paths.db_path) as ledger:
        assert ledger.get_run(paths.run_id)["status"] == "completed"
        assert ledger.count_work_items(paths.run_id) == 2
        assert len(ledger.workers(paths.run_id)) == 2


def test_creation_conflict_and_missing_scope_fail_before_work(tmp_path):
    storage = tmp_path / "runs"
    run_target = target(tmp_path)
    controller = InvestigationController(
        storage_root=storage, rekit=NoopRekit(), workers=CreationBackend(),
    )
    run_dir = controller.create(request_for(run_target))
    paths = resolve_run_dir(run_dir)
    meta = json.loads(paths.run_json.read_text(encoding="utf-8"))
    meta["strategyPlan"]["goal"] = "conflicting authority"
    paths.run_json.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(ValueError, match="strategyPlan"):
        controller.reconcile_run_creation(run_dir, request_for(run_target))

    meta["strategyPlan"]["goal"] = "Recover creation"
    paths.run_json.write_text(json.dumps(meta), encoding="utf-8")
    (paths.run_dir / "scope.json").unlink()
    with pytest.raises(ValueError, match="scope authority is missing"):
        controller.validate_run_concurrency(run_dir)


def test_event_log_once_is_transactionally_deduplicated(tmp_path):
    ledger = FactoryLedger(tmp_path / "ledger.sqlite")
    try:
        ledger.create_run(
            run_id="run-1", project_id="project-1", target_path="target",
            target_root="target", storage_root=str(tmp_path), run_dir=str(tmp_path),
            config_json="{}",
        )
        first = ledger.event_log_once("run-1", "semantic-key", "run.created", "created")
        second = ledger.event_log_once("run-1", "semantic-key", "run.created", "changed")
        assert first == second
        assert [event["message"] for event in ledger.events("run-1")] == ["created"]
    finally:
        ledger.close()
