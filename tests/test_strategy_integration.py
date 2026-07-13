from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from rekit_factory.control import InvestigationController, RunRequest
from rekit_factory.models import ModelProfile, WorkerReport
from rekit_factory.store import FactoryLedger
from muster import resolve_run_dir


class StrategyBackend:
    def __init__(self):
        self.profile = ModelProfile(
            name="fixture", provider="test", model="fixture",
            base_url="https://invalid.test", api_key="secret",
        )

    async def analyze(self, *, role, **kwargs):
        next_actions = (
            ["[follow-up:format-specialist] Inspect the evidence-backed header"]
            if role == "recon" else []
        )
        return WorkerReport(
            summary=f"{role} complete", observations=["header evidence"],
            next_actions=next_actions, status_update="complete",
        ), {"inputTokens": 1, "outputTokens": 1}


class NoopRekit:
    pass


class StrategyIntegrationTests(unittest.TestCase):
    def _target(self, root: str) -> Path:
        target = Path(root) / "target"
        target.mkdir()
        (target / "sample.txt").write_text("header", encoding="utf-8")
        return target

    def test_strategy_plan_persists_and_dependency_ids_are_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs", rekit=NoopRekit(),
                workers=StrategyBackend(),
            )
            request = RunRequest(
                target=self._target(tmp), goal="Inspect the target",
                strategy="recon-then-analysis", concurrency=2,
                retries_per_worker=3, cost_units=50, max_workers=4,
            )
            run_dir = controller.create(request)
            snapshot = controller.snapshot(run_dir)
            plan = snapshot["meta"]["strategyPlan"]
            self.assertEqual("recon-then-analysis", plan["strategy"])
            self.assertEqual(3, plan["ceilings"]["retries_per_worker"])
            recon, analyst = snapshot["workItems"]
            self.assertEqual(2, len(snapshot["workers"]))
            self.assertEqual(2, len(snapshot["workItems"]))
            self.assertEqual([recon["id"]], analyst["dependsOn"])

    def test_evidence_follow_up_is_enqueued_once_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "runs"
            target = self._target(tmp)
            controller = InvestigationController(
                storage_root=storage, rekit=NoopRekit(), workers=StrategyBackend(),
            )
            run_dir = controller.create(RunRequest(
                target=target, goal="Inspect the target", strategy="recon-analysis",
                concurrency=2, max_workers=3, cost_units=30,
            ))
            paths = resolve_run_dir(run_dir)
            with FactoryLedger(paths.db_path) as ledger:
                leased = ledger.lease_next_actionable(paths.run_id)
                self.assertIsNotNone(leased)  # simulate a process dying during fan-out
            first = asyncio.run(controller.drive(run_dir))
            self.assertEqual("completed", first["run"]["status"])
            self.assertEqual(3, len(first["workers"]))
            adaptive = [item for item in first["workItems"]
                        if item["payload"].get("origin") == "worker-proposal"]
            self.assertEqual(1, len(adaptive))
            self.assertTrue(adaptive[0]["payload"]["evidenceIds"])

            restarted = InvestigationController(
                storage_root=storage, rekit=NoopRekit(), workers=StrategyBackend(),
            )
            second = asyncio.run(restarted.drive(run_dir))
            adaptive = [item for item in second["workItems"]
                        if item["payload"].get("origin") == "worker-proposal"]
            self.assertEqual(1, len(adaptive))
            self.assertEqual(3, len(second["workers"]))

    def test_request_rejects_unknown_strategy_and_invalid_ceilings(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._target(tmp)
            with self.assertRaisesRegex(ValueError, "unknown worker strategy"):
                RunRequest(target, "goal", strategy="unknown").validate()
            with self.assertRaisesRegex(ValueError, "concurrency"):
                RunRequest(target, "goal", concurrency=4, max_workers=2).validate()


if __name__ == "__main__":
    unittest.main()
