"""Durable, redacted notification admission and delivery bookkeeping.

The outbox owns no transport or credentials.  A transport adapter may claim due records and
then report a sent or failed attempt using the returned lease token.  Candidate content is
canonicalized and hashed at admission so database corruption or mutation fails closed.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
from typing import Any, Callable, Iterable, Iterator, Mapping

from rekit_factory.notification_policy import CANDIDATE_SCHEMA_VERSION, POLICY_VERSION
from rekit_factory.campaign_notification_policy import POLICY_VERSION as CAMPAIGN_POLICY_VERSION


OUTBOX_SCHEMA_VERSION = 1
OUTBOX_STATUSES = frozenset({"queued", "sent", "failed", "acknowledged", "superseded"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_KINDS = frozenset({"operator-decision.waiting", "finding.reproduced", "finding.accepted"})
_SEVERITIES = frozenset({"action-required", "consequential"})
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_REVISION = re.compile(r"^sha256:[0-9a-f]{64}$")
_CAMPAIGN_KINDS = frozenset({
    "campaign.budget-threshold", "campaign.terminal", "campaign.infrastructure-action",
})


class InvalidNotificationCandidate(ValueError):
    """Candidate is unsupported, unsafe, or inconsistent with its stable identity."""


class NotificationStateConflict(ValueError):
    """Requested lifecycle transition conflicts with durable state."""


class NotificationNotFound(KeyError):
    """Requested notification identity does not exist in this outbox."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("notification clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_id(value: Any, field: str) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise InvalidNotificationCandidate(f"{field} must be a safe stable identifier")
    return value


def _payload(candidate: Mapping[str, Any]) -> dict[str, Any]:
    if type(candidate) is not dict:
        raise InvalidNotificationCandidate("candidate must be a JSON object")
    if candidate.get("policyVersion") == CAMPAIGN_POLICY_VERSION:
        return _campaign_payload(candidate)
    exact = {
        "schemaVersion", "policyVersion", "dedupeKey", "kind", "severity", "runId",
        "entity", "message",
    }
    if set(candidate) != exact:
        raise InvalidNotificationCandidate("candidate has unsupported fields")
    if candidate.get("schemaVersion") != CANDIDATE_SCHEMA_VERSION \
            or candidate.get("policyVersion") != POLICY_VERSION:
        raise InvalidNotificationCandidate("candidate version is unsupported")
    kind = candidate.get("kind")
    severity = candidate.get("severity")
    if kind not in _KINDS or severity not in _SEVERITIES:
        raise InvalidNotificationCandidate("candidate kind or severity is unsupported")
    entity = candidate.get("entity")
    if type(entity) is not dict or set(entity) != {"entityType", "entityId"}:
        raise InvalidNotificationCandidate("candidate entity is invalid")
    run_id = _safe_id(candidate.get("runId"), "runId")
    entity_type = _safe_id(entity.get("entityType"), "entity.entityType")
    entity_id = _safe_id(entity.get("entityId"), "entity.entityId")
    expected_types = ({"operator-decision"} if kind == "operator-decision.waiting"
                      else {"finding", "proof-bundle"})
    if entity_type not in expected_types:
        raise InvalidNotificationCandidate("candidate kind conflicts with entity type")
    messages = {
        "operator-decision.waiting": "Operator decision is waiting in Mission Control.",
        "finding.reproduced": "A finding reached the reproduced threshold.",
        "finding.accepted": "A finding was accepted by the operator.",
    }
    if candidate.get("message") != messages[kind]:
        raise InvalidNotificationCandidate("candidate message is not canonical redacted prose")
    identity = {
        "policyVersion": POLICY_VERSION, "runId": run_id, "entityType": entity_type,
        "entityId": entity_id, "transition": kind,
    }
    expected_dedupe = "sha256:" + _digest(_canonical(identity))
    if candidate.get("dedupeKey") != expected_dedupe:
        raise InvalidNotificationCandidate("candidate dedupe identity is invalid")
    tab = {"operator-decision": "decisions", "finding": "findings",
           "proof-bundle": "dossiers"}[entity_type]
    return {
        "schemaVersion": OUTBOX_SCHEMA_VERSION,
        "policyVersion": POLICY_VERSION,
        "dedupeKey": expected_dedupe,
        "kind": kind,
        "severity": severity,
        "message": messages[kind],
        "deepLink": {
            "view": "mission-control", "runId": run_id, "tab": tab,
            "entityType": entity_type, "entityId": entity_id,
        },
    }


