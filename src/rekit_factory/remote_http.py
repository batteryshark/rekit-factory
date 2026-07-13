"""Minimal authenticated HTTP transport for versioned Rekit worker envelopes."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ipaddress
import os
from pathlib import Path, PurePosixPath
import stat
import threading
import time
from typing import Any
import uuid
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

from rekit_factory.remote import (
    ArtifactRecord,
    InvocationRequest,
    InvocationResult,
    NetworkPolicy,
    WorkerCapabilities,
    WorkerEvent,
    WorkerLeaseRequest,
    WorkerLeaseState,
    WorkerTransport,
    validate_invocation_scope,
)


DEFAULT_MAX_BODY = 1_000_000
DEFAULT_MAX_RESPONSE = 4_000_000
DEFAULT_MAX_STATE = 16_000_000
MAX_PERSISTED_STREAM = 262_144
MAX_EVENTS_PER_INVOCATION = 1_000


class RemoteWorkerError(RuntimeError):
    pass


class PrunedInvocationError(KeyError):
    pass


class RemoteWorkerHTTPServer(ThreadingHTTPServer):
    """HTTP boundary around one worker; lifecycle/isolation remains adapter-owned."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        worker: WorkerTransport,
        *,
        auth_token: str,
        input_root: str | Path,
        state_root: str | Path,
        allowed_network_policies: tuple[NetworkPolicy, ...] = ("none",),
        max_body: int = DEFAULT_MAX_BODY,
        max_response: int = DEFAULT_MAX_RESPONSE,
        max_state: int = DEFAULT_MAX_STATE,
    ):
        if not auth_token:
            raise ValueError("auth_token must be explicit and non-empty")
        if max_body < 1 or max_response < 1 or max_state < 1:
            raise ValueError("HTTP size limits must be positive")
        if os.name != "posix":
            raise NotImplementedError(
                "the durable HTTP worker journal currently requires a POSIX host; "
                "a native Windows secure-storage backend is not yet implemented"
            )
        self.worker = worker
        self.worker_capabilities = worker.capabilities()
        self.auth_token = auth_token
        raw_input_root = Path(input_root).expanduser()
        if raw_input_root.is_symlink():
            raise ValueError("input_root must not be a symlink")
        self.input_root = raw_input_root.resolve()
        raw_state_root = Path(state_root).expanduser()
        if raw_state_root.is_symlink():
            raise ValueError("state_root must not be a symlink")
        prospective_state_root = raw_state_root.resolve()
        if (prospective_state_root == self.input_root
                or prospective_state_root.is_relative_to(self.input_root)
                or self.input_root.is_relative_to(prospective_state_root)):
            raise ValueError("state_root must be separate from staged input_root")
        raw_state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_root = raw_state_root.resolve()
        self.state_root.chmod(0o700)
        state_root_stat = self.state_root.stat()
        if not stat.S_ISDIR(state_root_stat.st_mode):
            raise ValueError("state_root must be a directory")
        self._state_root_identity = (state_root_stat.st_dev, state_root_stat.st_ino)
        self.allowed_network_policies = frozenset(allowed_network_policies)
        if not self.allowed_network_policies:
            raise ValueError("at least one network policy must be allowed")
        self.max_body = max_body
        self.max_response = max_response
        self.max_state = max_state
        self._lock = threading.Lock()
        self._state_path = self.state_root / "worker-state.json"
        self._lease_states: dict[str, WorkerLeaseState] = {}
        self._requests: dict[str, InvocationRequest] = {}
        self._tombstones: dict[str, str] = {}
        self._active_leases: dict[str, str] = {}
        self._lifecycle_reservations: set[str] = set()
        self._events: dict[str, list[WorkerEvent]] = {}
        self._results: dict[str, InvocationResult] = {}
        self._load_state()
        super().__init__(address, RemoteWorkerHTTPRequestHandler)

    def submit(self, request: InvocationRequest) -> str:
        if request.lease_id is None:
            raise PermissionError("remote invocation requires an explicit worker lease")
        with self._lock:
            prior = self._requests.get(request.invocation_id)
            if prior is not None:
                if prior.to_json() != request.to_json():
                    raise FileExistsError("invocation_id is bound to a different request")
                return "attached"
            tombstone = self._tombstones.get(request.invocation_id)
            if tombstone is not None:
                if tombstone != self._request_digest(request):
                    raise FileExistsError("invocation_id is bound to a different request")
                return "pruned"
        if request.network_policy not in self.allowed_network_policies:
            raise PermissionError(
                f"network policy {request.network_policy!r} is not enabled on this worker"
            )
        if request.mount_policy != "staged-input-read-only":
            raise PermissionError("remote invocations require staged-input-read-only policy")
        staged = PurePosixPath(request.target_path)
        if staged.is_absolute() or ".." in staged.parts:
            raise ValueError("remote target_path must be relative to the staged input root")
        candidate = self.input_root / Path(*staged.parts)
        relative_cursor = self.input_root
        for part in staged.parts:
            relative_cursor = relative_cursor / part
            if relative_cursor.is_symlink():
                raise ValueError("remote target_path must not traverse symlinks")
        target = candidate.resolve()
        try:
            target.relative_to(self.input_root)
        except ValueError as exc:  # defensive for platform-specific path behavior
            raise ValueError("remote target_path escapes the staged input root") from exc
        if not target.is_file():
            raise ValueError("staged target does not exist or is not a regular file")
        self._validate_scope(request, target)
        local_request = replace(request, target_path=str(target))
        worker_id = self.worker_capabilities.worker_id
        accepted = WorkerEvent(
            invocation_id=request.invocation_id,
            run_id=request.run_id,
            work_item_id=request.work_item_id,
            worker_id=worker_id,
            sequence=1,
            kind="invocation.accepted",
            message="Invocation accepted by worker",
        )
        with self._lock:
            prior = self._requests.get(request.invocation_id)
            if prior is not None:
                if prior.to_json() != request.to_json():
                    raise FileExistsError("invocation_id is bound to a different request")
                return "attached"
            tombstone = self._tombstones.get(request.invocation_id)
            if tombstone is not None:
                if tombstone != self._request_digest(request):
                    raise FileExistsError("invocation_id is bound to a different request")
                return "pruned"
            lease = self._lease_states.get(request.lease_id)
            if lease is None or lease.status != "ready":
                raise PermissionError("remote worker lease is not clean and ready")
            if (lease.run_id, lease.work_item_id, lease.worker_id) != (
                request.run_id, request.work_item_id, worker_id,
            ):
                raise PermissionError("remote invocation does not match leased authority")
            if request.lease_id in self._active_leases:
                raise PermissionError("remote worker lease already has an active invocation")
            if request.lease_id in self._lifecycle_reservations:
                raise PermissionError("remote worker lease has a lifecycle operation in progress")
            if request.invocation_id in self._events:
                raise FileExistsError("invocation_id already exists")
            prior_leases = dict(self._lease_states)
            prior_active = dict(self._active_leases)
            prior_requests = dict(self._requests)
            prior_events = {key: list(events) for key, events in self._events.items()}
            prior_results = dict(self._results)
            prior_tombstones = dict(self._tombstones)
            self._lease_states[lease.lease_id] = replace(lease, status="dirty")
            self._active_leases[lease.lease_id] = request.invocation_id
            self._requests[request.invocation_id] = request
            self._events[request.invocation_id] = [accepted]
            self._results.pop(request.invocation_id, None)
            try:
                self._results[request.invocation_id] = self._journal_failure(request)
                self._events[request.invocation_id].extend((
                    WorkerEvent(
                        invocation_id=request.invocation_id, run_id=request.run_id,
                        work_item_id=request.work_item_id, worker_id=worker_id,
                        sequence=2, kind="invocation.started",
                        message="Invocation started",
                    ),
                    WorkerEvent(
                        invocation_id=request.invocation_id, run_id=request.run_id,
                        work_item_id=request.work_item_id, worker_id=worker_id,
                        sequence=3, kind="invocation.failed",
                        message="Invocation failed",
                        payload={"exitCode": None, "manifestDigest": None},
                    ),
                ))
                self._compact_state_locked(protected=(request.invocation_id,))
                self._results.pop(request.invocation_id, None)
                del self._events[request.invocation_id][1:]
                self._persist_state_locked(protected=(request.invocation_id,))
            except Exception:
                self._lease_states = prior_leases
                self._active_leases = prior_active
                self._requests = prior_requests
                self._events = prior_events
                self._results = prior_results
                self._tombstones = prior_tombstones
                raise
        invocation_thread = threading.Thread(
            target=self._run_invocation,
            args=(local_request,),
            name=f"remote-worker-{request.invocation_id}",
            daemon=True,
        )
        try:
            invocation_thread.start()
        except BaseException as exc:
            self._finish_invocation(local_request, InvocationResult(
                invocation_id=request.invocation_id, run_id=request.run_id,
                work_item_id=request.work_item_id, worker_id=worker_id,
                status="failed", exit_code=None, stdout="",
                stderr=(f"worker thread failed to start ({type(exc).__name__})"
                        if isinstance(exc, Exception)
                        else "worker thread aborted before start; execution outcome is unknown"),
                lease_id=request.lease_id,
            ))
        return "accepted"

    @staticmethod
    def _request_digest(request: InvocationRequest) -> str:
        return hashlib.sha256(request.to_json().encode("utf-8")).hexdigest()

    def _validate_scope(self, request: InvocationRequest, target: Path) -> None:
        validate_invocation_scope(request, target)

    def _run_invocation(self, request: InvocationRequest) -> None:
        try:
            self._append_event(request, "invocation.started", "Invocation started")
            result = self.worker.invoke(request)
            expected = (
                request.invocation_id,
                request.run_id,
                request.work_item_id,
                self.worker_capabilities.worker_id,
                request.lease_id,
            )
            actual = (
                result.invocation_id,
                result.run_id,
                result.work_item_id,
                result.worker_id,
                result.lease_id,
            )
            if actual != expected:
                raise ValueError("worker result provenance does not match the leased invocation")
            if (result.manifest_digest is not None
                    and result.manifest_digest != request.expected_manifest_digest):
                raise ValueError(
                    "worker result manifest digest does not match the leased invocation"
                )
            if (result.status == "done" and request.expected_manifest_digest is not None
                    and result.manifest_digest is None):
                raise ValueError(
                    "worker result did not attest the leased invocation manifest"
                )
        except BaseException as exc:
            result = InvocationResult(
                invocation_id=request.invocation_id,
                run_id=request.run_id,
                work_item_id=request.work_item_id,
                worker_id=self.worker_capabilities.worker_id,
                status="failed",
                exit_code=None,
                stdout="",
                stderr=(f"worker invocation failed ({type(exc).__name__})"
                        if isinstance(exc, Exception)
                        else "worker invocation aborted; execution outcome is unknown"),
                lease_id=request.lease_id,
            )
        self._finish_invocation(request, result)

    def _append_event(
        self, request: InvocationRequest, kind: str, message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            events = self._events[request.invocation_id]
            if len(events) >= MAX_EVENTS_PER_INVOCATION:
                raise ValueError("invocation event history limit reached")
            events.append(WorkerEvent(
                invocation_id=request.invocation_id,
                run_id=request.run_id,
                work_item_id=request.work_item_id,
                worker_id=self.worker_capabilities.worker_id,
                sequence=len(events) + 1,
                kind=kind,
                message=message,
                payload=payload or {},
            ))
            try:
                self._persist_state_locked(protected=(request.invocation_id,))
            except Exception:
                self._events[request.invocation_id].pop()
                raise

    def _finish_invocation(self, request: InvocationRequest,
                           result: InvocationResult) -> None:
        result = self._bounded_result(request, result)
        with self._lock:
            if request.invocation_id in self._results:
                return
            events = self._events[request.invocation_id]
            replaced_event = None
            if len(events) >= MAX_EVENTS_PER_INVOCATION:
                replaced_event = events.pop()
            events.append(WorkerEvent(
                invocation_id=request.invocation_id, run_id=request.run_id,
                work_item_id=request.work_item_id,
                worker_id=self.worker_capabilities.worker_id,
                sequence=len(events) + 1, kind=f"invocation.{result.status}",
                message=f"Invocation {result.status}",
                payload={"exitCode": result.exit_code,
                         "manifestDigest": result.manifest_digest},
            ))
            self._results[request.invocation_id] = result
            prior_active = self._active_leases.get(request.lease_id or "")
            if request.lease_id is not None:
                self._active_leases.pop(request.lease_id, None)
            try:
                self._persist_state_locked(protected=(request.invocation_id,))
            except Exception:
                self._results.pop(request.invocation_id, None)
                restored_events = self._events[request.invocation_id]
                restored_events.pop()
                if replaced_event is not None:
                    restored_events.append(replaced_event)
                if request.lease_id is not None and prior_active is not None:
                    self._active_leases[request.lease_id] = prior_active
                fallback = self._journal_failure(request)
                fallback_events = self._events[request.invocation_id]
                if len(fallback_events) >= MAX_EVENTS_PER_INVOCATION:
                    fallback_events.pop()
                fallback_events.append(WorkerEvent(
                    invocation_id=request.invocation_id, run_id=request.run_id,
                    work_item_id=request.work_item_id,
                    worker_id=self.worker_capabilities.worker_id,
                    sequence=len(fallback_events) + 1, kind="invocation.failed",
                    message="Invocation failed",
                    payload={"exitCode": None, "manifestDigest": None},
                ))
                self._results[request.invocation_id] = fallback
                if request.lease_id is not None:
                    self._active_leases.pop(request.lease_id, None)
                try:
                    self._persist_state_locked(protected=(request.invocation_id,))
                except Exception:
                    self._results.pop(request.invocation_id, None)
                    self._events[request.invocation_id].pop()
                    if request.lease_id is not None and prior_active is not None:
                        self._active_leases[request.lease_id] = prior_active
                    raise

    def events_after(self, invocation_id: str, sequence: int) -> list[WorkerEvent] | None:
        with self._lock:
            events = self._events.get(invocation_id)
            return None if events is None else [event for event in events if event.sequence > sequence]

    def result(self, invocation_id: str) -> tuple[bool, InvocationResult | None]:
        with self._lock:
            return invocation_id in self._requests, self._results.get(invocation_id)

    def is_pruned(self, invocation_id: str) -> bool:
        with self._lock:
            return invocation_id in self._tombstones

    def cancel(self, invocation_id: str) -> bool:
        with self._lock:
            request = self._requests.get(invocation_id)
            result = self._results.get(invocation_id)
        if request is None:
            if self.is_pruned(invocation_id):
                raise PrunedInvocationError("terminal invocation history was pruned")
            raise KeyError("unknown invocation")
        if result is not None:
            return result.status == "cancelled"
        confirmed = self.worker.cancel(invocation_id)
        if not isinstance(confirmed, bool):
            raise ValueError("worker cancellation outcome must be a boolean")
        with self._lock:
            if invocation_id in self._results:
                return self._results[invocation_id].status == "cancelled"
            events = self._events[invocation_id]
            if len(events) >= MAX_EVENTS_PER_INVOCATION - (2 if confirmed else 1):
                raise ValueError("invocation event history limit reached")
            prior_length = len(events)
            prior_active = self._active_leases.get(request.lease_id or "")
            events.append(WorkerEvent(
                invocation_id=invocation_id, run_id=request.run_id,
                work_item_id=request.work_item_id,
                worker_id=self.worker_capabilities.worker_id,
                sequence=len(events) + 1, kind="invocation.cancel.requested",
                message="Invocation cancellation requested",
                payload={"confirmed": confirmed},
            ))
            if confirmed:
                cancelled = InvocationResult(
                    invocation_id=invocation_id, run_id=request.run_id,
                    work_item_id=request.work_item_id,
                    worker_id=self.worker_capabilities.worker_id,
                    status="cancelled", exit_code=None, stdout="", stderr="",
                    lease_id=request.lease_id,
                )
                events.append(WorkerEvent(
                    invocation_id=invocation_id, run_id=request.run_id,
                    work_item_id=request.work_item_id,
                    worker_id=self.worker_capabilities.worker_id,
                    sequence=len(events) + 1, kind="invocation.cancelled",
                    message="Invocation cancelled",
                    payload={"exitCode": None, "manifestDigest": None},
                ))
                self._results[invocation_id] = cancelled
                if request.lease_id is not None:
                    self._active_leases.pop(request.lease_id, None)
            try:
                self._persist_state_locked(protected=(invocation_id,))
            except Exception:
                del self._events[invocation_id][prior_length:]
                self._results.pop(invocation_id, None)
                if request.lease_id is not None and prior_active is not None:
                    self._active_leases[request.lease_id] = prior_active
                raise
        return confirmed

    def attach(self, invocation_id: str) -> str | None:
        with self._lock:
            request = self._requests.get(invocation_id)
        if request is None:
            if self.is_pruned(invocation_id):
                raise PrunedInvocationError("terminal invocation history was pruned")
            raise KeyError("unknown invocation")
        url = self.worker.attach_url(invocation_id)
        if url is not None and (not isinstance(url, str) or not url.strip()):
            raise ValueError("worker attach URL must be a non-empty string or null")
        if url is not None:
            _validate_attach_url(url)
        self._append_event(
            request, "invocation.attach.requested", "Interactive attachment requested",
            {"available": url is not None},
        )
        return url

    def lifecycle(self, action: str, request: WorkerLeaseRequest) -> WorkerLeaseState:
        if request.worker_id != self.worker_capabilities.worker_id:
            raise PermissionError("lease worker identity does not match endpoint")
        with self._lock:
            current = self._lease_states.get(request.lease_id)
            active_invocation = self._active_leases.get(request.lease_id)
            if request.lease_id in self._lifecycle_reservations:
                raise PermissionError("lease already has a lifecycle operation in progress")
            if action in {"reset", "teardown"} and active_invocation is not None:
                raise PermissionError(
                    f"lease still has active invocation {active_invocation}"
                )
            if action == "setup" and current is not None:
                expected = (request.run_id, request.work_item_id, request.worker_id,
                            request.route_sha256)
                actual = (current.run_id, current.work_item_id, current.worker_id,
                          current.route_sha256)
                if actual != expected:
                    raise PermissionError(
                        "lease identity is already bound to different authority"
                    )
                return current
            self._lifecycle_reservations.add(request.lease_id)
        try:
            operation = {
                "setup": self.worker.setup_lease,
                "reset": self.worker.reset_lease,
                "teardown": self.worker.teardown_lease,
            }[action]
            state = operation(request)
            expected = (request.lease_id, request.run_id, request.work_item_id,
                        request.worker_id, request.route_sha256)
            actual = (state.lease_id, state.run_id, state.work_item_id,
                      state.worker_id, state.route_sha256)
            if actual != expected:
                raise ValueError("worker lease state provenance mismatch")
            with self._lock:
                if request.lease_id in self._active_leases:
                    raise PermissionError("lease became active during lifecycle operation")
                prior_state = self._lease_states.get(state.lease_id)
                self._lease_states[state.lease_id] = state
                try:
                    self._persist_state_locked()
                except Exception:
                    if prior_state is None:
                        self._lease_states.pop(state.lease_id, None)
                    else:
                        self._lease_states[state.lease_id] = prior_state
                    raise
            return state
        finally:
            with self._lock:
                self._lifecycle_reservations.discard(request.lease_id)

    def _load_state(self) -> None:
        directory_fd = self._open_state_root()
        try:
            file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                state_fd = os.open(self._state_path.name, file_flags, dir_fd=directory_fd)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise ValueError("worker state must be a regular non-symlink file") from exc
            try:
                state_stat = os.fstat(state_fd)
                if not stat.S_ISREG(state_stat.st_mode):
                    raise ValueError("worker state must be a regular non-symlink file")
                os.fchmod(state_fd, 0o600)
                if state_stat.st_size > self.max_state:
                    raise ValueError("worker state exceeds configured size limit")
                with os.fdopen(state_fd, "rb", closefd=False) as stream:
                    raw_state = stream.read(self.max_state + 1)
            finally:
                os.close(state_fd)
        finally:
            os.close(directory_fd)
        if len(raw_state) > self.max_state:
            raise ValueError("worker state exceeds configured size limit")
        value = json.loads(raw_state)
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("worker state must be a version 1 object")
        leases = value.get("leases", {})
        invocations = value.get("invocations", {})
        tombstones = value.get("tombstones", {})
        if (not isinstance(leases, dict) or not isinstance(invocations, dict)
                or not isinstance(tombstones, dict)):
            raise ValueError("worker state collections must be objects")
        for invocation_id, digest in tombstones.items():
            if (not isinstance(invocation_id, str) or not invocation_id.strip()
                    or not isinstance(digest, str) or len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)):
                raise ValueError("persisted invocation tombstone is invalid")
        self._tombstones = dict(tombstones)
        self._lease_states = {}
        for key, item in leases.items():
            state = WorkerLeaseState.from_dict(item)
            if key != state.lease_id:
                raise ValueError("persisted lease map key does not match lease identity")
            self._lease_states[key] = (
                state if state.status == "closed" else replace(state, status="dirty")
            )
        changed = False
        interrupted_ids: list[str] = []
        for invocation_id, record in invocations.items():
            if invocation_id in self._tombstones:
                raise ValueError("invocation cannot have both live state and a tombstone")
            if not isinstance(record, dict):
                raise ValueError("invocation state must be an object")
            request = InvocationRequest.from_dict(record["request"])
            if invocation_id != request.invocation_id:
                raise ValueError("invocation state key does not match request")
            lease = self._lease_states.get(request.lease_id or "")
            if lease is None or (
                lease.run_id, lease.work_item_id, lease.worker_id,
            ) != (
                request.run_id, request.work_item_id,
                self.worker_capabilities.worker_id,
            ):
                raise ValueError("persisted invocation lease binding is invalid")
            events = [WorkerEvent.from_dict(item) for item in record.get("events", [])]
            if len(events) > MAX_EVENTS_PER_INVOCATION:
                raise ValueError("persisted invocation event history exceeds limit")
            for sequence, event in enumerate(events, 1):
                if (event.sequence != sequence
                        or (event.invocation_id, event.run_id, event.work_item_id,
                            event.worker_id) != (
                            request.invocation_id, request.run_id,
                            request.work_item_id, self.worker_capabilities.worker_id,
                        )):
                    raise ValueError("persisted worker event provenance is invalid")
            result_value = record.get("result")
            result = (None if result_value is None
                      else InvocationResult.from_dict(result_value))
            if result is not None and (
                result.invocation_id, result.run_id, result.work_item_id,
                result.worker_id, result.lease_id,
            ) != (
                request.invocation_id, request.run_id, request.work_item_id,
                self.worker_capabilities.worker_id, request.lease_id,
            ):
                raise ValueError("persisted worker result provenance is invalid")
            if (result is not None and result.manifest_digest is not None
                    and result.manifest_digest != request.expected_manifest_digest):
                raise ValueError(
                    "persisted worker result manifest digest does not match request"
                )
            if (result is not None and result.status == "done"
                    and request.expected_manifest_digest is not None
                    and result.manifest_digest is None):
                raise ValueError(
                    "persisted successful result is missing manifest attestation"
                )
            terminal_events = [
                event for event in events if event.kind in {
                    "invocation.done", "invocation.failed", "invocation.cancelled",
                    "invocation.interrupted_unknown",
                }
            ]
            if result is None and terminal_events:
                raise ValueError("persisted running invocation contains a terminal event")
            if result is not None:
                allowed_kinds = {f"invocation.{result.status}"}
                if result.status == "failed":
                    allowed_kinds.add("invocation.interrupted_unknown")
                if (len(terminal_events) != 1
                        or terminal_events[0].kind not in allowed_kinds):
                    raise ValueError("persisted terminal event does not match result status")
                terminal_payload = terminal_events[0].payload
                if ("exitCode" not in terminal_payload
                        or terminal_payload["exitCode"] != result.exit_code
                        or "manifestDigest" not in terminal_payload
                        or terminal_payload["manifestDigest"] != result.manifest_digest):
                    raise ValueError("persisted terminal event attestation does not match result")
            if result is None:
                if len(events) >= MAX_EVENTS_PER_INVOCATION:
                    events.pop()
                result = InvocationResult(
                    invocation_id=request.invocation_id, run_id=request.run_id,
                    work_item_id=request.work_item_id,
                    worker_id=self.worker_capabilities.worker_id,
                    status="failed", exit_code=None, stdout="",
                    stderr="worker process restarted; prior execution outcome is unknown",
                    lease_id=request.lease_id,
                )
                events.append(WorkerEvent(
                    invocation_id=request.invocation_id, run_id=request.run_id,
                    work_item_id=request.work_item_id,
                    worker_id=self.worker_capabilities.worker_id,
                    sequence=len(events) + 1, kind="invocation.interrupted_unknown",
                    message="Worker restarted before a terminal result was recorded",
                    payload={"exitCode": None, "manifestDigest": None},
                ))
                changed = True
                interrupted_ids.append(invocation_id)
            self._requests[invocation_id] = request
            self._events[invocation_id] = events
            self._results[invocation_id] = result
        if changed:
            self._persist_state_locked(protected=tuple(interrupted_ids))

    def _state_value_locked(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "leases": {key: state.to_dict()
                       for key, state in sorted(self._lease_states.items())},
            "tombstones": dict(sorted(self._tombstones.items())),
            "invocations": {
                key: {
                    "request": self._requests[key].to_dict(),
                    "events": [event.to_dict() for event in self._events[key]],
                    "result": (None if self._results.get(key) is None
                               else self._results[key].to_dict()),
                }
                for key in sorted(self._requests)
            },
        }

    def _persist_state_locked(self, *, protected: tuple[str, ...] = ()) -> None:
        prior_requests = dict(self._requests)
        prior_events = {key: list(events) for key, events in self._events.items()}
        prior_results = dict(self._results)
        prior_leases = dict(self._lease_states)
        prior_active = dict(self._active_leases)
        prior_tombstones = dict(self._tombstones)
        try:
            encoded = self._compact_state_locked(protected=protected)
            directory_fd = self._open_state_root()
            temporary_name = f".worker-state.{uuid.uuid4().hex}.tmp"
            try:
                file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                file_flags |= getattr(os, "O_NOFOLLOW", 0)
                temporary_fd = os.open(
                    temporary_name, file_flags, 0o600, dir_fd=directory_fd,
                )
                try:
                    with os.fdopen(temporary_fd, "wb", closefd=False) as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                finally:
                    os.close(temporary_fd)
                os.replace(
                    temporary_name, self._state_path.name,
                    src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                )
                os.fsync(directory_fd)
            finally:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                os.close(directory_fd)
        except Exception:
            self._requests = prior_requests
            self._events = prior_events
            self._results = prior_results
            self._lease_states = prior_leases
            self._active_leases = prior_active
            self._tombstones = prior_tombstones
            raise

    def _open_state_root(self) -> int:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(self.state_root, directory_flags)
        except OSError as exc:
            raise RuntimeError("state_root identity changed during worker lifetime") from exc
        state_root_stat = os.fstat(directory_fd)
        if (not stat.S_ISDIR(state_root_stat.st_mode)
                or (state_root_stat.st_dev, state_root_stat.st_ino)
                != self._state_root_identity):
            os.close(directory_fd)
            raise RuntimeError("state_root identity changed during worker lifetime")
        return directory_fd

    def _compact_state_locked(self, *, protected: tuple[str, ...] = ()) -> bytes:
        encoded = json.dumps(
            self._state_value_locked(), allow_nan=False, sort_keys=True,
        ).encode("utf-8")
        protected_ids = set(protected)
        while len(encoded) > self.max_state:
            candidates = sorted(
                invocation_id for invocation_id in self._requests
                if invocation_id in self._results
                and invocation_id not in protected_ids
                and invocation_id not in self._active_leases.values()
            )
            if candidates:
                evicted = candidates[0]
                self._tombstones[evicted] = self._request_digest(
                    self._requests[evicted]
                )
                del self._requests[evicted]
                self._events.pop(evicted, None)
                self._results.pop(evicted, None)
            else:
                referenced_leases = {
                    request.lease_id for request in self._requests.values()
                }
                closed = sorted(
                    lease_id for lease_id, state in self._lease_states.items()
                    if state.status == "closed" and lease_id not in referenced_leases
                )
                if not closed:
                    raise ValueError("worker state exceeds configured size limit")
                del self._lease_states[closed[0]]
            encoded = json.dumps(
                self._state_value_locked(), allow_nan=False, sort_keys=True,
            ).encode("utf-8")
        return encoded

    def _bounded_result(self, request: InvocationRequest,
                        result: InvocationResult) -> InvocationResult:
        if (len(result.stdout.encode("utf-8")) <= MAX_PERSISTED_STREAM
                and len(result.stderr.encode("utf-8")) <= MAX_PERSISTED_STREAM
                and len(result.to_json().encode("utf-8"))
                <= min(self.max_response, MAX_PERSISTED_STREAM * 2)):
            return result
        return self._journal_failure(request)

    def _journal_failure(self, request: InvocationRequest) -> InvocationResult:
        return InvocationResult(
            invocation_id=request.invocation_id, run_id=request.run_id,
            work_item_id=request.work_item_id,
            worker_id=self.worker_capabilities.worker_id,
            status="failed", exit_code=None, stdout="",
            stderr="worker result exceeded durable journal capacity and was rejected",
            lease_id=request.lease_id,
        )

    def artifact(self, invocation_id: str, digest: str) -> bytes | None:
        with self._lock:
            result = self._results.get(invocation_id)
        if result is None:
            return None
        matches = [item for item in result.artifacts if item.sha256 == digest]
        if len(matches) != 1:
            return None
        artifact = matches[0]
        data = self.worker.fetch_artifact(invocation_id, artifact)
        if len(data) != artifact.size or hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise ValueError("worker artifact bytes do not match declared size and digest")
        return data


