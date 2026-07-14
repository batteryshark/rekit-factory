"""Loopback-only deterministic automation boundary for external schedulers.

The gateway owns authentication, replay protection, command idempotency, audit, and a
redacted pull feed. It deliberately delegates all investigation, cancellation, handoff,
and dossier authority to ``AutomationOwner``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Callable, Mapping, Protocol
from urllib.parse import parse_qs, urlparse


AUTOMATION_VERSION = "factory-automation/v1"
MAX_AUTOMATION_BODY = 65_536
MAX_EVENTS = 100
MAX_CLOCK_SKEW_SECONDS = 300
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SECRET_KEYS = re.compile(
    r"(?:secret|token|password|credential|api[_-]?key|provider)", re.IGNORECASE,
)


class AutomationError(ValueError):
    """A bounded, public-safe automation request failure."""

    def __init__(self, code: str, status: int = 400):
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True)
class AutomationTemplate:
    """Environment-owned launch authority; clients submit only ``template_id``."""

    template_id: str
    revision: int
    target_ref: str
    scope_ref: str

    def __post_init__(self) -> None:
        for value, label in ((self.template_id, "template"),
                             (self.target_ref, "target"), (self.scope_ref, "scope")):
            if not isinstance(value, str) or _ID.fullmatch(value) is None:
                raise ValueError(f"automation {label} reference is invalid")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("automation template revision must be positive")


@dataclass(frozen=True)
class AutomationPrincipal:
    client_id: str
    secret: bytes
    requests_per_minute: int = 60

    def __post_init__(self) -> None:
        if _ID.fullmatch(self.client_id) is None:
            raise ValueError("automation client id is invalid")
        if not isinstance(self.secret, bytes) or len(self.secret) < 32:
            raise ValueError("automation HMAC keys must contain at least 32 bytes")
        if type(self.requests_per_minute) is not int \
                or not 1 <= self.requests_per_minute <= 600:
            raise ValueError("automation rate limit must be 1..600 requests per minute")


@dataclass(frozen=True)
class AutomationEvent:
    source_id: str
    run_id: str
    kind: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        for value in (self.source_id, self.run_id, self.kind):
            if not isinstance(value, str) or _ID.fullmatch(value) is None:
                raise ValueError("automation event identity is invalid")
        encoded = _canonical(dict(self.payload))
        if len(encoded) > MAX_AUTOMATION_BODY or _contains_secret(dict(self.payload)):
            raise ValueError("automation event is not a bounded redacted projection")


class AutomationOwner(Protocol):
    """Canonical owner adapter. Every mutating method must honor ``operation_id``."""

    def launch(self, template: AutomationTemplate, *, operation_id: str,
               origin: Mapping[str, object]) -> Mapping[str, object]: ...
    def cancel(self, run_id: str, *, reason_code: str,
               operation_id: str) -> Mapping[str, object]: ...
    def status(self, run_id: str) -> Mapping[str, object]: ...
    def acknowledge_handoff(self, run_id: str, handoff_id: str, *,
                            operation_id: str) -> Mapping[str, object]: ...
    def events(self, run_ids: tuple[str, ...]) -> tuple[AutomationEvent, ...]: ...
    def dossier(self, run_id: str, dossier_id: str) -> Mapping[str, object]: ...


SCHEMA = """
create table if not exists factory_automation_nonces (
    client_id text not null,
    nonce text not null,
    request_time integer not null,
    primary key(client_id,nonce)
);
create table if not exists factory_automation_commands (
    client_id text not null,
    idempotency_key text not null,
    command text not null,
    request_sha256 text not null,
    operation_id text not null unique,
    status text not null,
    result_json text,
    created_at text not null,
    updated_at text not null,
    primary key(client_id,idempotency_key)
);
create table if not exists factory_automation_runs (
    run_id text primary key,
    client_id text not null,
    operation_id text not null unique,
    origin_json text not null,
    created_at text not null
);
create table if not exists factory_automation_events (
    cursor integer primary key autoincrement,
    source_id text not null,
    run_id text not null,
    kind text not null,
    payload_json text not null,
    created_at text not null,
    unique(run_id,source_id)
);
create table if not exists factory_automation_audit (
    sequence integer primary key autoincrement,
    client_ref text not null,
    action text not null,
    outcome text not null,
    request_ref text not null,
    created_at text not null
);
"""


def _canonical(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, ensure_ascii=False,
                      separators=(",", ":"), sort_keys=True).encode("utf-8")


def _contains_secret(value: object) -> bool:
    if isinstance(value, dict):
        return any(_SECRET_KEYS.search(str(key)) or _contains_secret(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return False


def signature(secret: bytes, *, timestamp: int, nonce: str, method: str,
              path: str, idempotency_key: str, payload: Mapping[str, object]) -> str:
    digest = hashlib.sha256(_canonical(dict(payload))).hexdigest()
    message = "\n".join((str(timestamp), nonce, method.upper(), path,
                         idempotency_key, digest)).encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


class AutomationGateway:
    """Authenticated command facade and durable redacted cursor feed."""

    def __init__(self, path: str | Path, owner: AutomationOwner, *,
                 templates: Mapping[str, AutomationTemplate],
                 principals: Mapping[str, AutomationPrincipal],
                 clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.owner = owner
        self.templates = dict(templates)
        self.principals = dict(principals)
        if set(self.templates) != {item.template_id for item in self.templates.values()}:
            raise ValueError("automation template catalog keys do not match identities")
        if set(self.principals) != {item.client_id for item in self.principals.values()}:
            raise ValueError("automation principal catalog keys do not match identities")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.conn = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._lock = threading.RLock()

    def close(self) -> None:
        self.conn.close()

    def handle(self, method: str, raw_path: str, headers: Mapping[str, str],
               payload: Mapping[str, object] | None = None) -> tuple[int, dict[str, object]]:
        with self._lock:
            return self._handle(method, raw_path, headers, payload)

    def _handle(self, method: str, raw_path: str, headers: Mapping[str, str],
                payload: Mapping[str, object] | None = None) -> tuple[int, dict[str, object]]:
        payload = {} if payload is None else dict(payload)
        parsed = urlparse(raw_path)
        path = parsed.path
        if not path.startswith("/api/automation/v1/"):
            return 404, {"error": "not-found"}
        try:
            principal, idem = self._authenticate(method, raw_path, headers, payload)
            parts = [part for part in path.split("/") if part][3:]
            if method == "POST" and parts == ["launch"]:
                result = self._command(principal, idem, "launch", payload,
                                       lambda operation: self._launch(
                                           principal, operation, payload))
                return 202, result
            if method == "POST" and len(parts) == 3 and parts[0] == "runs" \
                    and parts[2] == "cancel":
                run_id = self._owned_run(principal.client_id, parts[1])
                result = self._command(principal, idem, "cancel", payload,
                    lambda operation: self._cancel(run_id, operation, payload))
                return 202, result
            if method == "POST" and len(parts) == 3 and parts[0] == "runs" \
                    and parts[2] == "acknowledge-handoff":
                run_id = self._owned_run(principal.client_id, parts[1])
                result = self._command(principal, idem, "acknowledge-handoff", payload,
                    lambda operation: self._ack(run_id, operation, payload))
                return 200, result
            if method == "GET" and len(parts) == 3 and parts[0] == "runs" \
                    and parts[2] == "status":
                self._require_empty(payload)
                run_id = self._owned_run(principal.client_id, parts[1])
                return 200, self._redacted_result(self.owner.status(run_id))
            if method == "GET" and len(parts) == 4 and parts[0] == "runs" \
                    and parts[2] == "dossiers":
                self._require_empty(payload)
                run_id = self._owned_run(principal.client_id, parts[1])
                return 200, self._redacted_result(self.owner.dossier(run_id, parts[3]))
            if method == "GET" and parts == ["events"]:
                self._require_empty(payload)
                query = parse_qs(parsed.query)
                if set(query) - {"after", "limit"}:
                    raise AutomationError("query-invalid")
                after = self._query_integer(query, "after", 0, minimum=0)
                limit = self._query_integer(query, "limit", 50, minimum=1)
                if limit > MAX_EVENTS:
                    raise AutomationError("event-limit-invalid")
                self._refresh_events(principal.client_id)
                return 200, self._event_page(principal.client_id, after, limit)
            raise AutomationError("endpoint-not-found", 404)
        except AutomationError as exc:
            client_id = headers.get("X-Factory-Client", "")
            self._audit(client_id or "unknown", "request", exc.code,
                        hashlib.sha256(_canonical(payload)).digest())
            return exc.status, {"error": exc.code, "schemaVersion": 1}

    def _authenticate(self, method: str, path: str, headers: Mapping[str, str],
                      payload: Mapping[str, object]) -> tuple[AutomationPrincipal, str]:
        encoded = _canonical(payload)
        if len(encoded) > MAX_AUTOMATION_BODY:
            self._audit("unknown", "authenticate", "payload-rejected", encoded)
            raise AutomationError("payload-too-large", 413)
        client_id = headers.get("X-Factory-Client", "")
        principal = self.principals.get(client_id)
        client_ref = "client:" + hashlib.sha256(client_id.encode()).hexdigest()[:16]
        if principal is None:
            self._audit(client_ref, "authenticate", "client-rejected", encoded)
            raise AutomationError("authentication-failed", 401)
        try:
            timestamp = int(headers.get("X-Factory-Timestamp", ""))
        except ValueError:
            timestamp = 0
        nonce = headers.get("X-Factory-Nonce", "")
        idem = headers.get("Idempotency-Key", "")
        supplied = headers.get("X-Factory-Signature", "")
        if _ID.fullmatch(nonce) is None or (method == "POST" and _ID.fullmatch(idem) is None) \
                or (method != "POST" and idem):
            self._audit(client_ref, "authenticate", "header-rejected", encoded)
            raise AutomationError("authentication-failed", 401)
        now = self._now()
        if abs(timestamp - int(now.timestamp())) > MAX_CLOCK_SKEW_SECONDS:
            self._audit(client_ref, "authenticate", "timestamp-rejected", encoded)
            raise AutomationError("request-expired", 401)
        expected = signature(principal.secret, timestamp=timestamp, nonce=nonce,
                             method=method, path=path, idempotency_key=idem, payload=payload)
        if not hmac.compare_digest(supplied, expected):
            self._audit(client_ref, "authenticate", "signature-rejected", encoded)
            raise AutomationError("authentication-failed", 401)
        cutoff = int(now.timestamp()) - 60
        recent = self.conn.execute(
            "select count(*) from factory_automation_nonces where client_id=? "
            "and request_time>=?", (client_id, cutoff),
        ).fetchone()[0]
        if recent >= principal.requests_per_minute:
            self._audit(client_ref, "authenticate", "rate-rejected", encoded)
            raise AutomationError("rate-limit-exceeded", 429)
        try:
            with self.conn:
                self.conn.execute(
                    "insert into factory_automation_nonces values (?,?,?)",
                    (client_id, nonce, timestamp),
                )
                self.conn.execute(
                    "delete from factory_automation_nonces where request_time<?", (cutoff,),
                )
        except sqlite3.IntegrityError as exc:
            self._audit(client_ref, "authenticate", "replay-rejected", encoded)
            raise AutomationError("request-replayed", 409) from exc
        self._audit(client_ref, "authenticate", "accepted", encoded)
        return principal, idem

    def _command(self, principal: AutomationPrincipal, key: str, command: str,
                 payload: Mapping[str, object], effect: Callable[[str], Mapping[str, object]],
                 ) -> dict[str, object]:
        request_sha = hashlib.sha256(_canonical(payload)).hexdigest()
        operation = "automation-" + hashlib.sha256(
            f"{principal.client_id}\n{key}\n{command}".encode()).hexdigest()
        now = self._now_text()
        try:
            with self.conn:
                self.conn.execute(
                    "insert into factory_automation_commands values (?,?,?,?,?,'reserved',null,?,?)",
                    (principal.client_id, key, command, request_sha, operation, now, now),
                )
        except sqlite3.IntegrityError:
            row = self.conn.execute(
                "select * from factory_automation_commands where client_id=? "
                "and idempotency_key=?", (principal.client_id, key),
            ).fetchone()
            if row is None or row["command"] != command or row["request_sha256"] != request_sha:
                raise AutomationError("idempotency-conflict", 409)
            if row["result_json"] is not None:
                return json.loads(row["result_json"])
            operation = row["operation_id"]
        try:
            result = self._redacted_result(effect(operation))
        except AutomationError:
            raise
        except Exception as exc:
            self._audit(principal.client_id, command, "owner-unavailable", request_sha.encode())
            raise AutomationError("canonical-owner-unavailable", 503) from exc
        encoded = _canonical(result).decode()
        with self.conn:
            self.conn.execute(
                "update factory_automation_commands set status='completed',result_json=?,"
                "updated_at=? where client_id=? and idempotency_key=?",
                (encoded, self._now_text(), principal.client_id, key),
            )
        self._audit(principal.client_id, command, "completed", request_sha.encode())
        return result

    def _launch(self, principal: AutomationPrincipal, operation: str,
                payload: Mapping[str, object]) -> Mapping[str, object]:
        if set(payload) != {"templateId", "schedule"}:
            raise AutomationError("launch-fields-invalid")
        if _contains_secret(payload):
            raise AutomationError("credential-fields-forbidden", 403)
        template = self.templates.get(payload["templateId"])
        if template is None:
            raise AutomationError("template-not-approved", 403)
        schedule = payload["schedule"]
        if not isinstance(schedule, dict) or set(schedule) != {"scheduleId", "scheduledFor"} \
                or _ID.fullmatch(str(schedule["scheduleId"])) is None \
                or not isinstance(schedule["scheduledFor"], str) \
                or len(schedule["scheduledFor"]) > 64:
            raise AutomationError("schedule-metadata-invalid")
        try:
            scheduled_for = datetime.fromisoformat(
                schedule["scheduledFor"].replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise AutomationError("schedule-metadata-invalid") from exc
        if scheduled_for.tzinfo is None or scheduled_for.utcoffset() is None:
            raise AutomationError("schedule-metadata-invalid")
        scheduled_text = scheduled_for.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        if scheduled_text != schedule["scheduledFor"]:
            raise AutomationError("schedule-metadata-invalid")
        origin = {"clientId": principal.client_id, "kind": "external-scheduler",
                  "scheduleId": schedule["scheduleId"],
                  "scheduledFor": scheduled_text,
                  "templateId": template.template_id,
                  "templateRevision": template.revision}
        result = self.owner.launch(template, operation_id=operation, origin=origin)
        run_id = result.get("runId")
        if not isinstance(run_id, str) or _ID.fullmatch(run_id) is None:
            raise AutomationError("canonical-owner-result-invalid", 503)
        with self.conn:
            self.conn.execute(
                "insert or ignore into factory_automation_runs values (?,?,?,?,?)",
                (run_id, principal.client_id, operation,
                 _canonical(origin).decode(), self._now_text()),
            )
        return result

    def _cancel(self, run_id: str, operation: str,
                payload: Mapping[str, object]) -> Mapping[str, object]:
        if set(payload) != {"reasonCode"} or _ID.fullmatch(str(payload["reasonCode"])) is None:
            raise AutomationError("cancel-fields-invalid")
        return self.owner.cancel(run_id, reason_code=str(payload["reasonCode"]),
                                 operation_id=operation)

    def _ack(self, run_id: str, operation: str,
             payload: Mapping[str, object]) -> Mapping[str, object]:
        if set(payload) != {"handoffId"} or _ID.fullmatch(str(payload["handoffId"])) is None:
            raise AutomationError("handoff-fields-invalid")
        return self.owner.acknowledge_handoff(
            run_id, str(payload["handoffId"]), operation_id=operation,
        )

    def _refresh_events(self, client_id: str) -> None:
        run_ids = tuple(row[0] for row in self.conn.execute(
            "select run_id from factory_automation_runs where client_id=? order by run_id",
            (client_id,),
        ))
        try:
            events = self.owner.events(run_ids)
        except Exception:
            return  # pull-consumer/owner outage cannot erase already materialized feed data
        with self.conn:
            for event in events[:1000]:
                if event.run_id not in run_ids:
                    continue
                self.conn.execute(
                    "insert or ignore into factory_automation_events "
                    "(source_id,run_id,kind,payload_json,created_at) values (?,?,?,?,?)",
                    (event.source_id, event.run_id, event.kind,
                     _canonical(dict(event.payload)).decode(), self._now_text()),
                )

    def _event_page(self, client_id: str, after: int, limit: int) -> dict[str, object]:
        rows = self.conn.execute(
            "select e.* from factory_automation_events e join factory_automation_runs r "
            "on r.run_id=e.run_id where r.client_id=? and e.cursor>? "
            "order by e.cursor limit ?", (client_id, after, limit),
        ).fetchall()
        events = [{"cursor": row["cursor"], "eventId": row["source_id"],
                   "runId": row["run_id"], "kind": row["kind"],
                   "payload": json.loads(row["payload_json"])} for row in rows]
        return {"schemaVersion": 1, "events": events,
                "nextCursor": after if not rows else rows[-1]["cursor"]}

    def _owned_run(self, client_id: str, run_id: str) -> str:
        if _ID.fullmatch(run_id) is None or self.conn.execute(
            "select 1 from factory_automation_runs where client_id=? and run_id=?",
            (client_id, run_id),
        ).fetchone() is None:
            raise AutomationError("run-not-found", 404)
        return run_id

    @staticmethod
    def _redacted_result(result: Mapping[str, object]) -> dict[str, object]:
        value = dict(result)
        encoded = _canonical(value)
        if len(encoded) > MAX_AUTOMATION_BODY or _contains_secret(value):
            raise AutomationError("canonical-owner-result-invalid", 503)
        return value

    @staticmethod
    def _require_empty(payload: Mapping[str, object]) -> None:
        if payload:
            raise AutomationError("get-payload-forbidden")

    @staticmethod
    def _query_integer(query: Mapping[str, list[str]], name: str, default: int,
                       *, minimum: int) -> int:
        values = query.get(name)
        if values is None:
            return default
        if len(values) != 1:
            raise AutomationError("query-invalid")
        try:
            value = int(values[0])
        except ValueError as exc:
            raise AutomationError("query-invalid") from exc
        if value < minimum:
            raise AutomationError("query-invalid")
        return value

    def _audit(self, client_ref: str, action: str, outcome: str, request: bytes) -> None:
        ref = "sha256:" + hashlib.sha256(request).hexdigest()
        safe_client = (client_ref if client_ref.startswith("client:") else
                       "client:" + hashlib.sha256(client_ref.encode()).hexdigest()[:16])
        with self.conn:
            self.conn.execute(
                "insert into factory_automation_audit "
                "(client_ref,action,outcome,request_ref,created_at) values (?,?,?,?,?)",
                (safe_client, action[:64], outcome[:64], ref, self._now_text()),
            )

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None \
                or value.utcoffset() is None:
            raise ValueError("automation clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _now_text(self) -> str:
        return self._now().isoformat(timespec="microseconds").replace("+00:00", "Z")
