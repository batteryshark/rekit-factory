"""Safe, network-free boundaries for notification delivery adapters.

This module deliberately provides protocols and request construction, not concrete network or
desktop implementations. Channel configuration is external operator configuration and must not
be stored in a run projection or notification outbox. Credentials remain resolver-owned and are
passed to webhook transports separately from the immutable request body. Adapter failures are
reduced to bounded machine codes so paths, endpoints, credentials, and hostile exception text
cannot reach the durable outbox.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

from rekit_factory.notification_policy import POLICY_VERSION


DELIVERY_SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DEDUPE_KEY = re.compile(r"^sha256:[0-9a-f]{64}$")
_CREDENTIAL_REF = re.compile(r"^credential:[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")
_ALLOWED_KINDS = frozenset({
    "operator-decision.waiting", "finding.reproduced", "finding.accepted",
})
_ALLOWED_SEVERITIES = frozenset({"action-required", "consequential"})
_CANONICAL_MESSAGES = {
    "operator-decision.waiting": "Operator decision is waiting in Mission Control.",
    "finding.reproduced": "A finding reached the reproduced threshold.",
    "finding.accepted": "A finding was accepted by the operator.",
}
_TITLES = {
    "operator-decision.waiting": "Rekit Factory needs you",
    "finding.reproduced": "Finding reproduced",
    "finding.accepted": "Finding accepted",
}
_TEST_TITLE = "Rekit Factory test"
_TEST_MESSAGE = "Test notification from Mission Control. No investigation content is included."


class InvalidDeliveryConfiguration(ValueError):
    """A delivery channel or canonical outbox record is unsafe or unsupported."""


class CredentialResolver(Protocol):
    """Resolve an opaque reference transiently; implementations own secret storage."""

    def resolve(self, credential_ref: str) -> str: ...


@dataclass(frozen=True)
class WebhookRequest:
    """Transport-neutral webhook request with no credential-bearing field."""

    channel_id: str
    url: str
    method: Literal["POST"]
    headers: Mapping[str, str]
    body: bytes


class WebhookTransport(Protocol):
    """Authenticated webhook boundary; concrete implementations may perform I/O."""

    def send(self, request: WebhookRequest, *, bearer_token: str) -> None: ...


class DesktopTransport(Protocol):
    """Best-effort local notification boundary; concrete implementations may perform I/O."""

    def notify(self, *, title: str, message: str, deep_link: str,
               idempotency_key: str) -> None: ...


@dataclass(frozen=True)
class WebhookChannel:
    """External channel configuration; never persist this object in run/outbox state."""

    channel_id: str
    endpoint: str
    credential_ref: str

    def __post_init__(self) -> None:
        _safe_id(self.channel_id, "channel_id")
        object.__setattr__(self, "endpoint", _endpoint(self.endpoint))
        if type(self.credential_ref) is not str \
                or _CREDENTIAL_REF.fullmatch(self.credential_ref) is None:
            raise InvalidDeliveryConfiguration(
                "credential_ref must be an opaque credential: reference")


@dataclass(frozen=True)
class DesktopChannel:
    channel_id: str

    def __post_init__(self) -> None:
        _safe_id(self.channel_id, "channel_id")


@dataclass(frozen=True)
class DeliveryAttempt:
    sent: bool
    error_code: Literal[
        "credential-unavailable", "credential-invalid", "request-invalid", "transport-failed",
    ] | None = None

    def __post_init__(self) -> None:
        if self.sent != (self.error_code is None):
            raise ValueError("sent attempts must not have an error code")


def _safe_id(value: Any, name: str) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise InvalidDeliveryConfiguration(f"{name} must be a safe stable identifier")
    return value


def _endpoint(value: Any) -> str:
    if type(value) is not str or len(value) > 2048 or any(ord(ch) < 0x20 for ch in value):
        raise InvalidDeliveryConfiguration("webhook endpoint must be a bounded HTTPS URL")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidDeliveryConfiguration("webhook endpoint has an invalid port") from exc
    host = parsed.hostname
    if parsed.scheme != "https" or not host or parsed.username is not None \
            or parsed.password is not None or parsed.fragment or port == 0:
        raise InvalidDeliveryConfiguration(
            "webhook endpoint must be HTTPS without credentials or fragments")
    # Canonicalize the scheme and host while retaining provider-specific path/query material.
    netloc = host.lower() + (f":{port}" if port is not None else "")
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _delivery(record: Mapping[str, Any]) -> dict[str, Any]:
    if type(record) is not dict:
        raise InvalidDeliveryConfiguration("delivery record must be an outbox object")
    outbox_id = _safe_id(record.get("id"), "notification id")
    payload = record.get("payload")
    if type(payload) is not dict or set(payload) != {
        "schemaVersion", "policyVersion", "dedupeKey", "kind", "severity", "message",
        "deepLink",
    } or payload.get("schemaVersion") != 1 or payload.get("policyVersion") != POLICY_VERSION:
        raise InvalidDeliveryConfiguration("delivery record payload is invalid")
    kind = payload.get("kind")
    severity = payload.get("severity")
    dedupe_key = payload.get("dedupeKey")
    deep_link = payload.get("deepLink")
    if kind not in _ALLOWED_KINDS or severity not in _ALLOWED_SEVERITIES \
            or payload.get("message") != _CANONICAL_MESSAGES.get(kind) \
            or type(dedupe_key) is not str or _DEDUPE_KEY.fullmatch(dedupe_key) is None \
            or type(deep_link) is not dict:
        raise InvalidDeliveryConfiguration("delivery payload is not canonical")
    expected_notification_id = "notification-" + dedupe_key.removeprefix("sha256:")
    if outbox_id != expected_notification_id:
        raise InvalidDeliveryConfiguration("notification id conflicts with idempotency identity")
    if set(deep_link) != {"view", "runId", "tab", "entityType", "entityId"} \
            or deep_link.get("view") != "mission-control":
        raise InvalidDeliveryConfiguration("delivery deep link is invalid")
    run_id = _safe_id(deep_link.get("runId"), "run id")
    entity_type = _safe_id(deep_link.get("entityType"), "entity type")
    entity_id = _safe_id(deep_link.get("entityId"), "entity id")
    expected_entity_type = "operator-decision" \
        if kind == "operator-decision.waiting" else "finding"
    expected_tab = "decisions" if expected_entity_type == "operator-decision" else "findings"
    if entity_type != expected_entity_type or deep_link.get("tab") != expected_tab:
        raise InvalidDeliveryConfiguration("delivery deep link is inconsistent")
    return {
        "schemaVersion": DELIVERY_SCHEMA_VERSION,
        "notificationId": outbox_id,
        "idempotencyKey": dedupe_key,
        "kind": kind,
        "severity": severity,
        "title": _TITLES[kind],
        "message": _CANONICAL_MESSAGES[kind],
        "deepLink": {
            "view": "mission-control", "runId": run_id, "tab": expected_tab,
            "entityType": entity_type, "entityId": entity_id,
        },
    }


def _test_delivery(channel_id: str, test_id: str) -> dict[str, Any]:
    channel_id = _safe_id(channel_id, "channel_id")
    test_id = _safe_id(test_id, "test_id")
    digest = hashlib.sha256(f"notification-test:{channel_id}:{test_id}".encode()).hexdigest()
    return {
        "schemaVersion": DELIVERY_SCHEMA_VERSION,
        "notificationId": f"test-{test_id}",
        "idempotencyKey": f"sha256:{digest}",
        "kind": "channel.test",
        "severity": "test",
        "title": _TEST_TITLE,
        "message": _TEST_MESSAGE,
        "deepLink": {"view": "mission-control", "tab": "settings"},
    }


def delivery_preview(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded, redacted preview; channel configuration is never included."""
    value = _delivery(record)
    return {key: value[key] for key in (
        "kind", "severity", "title", "message", "deepLink", "idempotencyKey",
    )}


