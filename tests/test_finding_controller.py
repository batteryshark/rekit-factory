from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from muster import resolve_run_dir

from rekit_factory.control import (
    InvestigationController,
    RunRequest,
    _project_memory_log,
    _target_snapshot,
)
from rekit_factory.findings import (
    FindingMemory,
    FindingProposal,
    ObservationEvidence,
    ReproductionAttempt,
    ReproductionRecipe,
    ReproductionResultProposal,
)
from rekit_factory.hypotheses import (
    DiscriminatingTestProposal,
    HypothesisProposal,
    HypothesisUpdate,
)
from rekit_factory.memory import EvidenceRef
from rekit_factory.models import ModelProfile, WorkerReport
from rekit_factory.store import FactoryLedger


class NoopRekit:
    pass


def _ref(identifier: str) -> EvidenceRef:
    return EvidenceRef("artifact", f"sha256:{identifier}")


def _hypothesis() -> HypothesisProposal:
    return HypothesisProposal(
        id="h-parser", claim="A length field controls parser allocation", scope="target",
        expected_observation="Changing the field changes allocation",
        falsifier="The field does not reach allocation", confidence=.65,
        references=[_ref("hypothesis")],
        proposed_test=DiscriminatingTestProposal(
            id="test-h-parser", objective="Trace the length", method="fixture trace",
            expected_observation="Length reaches allocation",
            falsifying_observation="Length is ignored", information_gain=80, risk=0,
            cost_units=5,
        ),
    )


def _finding() -> FindingProposal:
    observation = _ref("observation")
    fixture = _ref("fixture")
    return FindingProposal(
        id="f-length", hypothesis_id="h-parser", scope="target",
        observations=[ObservationEvidence(
            observation="Length reaches allocation without a bound", references=[observation],
        )], affected_component="record parser",
        impact_claim="A crafted record causes over-allocation",
        assumptions=["record reaches parser"], known_uncertainty="allocator limit unknown",
        finding_type="vulnerability", consequence="high", confidence=.7,
        references=[observation, fixture],
        recipe=ReproductionRecipe(
            id="recipe-length-v1", steps=["Build fixture", "Run staged record"],
            staged_inputs=[fixture], expected_observation="Allocation exceeds configured input limit",
            clean_environment_requirements=["fresh build", "empty process state"],
        ),
    )


class FindingBackend:
    def __init__(self, outcome="success"):
        self.profile = ModelProfile(
            name="finding-fixture", provider="test", model="fixture",
            base_url="https://invalid.test", api_key="secret",
        )
        self.outcome = outcome
        self.calls: list[tuple[str, str, str]] = []

    async def analyze(self, *, role, goal, tool_context, **kwargs):
        self.calls.append((role, goal, tool_context))
        if role == "recon":
            return WorkerReport(
                summary="structured candidate", status_update="proposed",
                proposed_hypotheses=[_hypothesis()], proposed_findings=[_finding()],
            ), {}
        if role.startswith("hypothesis-test:"):
            return WorkerReport(
                summary="test complete", status_update="supported",
                hypothesis_updates=[HypothesisUpdate(
                    hypothesis_id="h-parser", test_id="test-h-parser", status="supported",
                    confidence=.8, observations=["Length reaches allocation"],
                    references=[_ref("hypothesis-test")], reason="fixture trace",
                )],
            ), {}
        assert role == "finding-validator:f-length"
        return WorkerReport(
            summary="observable result", status_update="validated",
            reproduction_results=[ReproductionResultProposal(
                finding_id="f-length", attempt_id="repro-f-length-1",
                outcome=self.outcome,
                observations=["Allocation exceeded limit" if self.outcome == "success"
                              else "Allocation remained bounded"],
                references=[_ref(f"reproduction-{self.outcome}")],
                environmental_differences=[] if self.outcome == "success"
                else ["fresh allocator configuration contradicted the claim"],
            )],
        ), {}


