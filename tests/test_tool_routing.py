from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rekit_factory.cli import _load_remote_workers, parser
from rekit_factory.control import InvestigationController, RunRequest
from rekit_factory.models import ModelProfile, WorkerReport
from rekit_factory.rekit_client import ToolManifest, ToolResult
from rekit_factory.remote import (
    ArtifactRecord, InvocationRequest, InvocationResult, WorkerCapabilities,
)
from rekit_factory.scope import (
    ActionAuthority, author_scope, hash_path,
)
from rekit_factory.tool_routing import (
    RemoteWorkerBinding, ToolRoute, ToolWorkerRouter, WorkerRequirements,
)


class FakeTransport:
    def __init__(self, worker_id, *, tools=("scan",), platform="windows",
                 architecture="x86_64", isolation="vm"):
        self._capabilities = WorkerCapabilities(
            worker_id=worker_id,
            platform=platform,
            architecture=architecture,
            tools=tools,
            isolation=isolation,
        )
        self.requests: list[InvocationRequest] = []

    def capabilities(self):
        return self._capabilities

    def invoke(self, request):
        self.requests.append(request)
        return InvocationResult(
            invocation_id=request.invocation_id,
            run_id=request.run_id,
            work_item_id=request.work_item_id,
            worker_id=self._capabilities.worker_id,
            status="done",
            exit_code=0,
            stdout="remote fixture output",
            stderr="",
            artifacts=(ArtifactRecord(
                path="reports/scan.json", sha256="b" * 64, size=42,
                media_type="application/json",
            ),),
        )

    def cancel(self, invocation_id):
        return False

    def attach_url(self, invocation_id):
        return None


class FakeRekit:
    def __init__(self):
        self.calls = 0

    def manifest(self, tool_id):
        return ToolManifest(tool_id, tool_id, "fixture", 0, "no", "none")

    def list_tools(self):
        return [self.manifest("scan")]

    def run(self, tool_id, target, *, allow_dynamic=False):
        self.calls += 1
        return ToolResult(0, "local", "", "local scan")


class FakeBackend:
    profile = ModelProfile(
        name="fake", provider="test", model="test",
        base_url="https://model.invalid", api_key="not-persisted",
    )

    async def analyze(self, **kwargs):
        return WorkerReport(
            summary="done", observations=[], next_actions=[], status_update="done",
        ), {}