def channel_test_preview(channel_id: str, test_id: str) -> dict[str, Any]:
    """Return the exact fixed payload used to test a channel without investigation data."""
    value = _test_delivery(channel_id, test_id)
    return {key: value[key] for key in (
        "kind", "severity", "title", "message", "deepLink", "idempotencyKey",
    )}


def build_webhook_request(channel: WebhookChannel, record: Mapping[str, Any]) -> WebhookRequest:
    """Build the exact unauthenticated request; authentication remains resolver-owned."""
    value = _delivery(record)
    return _webhook_request(channel, value)


def build_test_webhook_request(channel: WebhookChannel, test_id: str) -> WebhookRequest:
    return _webhook_request(channel, _test_delivery(channel.channel_id, test_id))


def _webhook_request(channel: WebhookChannel, value: Mapping[str, Any]) -> WebhookRequest:
    idempotency_key = value["idempotencyKey"]
    return WebhookRequest(
        channel_id=channel.channel_id,
        url=channel.endpoint,
        method="POST",
        headers=MappingProxyType({
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
            "User-Agent": "rekit-factory-notifications/1",
        }),
        body=_canonical_json(value),
    )


def deliver_webhook(channel: WebhookChannel, request: WebhookRequest,
                    resolver: CredentialResolver, transport: WebhookTransport) -> DeliveryAttempt:
    """Resolve and deliver once, returning only safe durable result codes."""
    if not _request_matches_channel(channel, request):
        return DeliveryAttempt(False, "request-invalid")
    try:
        bearer_token = resolver.resolve(channel.credential_ref)
    except Exception:
        return DeliveryAttempt(False, "credential-unavailable")
    if type(bearer_token) is not str or not bearer_token or len(bearer_token) > 8192 \
            or "\r" in bearer_token or "\n" in bearer_token:
        return DeliveryAttempt(False, "credential-invalid")
    try:
        transport.send(request, bearer_token=bearer_token)
    except Exception:
        return DeliveryAttempt(False, "transport-failed")
    return DeliveryAttempt(True)


