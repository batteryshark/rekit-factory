"""Strict read-only attachment authorization for analysis-range leases.

This module does not open a provider console.  It defines the narrow decision and audit
boundary an adapter must cross before exposing an observer session.  Provider credentials,
host paths, keyboard input, and general infrastructure handles have no field in the contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Literal, Self

from rekit_factory.ranges import (
    ProviderHandleV1,
    RangeContract,
    RangeLeaseStateV1,
    RangeSpecV1,
    canonical_sha256,
)


SCHEMA_VERSION = 1
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
AttachmentAction = Literal["observe-console", "capture-screen"]
AttachmentDisposition = Literal["allowed", "denied"]
_ACTIONS = frozenset({"observe-console", "capture-screen"})
_REASONS = frozenset({
    "authorized", "policy-mismatch", "scope-mismatch", "lease-mismatch",
    "lease-unavailable", "node-mismatch", "action-denied", "expired",
})


def _strict(value: Any, name: str, fields: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{name} must contain exactly {sorted(fields)}")
    return value


def _version(value: Any) -> int:
    if type(value) is not int or value != SCHEMA_VERSION:
        raise ValueError("schema_version must be 1")
    return value


def _identifier(value: Any, name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded stable identifier")
    return value


def _digest(value: Any, name: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: Any, name: str) -> str:
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        raise ValueError(f"{name} must be a UTC whole-second timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid UTC timestamp") from exc
    return value


def _time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _format(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class RangeAttachmentPolicyV1(RangeContract):
    """Server-owned policy binding read-only observation to scope and evidence policy."""

    schema_version: int
    policy_id: str
    scope_sha256: str
    evidence_policy_sha256: str
    node_ids: tuple[str, ...]
    allowed_actions: tuple[AttachmentAction, ...]
    max_session_seconds: int

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _identifier(self.policy_id, "policy_id")
        _digest(self.scope_sha256, "scope_sha256")
        _digest(self.evidence_policy_sha256, "evidence_policy_sha256")
        nodes = tuple(sorted(_identifier(item, "node_id") for item in self.node_ids))
        actions = tuple(sorted(self.allowed_actions))
        if not nodes or len(nodes) != len(set(nodes)):
            raise ValueError("node_ids must contain unique stable identifiers")
        if not actions or len(actions) != len(set(actions)) or not set(actions) <= _ACTIONS:
            raise ValueError("allowed_actions must contain unique read-only actions")
        if type(self.max_session_seconds) is not int \
                or not 1 <= self.max_session_seconds <= 3600:
            raise ValueError("max_session_seconds must be between 1 and 3600")
        object.__setattr__(self, "node_ids", nodes)
        object.__setattr__(self, "allowed_actions", actions)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = {
            "schema_version", "policy_id", "scope_sha256", "evidence_policy_sha256",
            "node_ids", "allowed_actions", "max_session_seconds",
        }
        value = _strict(value, cls.__name__, fields)
        if type(value["node_ids"]) is not list or type(value["allowed_actions"]) is not list:
            raise ValueError("attachment policy collections must be arrays")
        return cls(
            **{key: value[key] for key in fields - {"node_ids", "allowed_actions"}},
            node_ids=tuple(value["node_ids"]),
            allowed_actions=tuple(value["allowed_actions"]),
        )


@dataclass(frozen=True)
class RangeAttachmentRequestV1(RangeContract):
    """An operator request for observation only; no input or provider authority exists."""

    schema_version: int
    operation_id: str
    range_id: str
    node_id: str
    node_handle: ProviderHandleV1
    action: AttachmentAction
    policy_sha256: str
    requested_by: str
    requested_at: str

    def __post_init__(self) -> None:
        _version(self.schema_version)
        for name in ("operation_id", "range_id", "node_id", "requested_by"):
            _identifier(getattr(self, name), name)
        if type(self.node_handle) is not ProviderHandleV1 or self.node_handle.kind != "node":
            raise ValueError("attachment requires one opaque node handle")
        if self.action not in _ACTIONS:
            raise ValueError("attachment action must be read-only")
        _digest(self.policy_sha256, "policy_sha256")
        _timestamp(self.requested_at, "requested_at")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = {
            "schema_version", "operation_id", "range_id", "node_id", "node_handle",
            "action", "policy_sha256", "requested_by", "requested_at",
        }
        value = _strict(value, cls.__name__, fields)
        return cls(
            **{key: value[key] for key in fields - {"node_handle"}},
            node_handle=ProviderHandleV1.from_dict(value["node_handle"]),
        )


@dataclass(frozen=True)
class RangeAttachmentAuditV1(RangeContract):
    schema_version: int
    audit_id: str
    request_sha256: str
    range_id: str
    node_id: str
    generation: int
    state_revision: int
    action: AttachmentAction
    requested_by: str
    disposition: AttachmentDisposition
    reason_code: str
    created_at: str
    expires_at: str | None

    def __post_init__(self) -> None:
        _version(self.schema_version)
        for name in ("audit_id", "range_id", "node_id", "requested_by"):
            _identifier(getattr(self, name), name)
        _digest(self.request_sha256, "request_sha256")
        if type(self.generation) is not int or self.generation < 1 \
                or type(self.state_revision) is not int or self.state_revision < 1:
            raise ValueError("attachment audit generation and revision must be positive")
        if self.action not in _ACTIONS or self.disposition not in {"allowed", "denied"} \
                or self.reason_code not in _REASONS:
            raise ValueError("attachment audit decision is invalid")
        _timestamp(self.created_at, "created_at")
        if self.disposition == "allowed":
            if self.reason_code != "authorized" or self.expires_at is None:
                raise ValueError("allowed attachment requires an authorized expiry")
            _timestamp(self.expires_at, "expires_at")
            if _time(self.expires_at) <= _time(self.created_at):
                raise ValueError("attachment expiry must follow its audit time")
        elif self.reason_code == "authorized" or self.expires_at is not None:
            raise ValueError("denied attachment cannot carry authority or expiry")


def authorize_range_attachment(
    request: RangeAttachmentRequestV1,
    policy: RangeAttachmentPolicyV1,
    spec: RangeSpecV1,
    lease: RangeLeaseStateV1,
    *,
    now: str,
) -> RangeAttachmentAuditV1:
    """Return one deterministic bounded audit decision without invoking an adapter."""

    if not all((
        type(request) is RangeAttachmentRequestV1,
        type(policy) is RangeAttachmentPolicyV1,
        type(spec) is RangeSpecV1,
        type(lease) is RangeLeaseStateV1,
    )):
        raise TypeError("attachment authorization requires exact v1 contracts")
    now = _timestamp(now, "now")
    reason = "authorized"
    if request.policy_sha256 != policy.digest:
        reason = "policy-mismatch"
    elif policy.scope_sha256 != spec.scope.digest:
        reason = "scope-mismatch"
    elif request.range_id != spec.range_id or lease.range_id != spec.range_id \
            or lease.spec_sha256 != spec.digest:
        reason = "lease-mismatch"
    elif lease.status not in {"ready", "in-use"}:
        reason = "lease-unavailable"
    else:
        handles = {item.node_id: item.handle for item in lease.node_handles}
        if request.node_id not in policy.node_ids \
                or handles.get(request.node_id) != request.node_handle:
            reason = "node-mismatch"
        elif request.action not in policy.allowed_actions:
            reason = "action-denied"
        elif _time(now) >= _time(spec.expires_at) or _time(request.requested_at) > _time(now):
            reason = "expired"
    allowed = reason == "authorized"
    expires_at = None
    if allowed:
        expires_at = _format(min(
            _time(spec.expires_at),
            _time(now) + timedelta(seconds=policy.max_session_seconds),
        ))
        if _time(expires_at) <= _time(now):
            allowed, reason, expires_at = False, "expired", None
    request_sha256 = request.digest
    identity = {
        "requestSha256": request_sha256,
        "stateRevision": lease.revision,
        "disposition": "allowed" if allowed else "denied",
        "reasonCode": reason,
    }
    return RangeAttachmentAuditV1(
        SCHEMA_VERSION,
        "range-attach:" + canonical_sha256(identity)[:32],
        request_sha256,
        request.range_id,
        request.node_id,
        lease.generation,
        lease.revision,
        request.action,
        request.requested_by,
        "allowed" if allowed else "denied",
        reason,
        now,
        expires_at,
    )
