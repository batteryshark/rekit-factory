"""Loopback-only JSON API and resumable SSE feed for Mission Control."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
import uuid

from rekit_factory.control import InvestigationController, RunRequest
from rekit_factory.evidence import EvidenceStore
from rekit_factory.dossiers import DossierNotReady, dossier_list, verify_published_dossier
from rekit_factory.scope import AuthorizedScope
from rekit_factory.store import FactoryLedger
from rekit_factory.strategies import DEFAULT_STRATEGIES


MAX_BODY = 1_000_000
UI_ASSETS = {
    "mission-control.css": "text/css; charset=utf-8",
    "mission-control.js": "text/javascript; charset=utf-8",
}


class DriveSupervisor:
    """Own one asyncio loop so every model worker runs on a stable event loop."""

    def __init__(self, controller: InvestigationController):
        self.controller = controller
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="factory-drives", daemon=True)
        self.thread.start()
        self._active: dict[str, Future] = {}
        self._lock = threading.Lock()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, run_dir: Path) -> bool:
        run_id = _run_meta(run_dir)["runId"]
        with self._lock:
            current = self._active.get(run_id)
            if current is not None and not current.done():
                return False
            future = asyncio.run_coroutine_threadsafe(self.controller.drive(run_dir), self.loop)
            self._active[run_id] = future
            future.add_done_callback(lambda _future: self._forget(run_id, _future))
            return True

    def _forget(self, run_id: str, future: Future) -> None:
        # Retrieve the exception so failed background drives do not emit an unhandled warning.
        try:
            future.result()
        except Exception:
            pass
        with self._lock:
            if self._active.get(run_id) is future:
                self._active.pop(run_id, None)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


class FactoryServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, controller: InvestigationController, *, allow_restart: bool = False):
        host = address[0]
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Factory API must bind to a loopback address")
        super().__init__(address, FactoryHandler)
        self.controller = controller
        self.storage_root = controller.storage_root.resolve()
        self.supervisor = DriveSupervisor(controller)
        self.allow_restart = allow_restart
        self.instance_id = uuid.uuid4().hex
        self.restart_requested = threading.Event()

    def request_restart(self) -> None:
        """Finish the current response, then return control to the CLI for re-exec."""
        if not self.allow_restart:
            raise RuntimeError("service restart is unavailable for this server")
        self.restart_requested.set()
        threading.Thread(target=self.shutdown, name="factory-restart", daemon=True).start()

    def server_close(self) -> None:
        if hasattr(self, "supervisor"):
            self.supervisor.close()
        super().server_close()


class FactoryHandler(BaseHTTPRequestHandler):
    server: FactoryServer

    def log_message(self, format: str, *args) -> None:
        # Keep operator output concise; run activity belongs in the durable event stream.
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        try:
            if parts in ([], ["mission-control"]):
                self._html((Path(__file__).with_name("ui") / "index.html").read_bytes())
                return
            if len(parts) == 2 and parts[0] == "ui" and parts[1] in UI_ASSETS:
                self._asset(parts[1], UI_ASSETS[parts[1]])
                return
            if parts == ["api", "config"]:
                tools = []
                if hasattr(self.server.controller.rekit, "list_tools"):
                    tools = [{
                        **tool.__dict__, "requires_permission": tool.requires_permission,
                    } for tool in self.server.controller.rekit.list_tools()]
                self._json(HTTPStatus.OK, {
                    "serviceInstance": self.server.instance_id,
                    "restartAvailable": self.server.allow_restart,
                    "storageRoot": str(self.server.storage_root),
                    "modelProfile": self.server.controller.workers.profile.public_dict(),
                    "modelProfiles": [backend.profile.public_dict()
                                      for backend in self.server.controller.worker_backends.values()],
                    "defaultModelProfile": self.server.controller.default_profile,
                    "strategies": [
                        {"name": item.name, "description": item.description}
                        for item in DEFAULT_STRATEGIES.values()
                    ],
                    "knowledgeRoots": [
                        {"name": root.name}
                        for root in (self.server.controller.knowledge.roots
                                     if self.server.controller.knowledge else ())
                    ],
                    "tools": tools,
                })
                return
            if parts == ["api", "fleet"]:
                self._json(HTTPStatus.OK, {"runs": _fleet(self.server.controller)})
                return
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                run_dir = _find_run(self.server.storage_root, parts[2])
                self._json(HTTPStatus.OK, self.server.controller.snapshot(run_dir))
                return
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "reports":
                run_dir = _find_run(self.server.storage_root, parts[2])
                snapshot = self.server.controller.snapshot(run_dir)
                self._json(HTTPStatus.OK, {"reports": _worker_reports(snapshot)})
                return
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "dossiers":
                run_dir = _find_run(self.server.storage_root, parts[2])
                with FactoryLedger(run_dir / "run.db") as ledger:
                    dossiers = dossier_list(ledger, parts[2], run_dir=run_dir)
                self._json(HTTPStatus.OK, {"runId": parts[2], "dossiers": dossiers})
                return
            if (len(parts) in {5, 6} and parts[:2] == ["api", "runs"]
                    and parts[3] == "dossiers"):
                run_dir = _find_run(self.server.storage_root, parts[2])
                snapshot = self.server.controller.snapshot(run_dir)
                with FactoryLedger(run_dir / "run.db") as ledger:
                    dossiers = dossier_list(ledger, parts[2], run_dir=run_dir)
                dossier = next(
                    (item for item in dossiers if item["id"] == parts[4]), None
                )
                if dossier is None:
                    raise FileNotFoundError(f"unknown dossier {parts[4]}")
                if not dossier["verified"]:
                    raise DossierNotReady("dossier is stale or invalid; republish from current state")
                dossier["runId"] = parts[2]
                verify_published_dossier(run_dir, dossier)
                kind = "proof-export" if len(parts) == 6 and parts[5] == "download" \
                    else "proof-report-html" if len(parts) == 5 else None
                if kind is None:
                    raise FileNotFoundError("unknown dossier resource")
                artifact_id = dossier["artifactIds"][kind]
                artifact = next(
                    item for item in snapshot["artifacts"] if item["id"] == artifact_id
                )
                path = _contained_artifact_path(run_dir, artifact)
                if kind == "proof-report-html":
                    self._dossier_html(path.read_bytes())
                else:
                    self._download(path, artifact["logical_path"])
                return
            if (len(parts) == 5 and parts[:2] == ["api", "runs"]
                    and parts[3] == "artifacts"):
                run_dir = _find_run(self.server.storage_root, parts[2])
                snapshot = self.server.controller.snapshot(run_dir)
                artifact = next(
                    (item for item in snapshot["artifacts"] if item["id"] == parts[4]), None
                )
                if artifact is None:
                    raise FileNotFoundError(f"unknown artifact {parts[4]}")
                path = _contained_artifact_path(run_dir, artifact)
                self._download(path, artifact["logical_path"])
                return
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "evidence":
                run_dir = _find_run(self.server.storage_root, parts[2])
                evidence_root = run_dir / "evidence"
                if not (evidence_root / "evidence.sqlite3").is_file():
                    self._json(HTTPStatus.OK, {"runId": parts[2], "records": []})
                    return
                records = EvidenceStore(evidence_root).public_records(parts[2])
                self._json(HTTPStatus.OK, {"runId": parts[2], "records": records})
                return
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "events":
                run_dir = _find_run(self.server.storage_root, parts[2])
                after = parse_qs(parsed.query).get("after", [None])[0]
                after = self.headers.get("Last-Event-ID") or after
                self._events(run_dir, after)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except FileNotFoundError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except PermissionError as exc:
            self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
        except DossierNotReady as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except Exception as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                       {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        try:
            payload = self._body()
            if parts == ["api", "restart"]:
                if not self.server.allow_restart:
                    self._json(HTTPStatus.CONFLICT, {
                        "error": "service restart is unavailable for this server",
                    })
                    return
                self._json(HTTPStatus.ACCEPTED, {
                    "restarting": True,
                    "serviceInstance": self.server.instance_id,
                })
                self.server.request_restart()
                return
            if parts == ["api", "runs"]:
                request = RunRequest(
                    target=Path(payload["target"]),
                    goal=str(payload["goal"]),
                    tools=tuple(payload.get("tools", [])),
                    model_tools=tuple(payload.get("modelTools", [])),
                    worker_roles=tuple(payload.get("workerRoles") or ("recon", "analyst")),
                    concurrency=int(payload.get("concurrency", 4)),
                    model_profile=payload.get("modelProfile"),
                    strategy=payload.get("strategy"),
                    retries_per_worker=int(payload.get("retriesPerWorker", 1)),
                    cost_units=int(payload.get("costUnits", 100)),
                    max_workers=int(payload.get("maxWorkers", 8)),
                    scope=(AuthorizedScope.from_dict(payload["scope"])
                           if payload.get("scope") is not None else None),
                )
                run_dir = self.server.controller.create(request)
                self.server.supervisor.submit(run_dir)
                snapshot = self.server.controller.snapshot(run_dir)
                snapshot["runDir"] = str(run_dir)
                self._json(HTTPStatus.ACCEPTED, snapshot)
                return
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "resume":
                run_dir = _find_run(self.server.storage_root, parts[2])
                started = self.server.supervisor.submit(run_dir)
                self._json(HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT,
                           {"runId": parts[2], "started": started})
                return
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "answers":
                run_dir = _find_run(self.server.storage_root, parts[2])
                result = self.server.controller.answer(
                    run_dir, str(payload["questionId"]), str(payload["answer"]), resume=False
                )
                started = self.server.supervisor.submit(run_dir)
                self._json(HTTPStatus.ACCEPTED, {
                    "runId": parts[2], "started": started,
                    "pendingQuestions": result["pendingQuestions"],
                })
                return
            if len(parts) == 6 and parts[:2] == ["api", "runs"] \
                    and parts[3] == "evidence":
                run_dir = _find_run(self.server.storage_root, parts[2])
                evidence_root = run_dir / "evidence"
                if not (evidence_root / "evidence.sqlite3").is_file():
                    raise FileNotFoundError(f"run has no evidence store: {parts[2]}")
                store = EvidenceStore(evidence_root)
                artifact_id, action = parts[4], parts[5]
                citation_id = str(payload.get("citationId") or f"operator:{parts[2]}")
                if action == "pin":
                    event = store.pin(artifact_id, citation_id)
                elif action == "unpin":
                    event = store.unpin(artifact_id, citation_id)
                elif action == "hold":
                    event = store.hold(artifact_id, True)
                elif action == "unhold":
                    event = store.hold(artifact_id, False)
                elif action == "delete":
                    event = store.request_delete(artifact_id)
                else:
                    raise ValueError(f"unknown evidence action: {action}")
                record = next(item for item in store.public_records(parts[2])
                              if item["artifactId"] == artifact_id)
                self._json(HTTPStatus.OK, {
                    "runId": parts[2], "record": record,
                    "event": {"action": event.action.value, "reason": event.reason,
                              "payload": event.payload, "createdAt": event.created_at},
                })
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (KeyError, TypeError, ValueError, FileNotFoundError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": f"{type(exc).__name__}: {exc}"})
        except Exception as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                       {"error": f"{type(exc).__name__}: {exc}"})

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > MAX_BODY:
            raise ValueError(f"request body must be 1..{MAX_BODY} bytes")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise TypeError("JSON body must be an object")
        return value

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: bytes) -> None:
        self._static(body, "text/html; charset=utf-8")

    def _dossier_html(self, body: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _asset(self, name: str, content_type: str) -> None:
        body = (Path(__file__).with_name("ui") / name).read_bytes()
        self._static(body, content_type)

    def _download(self, path: Path, logical_path: str) -> None:
        size = path.stat().st_size
        filename = Path(logical_path).name.replace('"', "") or "artifact.bin"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with path.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                self.wfile.write(chunk)

    def _static(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _events(self, run_dir: Path, after: str | None) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        cursor = after
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                events = self.server.controller.snapshot(run_dir)["events"]
                fresh = _after(events, cursor)
                for event in fresh:
                    encoded = json.dumps(event, sort_keys=True)
                    self.wfile.write(
                        f"id: {event['id']}\nevent: message\ndata: {encoded}\n\n".encode()
                    )
                    cursor = event["id"]
                if fresh:
                    self.wfile.flush()
                time.sleep(0.25)
            self.wfile.write(b"event: heartbeat\ndata: {}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def _after(events: list[dict[str, Any]], cursor: str | None) -> list[dict[str, Any]]:
    if cursor is None:
        return events
    for index, event in enumerate(events):
        if event["id"] == cursor:
            return events[index + 1:]
    return events


def _run_meta(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))


def _contained_artifact_path(run_dir: Path, artifact: dict[str, Any]) -> Path:
    path = Path(artifact["path"]).resolve()
    try:
        path.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise PermissionError("artifact path leaves its run directory") from exc
    if not path.is_file():
        raise FileNotFoundError(f"artifact file is unavailable: {artifact['logical_path']}")
    return path


def _run_dirs(storage_root: Path) -> list[Path]:
    if not storage_root.is_dir():
        return []
    return sorted(
        (path.parent for path in storage_root.glob("projects/*/runs/*/run.json")),
        key=lambda path: path.name,
        reverse=True,
    )


def _find_run(storage_root: Path, run_id: str) -> Path:
    for run_dir in _run_dirs(storage_root):
        if _run_meta(run_dir).get("runId") == run_id:
            return run_dir
    raise FileNotFoundError(f"unknown run {run_id}")


def _fleet(controller: InvestigationController) -> list[dict[str, Any]]:
    cards = []
    for run_dir in _run_dirs(controller.storage_root):
        snapshot = controller.snapshot(run_dir)
        run = snapshot["run"]
        meta = snapshot["meta"]
        cards.append({
            "runId": run["id"],
            "runDir": str(run_dir),
            "projectId": run["project_id"],
            "target": meta["target"],
            "goal": meta["goal"],
            "status": run["status"],
            "createdAt": run["created_at"],
            "updatedAt": run["updated_at"],
            "completedAt": run["completed_at"],
            "iteration": run["iteration"],
            "maxIterations": run["max_iterations"],
            "modelProfile": meta["modelProfile"],
            "coverage": snapshot["coverage"],
            "workers": snapshot["workers"],
            "needsYou": len(snapshot["pendingQuestions"]),
            "latestEvent": snapshot["events"][-1] if snapshot["events"] else None,
        })
    return cards


def _worker_reports(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    reports = []
    for item in snapshot.get("workItems", []):
        result = item.get("result")
        if not isinstance(result, dict) or not any(
                key in result for key in ("summary", "observations", "next_actions")):
            continue
        payload = item.get("payload") or {}
        reports.append({
            "id": item["id"],
            "role": payload.get("role") or item.get("category") or "worker",
            "title": item.get("title") or "Worker report",
            "summary": result.get("summary") or "Report completed.",
            "observations": result.get("observations") or [],
            "nextActions": result.get("next_actions") or result.get("nextActions") or [],
            "status": result.get("status_update") or result.get("statusUpdate")
                      or item.get("state_label") or item.get("status"),
        })
    return reports


def serve(controller: InvestigationController, *, host: str = "127.0.0.1",
          port: int = 8768) -> bool:
    """Serve until stopped, returning true when the CLI should re-exec itself."""
    server = FactoryServer((host, port), controller, allow_restart=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return server.restart_requested.is_set()