def _request_matches_channel(channel: WebhookChannel, request: WebhookRequest) -> bool:
    """Reject forged/cross-channel requests before resolving a credential."""
    try:
        body = json.loads(request.body)
        idempotency_key = request.headers["Idempotency-Key"]
        return request.channel_id == channel.channel_id \
            and request.url == channel.endpoint \
            and request.method == "POST" \
            and set(request.headers) == {"Content-Type", "Idempotency-Key", "User-Agent"} \
            and request.headers["Content-Type"] == "application/json" \
            and request.headers["User-Agent"] == "rekit-factory-notifications/1" \
            and type(idempotency_key) is str \
            and _DEDUPE_KEY.fullmatch(idempotency_key) is not None \
            and type(body) is dict \
            and body.get("idempotencyKey") == idempotency_key \
            and _canonical_json(body) == request.body \
            and len(request.body) <= 4096
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return False


def deliver_desktop(channel: DesktopChannel, record: Mapping[str, Any],
                    transport: DesktopTransport) -> DeliveryAttempt:
    return _deliver_desktop(channel, _delivery(record), transport)


def deliver_test_desktop(channel: DesktopChannel, test_id: str,
                         transport: DesktopTransport) -> DeliveryAttempt:
    return _deliver_desktop(channel, _test_delivery(channel.channel_id, test_id), transport)


def _deliver_desktop(channel: DesktopChannel, value: Mapping[str, Any],
                     transport: DesktopTransport) -> DeliveryAttempt:
    del channel  # The stable channel identity is used only to construct test idempotency keys.
    deep_link = "rekit-factory://mission-control"
    try:
        transport.notify(
            title=value["title"], message=value["message"], deep_link=deep_link,
            idempotency_key=value["idempotencyKey"],
        )
    except Exception:
        return DeliveryAttempt(False, "transport-failed")
    return DeliveryAttempt(True)
