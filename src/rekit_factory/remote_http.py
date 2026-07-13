"""Minimal authenticated HTTP transport for versioned Rekit worker envelopes."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path, PurePosixPath
import threading
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

from rekit_factory.remote import (
    InvocationRequest,
    InvocationResult,
    NetworkPolicy,
    WorkerCapabilities,
    WorkerEvent,
    WorkerTransport,
)
from rekit_factory.scope import (
    ActionAuthority,
    AuthorizedScope,
    NetworkMode,
    hash_path,
    normalize_endpoint,
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
        self._events: dict[str, list[WorkerEvent]] = {}
        self._results: dict[str, InvocationResult] = {}
        super().__init__(address, RemoteWorkerHTTPRequestHandler)

    def submit(self, request: InvocationRequest) -> None:
        if request.network_policy not in self.allowed_network_policies:
            raise PermissionError(
                f"network policy {request.network_policy!r} is not enabled on this worker"
            )
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
            if request.invocation_id in self._events:
                raise FileExistsError("invocation_id already exists")
            self._events[request.invocation_id] = [accepted]
        threading.Thread(
            target=self._run_invocation,
            args=(local_request,),
            name=f"remote-worker-{request.invocation_id}",
            daemon=True,
        ).start()

    def _validate_scope(self, request: InvocationRequest, target: Path) -> None:
        if request.scope_revision is None:
            if (request.network_policy != "none" or request.account_ref is not None
                    or request.uses_credentials
                    or any(value != ActionAuthority.READ_LOCAL_TARGET.value
                           for value in request.requested_actions)):
                raise PermissionError("verified scope is required for remote external intent")
            return  # compatibility for existing network-none staged invocations
        scope = AuthorizedScope.from_dict(request.scope_revision)
        now = datetime.now(timezone.utc).isoformat()
        scope.validate(now=now)
        if request.scope_digest != scope.envelope.content_digest:
            raise PermissionError("remote scope digest does not match verified revision")
        try:
            actions = tuple(ActionAuthority(value) for value in request.requested_actions)
        except ValueError as exc:
            raise PermissionError("remote invocation contains an unknown action authority") from exc
        if any(action not in scope.envelope.actions
               or action in scope.envelope.prohibited_actions for action in actions):
            raise PermissionError("remote action is outside the verified scope")
        if request.account_ref is not None and request.account_ref not in scope.envelope.account_refs:
            raise PermissionError("remote account is outside the verified scope")
        if request.uses_credentials and not scope.envelope.credential_use:
            raise PermissionError("remote credential use is outside the verified scope")
        if request.uses_credentials and request.account_ref is None:
            raise PermissionError("remote credential use requires an opaque account reference")
        if request.target_sha256 is None or request.target_sha256 not in {
            target.content_sha256 for target in scope.envelope.targets
        }:
            raise PermissionError("remote target hash is outside the verified scope")
        if hash_path(target) != request.target_sha256:
            raise PermissionError("staged remote target does not match its authorized hash")
        if request.network_policy == "none":
            return
        if request.network_policy != "restricted":
            raise PermissionError("verified scope permits only none or exact restricted egress")
        if (scope.envelope.network_mode is not NetworkMode.EXACT_ENDPOINTS
                or ActionAuthority.NETWORK_ACCESS not in actions):
            raise PermissionError("verified scope does not authorize remote network access")
        if request.endpoint is None:
            raise PermissionError("exact endpoint is required for restricted remote egress")
        if normalize_endpoint(request.endpoint) not in scope.envelope.endpoints:
            raise PermissionError("remote endpoint is outside the verified scope")

    def _run_invocation(self, request: InvocationRequest) -> None:
        self._append_event(request, "invocation.started", "Invocation started")
        try:
            result = self.worker.invoke(request)
            expected = (
                request.invocation_id,
                request.run_id,
                request.work_item_id,
                self.worker_capabilities.worker_id,
            )
            actual = (
                result.invocation_id,
                result.run_id,
                result.work_item_id,
                result.worker_id,
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
            )
        self._append_event(
            request,
            f"invocation.{result.status}",
            f"Invocation {result.status}",
            {"exitCode": result.exit_code},
        )
        with self._lock:
            self._results[request.invocation_id] = result

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


class RemoteWorkerHTTPRequestHandler(BaseHTTPRequestHandler):
    server: RemoteWorkerHTTPServer

    def do_GET(self) -> None:
        if not self._authenticated():
            return self._error(HTTPStatus.UNAUTHORIZED, "authentication required")
        parsed = urlparse(self.path)
        if parsed.path == "/v1/capabilities":
            return self._json(HTTPStatus.OK, self.server.worker_capabilities.to_dict())
        parts = parsed.path.strip("/").split("/")
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
    ):
        if not auth_token:
            raise ValueError("auth_token must be explicit and non-empty")
        if timeout <= 0 or result_timeout <= 0 or poll_interval <= 0 or max_response < 1:
            raise ValueError("timeouts, poll interval, and response limit must be positive")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.timeout = timeout
        self.result_timeout = result_timeout
        self.poll_interval = poll_interval
        self.max_response = max_response

    def capabilities(self) -> WorkerCapabilities:
        _, value = self._request("GET", "/v1/capabilities")
        return WorkerCapabilities.from_dict(value)

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
