from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from dataclasses import asdict
import hashlib
import tempfile
import threading
import unittest
from unittest import mock

from rekit_factory.remote import (
    ArtifactRecord, InvocationRequest, InvocationResult, WorkerCapabilities,
    WorkerLeaseRequest, WorkerLeaseState,
)
from rekit_factory.remote_http import (
    HTTPWorkerTransport,
    RemoteWorkerError,
    RemoteWorkerHTTPServer,
)
from rekit_factory.scope import (
    ActionAuthority, AuthorizedScope, NetworkMode, ScopeApproval, ScopeEnvelope,
    TargetGrant, hash_path, normalize_endpoint, opaque_ref,
)


def remote_scope(target_hash: str, endpoint: str, *, account_refs=(), credential_use=False) -> AuthorizedScope:
    envelope = ScopeEnvelope(
        scope_id="scope-remote", revision=1,
        valid_from="2026-07-01T00:00:00Z", valid_until="2026-08-01T00:00:00Z",
        targets=(TargetGrant(target_hash, opaque_ref("target-path", "/controller/input")),),
        endpoints=(endpoint,), network_mode=NetworkMode.EXACT_ENDPOINTS,
        actions=(ActionAuthority.READ_LOCAL_TARGET, ActionAuthority.NETWORK_ACCESS),
        account_refs=account_refs,
        credential_use=credential_use,
    )
    return AuthorizedScope(envelope, ScopeApproval(
        scope_id=envelope.scope_id, revision=1, content_digest=envelope.content_digest,
        approved_by="operator-remote", approved_at="2026-07-01T00:00:00Z",
        expires_at="2026-08-01T00:00:00Z", rationale="Exact remote lab endpoint",
    ))


def offline_scope(target_hash: str) -> AuthorizedScope:
    envelope = ScopeEnvelope(
        scope_id="scope-offline", revision=1,
        valid_from="2026-07-01T00:00:00Z", valid_until="2026-08-01T00:00:00Z",
        targets=(TargetGrant(target_hash, opaque_ref("target-path", "/controller/input")),),
        actions=(ActionAuthority.READ_LOCAL_TARGET,), network_mode=NetworkMode.NONE,
    )
    return AuthorizedScope(envelope, ScopeApproval(
        scope_id=envelope.scope_id, revision=1, content_digest=envelope.content_digest,
        approved_by="operator-remote", approved_at="2026-07-01T00:00:00Z",
        expires_at="2026-08-01T00:00:00Z", rationale="Exact offline fixture",
    ))


class FixtureWorker:
    def __init__(self):
        self.requests: list[InvocationRequest] = []
        self.result_worker_id = "fixture-worker"
        self.artifact_data = b"fixture artifact"
        self.started = threading.Event()
        self.release: threading.Event | None = None
        self.result_manifest_digest: str | None | object = object()

    def capabilities(self) -> WorkerCapabilities:
        return WorkerCapabilities(
            worker_id="fixture-worker",
            platform="linux",
            architecture="x86_64",
            tools=("fixture-scan",),
            isolation="test",
        )

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        self.requests.append(request)
        self.started.set()
        if self.release is not None:
            self.release.wait(timeout=5)
        manifest_digest = (
            request.expected_manifest_digest
            if not isinstance(self.result_manifest_digest, (str, type(None)))
            else self.result_manifest_digest
        )
        return InvocationResult(
            invocation_id=request.invocation_id,
            run_id=request.run_id,
            work_item_id=request.work_item_id,
            worker_id=self.result_worker_id,
            status="done",
            exit_code=0,
            stdout=Path(request.target_path).read_text(encoding="utf-8"),
            stderr="",
            artifacts=(ArtifactRecord(
                path="reports/result.txt",
                sha256=hashlib.sha256(self.artifact_data).hexdigest(),
                size=len(self.artifact_data), media_type="text/plain",
            ),),
            lease_id=request.lease_id,
            manifest_digest=manifest_digest,
        )

    def setup_lease(self, request):
        return WorkerLeaseState(**asdict(request), status="ready")

    def reset_lease(self, request):
        return WorkerLeaseState(**asdict(request), status="ready")

    def teardown_lease(self, request):
        return WorkerLeaseState(**asdict(request), status="closed")

    def fetch_artifact(self, invocation_id, artifact):
        return self.artifact_data

    def cancel(self, invocation_id: str) -> bool:
        return False

    def attach_url(self, invocation_id: str) -> str | None:
        return None