def _campaign_payload(candidate: Mapping[str, Any]) -> dict[str, Any]:
    exact = {
        "schemaVersion", "policyVersion", "dedupeKey", "kind", "severity", "campaignId",
        "transitionMarker", "entity", "message", "deepLink",
    }
    if set(candidate) != exact or candidate.get("schemaVersion") != 1:
        raise InvalidNotificationCandidate("campaign candidate has an invalid schema")
    campaign_id = _safe_id(candidate.get("campaignId"), "campaignId")
    marker = _safe_id(candidate.get("transitionMarker"), "transitionMarker")
    kind, severity = candidate.get("kind"), candidate.get("severity")
    if kind not in _CAMPAIGN_KINDS or severity not in _SEVERITIES:
        raise InvalidNotificationCandidate("campaign candidate kind or severity is unsupported")
    expected_severity = ("action-required" if kind == "campaign.infrastructure-action"
                         else "consequential")
    messages = {
        "campaign.budget-threshold": "A configured campaign budget threshold was crossed.",
        "campaign.terminal": "A campaign reached a terminal outcome.",
        "campaign.infrastructure-action":
            "A campaign infrastructure failure requires operator attention.",
    }
    entity = candidate.get("entity")
    deep_link = candidate.get("deepLink")
    expected_link = {"view": "mission-control", "tab": "campaigns",
                     "entityType": "campaign", "entityId": campaign_id}
    if entity != {"entityType": "campaign", "entityId": campaign_id} \
            or deep_link != expected_link or severity != expected_severity \
            or candidate.get("message") != messages[kind]:
        raise InvalidNotificationCandidate("campaign candidate content is not canonical")
    identity = {"policyVersion": CAMPAIGN_POLICY_VERSION, "campaignId": campaign_id,
                "transition": kind, "marker": marker}
    expected_dedupe = "sha256:" + _digest(_canonical(identity))
    if candidate.get("dedupeKey") != expected_dedupe:
        raise InvalidNotificationCandidate("campaign candidate dedupe identity is invalid")
    return {
        "schemaVersion": OUTBOX_SCHEMA_VERSION, "policyVersion": CAMPAIGN_POLICY_VERSION,
        "dedupeKey": expected_dedupe, "kind": kind, "severity": severity,
        "message": messages[kind], "transitionMarker": marker, "deepLink": expected_link,
    }