class ToolRoutingTests(unittest.TestCase):
    def test_cli_composition_loads_remote_token_only_from_environment(self):
        target_hash = "a" * 64
        args = parser().parse_args(["serve", "--remote-worker-env", "LABWORKER"])
        transport = FakeTransport("lab-worker")
        environment = {
            "LABWORKER_URL": "https://worker.internal/v1",
            "LABWORKER_TOKEN": "secret-worker-token",
            "LABWORKER_STAGED_TARGETS": (
                '{"' + target_hash + '":"input/staged/fixture.exe"}'
            ),
            "LABWORKER_PRIORITY": "7",
        }
        with patch.dict("os.environ", environment, clear=True), patch(
            "rekit_factory.cli.HTTPWorkerTransport", return_value=transport,
        ) as constructor:
            bindings = _load_remote_workers(args)
        self.assertEqual(1, len(bindings))
        self.assertEqual(7, bindings[0].priority)
        self.assertEqual("input/staged/fixture.exe", bindings[0].staged_targets[target_hash])
        constructor.assert_called_once_with(
            "https://worker.internal/v1", auth_token="secret-worker-token",
        )
        self.assertNotIn("secret-worker-token", repr(bindings))

    def test_selection_is_deterministic_and_capability_compatible(self):
        local = FakeTransport(
            "local", platform="local", architecture="native", isolation="host",
        )
        target_hash = "a" * 64
        later = RemoteWorkerBinding(
            FakeTransport("worker-z"), {target_hash: "input/a/fixture.exe"}, priority=20,
        )
        preferred = RemoteWorkerBinding(
            FakeTransport("worker-a"), {target_hash: "input/a/fixture.exe"}, priority=10,
        )
        router = ToolWorkerRouter(local, (later, preferred))

        route = router.select("scan", "/local/fixture", target_hash)
        self.assertTrue(route.remote)
        self.assertEqual("worker-a", route.capabilities.worker_id)
        self.assertEqual("input/a/fixture.exe", route.target_path)

        compatible = router.select(
            "scan", "/local/fixture", target_hash,
            requirements=WorkerRequirements(
                platform="windows", architecture="x86_64", isolation="vm",
            ),
        )
        self.assertEqual("worker-a", compatible.capabilities.worker_id)
        with self.assertRaisesRegex(LookupError, "no capability-compatible"):
            router.select(
                "scan", "/local/fixture", target_hash,
                requirements=WorkerRequirements(platform="linux", require_remote=True),
            )

    def test_selected_remote_never_falls_back_when_target_is_not_explicitly_staged(self):
        local = FakeTransport("local", platform="local", architecture="native")
        router = ToolWorkerRouter(
            local, (RemoteWorkerBinding(FakeTransport("remote"), {}),),
        )
        with self.assertRaisesRegex(PermissionError, "not explicitly staged"):
            router.select("scan", "/local/fixture", "a" * 64)

    def test_invocation_carries_exact_scope_intent_and_policies(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fixture.exe"
            target.write_bytes(b"fixture")
            endpoint = "https://lab.example.test:443/api"
            scope = author_scope(
                target,
                scope_id="remote-route", revision=1,
                actions=(
                    ActionAuthority.READ_LOCAL_TARGET,
                    ActionAuthority.EXECUTE_UNTRUSTED,
                    ActionAuthority.NETWORK_ACCESS,
                ),
                endpoints=(endpoint,),
                account_refs=("account:lab",), credential_use=True,
                approved_by="operator:route", rationale="Exact staged lab fixture",
                approved_at="2026-07-13T05:00:00Z",
                valid_until="2026-07-14T05:00:00Z",
                expires_at="2026-07-14T05:00:00Z",
            )
            transport = FakeTransport("remote")
            route = ToolRoute(
                transport, transport.capabilities(), True, "input/hash/fixture.exe",
            )
            actions = (
                ActionAuthority.READ_LOCAL_TARGET,
                ActionAuthority.EXECUTE_UNTRUSTED,
                ActionAuthority.NETWORK_ACCESS,
            )
            request = route.invocation(
                run_id="run-1", work_item_id="work-1", invocation_id="tool-1",
                tool_id="scan", target_sha256=hash_path(target), scope=scope,
                actions=actions, approval_id="question-allow", endpoint=endpoint,
                account_ref="account:lab", uses_credentials=True,
            )
            self.assertEqual("restricted", request.network_policy)
            self.assertEqual("staged-input-read-only", request.mount_policy)
            self.assertEqual(scope.envelope.content_digest, request.scope_digest)
            self.assertEqual(scope.to_dict(), request.scope_revision)
            self.assertEqual(tuple(action.value for action in actions), request.requested_actions)
            self.assertEqual("account:lab", request.account_ref)
            self.assertTrue(request.uses_credentials)
            self.assertNotIn("api_key", request.to_json().lower())

    def test_controller_routes_to_remote_and_preserves_result_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            target_hash = hash_path(target)
            remote = FakeTransport("windows-analysis")
            binding = RemoteWorkerBinding(
                remote, {target_hash: "input/staged/fixture.txt"},
            )
            rekit = FakeRekit()
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs",
                rekit=rekit,
                workers=FakeBackend(),
                remote_tool_workers=(binding,),
            )
            result = controller.run(RunRequest(
                target, "scan the staged fixture", tools=("scan",),
                worker_roles=("analyst",),
            ))
            self.assertEqual(0, rekit.calls)
            self.assertEqual(1, len(remote.requests))
            request = remote.requests[0]
            self.assertEqual(target_hash, request.target_sha256)
            self.assertEqual("input/staged/fixture.txt", request.target_path)
            self.assertEqual("none", request.network_policy)
            self.assertEqual("staged-input-read-only", request.mount_policy)
            tool_item = next(item for item in result["workItems"]
                             if item["operation"] == "rekit-tool")
            self.assertEqual("windows-analysis", tool_item["payload"]["toolWorkerId"])
            self.assertTrue(tool_item["payload"]["requireRemote"])
            artifact = next(item for item in result["artifacts"]
                            if item["kind"] == "tool-output")
            self.assertIn("windows-analysis", artifact["metadata_json"])
            self.assertIn("reports/scan.json", artifact["metadata_json"])


if __name__ == "__main__":
    unittest.main()
