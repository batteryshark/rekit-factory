from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rekit_factory.api import FactoryServer
from rekit_factory.control import (
    InvestigationController,
    RunRequest,
    enforce_model_profile_concurrency,
)
from rekit_factory.models import ModelProfile, WorkerReport


class ProfileBackend:
    def __init__(self, name: str, concurrency_limit: int):
        self.profile = ModelProfile(
            name=name,
            provider="test",
            model=f"model-{name}",
            base_url="https://private-model.invalid/v1",
            api_key="credential-must-stay-private",
            concurrency_limit=concurrency_limit,
        )
        self.calls = 0

    async def analyze(self, *, role, **kwargs):
        self.calls += 1
        return WorkerReport(
            summary=f"{role} complete",
            observations=[],
            next_actions=[],
            status_update="complete",
        ), {"inputTokens": 1, "outputTokens": 1}


class NoopRekit:
    pass


class ProfileConcurrencyControlTests(unittest.TestCase):
    def _target(self, root: str) -> Path:
        target = Path(root) / "target"
        target.mkdir()
        (target / "fixture.txt").write_text("fixture", encoding="utf-8")
        return target

    def _controller(self, storage: Path, **profiles: int) -> InvestigationController:
        return InvestigationController(
            storage_root=storage,
            rekit=NoopRekit(),
            workers={name: ProfileBackend(name, limit) for name, limit in profiles.items()},
        )

    def _request(
        self, url: str, payload: dict | None = None, *, expected: int = 200,
    ) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            with urlopen(request, timeout=5) as response:
                self.assertEqual(expected, response.status)
                return json.loads(response.read())
        except HTTPError as exc:
            self.assertEqual(expected, exc.code)
            return json.loads(exc.read())

    def test_controller_rejects_before_creation_and_applies_each_named_profile_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "runs"
            target = self._target(tmp)
            controller = self._controller(storage, wide=4, tight=2)

            with self.assertRaisesRegex(
                ValueError, "model profile 'tight' ceiling 2",
            ) as denied:
                controller.create(RunRequest(
                    target=target,
                    goal="Reject before durable creation",
                    worker_roles=("analyst",),
                    concurrency=3,
                    model_profile="tight",
                ))
            self.assertFalse(storage.exists())
            self.assertNotIn("credential-must-stay-private", str(denied.exception))
            self.assertNotIn("private-model.invalid", str(denied.exception))

            exact = controller.create(RunRequest(
                target=target,
                goal="Permit the exact profile ceiling",
                worker_roles=("analyst",),
                concurrency=2,
                model_profile="tight",
            ))
            wider = controller.create(RunRequest(
                target=target,
                goal="Apply the independently selected wider profile",
                worker_roles=("analyst",),
                concurrency=3,
                model_profile="wide",
            ))
            self.assertEqual("tight", controller.snapshot(exact)["meta"]["modelProfile"]["name"])
            self.assertEqual("wide", controller.snapshot(wider)["meta"]["modelProfile"]["name"])

    def test_resume_rechecks_persisted_plan_before_mutating_or_dispatching(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "runs"
            target = self._target(tmp)
            original = self._controller(storage, selected=4)
            run_dir = original.create(RunRequest(
                target=target,
                goal="Persist a four-worker fan-out ceiling",
                worker_roles=("analyst",),
                concurrency=4,
                model_profile="selected",
            ))
            before = original.snapshot(run_dir)

            stricter = self._controller(storage, selected=2)
            with self.assertRaisesRegex(
                ValueError, "model profile 'selected' ceiling 2",
            ):
                asyncio.run(stricter.drive(run_dir))

            after = stricter.snapshot(run_dir)
            self.assertEqual(before["run"]["status"], after["run"]["status"])
            self.assertEqual(before["events"], after["events"])
            self.assertEqual(0, stricter.workers.calls)

    def test_http_rejects_crafted_over_limit_request_without_leaving_a_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "runs"
            target = self._target(tmp)
            controller = self._controller(storage, bounded=1)
            server = FactoryServer(("127.0.0.1", 0), controller)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                denied = self._request(base + "/api/runs", {
                    "target": str(target),
                    "goal": "Crafted request must reach the controller policy",
                    "workerRoles": ["analyst"],
                    "concurrency": 2,
                    "modelProfile": "bounded",
                }, expected=400)
                self.assertIn("model profile 'bounded' ceiling 1", denied["error"])
                self.assertLess(len(denied["error"]), 256)
                self.assertNotIn("credential-must-stay-private", denied["error"])
                self.assertNotIn("private-model.invalid", denied["error"])
                self.assertFalse(storage.exists())

                accepted = self._request(base + "/api/runs", {
                    "target": str(target),
                    "goal": "Exact limit remains launchable",
                    "workerRoles": ["analyst"],
                    "concurrency": 1,
                    "modelProfile": "bounded",
                }, expected=202)
                run_id = accepted["run"]["id"]
                for _ in range(100):
                    snapshot = self._request(base + f"/api/runs/{run_id}")
                    if snapshot["run"]["status"] == "completed":
                        break
                    time.sleep(0.01)
                self.assertEqual("completed", snapshot["run"]["status"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_http_resume_surfaces_stricter_runtime_profile_before_supervisor_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "runs"
            target = self._target(tmp)
            original = self._controller(storage, selected=4)
            run_dir = original.create(RunRequest(
                target=target,
                goal="Resume must use the current named profile ceiling",
                worker_roles=("analyst",),
                concurrency=4,
                model_profile="selected",
            ))
            before = original.snapshot(run_dir)
            run_id = before["run"]["id"]

            stricter = self._controller(storage, selected=2)
            server = FactoryServer(("127.0.0.1", 0), stricter)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                denied = self._request(
                    base + f"/api/runs/{run_id}/resume", {}, expected=400,
                )
                self.assertIn("model profile 'selected' ceiling 2", denied["error"])
                after = stricter.snapshot(run_dir)
                self.assertEqual(before["run"]["status"], after["run"]["status"])
                self.assertEqual(before["events"], after["events"])
                self.assertEqual(0, stricter.workers.calls)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_policy_error_bounds_untrusted_profile_name_and_exposes_no_config(self):
        profile = ModelProfile(
            name="profile\n" + "x" * 500,
            provider="private-provider",
            model="private-model",
            base_url="https://private-model.invalid/secret-path",
            api_key="credential-must-stay-private",
            concurrency_limit=1,
        )
        with self.assertRaises(ValueError) as denied:
            enforce_model_profile_concurrency(profile, 10**10_000)
        message = str(denied.exception)
        self.assertLess(len(message), 256)
        self.assertNotIn("\n", message)
        self.assertIn("ceiling 1", message)
        for secret in (profile.api_key, profile.base_url, profile.model, profile.provider):
            self.assertNotIn(secret, message)


if __name__ == "__main__":
    unittest.main()
