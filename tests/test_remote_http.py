from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from dataclasses import asdict, replace
import hashlib
import json
import stat
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
        self.cancel_confirmed = False
        self.attach_value: str | None = None
        self.crash = False
        self.stdout_value: str | None = None
        self.lifecycle_started = threading.Event()
        self.lifecycle_release: threading.Event | None = None

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
        if self.crash:
            raise SystemExit("simulated worker process loss")
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
            stdout=(self.stdout_value if self.stdout_value is not None
                    else Path(request.target_path).read_text(encoding="utf-8")),
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
        if self.lifecycle_release is not None:
            self.lifecycle_started.set()
            self.lifecycle_release.wait(timeout=5)
        return WorkerLeaseState(**asdict(request), status="ready")

    def teardown_lease(self, request):
        return WorkerLeaseState(**asdict(request), status="closed")

    def fetch_artifact(self, invocation_id, artifact):
        return self.artifact_data

    def cancel(self, invocation_id: str) -> bool:
        return self.cancel_confirmed

    def attach_url(self, invocation_id: str) -> str | None:
        return self.attach_value


@contextmanager
def running_server(root: Path, **kwargs):
    worker = FixtureWorker()
    state_context = tempfile.TemporaryDirectory()
    state_root = kwargs.pop("state_root", state_context.name)
    server = RemoteWorkerHTTPServer(
        ("127.0.0.1", 0), worker,
        auth_token="test-token",
        input_root=root,
        state_root=state_root,
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
        state_context.cleanup()


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
            inputs = Path(tmp) / "inputs"
            inputs.mkdir()
            with self.assertRaisesRegex(ValueError, "auth_token"):
                RemoteWorkerHTTPServer(
                    ("127.0.0.1", 0), worker, auth_token="", input_root=inputs,
                    state_root=Path(tmp) / "state-empty-token",
                )
            with self.assertRaisesRegex(ValueError, "network policy"):
                RemoteWorkerHTTPServer(
                    ("127.0.0.1", 0), worker, auth_token="token", input_root=inputs,
                    state_root=Path(tmp) / "state-empty-policy",
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
            ("https://:443", "valid host and port"),
            ("https://worker.invalid:not-a-port", "valid host and port"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                HTTPWorkerTransport(url, auth_token="token")

    def test_native_windows_journal_fails_explicitly_until_secure_backend_exists(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "rekit_factory.remote_http.os.name", "nt",
        ):
            with self.assertRaisesRegex(NotImplementedError, "native Windows"):
                RemoteWorkerHTTPServer(
                    ("127.0.0.1", 0), FixtureWorker(), auth_token="token",
                    input_root=tmp + "/inputs", state_root=tmp + "/state",
                )

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

    def test_submit_rejects_lease_reserved_by_blocking_lifecycle_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            with running_server(root) as (worker, client):
                request = self.scoped_request(
                    target, invocation_id="invoke-lifecycle-race",
                    expected_manifest_digest="a" * 64,
                )
                lease = WorkerLeaseRequest(
                    lease_id=request.lease_id, run_id=request.run_id,
                    work_item_id=request.work_item_id, worker_id="fixture-worker",
                    route_sha256="a" * 64,
                )
                self.assertEqual("ready", client.setup_lease(lease).status)
                worker.lifecycle_release = threading.Event()
                reset_states: list[WorkerLeaseState] = []
                reset_thread = threading.Thread(
                    target=lambda: reset_states.append(client.reset_lease(lease)),
                )
                reset_thread.start()
                self.assertTrue(worker.lifecycle_started.wait(timeout=2))
                with self.assertRaisesRegex(RemoteWorkerError, "lifecycle operation"):
                    client.invoke(request)
                self.assertEqual([], worker.requests)
                worker.lifecycle_release.set()
                reset_thread.join(timeout=3)
                self.assertFalse(reset_thread.is_alive())
                self.assertEqual("ready", reset_states[0].status)
                self.assertEqual("done", client.invoke(request).status)

    def test_concurrent_exact_duplicate_attaches_without_reexecution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            with running_server(root) as (worker, client):
                worker.release = threading.Event()
                request = self.scoped_request(
                    target, invocation_id="invoke-concurrent-duplicate",
                    expected_manifest_digest="a" * 64,
                )
                lease = WorkerLeaseRequest(
                    lease_id=request.lease_id, run_id=request.run_id,
                    work_item_id=request.work_item_id, worker_id="fixture-worker",
                    route_sha256="a" * 64,
                )
                client.setup_lease(lease)
                results: list[InvocationResult] = []
                threads = [threading.Thread(
                    target=lambda: results.append(client.invoke(request)),
                ) for _ in range(2)]
                for thread in threads:
                    thread.start()
                self.assertTrue(worker.started.wait(timeout=2))
                worker.release.set()
                for thread in threads:
                    thread.join(timeout=3)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(2, len(results))
                self.assertEqual(1, len(worker.requests))
                self.assertEqual(results[0].to_dict(), results[1].to_dict())

    def test_thread_start_failure_records_terminal_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            with running_server(root) as (_, client):
                request = self.scoped_request(
                    target, invocation_id="invoke-thread-start-failure",
                )
                original_start = threading.Thread.start

                def selective_start(thread):
                    if thread.name.startswith("remote-worker-"):
                        raise RuntimeError("deterministic start failure")
                    return original_start(thread)

                with mock.patch(
                    "rekit_factory.remote_http.threading.Thread.start",
                    new=selective_start,
                ):
                    result = self.invoke(client, request)
                self.assertEqual("failed", result.status)
                self.assertIn("failed to start", result.stderr)

    def test_thread_start_base_exception_records_terminal_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            with running_server(root) as (_, client):
                request = self.scoped_request(
                    target, invocation_id="invoke-thread-start-abort",
                )
                original_start = threading.Thread.start

                def selective_start(thread):
                    if thread.name.startswith("remote-worker-"):
                        raise SystemExit("deterministic start abort")
                    return original_start(thread)

                with mock.patch(
                    "rekit_factory.remote_http.threading.Thread.start",
                    new=selective_start,
                ):
                    result = self.invoke(client, request)
                self.assertEqual("failed", result.status)
                self.assertIn("outcome is unknown", result.stderr)

    def test_started_event_persistence_failure_becomes_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            original_persist = RemoteWorkerHTTPServer._persist_state_locked
            failed_once = False

            def fail_started_once(server, *, protected=()):
                nonlocal failed_once
                events = [
                    event for history in server._events.values() for event in history
                ]
                if (not failed_once and events
                        and events[-1].kind == "invocation.started"):
                    failed_once = True
                    raise OSError("deterministic started-event write failure")
                return original_persist(server, protected=protected)

            with mock.patch.object(
                RemoteWorkerHTTPServer, "_persist_state_locked", fail_started_once,
            ), running_server(root) as (worker, client):
                result = self.invoke(client, self.scoped_request(
                    target, invocation_id="invoke-start-event-write-failure",
                ))
                self.assertTrue(failed_once)
                self.assertEqual([], worker.requests)
                self.assertEqual("failed", result.status)
                self.assertEqual(
                    "invocation.failed",
                    client.events(result.invocation_id)[-1].kind,
                )

    def test_dirty_lease_survives_server_restart_and_requires_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            state_root = root / "state"
            target = inputs / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            request = self.scoped_request(target, invocation_id="invoke-restart")
            lease = WorkerLeaseRequest(
                lease_id=request.lease_id, run_id=request.run_id,
                work_item_id=request.work_item_id, worker_id="fixture-worker",
                route_sha256="a" * 64,
            )
            with running_server(inputs, state_root=state_root) as (_, client):
                self.assertEqual("done", self.invoke(client, request).status)
            with running_server(inputs, state_root=state_root) as (_, resumed):
                self.assertEqual("dirty", resumed.setup_lease(lease).status)
                self.assertEqual("ready", resumed.reset_lease(lease).status)

    def test_ready_lease_is_demoted_to_dirty_after_server_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            state_root = root / "state"
            lease = WorkerLeaseRequest(
                lease_id="lease-ready-restart", run_id="run-1", work_item_id="work-1",
                worker_id="fixture-worker", route_sha256="a" * 64,
            )
            with running_server(inputs, state_root=state_root) as (_, client):
                self.assertEqual("ready", client.setup_lease(lease).status)
            with running_server(inputs, state_root=state_root) as (_, resumed):
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
                self.assertFalse(client.cancel(request.invocation_id))
                cancel_event = client.events(request.invocation_id)[-1]
                self.assertEqual("invocation.cancel.requested", cancel_event.kind)
                self.assertEqual({"confirmed": False}, cancel_event.payload)
                with self.assertRaisesRegex(RemoteWorkerError, "active invocation"):
                    client.reset_lease(lease)
                worker.release.set()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                self.assertEqual("done", result[0].status)
                self.assertEqual("ready", client.reset_lease(lease).status)
                self.assertEqual("closed", client.teardown_lease(lease).status)

    def test_terminal_result_events_and_exact_duplicate_resume_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            state_root = root / "state"
            target = inputs / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            request = self.scoped_request(
                target, invocation_id="invoke-durable",
                expected_manifest_digest="a" * 64,
            )
            with running_server(inputs, state_root=state_root) as (_, client):
                original = self.invoke(client, request)
                self.assertEqual(request.expected_manifest_digest,
                                 original.manifest_digest)
                original_events = client.events(request.invocation_id)
            with running_server(inputs, state_root=state_root) as (worker, resumed):
                attached = resumed.invoke(request)
                self.assertEqual(original.to_dict(), attached.to_dict())
                self.assertEqual(original_events, resumed.events(request.invocation_id))
                self.assertEqual([], worker.requests)
                with self.assertRaisesRegex(RemoteWorkerError, "different request"):
                    resumed.invoke(replace(request, arguments=("changed",)))

    def test_restart_rejects_missing_or_spoofed_persisted_success_attestation(self):
        for persisted_digest, message in (
            (None, "missing manifest attestation"),
            ("b" * 64, "manifest digest does not match"),
        ):
            with self.subTest(persisted_digest=persisted_digest), \
                    tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                inputs = root / "inputs"
                inputs.mkdir()
                state_root = root / "state"
                target = inputs / "fixture.txt"
                target.write_text("fixture", encoding="utf-8")
                request = self.scoped_request(
                    target, invocation_id="invoke-tampered-success",
                    expected_manifest_digest="a" * 64,
                )
                with running_server(inputs, state_root=state_root) as (_, client):
                    self.assertEqual("done", self.invoke(client, request).status)
                state_path = state_root / "worker-state.json"
                value = json.loads(state_path.read_text(encoding="utf-8"))
                value["invocations"][request.invocation_id]["result"][
                    "manifest_digest"
                ] = persisted_digest
                state_path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    RemoteWorkerHTTPServer(
                        ("127.0.0.1", 0), FixtureWorker(), auth_token="token",
                        input_root=inputs, state_root=state_root,
                    )

    def test_restart_rejects_spoofed_digest_on_persisted_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            state_root = root / "state"
            target = inputs / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            request = self.scoped_request(
                target, invocation_id="invoke-tampered-failure",
                expected_manifest_digest="a" * 64,
            )
            with running_server(inputs, state_root=state_root) as (_, client):
                self.assertEqual("done", self.invoke(client, request).status)
            state_path = state_root / "worker-state.json"
            value = json.loads(state_path.read_text(encoding="utf-8"))
            persisted = value["invocations"][request.invocation_id]["result"]
            persisted.update({
                "status": "failed", "exit_code": 7,
                "manifest_digest": "b" * 64,
            })
            state_path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest digest does not match"):
                RemoteWorkerHTTPServer(
                    ("127.0.0.1", 0), FixtureWorker(), auth_token="token",
                    input_root=inputs, state_root=state_root,
                )

    def test_restart_rejects_terminal_event_attestation_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            state_root = root / "state"
            target = inputs / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            request = self.scoped_request(
                target, invocation_id="invoke-tampered-terminal-event",
                expected_manifest_digest="a" * 64,
            )
            with running_server(inputs, state_root=state_root) as (_, client):
                self.assertEqual("done", self.invoke(client, request).status)
            state_path = state_root / "worker-state.json"
            value = json.loads(state_path.read_text(encoding="utf-8"))
            value["invocations"][request.invocation_id]["events"][-1]["payload"][
                "manifestDigest"
            ] = "b" * 64
            state_path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "terminal event attestation"):
                RemoteWorkerHTTPServer(
                    ("127.0.0.1", 0), FixtureWorker(), auth_token="token",
                    input_root=inputs, state_root=state_root,
                )

    def test_worker_base_exception_becomes_terminal_unknown_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            state_root = root / "state"
            target = inputs / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            request = self.scoped_request(target, invocation_id="invoke-interrupted")
            with running_server(inputs, state_root=state_root) as (worker, client):
                worker.crash = True
                result = self.invoke(client, request)
                self.assertEqual("failed", result.status)
                self.assertIn("outcome is unknown", result.stderr)
            with running_server(inputs, state_root=state_root) as (_, resumed):
                result = resumed.invoke(request)
                self.assertEqual("failed", result.status)
                self.assertIn("outcome is unknown", result.stderr)
                self.assertEqual(
                    "invocation.failed",
                    resumed.events(request.invocation_id)[-1].kind,
                )

    def test_restart_terminalizes_persisted_running_record_as_interrupted_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            state_root = root / "state"
            target = inputs / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            request = self.scoped_request(target, invocation_id="invoke-restart-running")
            with running_server(inputs, state_root=state_root) as (_, client):
                self.assertEqual("done", self.invoke(client, request).status)
            state_path = state_root / "worker-state.json"
            value = json.loads(state_path.read_text(encoding="utf-8"))
            record = value["invocations"][request.invocation_id]
            record["result"] = None
            record["events"].pop()
            state_path.write_text(json.dumps(value), encoding="utf-8")
            with running_server(inputs, state_root=state_root) as (_, resumed):
                result = resumed.invoke(request)
                self.assertEqual("failed", result.status)
                self.assertIn("outcome is unknown", result.stderr)
                self.assertEqual(
                    "invocation.interrupted_unknown",
                    resumed.events(request.invocation_id)[-1].kind,
                )

    def test_confirmed_cancel_is_terminal_persisted_and_allows_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            state_root = root / "state"
            target = inputs / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            request = self.scoped_request(target, invocation_id="invoke-cancel")
            lease = WorkerLeaseRequest(
                lease_id=request.lease_id, run_id=request.run_id,
                work_item_id=request.work_item_id, worker_id="fixture-worker",
                route_sha256="a" * 64,
            )
            with running_server(inputs, state_root=state_root) as (worker, client):
                worker.release = threading.Event()
                worker.cancel_confirmed = True
                client.setup_lease(lease)
                results: list[InvocationResult] = []
                thread = threading.Thread(target=lambda: results.append(client.invoke(request)))
                thread.start()
                self.assertTrue(worker.started.wait(timeout=2))
                self.assertTrue(client.cancel(request.invocation_id))
                thread.join(timeout=3)
                self.assertEqual("cancelled", results[0].status)
                self.assertEqual("ready", client.reset_lease(lease).status)
                worker.release.set()
            with running_server(inputs, state_root=state_root) as (_, resumed):
                self.assertEqual("cancelled", resumed.invoke(request).status)
                kinds = [event.kind for event in resumed.events(request.invocation_id)]
                self.assertIn("invocation.cancel.requested", kinds)
                self.assertIn("invocation.cancelled", kinds)

    def test_attach_is_audited_without_persisting_attach_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            state_root = root / "state"
            target = inputs / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            request = self.scoped_request(target, invocation_id="invoke-attach")
            with running_server(inputs, state_root=state_root) as (worker, client):
                self.invoke(client, request)
                worker.attach_value = "https://console.invalid/session?token=attach-secret"
                self.assertEqual(worker.attach_value, client.attach_url(request.invocation_id))
                event = client.events(request.invocation_id)[-1]
                self.assertEqual("invocation.attach.requested", event.kind)
                self.assertEqual({"available": True}, event.payload)
            persisted = (state_root / "worker-state.json").read_text(encoding="utf-8")
            self.assertNotIn("attach-secret", persisted)
            self.assertNotIn("test-token", persisted)
            self.assertEqual(0o700, stat.S_IMODE(state_root.stat().st_mode))
            self.assertEqual(
                0o600,
                stat.S_IMODE((state_root / "worker-state.json").stat().st_mode),
            )

    def test_attach_rejects_unsafe_schemes_and_userinfo_server_and_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            with running_server(root) as (worker, client):
                request = self.scoped_request(target, invocation_id="invoke-unsafe-attach")
                self.invoke(client, request)
                for unsafe in (
                    "javascript:alert(1)",
                    "https://user:secret@console.invalid/session",
                    "http://console.invalid/session",
                    "https://:443/session",
                    "wss://console.invalid:not-a-port/session",
                ):
                    with self.subTest(unsafe=unsafe):
                        worker.attach_value = unsafe
                        with self.assertRaises(RemoteWorkerError):
                            client.attach_url(request.invocation_id)

        client = HTTPWorkerTransport("https://worker.invalid", auth_token="token")
        for unsafe, message in (
            ("https://user:secret@console.invalid/session", "userinfo"),
            ("https://:443/session", "valid host and port"),
            ("wss://console.invalid:not-a-port/session", "valid host and port"),
        ):
            with self.subTest(client_unsafe=unsafe), mock.patch.object(
                client, "_request", return_value=(200, {"attach_url": unsafe}),
            ):
                with self.assertRaisesRegex(RemoteWorkerError, message):
                    client.attach_url("invoke-client-unsafe")

    def test_oversized_worker_result_records_bounded_failure_and_remains_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            with running_server(root) as (worker, client):
                worker.stdout_value = "x" * (262_144 + 1)
                oversized = self.invoke(client, self.scoped_request(
                    target, invocation_id="invoke-oversized",
                ))
                self.assertEqual("failed", oversized.status)
                self.assertIn("durable journal capacity", oversized.stderr)
                worker.stdout_value = None
                healthy = self.invoke(client, self.scoped_request(
                    target, invocation_id="invoke-after-oversized",
                ))
                self.assertEqual("done", healthy.status)

    def test_full_journal_deterministically_evicts_terminal_history_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            with running_server(root, max_state=8_000) as (worker, client):
                first_result = None
                for index in range(6):
                    result = self.invoke(client, self.scoped_request(
                        target, invocation_id=f"invoke-history-{index}",
                    ))
                    self.assertEqual("done", result.status)
                    if index == 0:
                        first_result = result
                self.assertIsNotNone(first_result)
                first = self.scoped_request(
                    target, invocation_id="invoke-history-0",
                )
                requests_before_retry = len(worker.requests)
                with self.assertRaisesRegex(RemoteWorkerError, "410"):
                    client.events("invoke-history-0")
                with self.assertRaisesRegex(RemoteWorkerError, "history was pruned"):
                    client.invoke(first)
                self.assertEqual(requests_before_retry, len(worker.requests))
                with self.assertRaisesRegex(RemoteWorkerError, "different request"):
                    client.invoke(replace(first, arguments=("drift",)))
                with self.assertRaisesRegex(RemoteWorkerError, "410"):
                    client.attach_url(first.invocation_id)
                with self.assertRaisesRegex(RemoteWorkerError, "410"):
                    client.fetch_artifact(first.invocation_id, first_result.artifacts[0])
                self.assertEqual(
                    "done",
                    self.invoke(client, self.scoped_request(
                        target, invocation_id="invoke-history-latest",
                    )).status,
                )
                self.assertEqual(
                    "invocation.done", client.events("invoke-history-latest")[-1].kind,
                )

    def test_journal_full_admission_rolls_back_and_server_remains_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            with running_server(root, max_state=8_000) as (_, client):
                oversized = self.scoped_request(
                    target, invocation_id="invoke-admission-rollback",
                    arguments=("x" * 10_000,),
                )
                lease = WorkerLeaseRequest(
                    lease_id=oversized.lease_id, run_id=oversized.run_id,
                    work_item_id=oversized.work_item_id, worker_id="fixture-worker",
                    route_sha256="a" * 64,
                )
                client.setup_lease(lease)
                with self.assertRaisesRegex(RemoteWorkerError, "state exceeds"):
                    client.invoke(oversized)
                recovered = replace(oversized, arguments=())
                self.assertEqual("done", client.invoke(recovered).status)

    def test_state_and_staged_input_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            state_target = root / "real-state"
            state_target.mkdir()
            state_link = root / "state-link"
            state_link.symlink_to(state_target, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "state_root.*symlink"):
                RemoteWorkerHTTPServer(
                    ("127.0.0.1", 0), FixtureWorker(), auth_token="token",
                    input_root=inputs, state_root=state_link,
                )
            real_target = inputs / "real.txt"
            real_target.write_text("fixture", encoding="utf-8")
            linked_target = inputs / "linked.txt"
            linked_target.symlink_to(real_target)
            with running_server(inputs) as (_, client):
                request = self.scoped_request(
                    real_target, invocation_id="invoke-symlink", target_path="linked.txt",
                )
                lease = WorkerLeaseRequest(
                    lease_id=request.lease_id, run_id=request.run_id,
                    work_item_id=request.work_item_id, worker_id="fixture-worker",
                    route_sha256="a" * 64,
                )
                client.setup_lease(lease)
                with self.assertRaisesRegex(RemoteWorkerError, "must not traverse symlinks"):
                    client.invoke(request)

    def test_persisted_lease_map_key_must_match_lease_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            state_root = root / "state"
            lease = WorkerLeaseRequest(
                lease_id="lease-key-check", run_id="run-1", work_item_id="work-1",
                worker_id="fixture-worker", route_sha256="a" * 64,
            )
            with running_server(inputs, state_root=state_root) as (_, client):
                client.setup_lease(lease)
            state_path = state_root / "worker-state.json"
            value = json.loads(state_path.read_text(encoding="utf-8"))
            value["leases"]["wrong-key"] = value["leases"].pop(lease.lease_id)
            state_path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "map key"):
                RemoteWorkerHTTPServer(
                    ("127.0.0.1", 0), FixtureWorker(), auth_token="token",
                    input_root=inputs, state_root=state_root,
                )

    def test_persisted_event_count_must_not_exceed_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            state_root = root / "state"
            target = inputs / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            request = self.scoped_request(target, invocation_id="invoke-event-limit")
            with running_server(inputs, state_root=state_root) as (_, client):
                self.assertEqual("done", self.invoke(client, request).status)
            state_path = state_root / "worker-state.json"
            value = json.loads(state_path.read_text(encoding="utf-8"))
            events = value["invocations"][request.invocation_id]["events"]
            template = dict(events[-1])
            for sequence in range(len(events) + 1, 1002):
                event = dict(template)
                event["sequence"] = sequence
                events.append(event)
            state_path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "event history exceeds"):
                RemoteWorkerHTTPServer(
                    ("127.0.0.1", 0), FixtureWorker(), auth_token="token",
                    input_root=inputs, state_root=state_root,
                )

    def test_state_root_final_component_swap_fails_persistence_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            state_root = root / "state"
            replacement = root / "replacement"
            replacement.mkdir()
            lease = WorkerLeaseRequest(
                lease_id="lease-state-swap", run_id="run-1", work_item_id="work-1",
                worker_id="fixture-worker", route_sha256="a" * 64,
            )
            with running_server(inputs, state_root=state_root) as (_, client):
                client.setup_lease(lease)
                original = root / "state-original"
                state_root.rename(original)
                state_root.symlink_to(replacement, target_is_directory=True)
                with self.assertRaisesRegex(RemoteWorkerError, "lifecycle failed"):
                    client.reset_lease(lease)

    def test_state_root_real_directory_swap_fails_persistence_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            state_root = root / "state"
            lease = WorkerLeaseRequest(
                lease_id="lease-state-real-swap", run_id="run-1", work_item_id="work-1",
                worker_id="fixture-worker", route_sha256="a" * 64,
            )
            with running_server(inputs, state_root=state_root) as (_, client):
                client.setup_lease(lease)
                original = root / "state-original"
                state_root.rename(original)
                state_root.mkdir()
                with self.assertRaisesRegex(RemoteWorkerError, "lifecycle failed"):
                    client.reset_lease(lease)

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
