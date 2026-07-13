"""Restart-safe notification schedule and delivery supervision.

The supervisor persists only canonical preferences, schedules, opaque channel references, and
bounded delivery results.  Concrete channel configuration remains environment-owned and is
supplied to ``run_once`` at send time; endpoints and credentials never enter this ledger.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sqlite3
import subprocess
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import urlsplit

from rekit_factory.notification_delivery import (
    CredentialResolver,
    DeliveryAttempt,
    DesktopChannel,
    DesktopTransport,
    WebhookChannel,
    WebhookRequest,
    WebhookTransport,
    build_webhook_request,
    deliver_desktop,
    deliver_webhook,
)
from rekit_factory.notification_outbox import NotificationOutbox, NotificationStateConflict
from rekit_factory.notification_preferences import (
    NotificationPreferences,
    schedule_is_due,
    schedule_notification,
)


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_STATUSES = frozenset({"queued", "sent", "failed", "muted", "superseded"})
_PHASES = frozenset({"delivery", "escalation"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("notification clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _safe_id(value: Any, field: str) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field} must be a safe stable identifier")
    return value


class NotificationDeliverySupervisor:
    """Own durable schedule/channel work without becoming authoritative over Factory state."""

    def __init__(self, connection: sqlite3.Connection, *,
                 clock: Callable[[], datetime] = _utc_now, max_attempts: int = 5,
                 base_backoff_seconds: int = 5, max_backoff_seconds: int = 300,
                 lease_seconds: int = 60):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 32:
            raise ValueError("max_attempts must be between 1 and 32")
        if any(type(value) is not int or value < 1 for value in (
                base_backoff_seconds, max_backoff_seconds, lease_seconds)):
            raise ValueError("backoff and lease durations must be positive integers")
        self.connection = connection
        self.clock = clock
        self.max_attempts = max_attempts
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.lease_seconds = lease_seconds

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        nested = self.connection.in_transaction
        if nested:
            self.connection.execute("savepoint notification_delivery_supervisor")
        else:
            self.connection.execute("begin immediate")
        try:
            yield
        except BaseException:
            if nested:
                self.connection.execute("rollback to notification_delivery_supervisor")
                self.connection.execute("release notification_delivery_supervisor")
            else:
                self.connection.rollback()
            raise
        else:
            if nested:
                self.connection.execute("release notification_delivery_supervisor")
            else:
                self.connection.commit()

    def schedule(self, notification_id: str, preferences: NotificationPreferences, *,
                 project_id: str, campaign_id: str,
                 channel_refs: Sequence[str]) -> dict[str, Any]:
        """Persist one exact schedule and its channel dispositions idempotently."""
        if type(channel_refs) not in {list, tuple} or not 1 <= len(channel_refs) <= 16:
            raise ValueError("channel_refs must contain between 1 and 16 references")
        refs = [_safe_id(item, "channel_ref") for item in channel_refs]
        if len(set(refs)) != len(refs):
            raise ValueError("channel_refs must be unique")
        record = NotificationOutbox(self.connection, clock=self.clock).get(notification_id)
        if record is None:
            raise KeyError(notification_id)
        schedule = schedule_notification(
            record, preferences, project_id=project_id, campaign_id=campaign_id,
        )
        # Revalidate the content-addressed schedule before it becomes durable.
        schedule_is_due(schedule, self.clock())
        preferences_json = preferences.canonical_json
        schedule_json = _canonical(schedule)
        now = _timestamp(self.clock())
        phases = [("delivery", schedule["deliverAt"])]
        if schedule["escalateAt"] is not None:
            phases.append(("escalation", schedule["escalateAt"]))
        status = "muted" if schedule["disposition"] == "muted" else "queued"
        with self._transaction():
            existing = self.connection.execute(
                "select * from factory_notification_schedules where notification_id=?",
                (notification_id,),
            ).fetchone()
            exact = (
                schedule["scheduleId"], schedule["preferencesId"],
                _digest(preferences_json), _digest(schedule_json), schedule["disposition"],
            )
            if existing is not None:
                stored = (
                    existing["schedule_id"], existing["preferences_id"],
                    existing["preferences_sha256"], existing["schedule_sha256"],
                    existing["disposition"],
                )
                if stored != exact or existing["preferences_json"] != preferences_json \
                        or existing["schedule_json"] != schedule_json:
                    raise NotificationStateConflict(
                        "notification already has a different durable schedule")
                stored_refs = [row[0] for row in self.connection.execute(
                    "select distinct channel_ref from factory_notification_deliveries "
                    "where schedule_id=? order by channel_ref", (schedule["scheduleId"],),
                ).fetchall()]
                if stored_refs != sorted(refs):
                    raise NotificationStateConflict(
                        "notification already has different durable channel references")
            else:
                self.connection.execute(
                    "insert into factory_notification_schedules "
                    "(schedule_id,notification_id,preferences_id,preferences_revision,"
                    "preferences_json,preferences_sha256,schedule_json,schedule_sha256,"
                    "disposition,created_at,updated_at) values (?,?,?,?,?,?,?,?,?,?,?)",
                    (schedule["scheduleId"], notification_id, schedule["preferencesId"],
                     schedule["preferencesRevision"], preferences_json, _digest(preferences_json),
                     schedule_json, _digest(schedule_json), schedule["disposition"], now, now),
                )
            for channel_ref in refs:
                for phase, due_at in phases:
                    identity = f"{schedule['scheduleId']}:{channel_ref}:{phase}"
                    delivery_id = "notification-delivery-" + _digest(identity)
                    self.connection.execute(
                        "insert or ignore into factory_notification_deliveries "
                        "(id,schedule_id,notification_id,channel_ref,phase,status,due_at,"
                        "next_attempt_at,created_at,updated_at) values (?,?,?,?,?,?,?,?,?,?)",
                        (delivery_id, schedule["scheduleId"], notification_id, channel_ref,
                         phase, status, due_at, due_at, now, now),
                    )
        return schedule

    def claim_due(self, worker_id: str, *, limit: int = 32) -> list[dict[str, Any]]:
        worker_id = _safe_id(worker_id, "worker_id")
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("limit must be between 1 and 128")
        now_dt = self.clock()
        now = _timestamp(now_dt)
        expires = _timestamp(now_dt + timedelta(seconds=self.lease_seconds))
        claimed: list[dict[str, Any]] = []
        with self._transaction():
            self.connection.execute(
                "update factory_notification_deliveries set status='failed',lease_token=null,"
                "lease_expires_at=null,next_attempt_at=null,last_error_code="
                "'delivery-lease-expired',updated_at=? where status in ('queued','failed') "
                "and attempt_count >= ? and lease_token is not null and lease_expires_at <= ?",
                (now, self.max_attempts, now),
            )
            rows = self.connection.execute(
                "select d.*,s.schedule_json,s.schedule_sha256,s.preferences_json,"
                "s.preferences_sha256,s.preferences_id,s.preferences_revision "
                "from factory_notification_deliveries d "
                "join factory_notification_schedules s on s.schedule_id=d.schedule_id "
                "where d.status in ('queued','failed') and d.attempt_count < ? "
                "and d.next_attempt_at is not null and d.next_attempt_at <= ? "
                "and (d.lease_expires_at is null or d.lease_expires_at <= ?) "
                "order by d.next_attempt_at,d.created_at,d.id limit ?",
                (self.max_attempts, now, now, limit),
            ).fetchall()
            for row in rows:
                if _digest(row["schedule_json"]) != row["schedule_sha256"] \
                        or _digest(row["preferences_json"]) != row["preferences_sha256"]:
                    raise ValueError("notification schedule failed integrity verification")
                stored_preferences = NotificationPreferences.from_json(row["preferences_json"])
                if stored_preferences.identity != row["preferences_id"] \
                        or stored_preferences.document["revision"] != row["preferences_revision"]:
                    raise ValueError("notification preference identity is corrupt")
                schedule = json.loads(row["schedule_json"])
                if schedule["preferencesId"] != stored_preferences.identity \
                        or schedule["preferencesRevision"] != row["preferences_revision"]:
                    raise ValueError("notification schedule conflicts with preferences")
                schedule_is_due(schedule, now_dt, escalation=row["phase"] == "escalation")
                attempt = row["attempt_count"] + 1
                token = "lease-" + _digest(f"{row['id']}:{attempt}:{worker_id}:{now}")
                updated = self.connection.execute(
                    "update factory_notification_deliveries set attempt_count=?,lease_token=?,"
                    "lease_expires_at=?,next_attempt_at=null,updated_at=? where id=? "
                    "and attempt_count=? and (lease_expires_at is null or lease_expires_at <= ?)",
                    (attempt, token, expires, now, row["id"], row["attempt_count"], now),
                )
                if updated.rowcount != 1:
                    continue
                record = NotificationOutbox(self.connection, clock=self.clock).get(
                    row["notification_id"])
                if record is None:
                    raise ValueError("scheduled notification is missing")
                claimed.append({
                    "deliveryId": row["id"], "notificationId": row["notification_id"],
                    "channelRef": row["channel_ref"], "phase": row["phase"],
                    "attemptCount": attempt, "leaseToken": token,
                    "leaseExpiresAt": expires, "schedule": schedule, "record": record,
                })
        return claimed

    def _leased(self, delivery_id: str, lease_token: str) -> sqlite3.Row:
        row = self.connection.execute(
            "select * from factory_notification_deliveries where id=?", (delivery_id,),
        ).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        if row["lease_token"] != lease_token:
            raise NotificationStateConflict("notification delivery lease is stale or invalid")
        if row["lease_expires_at"] is None or row["lease_expires_at"] <= _timestamp(self.clock()):
            raise NotificationStateConflict("notification delivery lease is expired")
        return row

    def record_sent(self, delivery_id: str, lease_token: str) -> None:
        now = _timestamp(self.clock())
        with self._transaction():
            self._leased(delivery_id, lease_token)
            self.connection.execute(
                "update factory_notification_deliveries set status='sent',lease_token=null,"
                "lease_expires_at=null,last_error_code=null,updated_at=?,sent_at=? where id=?",
                (now, now, delivery_id),
            )

    def record_failed(self, delivery_id: str, lease_token: str, error_code: str) -> None:
        if type(error_code) is not str or _ERROR_CODE.fullmatch(error_code) is None:
            raise ValueError("error_code must be a bounded machine-readable code")
        now_dt = self.clock()
        now = _timestamp(now_dt)
        with self._transaction():
            row = self._leased(delivery_id, lease_token)
            retry_at = None
            if row["attempt_count"] < self.max_attempts:
                delay = min(
                    self.max_backoff_seconds,
                    self.base_backoff_seconds * (2 ** (row["attempt_count"] - 1)),
                )
                retry_at = _timestamp(now_dt + timedelta(seconds=delay))
            self.connection.execute(
                "update factory_notification_deliveries set status='failed',lease_token=null,"
                "lease_expires_at=null,last_error_code=?,next_attempt_at=?,updated_at=? where id=?",
                (error_code, retry_at, now, delivery_id),
            )

    def supersede(self, notification_id: str) -> None:
        """Cancel pending/escalation work after the canonical alert resolves itself."""
        now = _timestamp(self.clock())
        with self._transaction():
            self.connection.execute(
                "update factory_notification_deliveries set status='superseded',"
                "next_attempt_at=null,lease_token=null,lease_expires_at=null,superseded_at=?,"
                "updated_at=? where notification_id=? and status in ('queued','failed')",
                (now, now, notification_id),
            )

    def get_deliveries(self, notification_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "select id,channel_ref,phase,status,due_at,attempt_count,next_attempt_at,"
            "last_error_code,sent_at,superseded_at from factory_notification_deliveries "
            "where notification_id=? order by channel_ref,phase", (notification_id,),
        ).fetchall()
        result = []
        for row in rows:
            if row["status"] not in _STATUSES or row["phase"] not in _PHASES:
                raise ValueError("notification delivery state is corrupt")
            result.append({
                "id": row["id"], "channelRef": row["channel_ref"], "phase": row["phase"],
                "status": row["status"], "dueAt": row["due_at"],
                "attemptCount": row["attempt_count"], "nextAttemptAt": row["next_attempt_at"],
                "lastErrorCode": row["last_error_code"], "sentAt": row["sent_at"],
                "supersededAt": row["superseded_at"],
            })
        return result

    def run_once(self, worker_id: str, *,
                 channels: Mapping[str, DesktopChannel | WebhookChannel],
                 desktop_transport: DesktopTransport | None = None,
                 webhook_transport: WebhookTransport | None = None,
                 credential_resolver: CredentialResolver | None = None,
                 limit: int = 32) -> list[dict[str, Any]]:
        """Deliver one bounded due batch; all adapter failures become durable safe codes."""
        results = []
        for work in self.claim_due(worker_id, limit=limit):
            channel = channels.get(work["channelRef"])
            if channel is None or channel.channel_id != work["channelRef"]:
                attempt = DeliveryAttempt(False, "request-invalid")
            elif isinstance(channel, DesktopChannel):
                attempt = DeliveryAttempt(False, "transport-failed") \
                    if desktop_transport is None \
                    else deliver_desktop(channel, work["record"], desktop_transport)
            elif webhook_transport is None or credential_resolver is None:
                attempt = DeliveryAttempt(False, "transport-failed")
            else:
                request = build_webhook_request(channel, work["record"])
                attempt = deliver_webhook(
                    channel, request, credential_resolver, webhook_transport,
                )
            if attempt.sent:
                self.record_sent(work["deliveryId"], work["leaseToken"])
            else:
                self.record_failed(
                    work["deliveryId"], work["leaseToken"],
                    attempt.error_code or "transport-failed",
                )
            results.append({
                "deliveryId": work["deliveryId"], "sent": attempt.sent,
                "errorCode": attempt.error_code,
            })
        return results


class MacOSDesktopTransport:
    """Environment-owned macOS Notification Center adapter with no shell interpolation."""

    def __init__(self, *, timeout_seconds: int = 5):
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be between 1 and 30")
        self.timeout_seconds = timeout_seconds

    def notify(self, *, title: str, message: str, deep_link: str,
               idempotency_key: str) -> None:
        del deep_link, idempotency_key
        script = (
            "on run argv\n"
            "display notification (item 2 of argv) with title (item 1 of argv)\n"
            "end run"
        )
        subprocess.run(
            ["/usr/bin/osascript", "-e", script, title, message], check=True,
            timeout=self.timeout_seconds, capture_output=True,
        )


class SafeHttpsWebhookTransport:
    """Opt-in HTTPS sender with public-address pinning, no redirects, and bounded I/O."""

    def __init__(self, *, timeout_seconds: int = 10,
                 resolver: Callable[..., Any] = socket.getaddrinfo):
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be between 1 and 30")
        self.timeout_seconds = timeout_seconds
        self.resolver = resolver

    def send(self, request: WebhookRequest, *, bearer_token: str) -> None:
        parsed = urlsplit(request.url)
        host = parsed.hostname
        if parsed.scheme != "https" or host is None:
            raise RuntimeError("webhook-request-rejected")
        port = parsed.port or 443
        try:
            addresses = {
                item[4][0] for item in self.resolver(host, port, type=socket.SOCK_STREAM)
            }
            if not addresses or any(not ipaddress.ip_address(item).is_global for item in addresses):
                raise RuntimeError("webhook-address-rejected")
            address = sorted(addresses)[0]
        except (OSError, ValueError):
            raise RuntimeError("webhook-address-rejected") from None
        connection = _PinnedHTTPSConnection(
            host, address, port, timeout=self.timeout_seconds,
        )
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        headers = dict(request.headers)
        headers["Authorization"] = "Bearer " + bearer_token
        try:
            connection.request("POST", path, body=request.body, headers=headers)
            response = connection.getresponse()
            response.read(1025)
            if not 200 <= response.status < 300:
                # 3xx is deliberately a terminal attempt failure: redirects are never followed.
                raise RuntimeError("webhook-response-rejected")
        except (OSError, http.client.HTTPException):
            raise RuntimeError("webhook-transport-failed") from None
        finally:
            connection.close()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, port: int, *, timeout: int):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self) -> None:
        raw = socket.create_connection(
            (self._pinned_address, self.port), self.timeout, self.source_address,
        )
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
