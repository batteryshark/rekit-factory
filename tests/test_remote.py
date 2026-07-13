from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from rekit_factory.rekit_client import ToolManifest, ToolResult
from rekit_factory.remote import (
    ArtifactRecord,
    InvocationRequest,
    InvocationResult,
    LocalRekitWorker,
    WorkerCapabilities,
    WorkerEvent,
)
from rekit_factory.scope import (
    ActionAuthority, AuthorizedScope, ScopeApproval, ScopeEnvelope, TargetGrant,
)


class FakeRekit:
    def __init__(self, *, risky: bool = False):
        self.risky = risky
        self.calls: list[tuple[str, Path, bool]] = []

    def manifest(self, tool_id: str) -> ToolManifest:
        return ToolManifest(
            id=tool_id,
            name="Fixture",
            description="Fixture tool",
            safety_tier=2 if self.risky else 0,
            executes_input="full" if self.risky else "no",
            network="none",
        )

    def list_tools(self) -> list[ToolManifest]:
        return [self.manifest("fixture-scan")]

    def run(self, tool_id: str, target: Path, *, allow_dynamic: bool = False) -> ToolResult:
        self.calls.append((tool_id, target, allow_dynamic))
        return ToolResult(0, "fixture output", "", f"rekit run {tool_id} <target>")


class RemoteEnvelopeTests(unittest.TestCase):
    def assert_round_trip(self, envelope, envelope_type):
        wire = json.loads(envelope.to_json())
        self.assertEqual(1, wire["schema_version"])
        self.assertEqual(envelope, envelope_type.from_dict(wire))

    def test_all_envelopes_round_trip_through_json(self):
        request = InvocationRequest(
            invocation_id="invoke-1",
            run_id="run-1",
            work_item_id="work-1",
            tool_id="fixture-scan",
            target_path="input/fixture.exe",
            target_sha256="a" * 64,
            arguments=("--format", "json"),
            network_policy="none",
        )
        capabilities = WorkerCapabilities(
            worker_id="worker-1",
            platform="windows",
            architecture="x86_64",
            tools=("fixture-scan",),
            isolation="vm",
        )
        event = WorkerEvent(
            invocation_id="invoke-1",
            run_id="run-1",
            work_item_id="work-1",
            worker_id="worker-1",
            sequence=1,
            kind="tool.completed",
            message="Fixture completed",
            payload={"exitCode": 0},
        )
        artifact = ArtifactRecord(
            path="reports/fixture.json", sha256="b" * 64, size=42,
            media_type="application/json",
        )
        result = InvocationResult(
            invocation_id="invoke-1",
            run_id="run-1",
            work_item_id="work-1",
            worker_id="worker-1",
            status="done",
            exit_code=0,
            stdout="ok",
            stderr="",
            artifacts=(artifact,),
        )

        for envelope, envelope_type in (
            (request, InvocationRequest),
            (capabilities, WorkerCapabilities),
            (event, WorkerEvent),
            (artifact, ArtifactRecord),
            (result, InvocationResult),
        ):
            with self.subTest(envelope_type=envelope_type.__name__):
                self.assert_round_trip(envelope, envelope_type)

    def test_rejects_unknown_versions_and_non_json_event_payloads(self):
        with self.assertRaisesRegex(ValueError, "schema_version"):
            InvocationRequest.from_dict({"schema_version": 2})
        with self.assertRaisesRegex(ValueError, "JSON"):
            WorkerEvent(
                invocation_id="invoke-1", run_id="run-1", work_item_id="work-1",
                worker_id="worker-1", sequence=1, kind="log", message="bad",
                payload={"value": object()},
            )

    def test_validates_hashes_sequences_and_artifact_paths(self):
        with self.assertRaisesRegex(ValueError, "target_sha256"):
            InvocationRequest(
                run_id="run-1", work_item_id="work-1", tool_id="scan",
                target_path="input/a", target_sha256="not-a-digest",
            )
        with self.assertRaisesRegex(ValueError, "positive integer"):
            WorkerEvent(
                invocation_id="invoke-1", run_id="run-1", work_item_id="work-1",
                worker_id="worker-1", sequence=0, kind="log", message="bad",
            )
        with self.assertRaisesRegex(ValueError, "output root"):
            ArtifactRecord(path="../host.txt", sha256="a" * 64, size=1)

    def test_capabilities_reject_duplicate_tools(self):
        with self.assertRaisesRegex(ValueError, "duplicates"):
            WorkerCapabilities(
                worker_id="worker", platform="linux", architecture="x86_64",
                tools=("scan", "scan"),
            )

    def test_local_worker_preserves_provenance_and_approval_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fixture.bin"
            target.write_bytes(b"fixture")
            rekit = FakeRekit(risky=True)
            worker = LocalRekitWorker(rekit, worker_id="worker-local")
            envelope = ScopeEnvelope(
                scope_id="scope-local-envelope", revision=1,
                valid_from="2026-07-01T00:00:00Z", valid_until="2026-08-01T00:00:00Z",
                targets=(TargetGrant.from_path(target),),
                actions=(ActionAuthority.READ_LOCAL_TARGET,
                         ActionAuthority.EXECUTE_UNTRUSTED),
            )
            scope = AuthorizedScope(envelope, ScopeApproval(
                scope_id=envelope.scope_id, revision=1,
                content_digest=envelope.content_digest, approved_by="test-operator",
                approved_at="2026-07-01T00:00:00Z", expires_at="2026-08-01T00:00:00Z",
                rationale="Exact local envelope fixture",
            ))
            common = {
                "target_sha256": TargetGrant.from_path(target).content_sha256,
                "scope_digest": scope.envelope.content_digest,
                "scope_revision": scope.to_dict(),
                "requested_actions": (
                    ActionAuthority.READ_LOCAL_TARGET.value,
                    ActionAuthority.EXECUTE_UNTRUSTED.value,
                ),
            }
            denied = InvocationRequest(
                invocation_id="invoke-1", run_id="run-1", work_item_id="work-1",
                tool_id="fixture-scan", target_path=str(target), **common,
            )
            with self.assertRaises(PermissionError):
                worker.invoke(denied)

            allowed = InvocationRequest(
                invocation_id="invoke-1", run_id="run-1", work_item_id="work-1",
                tool_id="fixture-scan", target_path=str(target), approval_id="approval-1",
                **common,
            )
            result = worker.invoke(allowed)

            self.assertEqual("invoke-1", result.invocation_id)
            self.assertEqual("run-1", result.run_id)
            self.assertEqual("work-1", result.work_item_id)
            self.assertEqual("worker-local", result.worker_id)
            self.assertTrue(rekit.calls[0][2])


if __name__ == "__main__":
    unittest.main()