class NotificationOutbox:
    """SQLite-backed outbox with deterministic bounded retry bookkeeping."""

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
            self.connection.execute("savepoint notification_outbox")
        else:
            self.connection.execute("begin immediate")
        try:
            yield
        except BaseException:
            if nested:
                self.connection.execute("rollback to notification_outbox")
                self.connection.execute("release notification_outbox")
            else:
                self.connection.rollback()
            raise
        else:
            if nested:
                self.connection.execute("release notification_outbox")
            else:
                self.connection.commit()

    def admit(self, candidates: Iterable[Mapping[str, Any]]) -> list[str]:
        """Atomically admit a batch, returning stable ids in input order."""
        payloads = [_payload(candidate) for candidate in candidates]
        ids: list[str] = []
        now = _timestamp(self.clock())
        with self._transaction():
            for payload in payloads:
                payload_json = _canonical(payload)
                payload_sha = _digest(payload_json)
                dedupe_key = payload["dedupeKey"]
                outbox_id = "notification-" + dedupe_key.removeprefix("sha256:")
                existing = self.connection.execute(
                    "select id,payload_json,payload_sha256 from factory_notification_outbox "
                    "where dedupe_key=?", (dedupe_key,),
                ).fetchone()
                if existing is not None:
                    if existing["id"] != outbox_id or existing["payload_json"] != payload_json \
                            or existing["payload_sha256"] != payload_sha:
                        raise InvalidNotificationCandidate(
                            "dedupe identity conflicts with immutable outbox payload")
                    ids.append(outbox_id)
                    continue
                deep_link = payload["deepLink"]
                routing_id = deep_link.get("runId", deep_link.get("entityId"))
                self.connection.execute(
                    "insert into factory_notification_outbox "
                    "(id,dedupe_key,run_id,kind,severity,entity_type,entity_id,payload_json,"
                    "payload_sha256,status,next_attempt_at,created_at,updated_at) "
                    "values (?,?,?,?,?,?,?,?,?,'queued',?,?,?)",
                    (outbox_id, dedupe_key, routing_id, payload["kind"],
                     payload["severity"], deep_link["entityType"], deep_link["entityId"],
                     payload_json, payload_sha, now, now, now),
                )
                ids.append(outbox_id)
        return ids

    def _record(self, row: sqlite3.Row) -> dict[str, Any]:
        if row["status"] not in OUTBOX_STATUSES:
            raise ValueError("notification outbox status is corrupt")
        payload_json = row["payload_json"]
        if _digest(payload_json) != row["payload_sha256"]:
            raise ValueError("notification outbox payload failed integrity verification")
        payload = json.loads(payload_json)
        # Revalidation detects structurally valid but unauthorized database mutation.
        if payload.get("policyVersion") == CAMPAIGN_POLICY_VERSION:
            campaign_id = payload["deepLink"]["entityId"]
            candidate = {
                "schemaVersion": 1, "policyVersion": payload["policyVersion"],
                "dedupeKey": payload["dedupeKey"], "kind": payload["kind"],
                "severity": payload["severity"], "campaignId": campaign_id,
                "transitionMarker": payload["transitionMarker"],
                "entity": {"entityType": "campaign", "entityId": campaign_id},
                "message": payload["message"], "deepLink": payload["deepLink"],
            }
        else:
            candidate = {
                "schemaVersion": CANDIDATE_SCHEMA_VERSION,
                "policyVersion": payload["policyVersion"], "dedupeKey": payload["dedupeKey"],
                "kind": payload["kind"], "severity": payload["severity"],
                "runId": payload["deepLink"]["runId"],
                "entity": {"entityType": payload["deepLink"]["entityType"],
                           "entityId": payload["deepLink"]["entityId"]},
                "message": payload["message"],
            }
        if _payload(candidate) != payload:
            raise ValueError("notification outbox payload is not canonical")
        deep_link = payload["deepLink"]
        expected_id = "notification-" + payload["dedupeKey"].removeprefix("sha256:")
        routing_id = deep_link.get("runId", deep_link.get("entityId"))
        columns = (
            (row["id"], expected_id), (row["dedupe_key"], payload["dedupeKey"]),
            (row["run_id"], routing_id), (row["kind"], payload["kind"]),
            (row["severity"], payload["severity"]),
            (row["entity_type"], deep_link["entityType"]),
            (row["entity_id"], deep_link["entityId"]),
        )
        if any(actual != expected for actual, expected in columns):
            raise ValueError("notification outbox identity columns conflict with payload")
        return {
            "id": row["id"], "status": row["status"], "attemptCount": row["attempt_count"],
            "nextAttemptAt": row["next_attempt_at"], "lastErrorCode": row["last_error_code"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
            "sentAt": row["sent_at"], "acknowledgedAt": row["acknowledged_at"],
            "supersededAt": row["superseded_at"], "payload": payload,
        }

    def get(self, outbox_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "select * from factory_notification_outbox where id=?", (outbox_id,),
        ).fetchone()
        return None if row is None else self._record(row)

    @staticmethod
    def public_projection(record: Mapping[str, Any]) -> dict[str, Any]:
        """Project an intact record for Mission Control without lease material."""
        if type(record) is not dict:
            raise ValueError("notification record must be an object")
        required = {
            "id", "status", "attemptCount", "nextAttemptAt", "lastErrorCode",
            "createdAt", "updatedAt", "sentAt", "acknowledgedAt", "supersededAt",
            "payload",
        }
        if set(record) != required:
            raise ValueError("notification record has unsupported fields")
        revision_document = _canonical({
            "id": record["id"], "status": record["status"],
            "updatedAt": record["updatedAt"],
        })
        return {
            "schemaVersion": OUTBOX_SCHEMA_VERSION,
            "id": record["id"],
            "revision": "sha256:" + _digest(revision_document),
            "status": record["status"],
            "attemptCount": record["attemptCount"],
            "nextAttemptAt": record["nextAttemptAt"],
            "lastErrorCode": record["lastErrorCode"],
            "createdAt": record["createdAt"],
            "updatedAt": record["updatedAt"],
            "sentAt": record["sentAt"],
            "acknowledgedAt": record["acknowledgedAt"],
            "supersededAt": record["supersededAt"],
            "payload": record["payload"],
        }

    def public_records(self, *, limit: int = 128) -> list[dict[str, Any]]:
        """Return a bounded newest-first read model, revalidating every stored row."""
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("limit must be between 1 and 128")
        rows = self.connection.execute(
            "select * from factory_notification_outbox "
            "order by created_at desc,id desc limit ?", (limit,),
        ).fetchall()
        return [self.public_projection(self._record(row)) for row in rows]

    def acknowledge_revision(self, outbox_id: str, expected_revision: str) -> dict[str, Any]:
        """Acknowledge an exact sent revision; replays converge on the terminal record."""
        _safe_id(outbox_id, "notificationId")
        if type(expected_revision) is not str or _REVISION.fullmatch(expected_revision) is None:
            raise ValueError("expectedRevision must be a sha256 revision")
        with self._transaction():
            record = self.get(outbox_id)
            if record is None:
                raise NotificationNotFound(outbox_id)
            current = self.public_projection(record)
            if current["status"] == "acknowledged":
                return current
            if current["revision"] != expected_revision:
                raise NotificationStateConflict("notification revision is stale")
            self.acknowledge(outbox_id)
            acknowledged = self.get(outbox_id)
            assert acknowledged is not None
            return self.public_projection(acknowledged)

    def claim_due(self, worker_id: str, *, limit: int = 32) -> list[dict[str, Any]]:
        """Lease due delivery work. Expired leases are safely claimable after a crash."""
        worker_id = _safe_id(worker_id, "workerId")
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("limit must be between 1 and 128")
        now_dt = self.clock()
        now = _timestamp(now_dt)
        lease_expiry = _timestamp(now_dt + timedelta(seconds=self.lease_seconds))
        claimed: list[dict[str, Any]] = []
        with self._transaction():
            # A sender that crashes during the final permitted attempt must not leave a
            # permanently leased queued row.  Expiry deterministically closes that budget.
            self.connection.execute(
                "update factory_notification_outbox set status='failed',lease_token=null,"
                "lease_expires_at=null,next_attempt_at=null,last_error_code='delivery-lease-expired',"
                "updated_at=? where status in ('queued','failed') and attempt_count >= ? "
                "and lease_token is not null and lease_expires_at <= ?",
                (now, self.max_attempts, now),
            )
            rows = self.connection.execute(
                "select * from factory_notification_outbox where "
                "status in ('queued','failed') and attempt_count < ? "
                "and ((next_attempt_at is not null and next_attempt_at <= ?) "
                "or (lease_token is not null and lease_expires_at <= ?)) "
                "and (lease_expires_at is null or lease_expires_at <= ?) "
                "order by next_attempt_at,created_at,id limit ?",
                (self.max_attempts, now, now, now, limit),
            ).fetchall()
            for row in rows:
                attempt = row["attempt_count"] + 1
                token_seed = f"{row['id']}:{attempt}:{worker_id}:{now}"
                token = "lease-" + _digest(token_seed)
                updated = self.connection.execute(
                    "update factory_notification_outbox set attempt_count=?,lease_token=?,"
                    "lease_expires_at=?,next_attempt_at=null,updated_at=? where id=? "
                    "and attempt_count=? and (lease_expires_at is null or lease_expires_at <= ?)",
                    (attempt, token, lease_expiry, now, row["id"], row["attempt_count"], now),
                )
                if updated.rowcount != 1:
                    continue
                record = self.get(row["id"])
                assert record is not None
                record["leaseToken"] = token
                record["leaseExpiresAt"] = lease_expiry
                claimed.append(record)
        return claimed

    def _leased(self, outbox_id: str, lease_token: str) -> sqlite3.Row:
        row = self.connection.execute(
            "select * from factory_notification_outbox where id=?", (outbox_id,),
        ).fetchone()
        if row is None:
            raise KeyError(outbox_id)
        if row["lease_token"] != lease_token:
            raise NotificationStateConflict("notification lease is stale or invalid")
        lease_expires_at = row["lease_expires_at"]
        if lease_expires_at is None or lease_expires_at <= _timestamp(self.clock()):
            raise NotificationStateConflict("notification lease is expired")
        return row

    def record_sent(self, outbox_id: str, lease_token: str) -> None:
        now = _timestamp(self.clock())
        with self._transaction():
            row = self._leased(outbox_id, lease_token)
            if row["status"] not in {"queued", "failed"}:
                raise NotificationStateConflict("only queued or failed notification may be sent")
            self.connection.execute(
                "update factory_notification_outbox set status='sent',lease_token=null,"
                "lease_expires_at=null,last_error_code=null,updated_at=?,sent_at=? where id=?",
                (now, now, outbox_id),
            )

    def record_failed(self, outbox_id: str, lease_token: str, error_code: str) -> None:
        if type(error_code) is not str or _ERROR_CODE.fullmatch(error_code) is None:
            raise ValueError("error_code must be a bounded machine-readable code")
        now_dt = self.clock()
        now = _timestamp(now_dt)
        with self._transaction():
            row = self._leased(outbox_id, lease_token)
            if row["status"] not in {"queued", "failed"}:
                raise NotificationStateConflict("only queued or failed notification may fail")
            if row["attempt_count"] >= self.max_attempts:
                next_attempt = None
            else:
                delay = min(
                    self.max_backoff_seconds,
                    self.base_backoff_seconds * (2 ** (row["attempt_count"] - 1)),
                )
                next_attempt = _timestamp(now_dt + timedelta(seconds=delay))
            self.connection.execute(
                "update factory_notification_outbox set status='failed',lease_token=null,"
                "lease_expires_at=null,last_error_code=?,next_attempt_at=?,updated_at=? where id=?",
                (error_code, next_attempt, now, outbox_id),
            )

    def acknowledge(self, outbox_id: str) -> None:
        self._terminal(outbox_id, "acknowledged")

    def supersede(self, outbox_id: str) -> None:
        self._terminal(outbox_id, "superseded")

    def _terminal(self, outbox_id: str, status: str) -> None:
        now = _timestamp(self.clock())
        with self._transaction():
            row = self.connection.execute(
                "select status from factory_notification_outbox where id=?", (outbox_id,),
            ).fetchone()
            if row is None:
                raise KeyError(outbox_id)
            if row["status"] == status:
                return
            if status == "acknowledged" and row["status"] != "sent":
                raise NotificationStateConflict("only a sent notification may be acknowledged")
            if status == "superseded" and row["status"] in {"acknowledged", "superseded"}:
                raise NotificationStateConflict("terminal notification cannot be superseded")
            column = "acknowledged_at" if status == "acknowledged" else "superseded_at"
            self.connection.execute(
                f"update factory_notification_outbox set status=?,{column}=?,updated_at=?,"
                "next_attempt_at=null,lease_token=null,lease_expires_at=null where id=?",
                (status, now, now, outbox_id),
            )