@contextmanager
def running_server(root: Path, **kwargs):
    worker = FixtureWorker()
    server = RemoteWorkerHTTPServer(
        ("127.0.0.1", 0), worker,
        auth_token="test-token",
        input_root=root,
        **kwargs,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield worker, HTTPWorkerTransport(
            f"http://127.0.0.1:{server.server_port}",
            auth_token="test-token",
            poll_interval=0.005,
            allow_loopback_http=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class RemoteHTTPTransportTests(unittest.TestCase):
    def request(self, **overrides) -> InvocationRequest:
        values = {
            "invocation_id": "invoke-http-1",
            "run_id": "run-1",
            "work_item_id": "work-1",
            "tool_id": "fixture-scan",
            "target_path": "fixture.txt",
            "target_sha256": "a" * 64,
            "mount_policy": "staged-input-read-only",
            "lease_id": "lease-http-1",
        }
        values.update(overrides)
        return InvocationRequest(**values)

    def scoped_request(self, target: Path, **overrides) -> InvocationRequest:
        digest = hash_path(target)
        scope = offline_scope(digest)
        return self.request(
            target_sha256=digest,
            scope_digest=scope.envelope.content_digest,
            scope_revision=scope.to_dict(),
            requested_actions=(ActionAuthority.READ_LOCAL_TARGET.value,),
            **overrides,
        )

    def invoke(self, client: HTTPWorkerTransport,
               request: InvocationRequest) -> InvocationResult:
        lease = WorkerLeaseRequest(
            lease_id=request.lease_id, run_id=request.run_id,
            work_item_id=request.work_item_id, worker_id="fixture-worker",
            route_sha256="a" * 64,
        )
        state = client.setup_lease(lease)
        if state.status != "ready":
            state = client.reset_lease(lease)
        self.assertEqual("ready", state.status)
        return client.invoke(request)

    def test_discovers_capabilities_invokes_and_resumes_ordered_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fixture.txt").write_text("benign fixture", encoding="utf-8")
            with running_server(root) as (worker, client):
                capabilities = client.capabilities()
                self.assertEqual("fixture-worker", capabilities.worker_id)
                self.assertEqual(("fixture-scan",), capabilities.tools)

                result = self.invoke(client, self.scoped_request(root / "fixture.txt"))
                self.assertEqual("done", result.status)
                self.assertEqual("benign fixture", result.stdout)
                self.assertEqual("run-1", result.run_id)
                self.assertEqual("lease-http-1", result.lease_id)
                self.assertEqual(worker.artifact_data, client.fetch_artifact(
                    result.invocation_id, result.artifacts[0],
                ))
                self.assertEqual(
                    (root / "fixture.txt").resolve(),
                    Path(worker.requests[0].target_path).resolve(),
                )

                events = client.events("invoke-http-1")
                self.assertEqual([1, 2, 3], [event.sequence for event in events])
                self.assertEqual("invocation.accepted", events[0].kind)
                self.assertEqual("invocation.done", events[-1].kind)
                self.assertEqual((events[-1],), client.events("invoke-http-1", after=2))

    def test_requires_correct_bearer_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with running_server(root) as (_, client):
                wrong = HTTPWorkerTransport(
                    client.base_url, auth_token="wrong-token", allow_loopback_http=True,
                )
                with self.assertRaisesRegex(RemoteWorkerError, "401"):
                    wrong.capabilities()

    def test_rejects_unstaged_paths_and_network_relaxation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fixture.txt").write_text("fixture", encoding="utf-8")
            with running_server(root) as (_, client):
                with self.assertRaisesRegex(RemoteWorkerError, "relative to the staged input"):
                    self.invoke(client, self.request(invocation_id="invoke-absolute", target_path="/tmp/a"))
                with self.assertRaisesRegex(RemoteWorkerError, "network policy"):
                    self.invoke(client, self.request(
                        invocation_id="invoke-network", network_policy="unrestricted"
                    ))

    def test_enforces_request_body_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fixture.txt").write_text("fixture", encoding="utf-8")
            with running_server(root, max_body=300) as (_, client):
                with self.assertRaisesRegex(RemoteWorkerError, "request body"):
                    self.invoke(client, self.request(arguments=("x" * 500,)))

    def test_rejects_worker_result_with_mismatched_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fixture.txt").write_text("fixture", encoding="utf-8")
            with running_server(root) as (worker, client):
                worker.result_worker_id = "spoofed-worker"
                result = self.invoke(client, self.scoped_request(
                    root / "fixture.txt", invocation_id="invoke-spoofed",
                ))
                self.assertEqual("failed", result.status)
                self.assertEqual("fixture-worker", result.worker_id)
                self.assertIn("ValueError", result.stderr)

    def test_rejects_missing_or_spoofed_remote_manifest_attestation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            expected = "a" * 64
            for attestation in (None, "b" * 64):
                with self.subTest(attestation=attestation), running_server(root) as (worker, client):
                    worker.result_manifest_digest = attestation
                    result = self.invoke(client, self.scoped_request(
                        root / "fixture.txt",
                        invocation_id="invoke-attestation-" + (attestation or "missing")[:8],
                        expected_manifest_digest=expected,
                    ))
                    self.assertEqual("failed", result.status)
                    self.assertIsNone(result.manifest_digest)
                    self.assertIn("ValueError", result.stderr)

    def test_controller_rejects_spoofed_manifest_from_remote_server(self):
        client = HTTPWorkerTransport("https://worker.invalid", auth_token="token")
        request = self.request(expected_manifest_digest="a" * 64)
        for status, exit_code in (("done", 0), ("failed", 7)):
            with self.subTest(status=status):
                spoofed = InvocationResult(
                    invocation_id=request.invocation_id,
                    run_id=request.run_id,
                    work_item_id=request.work_item_id,
                    worker_id="fixture-worker",
                    status=status, exit_code=exit_code, stdout="spoof", stderr="",
                    lease_id=request.lease_id,
                    manifest_digest="b" * 64,
                )
                with mock.patch.object(client, "_request", side_effect=[
                    (202, {"status": "accepted"}), (200, spoofed.to_dict()),
                ]):
                    with self.assertRaisesRegex(RemoteWorkerError, "manifest digest"):
                        client.invoke(request)

    def test_auth_and_policy_configuration_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = FixtureWorker()
            with self.assertRaisesRegex(ValueError, "auth_token"):
                RemoteWorkerHTTPServer(
                    ("127.0.0.1", 0), worker, auth_token="", input_root=tmp,
                )
            with self.assertRaisesRegex(ValueError, "network policy"):
                RemoteWorkerHTTPServer(
                    ("127.0.0.1", 0), worker, auth_token="token", input_root=tmp,
                    allowed_network_policies=(),
                )
        with self.assertRaisesRegex(ValueError, "plaintext HTTP"):
            HTTPWorkerTransport("http://worker.internal/v1", auth_token="token")
        with self.assertRaisesRegex(ValueError, "plaintext HTTP"):
            HTTPWorkerTransport("http://127.0.0.1:8765", auth_token="token")
        HTTPWorkerTransport(
            "http://127.0.0.1:8765", auth_token="token", allow_loopback_http=True,
        )
        for url, message in (
            ("https://user:secret@worker.invalid", "userinfo"),
            ("https://worker.invalid?mode=unsafe", "query or fragment"),
            ("https://worker.invalid#fragment", "query or fragment"),
            ("https://worker.invalid/prefix", "path prefixes"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                HTTPWorkerTransport(url, auth_token="token")

    def test_lease_lifecycle_round_trips_exact_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            with running_server(Path(tmp)) as (_, client):
                request = WorkerLeaseRequest(
                    lease_id="lease-roundtrip", run_id="run-1", work_item_id="work-1",
                    worker_id="fixture-worker", route_sha256="a" * 64,
                )
                self.assertEqual("ready", client.setup_lease(request).status)
                self.assertEqual("ready", client.reset_lease(request).status)
                self.assertEqual("closed", client.teardown_lease(request).status)

    def test_dirty_lease_survives_server_restart_and_requires_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            request = self.scoped_request(target, invocation_id="invoke-restart")
            lease = WorkerLeaseRequest(
                lease_id=request.lease_id, run_id=request.run_id,
                work_item_id=request.work_item_id, worker_id="fixture-worker",
                route_sha256="a" * 64,
            )
            with running_server(root) as (_, client):
                self.assertEqual("done", self.invoke(client, request).status)
            with running_server(root) as (_, resumed):
                self.assertEqual("dirty", resumed.setup_lease(lease).status)
                self.assertEqual("ready", resumed.reset_lease(lease).status)

    def test_ready_lease_is_demoted_to_dirty_after_server_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lease = WorkerLeaseRequest(
                lease_id="lease-ready-restart", run_id="run-1", work_item_id="work-1",
                worker_id="fixture-worker", route_sha256="a" * 64,
            )
            with running_server(root) as (_, client):
                self.assertEqual("ready", client.setup_lease(lease).status)
            with running_server(root) as (_, resumed):
                self.assertEqual("dirty", resumed.setup_lease(lease).status)

    def test_reset_is_rejected_while_invocation_is_active_then_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            with running_server(root) as (worker, client):
                worker.release = threading.Event()
                request = self.scoped_request(target, invocation_id="invoke-active")
                lease = WorkerLeaseRequest(
                    lease_id=request.lease_id, run_id=request.run_id,
                    work_item_id=request.work_item_id, worker_id="fixture-worker",
                    route_sha256="a" * 64,
                )
                self.assertEqual("ready", client.setup_lease(lease).status)
                result: list[InvocationResult] = []
                thread = threading.Thread(target=lambda: result.append(client.invoke(request)))
                thread.start()
                self.assertTrue(worker.started.wait(timeout=2))
                with self.assertRaisesRegex(RemoteWorkerError, "active invocation"):
                    client.reset_lease(lease)
                worker.release.set()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                self.assertEqual("done", result[0].status)
                self.assertEqual("ready", client.reset_lease(lease).status)
                self.assertEqual("closed", client.teardown_lease(lease).status)

    def test_scope_is_required_even_for_offline_read_only_and_endpoint_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            with running_server(root) as (_, client):
                with self.assertRaisesRegex(RemoteWorkerError, "verified scope is required"):
                    self.invoke(client, self.request(
                        invocation_id="invoke-unscoped", target_sha256=hash_path(target),
                        requested_actions=(ActionAuthority.READ_LOCAL_TARGET.value,),
                    ))
                with self.assertRaisesRegex(RemoteWorkerError, "endpoint intent requires"):
                    self.invoke(client, self.scoped_request(
                        target, invocation_id="invoke-offline-endpoint",
                        endpoint="https://injected.invalid/collect",
                    ))

    def test_remote_revalidates_scope_hash_revision_and_exact_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            target_hash = hash_path(target)
            allowed = normalize_endpoint("https://lab.example.test/api")
            scope = remote_scope(target_hash, allowed)
            with running_server(root, allowed_network_policies=("none", "restricted")) as (_, client):
                request = self.request(
                    invocation_id="invoke-scoped",
                    target_sha256=target_hash,
                    network_policy="restricted",
                    endpoint=allowed,
                    scope_digest=scope.envelope.content_digest,
                    scope_revision=scope.to_dict(),
                    requested_actions=(ActionAuthority.READ_LOCAL_TARGET.value,
                                       ActionAuthority.NETWORK_ACCESS.value),
                )
                self.assertEqual("done", self.invoke(client, request).status)

                injected = self.request(
                    invocation_id="invoke-injected",
                    target_sha256=target_hash,
                    network_policy="restricted",
                    endpoint="https://injected.invalid/collect",
                    scope_digest=scope.envelope.content_digest,
                    scope_revision=scope.to_dict(),
                    requested_actions=(ActionAuthority.READ_LOCAL_TARGET.value,
                                       ActionAuthority.NETWORK_ACCESS.value),
                )
                with self.assertRaisesRegex(RemoteWorkerError, "outside the verified scope"):
                    self.invoke(client, injected)

                mismatched = self.request(
                    invocation_id="invoke-mismatch",
                    target_sha256=target_hash,
                    network_policy="restricted",
                    endpoint=allowed,
                    scope_digest="f" * 64,
                    scope_revision=scope.to_dict(),
                    requested_actions=(ActionAuthority.READ_LOCAL_TARGET.value,
                                       ActionAuthority.NETWORK_ACCESS.value),
                )
                with self.assertRaisesRegex(RemoteWorkerError, "digest"):
                    self.invoke(client, mismatched)

                missing = self.request(
                    invocation_id="invoke-missing-scope",
                    target_sha256=target_hash,
                    network_policy="restricted",
                    endpoint=allowed,
                )
                with self.assertRaisesRegex(RemoteWorkerError, "verified scope is required"):
                    self.invoke(client, missing)

    def test_remote_revalidates_opaque_account_and_credential_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            target_hash = hash_path(target)
            allowed = normalize_endpoint("https://lab.example.test/api")
            scope = remote_scope(
                target_hash, allowed,
                account_refs=("account:approved",), credential_use=True,
            )
            with running_server(root, allowed_network_policies=("restricted",)) as (_, client):
                common = dict(
                    target_sha256=target_hash,
                    network_policy="restricted",
                    endpoint=allowed,
                    scope_digest=scope.envelope.content_digest,
                    scope_revision=scope.to_dict(),
                    requested_actions=(ActionAuthority.READ_LOCAL_TARGET.value,
                                       ActionAuthority.NETWORK_ACCESS.value),
                    uses_credentials=True,
                )
                approved = self.request(
                    invocation_id="invoke-account-approved",
                    account_ref="account:approved",
                    **common,
                )
                self.assertEqual("done", self.invoke(client, approved).status)
                denied = self.request(
                    invocation_id="invoke-account-denied",
                    account_ref="account:unlisted",
                    **common,
                )
                with self.assertRaisesRegex(RemoteWorkerError, "account"):
                    self.invoke(client, denied)

                no_account = self.request(
                    invocation_id="invoke-account-missing",
                    **common,
                )
                with self.assertRaisesRegex(RemoteWorkerError, "requires an opaque account"):
                    self.invoke(client, no_account)


if __name__ == "__main__":
    unittest.main()