class RemoteWorkerHTTPRequestHandler(BaseHTTPRequestHandler):
    server: RemoteWorkerHTTPServer

    def do_GET(self) -> None:
        if not self._authenticated():
            return self._error(HTTPStatus.UNAUTHORIZED, "authentication required")
        parsed = urlparse(self.path)
        if parsed.path == "/v1/capabilities":
            return self._json(HTTPStatus.OK, self.server.worker_capabilities.to_dict())
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 5 and parts[:2] == ["v1", "invocations"] and parts[3] == "artifacts":
            invocation_id = unquote(parts[2])
            if self.server.is_pruned(invocation_id):
                return self._error(
                    HTTPStatus.GONE, "terminal invocation history was pruned"
                )
            try:
                data = self.server.artifact(invocation_id, parts[4])
            except ValueError as exc:
                return self._error(HTTPStatus.BAD_GATEWAY, str(exc))
            if data is None:
                return self._error(HTTPStatus.NOT_FOUND, "unknown invocation artifact")
            return self._bytes(HTTPStatus.OK, data)
        if len(parts) == 4 and parts[:2] == ["v1", "invocations"]:
            invocation_id, resource = unquote(parts[2]), parts[3]
            if self.server.is_pruned(invocation_id):
                return self._error(
                    HTTPStatus.GONE, "terminal invocation history was pruned"
                )
            if resource == "events":
                try:
                    after = int(parse_qs(parsed.query).get("after", ["0"])[0])
                except ValueError:
                    return self._error(HTTPStatus.BAD_REQUEST, "after must be an integer")
                if after < 0:
                    return self._error(HTTPStatus.BAD_REQUEST, "after must be non-negative")
                events = self.server.events_after(invocation_id, after)
                if events is None:
                    return self._error(HTTPStatus.NOT_FOUND, "unknown invocation")
                return self._json(HTTPStatus.OK, {"events": [event.to_dict() for event in events]})
            if resource == "result":
                exists, result = self.server.result(invocation_id)
                if not exists:
                    return self._error(HTTPStatus.NOT_FOUND, "unknown invocation")
                if result is None:
                    return self._json(HTTPStatus.ACCEPTED, {"status": "running"})
                return self._json(HTTPStatus.OK, result.to_dict())
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        if not self._authenticated():
            return self._error(HTTPStatus.UNAUTHORIZED, "authentication required")
        parsed = urlparse(self.path)
        if parsed.path == "/v1/invocations":
            try:
                request = InvocationRequest.from_dict(self._read_json())
                submitted = self.server.submit(request)
            except FileExistsError as exc:
                return self._error(HTTPStatus.CONFLICT, str(exc))
            except PermissionError as exc:
                return self._error(HTTPStatus.FORBIDDEN, str(exc))
            except (KeyError, TypeError, ValueError) as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return self._json(HTTPStatus.ACCEPTED, {
                "invocation_id": request.invocation_id,
                "status": submitted,
            })
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["v1", "invocations"] and parts[3] in {
            "cancel", "attach",
        }:
            invocation_id = unquote(parts[2])
            try:
                self._read_json()
                if parts[3] == "cancel":
                    return self._json(HTTPStatus.OK, {
                        "invocation_id": invocation_id,
                        "confirmed": self.server.cancel(invocation_id),
                    })
                return self._json(HTTPStatus.OK, {
                    "invocation_id": invocation_id,
                    "attach_url": self.server.attach(invocation_id),
                })
            except PrunedInvocationError as exc:
                return self._error(HTTPStatus.GONE, str(exc))
            except KeyError as exc:
                return self._error(HTTPStatus.NOT_FOUND, str(exc))
            except (TypeError, ValueError) as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception as exc:
                return self._error(
                    HTTPStatus.BAD_GATEWAY,
                    f"worker {parts[3]} failed ({type(exc).__name__})",
                )
        if len(parts) == 4 and parts[:2] == ["v1", "leases"] and parts[3] in {
            "setup", "reset", "teardown",
        }:
            try:
                request = WorkerLeaseRequest.from_dict(self._read_json())
                if unquote(parts[2]) != request.lease_id:
                    raise ValueError("lease path and body identity differ")
                state = self.server.lifecycle(parts[3], request)
            except PermissionError as exc:
                return self._error(HTTPStatus.FORBIDDEN, str(exc))
            except (KeyError, TypeError, ValueError) as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception as exc:
                return self._error(
                    HTTPStatus.BAD_GATEWAY,
                    f"worker lifecycle failed ({type(exc).__name__})",
                )
            return self._json(HTTPStatus.OK, state.to_dict())
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def _authenticated(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.auth_token}"
        return hmac.compare_digest(supplied, expected)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise ValueError("valid Content-Length is required") from exc
        if length < 1 or length > self.server.max_body:
            raise ValueError(f"request body must be 1..{self.server.max_body} bytes")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type must be application/json")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        body = json.dumps(value, allow_nan=False, sort_keys=True).encode("utf-8")
        if len(body) > self.server.max_response:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "response exceeds size limit")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, status: HTTPStatus, body: bytes) -> None:
        if len(body) > self.server.max_response:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "response exceeds size limit")
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str) -> None:
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class HTTPWorkerTransport:
    """Controller-side client for a RemoteWorkerHTTPServer-compatible endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        auth_token: str,
        timeout: float = 10.0,
        result_timeout: float = 180.0,
        poll_interval: float = 0.05,
        max_response: int = DEFAULT_MAX_RESPONSE,
        allow_loopback_http: bool = False,
    ):
        if not auth_token:
            raise ValueError("auth_token must be explicit and non-empty")
        if timeout <= 0 or result_timeout <= 0 or poll_interval <= 0 or max_response < 1:
            raise ValueError("timeouts, poll interval, and response limit must be positive")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        hostname = _validated_url_host(parsed, "base_url")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain userinfo")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        if parsed.scheme == "http" and (
            not allow_loopback_http or not _is_loopback_host(hostname)
        ):
            raise ValueError(
                "plaintext HTTP is allowed only for loopback with explicit development opt-in"
            )
        if parsed.path not in {"", "/"}:
            raise ValueError("base_url path prefixes are not supported")
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.timeout = timeout
        self.result_timeout = result_timeout
        self.poll_interval = poll_interval
        self.max_response = max_response
        self.allow_loopback_http = allow_loopback_http

    def capabilities(self) -> WorkerCapabilities:
        _, value = self._request("GET", "/v1/capabilities")
        return WorkerCapabilities.from_dict(value)

    def _lease(self, action: str, request: WorkerLeaseRequest) -> WorkerLeaseState:
        path = f"/v1/leases/{quote(request.lease_id, safe='')}/{action}"
        _, value = self._request("POST", path, request.to_dict())
        return WorkerLeaseState.from_dict(value)

    def setup_lease(self, request: WorkerLeaseRequest) -> WorkerLeaseState:
        return self._lease("setup", request)

    def reset_lease(self, request: WorkerLeaseRequest) -> WorkerLeaseState:
        return self._lease("reset", request)

    def teardown_lease(self, request: WorkerLeaseRequest) -> WorkerLeaseState:
        return self._lease("teardown", request)

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        _, submission = self._request(
            "POST", "/v1/invocations", request.to_dict(), expected=(202,),
        )
        if submission.get("status") == "pruned":
            raise RemoteWorkerError("terminal invocation history was pruned")
        deadline = time.monotonic() + self.result_timeout
        path = f"/v1/invocations/{quote(request.invocation_id, safe='')}/result"
        while time.monotonic() < deadline:
            status, value = self._request("GET", path, expected=(200, 202))
            if status == 200:
                result = InvocationResult.from_dict(value)
                if (result.manifest_digest is not None
                        and result.manifest_digest != request.expected_manifest_digest):
                    raise RemoteWorkerError(
                        "remote result manifest digest does not match the leased invocation"
                    )
                if (result.status == "done" and request.expected_manifest_digest is not None
                        and result.manifest_digest is None):
                    raise RemoteWorkerError(
                        "remote result did not attest the leased invocation manifest"
                    )
                return result
            time.sleep(self.poll_interval)
        raise TimeoutError(f"timed out waiting for invocation {request.invocation_id}")

    def events(self, invocation_id: str, *, after: int = 0) -> tuple[WorkerEvent, ...]:
        path = f"/v1/invocations/{quote(invocation_id, safe='')}/events?after={after}"
        _, value = self._request("GET", path)
        return tuple(WorkerEvent.from_dict(item) for item in value["events"])

    def fetch_artifact(self, invocation_id: str, artifact: ArtifactRecord) -> bytes:
        path = (f"/v1/invocations/{quote(invocation_id, safe='')}/artifacts/"
                f"{artifact.sha256}")
        return self._request_bytes("GET", path)

    def cancel(self, invocation_id: str) -> bool:
        path = f"/v1/invocations/{quote(invocation_id, safe='')}/cancel"
        _, value = self._request("POST", path, {})
        confirmed = value.get("confirmed")
        if not isinstance(confirmed, bool):
            raise RemoteWorkerError("remote cancellation response must contain a boolean")
        return confirmed

    def attach_url(self, invocation_id: str) -> str | None:
        path = f"/v1/invocations/{quote(invocation_id, safe='')}/attach"
        _, value = self._request("POST", path, {})
        url = value.get("attach_url")
        if url is not None and (not isinstance(url, str) or not url.strip()):
            raise RemoteWorkerError("remote attach response contains an invalid URL")
        if url is not None:
            try:
                _validate_attach_url(url)
            except ValueError as exc:
                raise RemoteWorkerError(str(exc)) from exc
        return url

    def _request(
        self, method: str, path: str, value: dict[str, Any] | None = None,
        *, expected: tuple[int, ...] = (200,),
    ) -> tuple[int, dict[str, Any]]:
        body = None if value is None else json.dumps(value, allow_nan=False).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.auth_token}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        try:
            response = urlopen(request, timeout=self.timeout)
            with response:
                status = response.status
                raw = response.read(self.max_response + 1)
        except HTTPError as exc:
            raw = exc.read(self.max_response + 1)
            try:
                detail = json.loads(raw).get("error", exc.reason)
            except (AttributeError, json.JSONDecodeError):
                detail = exc.reason
            raise RemoteWorkerError(f"remote worker HTTP {exc.code}: {detail}") from exc
        if status not in expected:
            raise RemoteWorkerError(f"unexpected remote worker HTTP status {status}")
        if len(raw) > self.max_response:
            raise RemoteWorkerError("remote worker response exceeds size limit")
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise RemoteWorkerError("remote worker response must be a JSON object")
        return status, decoded

    def _request_bytes(self, method: str, path: str) -> bytes:
        request = Request(
            self.base_url + path, method=method,
            headers={"Authorization": f"Bearer {self.auth_token}",
                     "Accept": "application/octet-stream"},
        )
        try:
            response = urlopen(request, timeout=self.timeout)
            with response:
                raw = response.read(self.max_response + 1)
        except HTTPError as exc:
            raise RemoteWorkerError(f"remote worker HTTP {exc.code}: artifact unavailable") from exc
        if len(raw) > self.max_response:
            raise RemoteWorkerError("remote worker response exceeds size limit")
        return raw


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_attach_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.netloc:
        raise ValueError("worker attach URL must use an absolute HTTP(S) or WS(S) URL")
    hostname = _validated_url_host(parsed, "worker attach URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("worker attach URL must not contain userinfo")
    if parsed.scheme in {"http", "ws"} and not _is_loopback_host(hostname):
        raise ValueError("plaintext worker attach URL is allowed only for loopback")


def _validated_url_host(parsed: Any, label: str) -> str:
    try:
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} must contain a valid host and port") from exc
    if not isinstance(hostname, str) or not hostname.strip():
        raise ValueError(f"{label} must contain a valid host and port")
    return hostname
