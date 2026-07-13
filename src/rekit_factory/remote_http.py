"""Minimal authenticated HTTP transport for versioned Rekit worker envelopes."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ipaddress
from pathlib import Path, PurePosixPath
import threading
import time
from typing import Any
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


class RemoteWorkerError(RuntimeError):
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
        allowed_network_policies: tuple[NetworkPolicy, ...] = ("none",),
        max_body: int = DEFAULT_MAX_BODY,
        max_response: int = DEFAULT_MAX_RESPONSE,
    ):
        if not auth_token:
            raise ValueError("auth_token must be explicit and non-empty")
        if max_body < 1 or max_response < 1:
            raise ValueError("HTTP size limits must be positive")
        self.worker = worker
        self.worker_capabilities = worker.capabilities()
        self.auth_token = auth_token
        self.input_root = Path(input_root).expanduser().resolve()
        self.allowed_network_policies = frozenset(allowed_network_policies)
        if not self.allowed_network_policies:
            raise ValueError("at least one network policy must be allowed")
        self.max_body = max_body
        self.max_response = max_response
        self._lock = threading.Lock()
        self._lease_state_path = self.input_root / ".rekit-worker-leases.json"
        self._lease_states = self._load_lease_states()
        self._active_leases: dict[str, str] = {}
        self._events: dict[str, list[WorkerEvent]] = {}
        self._results: dict[str, InvocationResult] = {}
        super().__init__(address, RemoteWorkerHTTPRequestHandler)

    def submit(self, request: InvocationRequest) -> None:
        if request.lease_id is None:
            raise PermissionError("remote invocation requires an explicit worker lease")
        if request.network_policy not in self.allowed_network_policies:
            raise PermissionError(
                f"network policy {request.network_policy!r} is not enabled on this worker"
            )
        if request.mount_policy != "staged-input-read-only":
            raise PermissionError("remote invocations require staged-input-read-only policy")
        staged = PurePosixPath(request.target_path)
        if staged.is_absolute() or ".." in staged.parts:
            raise ValueError("remote target_path must be relative to the staged input root")
        target = (self.input_root / Path(*staged.parts)).resolve()
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
            lease = self._lease_states.get(request.lease_id)
            if lease is None or lease.status != "ready":
                raise PermissionError("remote worker lease is not clean and ready")
            if (lease.run_id, lease.work_item_id, lease.worker_id) != (
                request.run_id, request.work_item_id, worker_id,
            ):
                raise PermissionError("remote invocation does not match leased authority")
            if request.lease_id in self._active_leases:
                raise PermissionError("remote worker lease already has an active invocation")
            if request.invocation_id in self._events:
                raise FileExistsError("invocation_id already exists")
            self._lease_states[lease.lease_id] = replace(lease, status="dirty")
            self._active_leases[lease.lease_id] = request.invocation_id
            self._persist_lease_states_locked()
            self._events[request.invocation_id] = [accepted]
        threading.Thread(
            target=self._run_invocation,
            args=(local_request,),
            name=f"remote-worker-{request.invocation_id}",
            daemon=True,
        ).start()

    def _validate_scope(self, request: InvocationRequest, target: Path) -> None:
        validate_invocation_scope(request, target)

    def _run_invocation(self, request: InvocationRequest) -> None:
        self._append_event(request, "invocation.started", "Invocation started")
        try:
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
        except Exception as exc:
            result = InvocationResult(
                invocation_id=request.invocation_id,
                run_id=request.run_id,
                work_item_id=request.work_item_id,
                worker_id=self.worker_capabilities.worker_id,
                status="failed",
                exit_code=None,
                stdout="",
                stderr=f"worker invocation failed ({type(exc).__name__})",
                lease_id=request.lease_id,
            )
        self._append_event(
            request,
            f"invocation.{result.status}",
            f"Invocation {result.status}",
            {"exitCode": result.exit_code},
        )
        with self._lock:
            self._results[request.invocation_id] = result
            if request.lease_id is not None:
                self._active_leases.pop(request.lease_id, None)

    def _append_event(
        self, request: InvocationRequest, kind: str, message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            events = self._events[request.invocation_id]
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

    def events_after(self, invocation_id: str, sequence: int) -> list[WorkerEvent] | None:
        with self._lock:
            events = self._events.get(invocation_id)
            return None if events is None else [event for event in events if event.sequence > sequence]

    def result(self, invocation_id: str) -> tuple[bool, InvocationResult | None]:
        with self._lock:
            return invocation_id in self._events, self._results.get(invocation_id)

    def lifecycle(self, action: str, request: WorkerLeaseRequest) -> WorkerLeaseState:
        if request.worker_id != self.worker_capabilities.worker_id:
            raise PermissionError("lease worker identity does not match endpoint")
        with self._lock:
            current = self._lease_states.get(request.lease_id)
            active_invocation = self._active_leases.get(request.lease_id)
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
                raise PermissionError("lease identity is already bound to different authority")
            return current
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
            self._lease_states[state.lease_id] = state
            self._persist_lease_states_locked()
        return state

    def _load_lease_states(self) -> dict[str, WorkerLeaseState]:
        if not self._lease_state_path.exists():
            return {}
        value = json.loads(self._lease_state_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("worker lease state file must contain an object")
        states = {key: WorkerLeaseState.from_dict(item) for key, item in value.items()}
        return {
            key: (state if state.status == "closed" else replace(state, status="dirty"))
            for key, state in states.items()
        }

    def _persist_lease_states_locked(self) -> None:
        temporary = self._lease_state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(
            {key: state.to_dict() for key, state in sorted(self._lease_states.items())},
            allow_nan=False, sort_keys=True,
        ), encoding="utf-8")
        temporary.replace(self._lease_state_path)

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
            try:
                data = self.server.artifact(unquote(parts[2]), parts[4])
            except ValueError as exc:
                return self._error(HTTPStatus.BAD_GATEWAY, str(exc))
            if data is None:
                return self._error(HTTPStatus.NOT_FOUND, "unknown invocation artifact")
            return self._bytes(HTTPStatus.OK, data)
        if len(parts) == 4 and parts[:2] == ["v1", "invocations"]:
            invocation_id, resource = unquote(parts[2]), parts[3]
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
                self.server.submit(request)
            except FileExistsError as exc:
                return self._error(HTTPStatus.CONFLICT, str(exc))
            except PermissionError as exc:
                return self._error(HTTPStatus.FORBIDDEN, str(exc))
            except (KeyError, TypeError, ValueError) as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return self._json(HTTPStatus.ACCEPTED, {
                "invocation_id": request.invocation_id,
                "status": "accepted",
            })
        parts = parsed.path.strip("/").split("/")
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
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain userinfo")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        if parsed.scheme == "http" and (
            not allow_loopback_http or not _is_loopback_host(parsed.hostname)
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
        self._request("POST", "/v1/invocations", request.to_dict(), expected=(202,))
        deadline = time.monotonic() + self.result_timeout
        path = f"/v1/invocations/{quote(request.invocation_id, safe='')}/result"
        while time.monotonic() < deadline:
            status, value = self._request("GET", path, expected=(200, 202))
            if status == 200:
                return InvocationResult.from_dict(value)
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
        return False  # Cancellation requires adapter-specific interrupt semantics.

    def attach_url(self, invocation_id: str) -> str | None:
        return None  # Interactive attachment is intentionally outside this slice.

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