def _target(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    (target / "fixture.bin").write_bytes(b"fixture")
    return target


def test_valid_clean_reproduction_is_independent_minimal_and_restart_idempotent(tmp_path):
    target = _target(tmp_path)
    first_backend = FindingBackend()
    controller = InvestigationController(
        storage_root=tmp_path / "runs", rekit=NoopRekit(), workers=first_backend,
    )
    run_dir = controller.create(RunRequest(
        target, "Assess parser", worker_roles=("recon",), concurrency=1,
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
        validator = dict(ledger.conn.execute(
            "select * from work_items where category='finding-validation'"
        ).fetchone())
        validator_payload = json.loads(validator["payload_json"])
        assert validator_payload["workerId"] != validator_payload["originWorkerId"]
        assert validator_payload["validatorSessionId"] != \
               f"session:{validator_payload['originWorkerId']}"
        assert validator_payload["cleanEnvironment"] is True
        assert validator_payload["validatorEnvironmentId"].startswith("clean:")

    # A fresh controller resumes durable validator work without creating another attempt.
    resumed_backend = FindingBackend()
    resumed = InvestigationController(
        storage_root=tmp_path / "runs", rekit=NoopRekit(), workers=resumed_backend,
    )
    result = asyncio.run(resumed.drive(run_dir))
    assert [item["id"] for item in result["findingState"]["validated"]] == ["f-length"]
    assert len(result["findingState"]["attempts"]) == 1
    validation_items = [item for item in result["workItems"]
                        if item["category"] == "finding-validation"]
    assert len(validation_items) == 1
    assert validation_items[0]["attempts"] == 1
    validator_calls = [call for call in resumed_backend.calls
                       if call[0].startswith("finding-validator:")]
    assert len(validator_calls) == 1
    _, goal, context = validator_calls[0]
    assert "crafted record causes over-allocation" not in goal
    assert "confidence" not in goal.lower()
    assert "originating reasoning withheld" in context
    assert "A length field controls parser allocation" not in context


def test_false_positive_records_contradictory_attempt_and_stays_out_of_validated(tmp_path):
    controller = InvestigationController(
        storage_root=tmp_path / "runs", rekit=NoopRekit(),
        workers=FindingBackend(outcome="contradictory"),
    )
    result = controller.run(RunRequest(
        _target(tmp_path), "Assess parser", worker_roles=("recon",), concurrency=1,
    ))
    assert result["findingState"]["validated"] == []
    assert result["findingState"]["findings"][0]["status"] == "inconclusive"
    assert result["findingState"]["attempts"][0]["outcome"] == "contradictory"
    assert result["findingState"]["attempts"][0]["environment"]["clean"] is True


def test_scheduler_continues_after_a_success_that_fails_independence_policy(tmp_path):
    target = _target(tmp_path)
    controller = InvestigationController(
        storage_root=tmp_path / "runs", rekit=NoopRekit(), workers=FindingBackend(),
    )
    run_dir = controller.create(RunRequest(
        target, "Assess parser", worker_roles=("recon",), concurrency=1,
    ))
    paths = resolve_run_dir(run_dir)
    with FactoryLedger(paths.db_path) as ledger:
        parent = dict(ledger.lease_next_actionable(paths.run_id))
        ctx = SimpleNamespace(
            state=SimpleNamespace(run_id=paths.run_id, iteration=0),
            deps=SimpleNamespace(
                ledger=ledger, paths=paths,
                scratch={"targetSnapshot": _target_snapshot(target)},
            ),
        )
        asyncio.run(controller._worker_handler(ctx, parent))
        finding = FindingMemory(_project_memory_log(paths))
        finding.record_attempt(ReproductionAttempt(
            id="repro-f-length-1", finding_id="f-length",
            recipe_id="recipe-length-v1", outcome="success",
            worker_id=json.loads(parent["payload_json"])["workerId"],
            session_id=f"session:{json.loads(parent['payload_json'])['workerId']}",
            environment_id="clean:origin", clean_environment=True,
            model_profile="finding-fixture", observations=["Allocation exceeded limit"],
            references=[_ref("origin-reproduction")],
        ))

        controller._schedule_remaining_reproduction(
            ledger, paths, paths.run_id, parent, "finding-fixture", (), "f-length",
        )
        validation_items = ledger.conn.execute(
            "select payload_json from work_items where category='finding-validation' "
            "order by id"
        ).fetchall()
        attempt_ids = {
            json.loads(item["payload_json"])["reproductionAttemptId"]
            for item in validation_items
        }
        assert attempt_ids == {"repro-f-length-1", "repro-f-length-2"}
