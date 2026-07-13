from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import unittest

from rekit_factory.remote import InvocationRequest, InvocationResult, WorkerCapabilities
from rekit_factory.remote_http import (
    HTTPWorkerTransport,
    RemoteWorkerError,
    RemoteWorkerHTTPServer,
)
from rekit_factory.scope import (
    ActionAuthority, AuthorizedScope, NetworkMode, ScopeApproval, ScopeEnvelope,
    TargetGrant, hash_path, normalize_endpoint, opaque_ref,
)


def remote_scope(target_hash: str, endpoint: str) -> AuthorizedScope:
    envelope = ScopeEnvelope(
        scope_id="scope-remote", revision=1,
        valid_from="2026-07-01T00:00:00Z", valid_until="2026-08-01T00:00:00Z",
        targets=(TargetGrant(target_hash, opaque_ref("target-path", "/controller/input")),),
        endpoints=(endpoint,), network_mode=NetworkMode.EXACT_ENDPOINTS,
        actions=(ActionAuthority.READ_LOCAL_TARGET, ActionAuthority.NETWORK_ACCESS),
    )
    return AuthorizedScope(envelope, ScopeApproval(
        scope_id=envelope.scope_id, revision=1, content_digest=envelope.content_digest,
        approved_by="operator-remote", approved_at="2026-07-01T00:00:00Z",
        expires_at="2026-08-01T00:00:00Z", rationale="Exact remote lab endpoint",
    ))


class FixtureWorker:
    def __init__(self):
        self.requests: list[InvocationRequest] = []
        self.result_worker_id = "fixture-worker"

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
        return InvocationResult(
            invocation_id=request.invocation_id,
            run_id=request.run_id,
            work_item_id=request.work_item_id,
            worker_id=self.result_worker_id,
            status="done",
            exit_code=0,
            stdout=Path(request.target_path).read_text(encoding="utf-8"),
            stderr="",
        )

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
        }
        values.update(overrides)
        return InvocationRequest(**values)

    def test_discovers_capabilities_invokes_and_resumes_ordered_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fixture.txt").write_text("benign fixture", encoding="utf-8")
            with running_server(root) as (worker, client):
                capabilities = client.capabilities()
                self.assertEqual("fixture-worker", capabilities.worker_id)
                self.assertEqual(("fixture-scan",), capabilities.tools)

                result = client.invoke(self.request())
                self.assertEqual("done", result.status)
                self.assertEqual("benign fixture", result.stdout)
                self.assertEqual("run-1", result.run_id)
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
                wrong = HTTPWorkerTransport(client.base_url, auth_token="wrong-token")
                with self.assertRaisesRegex(RemoteWorkerError, "401"):
                    wrong.capabilities()

    def test_rejects_unstaged_paths_and_network_relaxation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fixture.txt").write_text("fixture", encoding="utf-8")
            with running_server(root) as (_, client):
                with self.assertRaisesRegex(RemoteWorkerError, "relative to the staged input"):
                    client.invoke(self.request(invocation_id="invoke-absolute", target_path="/tmp/a"))
                with self.assertRaisesRegex(RemoteWorkerError, "network policy"):
                    client.invoke(self.request(
                        invocation_id="invoke-network", network_policy="unrestricted"
                    ))

    def test_enforces_request_body_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fixture.txt").write_text("fixture", encoding="utf-8")
            with running_server(root, max_body=300) as (_, client):
                with self.assertRaisesRegex(RemoteWorkerError, "request body"):
                    client.invoke(self.request(arguments=("x" * 500,)))

    def test_rejects_worker_result_with_mismatched_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fixture.txt").write_text("fixture", encoding="utf-8")
            with running_server(root) as (worker, client):
                worker.result_worker_id = "spoofed-worker"
                result = client.invoke(self.request(invocation_id="invoke-spoofed"))
                self.assertEqual("failed", result.status)
                self.assertEqual("fixture-worker", result.worker_id)
                self.assertIn("ValueError", result.stderr)

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
                self.assertEqual("done", client.invoke(request).status)

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
                    client.invoke(injected)

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
                    client.invoke(mismatched)

                missing = self.request(
                    invocation_id="invoke-missing-scope",
                    target_sha256=target_hash,
                    network_policy="restricted",
                    endpoint=allowed,
                )
                with self.assertRaisesRegex(RemoteWorkerError, "verified scope is required"):
                    client.invoke(missing)


if __name__ == "__main__":
    unittest.main()
