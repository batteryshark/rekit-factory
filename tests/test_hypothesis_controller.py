from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile

from muster import resolve_run_dir

from rekit_factory.control import InvestigationController, RunRequest, _target_snapshot
from rekit_factory.hypotheses import (
    DiscriminatingTestProposal,
    HypothesisProposal,
    HypothesisUpdate,
    StopCondition,
)
from rekit_factory.memory import EvidenceRef
from rekit_factory.models import ModelProfile, WorkerReport
from rekit_factory.store import FactoryLedger


class NoopRekit:
    pass


def hypothesis(identifier, claim, expected, falsifier, information_gain):
    return HypothesisProposal(
        id=identifier, claim=claim, scope="target", expected_observation=expected,
        falsifier=falsifier, confidence=.5,
        references=[EvidenceRef("artifact", "sha256:recon")],
        owner_workstream="ws-validation", stop_condition=StopCondition(max_attempts=2),
        proposed_test=DiscriminatingTestProposal(
            id=f"test-{identifier}", objective=f"Discriminate {identifier}", method="fixture test",
            expected_observation=expected, falsifying_observation=falsifier,
            information_gain=information_gain, risk=0, cost_units=5,
        ),
        competing_with=["h-table" if identifier == "h-checksum" else "h-checksum"],
    )


class HypothesisBackend:
    def __init__(self):
        self.profile = ModelProfile(
            name="hypothesis-fixture", provider="test", model="fixture",
            base_url="https://invalid.test", api_key="secret",
        )
        self.calls = []

    async def analyze(self, *, role, **kwargs):
        self.calls.append(role)
        if role == "recon":
            return WorkerReport(
                summary="two explanations", status_update="proposed",
                proposed_hypotheses=[
                    hypothesis("h-table", "A lookup table controls validation",
                               "Indexed value reaches verdict", "No indexed flow", 90),
                    hypothesis("h-checksum", "A checksum controls validation",
                               "Checksum comparison reaches verdict", "No checksum comparison", 80),
                ],
            ), {}
        identifier = role.removeprefix("hypothesis-test:")
        supported = identifier == "h-table"
        return WorkerReport(
            summary="test complete", status_update="tested",
            hypothesis_updates=[HypothesisUpdate(
                hypothesis_id=identifier, test_id=f"test-{identifier}",
                status="supported" if supported else "disproved",
                confidence=.9 if supported else .05,
                observations=["fixture matched" if supported else "falsifier observed"],
                references=[EvidenceRef("artifact", f"sha256:{identifier}-test")],
                reason="deterministic fixture outcome",
            )],
        ), {}


def _target(tmp):
    target = Path(tmp) / "target"
    target.mkdir()
    (target / "fixture.txt").write_text("fixture", encoding="utf-8")
    return target


def test_competing_hypotheses_schedule_as_durable_work_and_survive_restart_once():
    with tempfile.TemporaryDirectory() as tmp:
        target = _target(tmp)
        backend = HypothesisBackend()
        controller = InvestigationController(
            storage_root=Path(tmp) / "runs", rekit=NoopRekit(), workers=backend,
        )
        run_dir = controller.create(RunRequest(
            target, "Explain validation", worker_roles=("recon",), concurrency=1,
        ))
        paths = resolve_run_dir(run_dir)
        with FactoryLedger(paths.db_path) as ledger:
            item = dict(ledger.lease_next_actionable(paths.run_id))
            ctx = SimpleNamespace(
                state=SimpleNamespace(run_id=paths.run_id, iteration=0),
                deps=SimpleNamespace(
                    ledger=ledger, paths=paths,
                    scratch={"targetSnapshot": _target_snapshot(target)},
                ),
            )
            asyncio.run(controller._worker_handler(ctx, item))
            tests = ledger.conn.execute(
                "select * from work_items where category='hypothesis-test'"
            ).fetchall()
            assert len(tests) == 2

        # New process/controller reclaims and executes each queued test once.
        resumed_backend = HypothesisBackend()
        resumed = InvestigationController(
            storage_root=Path(tmp) / "runs", rekit=NoopRekit(), workers=resumed_backend,
        )
        result = asyncio.run(resumed.drive(run_dir))
        states = {item["id"]: item["status"] for item in result["hypothesisState"]["hypotheses"]}
        assert states == {"h-checksum": "disproved", "h-table": "supported"}
        assert len(result["hypothesisState"]["observations"]) == 2
        hypothesis_items = [item for item in result["workItems"]
                            if item["category"] == "hypothesis-test"]
        assert len(hypothesis_items) == 2
        assert all(item["attempts"] == 1 for item in hypothesis_items)
        assert sorted(resumed_backend.calls) == [
            "hypothesis-test:h-checksum", "hypothesis-test:h-table",
        ]


class OutOfScopeBackend(HypothesisBackend):
    async def analyze(self, **kwargs):
        proposal = hypothesis("h-broad", "Another target is vulnerable", "x", "y", 99)
        proposal.proposed_test.scope = "other-target"
        return WorkerReport(
            summary="bad scope", status_update="complete", proposed_hypotheses=[proposal]
        ), {}


def test_controller_rejects_out_of_scope_hypothesis_work():
    with tempfile.TemporaryDirectory() as tmp:
        controller = InvestigationController(
            storage_root=Path(tmp) / "runs", rekit=NoopRekit(), workers=OutOfScopeBackend(),
        )
        result = controller.run(RunRequest(
            _target(tmp), "Stay scoped", worker_roles=("recon",),
        ))
        assert result["hypothesisState"]["hypotheses"] == []
        assert not [item for item in result["workItems"] if item["category"] == "hypothesis-test"]


class ExhaustingBackend(HypothesisBackend):
    async def analyze(self, *, role, **kwargs):
        if role == "recon":
            return WorkerReport(
                summary="proposal", status_update="proposed",
                proposed_hypotheses=[hypothesis(
                    "h-exhaust", "A hidden state controls validation", "state reaches verdict",
                    "no state flow", 80,
                )],
            ), {}
        raise RuntimeError("environment cannot execute discriminating test")


def test_exhausted_test_stop_condition_blocks_with_reason_instead_of_looping():
    with tempfile.TemporaryDirectory() as tmp:
        controller = InvestigationController(
            storage_root=Path(tmp) / "runs", rekit=NoopRekit(), workers=ExhaustingBackend(),
        )
        result = controller.run(RunRequest(
            _target(tmp), "Bound retries", worker_roles=("recon",), concurrency=1,
        ))
        state = result["hypothesisState"]["hypotheses"][0]
        assert state["status"] == "blocked"
        assert "Stop condition exhausted after 2 attempt" in state["lastReason"]
        test = result["hypothesisState"]["tests"][0]
        assert test["status"] == "blocked"
        assert test["attempts"] == 2
        item = [item for item in result["workItems"] if item["category"] == "hypothesis-test"][0]
        assert item["attempts"] == 2
