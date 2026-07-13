"""Versioned, content-bound authorization scopes and fail-closed decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class NetworkMode(str, Enum):
    NONE = "none"
    EXACT_ENDPOINTS = "exact_endpoints"


class ActionAuthority(str, Enum):
    READ_LOCAL_TARGET = "read_local_target"
    EXECUTE_UNTRUSTED = "execute_untrusted"
    MODIFY_TARGET = "modify_target"
    NETWORK_ACCESS = "network_access"
    REGISTER_ACCOUNT = "register_account"
    ENROLL_CHALLENGE = "enroll_challenge"
    CREATE_CREDENTIAL = "create_credential"
    SUBMIT_CHALLENGE = "submit_challenge"
    PERSISTENCE = "persistence"
    DESTRUCTIVE = "destructive"
    THIRD_PARTY_MESSAGE = "third_party_message"
    EXPAND_SCOPE = "expand_scope"


class DataHandling(str, Enum):
    LOCAL_ONLY = "local_only"
    APPROVED_EXPORT = "approved_export"


DEFAULT_PROHIBITED_ACTIONS = (
    ActionAuthority.REGISTER_ACCOUNT,
    ActionAuthority.ENROLL_CHALLENGE,
    ActionAuthority.CREATE_CREDENTIAL,
    ActionAuthority.SUBMIT_CHALLENGE,
    ActionAuthority.PERSISTENCE,
    ActionAuthority.DESTRUCTIVE,
    ActionAuthority.THIRD_PARTY_MESSAGE,
    ActionAuthority.EXPAND_SCOPE,
)
MAX_SCOPE_SECONDS = 30 * 24 * 60 * 60
MAX_APPROVAL_SECONDS = 7 * 24 * 60 * 60
_ACCOUNT_REF = re.compile(r"^account:[A-Za-z0-9._-]{1,128}$")


@dataclass(frozen=True)
class TargetGrant:
    content_sha256: str
    path_fingerprint: str

    @classmethod
    def from_path(cls, path: str | Path) -> "TargetGrant":
        resolved = Path(path).expanduser().resolve()
        return cls(
            content_sha256=hash_path(resolved),
            path_fingerprint=opaque_ref("target-path", str(resolved)),
        )


@dataclass(frozen=True)
class ScopeEnvelope:
    scope_id: str
    revision: int
    valid_from: str
    valid_until: str
    targets: tuple[TargetGrant, ...]
    endpoints: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    ip_ranges: tuple[str, ...] = ()
    account_refs: tuple[str, ...] = ()
    actions: tuple[ActionAuthority, ...] = (ActionAuthority.READ_LOCAL_TARGET,)
    network_mode: NetworkMode = NetworkMode.NONE
    credential_use: bool = False
    data_handling: DataHandling = DataHandling.LOCAL_ONLY
    prohibited_actions: tuple[ActionAuthority, ...] = DEFAULT_PROHIBITED_ACTIONS
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ValueError(f"unsupported scope envelope version {self.version}")
        if not self.scope_id or self.revision < 1:
            raise ValueError("scope_id and positive revision are required")
        start, end = _time(self.valid_from), _time(self.valid_until)
        if end <= start:
            raise ValueError("scope validity window must end after it begins")
        if not self.targets:
            raise ValueError("scope requires at least one exact target grant")
        if not all(isinstance(action, ActionAuthority) for action in self.actions):
            raise ValueError("actions must use ActionAuthority values")
        if not all(isinstance(action, ActionAuthority) for action in self.prohibited_actions):
            raise ValueError("prohibited_actions must use ActionAuthority values")
        if not isinstance(self.network_mode, NetworkMode):
            raise ValueError("network_mode must use a NetworkMode value")
        if not isinstance(self.data_handling, DataHandling):
            raise ValueError("data_handling must use a DataHandling value")
        for target in self.targets:
            _digest(target.content_sha256, "target content_sha256")
            if not target.path_fingerprint.startswith("target-path:"):
                raise ValueError("target path must be stored as an opaque fingerprint")
        normalized = tuple(normalize_endpoint(value) for value in self.endpoints)
        if normalized != self.endpoints:
            raise ValueError("endpoints must be normalized exact HTTP(S) endpoints")
        for domain in self.domains:
            if not domain or domain != domain.lower() or "://" in domain:
                raise ValueError("domains must be lowercase host names")
        for value in self.ip_ranges:
            ipaddress.ip_network(value, strict=True)
        if any(not _ACCOUNT_REF.fullmatch(value) for value in self.account_refs):
            raise ValueError("accounts must be stored as opaque account references")
        if self.network_mode is NetworkMode.NONE and (
            self.endpoints or self.domains or self.ip_ranges or ActionAuthority.NETWORK_ACCESS in self.actions
        ):
            raise ValueError("network-none scope cannot contain network grants")
        if self.network_mode is NetworkMode.EXACT_ENDPOINTS and not self.endpoints:
            raise ValueError("exact-endpoints network mode requires an endpoint")
        overlap = set(self.actions).intersection(self.prohibited_actions)
        if overlap:
            raise ValueError(f"actions cannot also be prohibited: {sorted(x.value for x in overlap)!r}")
        if self.credential_use and not self.account_refs:
            # Existing opaque credentials can be authorized without authorizing creation.
            raise ValueError("credential use requires an approved opaque account reference")

    @property
    def content_digest(self) -> str:
        payload = _jsonable(asdict(self))
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def public_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "scopeId": self.scope_id,
            "revision": self.revision,
            "digest": self.content_digest,
            "validUntil": self.valid_until,
            "networkMode": self.network_mode.value,
            "targetRefs": [target.path_fingerprint for target in self.targets],
            "endpointRefs": [opaque_ref("endpoint", endpoint) for endpoint in self.endpoints],
            "actions": [action.value for action in self.actions],
            "credentialUse": self.credential_use,
            "dataHandling": self.data_handling.value,
        }


@dataclass(frozen=True)
class ScopeApproval:
    scope_id: str
    revision: int
    content_digest: str
    approved_by: str
    approved_at: str
    expires_at: str
    rationale: str

    def __post_init__(self) -> None:
        _digest(self.content_digest, "approval content_digest")
        if not self.approved_by.strip() or not self.rationale.strip():
            raise ValueError("approval identity and rationale are required")
        if _time(self.expires_at) <= _time(self.approved_at):
            raise ValueError("approval must expire after approval time")


@dataclass(frozen=True)
class AuthorizedScope:
    envelope: ScopeEnvelope
    approval: ScopeApproval

    def validate(self, *, now: str) -> None:
        if (self.approval.scope_id, self.approval.revision) != (
            self.envelope.scope_id, self.envelope.revision
        ):
            raise ScopeAuthorizationError("scope.approval_mismatch")
        if self.approval.content_digest != self.envelope.content_digest:
            raise ScopeAuthorizationError("scope.content_mismatch")
        if (_time(self.approval.approved_at) < _time(self.envelope.valid_from)
                or _time(self.approval.expires_at) > _time(self.envelope.valid_until)):
            raise ScopeAuthorizationError("scope.approval_window_mismatch")
        current = _time(now)
        if current < _time(self.envelope.valid_from):
            raise ScopeAuthorizationError("scope.not_yet_valid")
        if current >= min(_time(self.envelope.valid_until), _time(self.approval.expires_at)):
            raise ScopeAuthorizationError("scope.expired")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AuthorizedScope":
        envelope_value = dict(value["envelope"])
        envelope_value["targets"] = tuple(TargetGrant(**item) for item in envelope_value["targets"])
        envelope_value["actions"] = tuple(ActionAuthority(item) for item in envelope_value["actions"])
        envelope_value["prohibited_actions"] = tuple(
            ActionAuthority(item) for item in envelope_value["prohibited_actions"]
        )
        envelope_value["network_mode"] = NetworkMode(envelope_value["network_mode"])
        envelope_value["data_handling"] = DataHandling(envelope_value["data_handling"])
        for name in ("endpoints", "domains", "ip_ranges", "account_refs"):
            envelope_value[name] = tuple(envelope_value[name])
        return cls(
            envelope=ScopeEnvelope(**envelope_value),
            approval=ScopeApproval(**value["approval"]),
        )


@dataclass(frozen=True)
class ScopeRequest:
    action: ActionAuthority
    target: TargetGrant
    endpoint: str | None = None
    account_ref: str | None = None
    uses_credentials: bool = False


@dataclass(frozen=True)
class ScopeDecision:
    allowed: bool
    reason_code: str
    scope_ref: str
    target_ref: str
    endpoint_ref: str | None = None
    action: str | None = None

    def browser_dict(self) -> dict[str, Any]:
        return asdict(self)


class ScopeAuthorizationError(PermissionError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def decide_scope(scope: AuthorizedScope, request: ScopeRequest, *, now: str) -> ScopeDecision:
    scope_ref = f"scope:{scope.envelope.scope_id}:r{scope.envelope.revision}:{scope.envelope.content_digest[:12]}"
    endpoint_ref = opaque_ref("endpoint", request.endpoint) if request.endpoint else None

    def denied(code: str) -> ScopeDecision:
        return ScopeDecision(False, code, scope_ref, request.target.path_fingerprint,
                             endpoint_ref, request.action.value)

    try:
        scope.validate(now=now)
    except ScopeAuthorizationError as exc:
        return denied(exc.reason_code)
    if request.target not in scope.envelope.targets:
        return denied("scope.target_mismatch")
    if request.action in scope.envelope.prohibited_actions:
        return denied("scope.action_prohibited")
    if request.action not in scope.envelope.actions:
        return denied("scope.action_not_authorized")
    if request.uses_credentials and not scope.envelope.credential_use:
        return denied("scope.credentials_not_authorized")
    if request.account_ref is not None and request.account_ref not in scope.envelope.account_refs:
        return denied("scope.account_mismatch")
    if request.action is ActionAuthority.NETWORK_ACCESS:
        if scope.envelope.network_mode is NetworkMode.NONE:
            return denied("scope.network_disabled")
        if request.endpoint is None:
            return denied("scope.endpoint_required")
        try:
            endpoint = normalize_endpoint(request.endpoint)
        except ValueError:
            return denied("scope.endpoint_invalid")
        if endpoint not in scope.envelope.endpoints:
            return denied("scope.endpoint_not_authorized")
    elif request.endpoint is not None:
        return denied("scope.unexpected_endpoint")
    return ScopeDecision(True, "scope.allowed", scope_ref, request.target.path_fingerprint,
                         endpoint_ref, request.action.value)


def legacy_local_read_only_scope(target: str | Path, *, now: str) -> AuthorizedScope:
    """Narrow migration scope for scope-less local runs; never grants network/actions."""
    current = _time(now)
    valid_from = current.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    # Migration scope is deliberately short-lived and reconstructed only for old runs.
    expires = current.replace(microsecond=0).timestamp() + 86400
    valid_until = datetime.fromtimestamp(expires, timezone.utc).isoformat().replace("+00:00", "Z")
    target_grant = TargetGrant.from_path(target)
    envelope = ScopeEnvelope(
        scope_id="legacy-local-read-only",
        revision=1,
        valid_from=valid_from,
        valid_until=valid_until,
        targets=(target_grant,),
    )
    approval = ScopeApproval(
        scope_id=envelope.scope_id,
        revision=envelope.revision,
        content_digest=envelope.content_digest,
        approved_by="factory:migration",
        approved_at=valid_from,
        expires_at=valid_until,
        rationale="Compatibility scope for an existing local read-only run",
    )
    return AuthorizedScope(envelope, approval)


def author_scope(
    target: str | Path,
    *,
    scope_id: str,
    revision: int,
    actions: tuple[ActionAuthority, ...],
    endpoints: tuple[str, ...] = (),
    account_refs: tuple[str, ...] = (),
    credential_use: bool = False,
    approved_by: str,
    rationale: str,
    approved_at: str,
    valid_until: str,
    expires_at: str,
) -> AuthorizedScope:
    """Create an exact, inspectable scope without accepting credential values."""
    if not scope_id or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_." for character in scope_id):
        raise ValueError("scope_id must use lowercase letters, digits, '-', '_' or '.'")
    if revision < 1:
        raise ValueError("revision must be positive")
    if not actions:
        raise ValueError("at least one explicit action authority is required")
    if len(set(actions)) != len(actions):
        raise ValueError("action authorities must not contain duplicates")
    if not all(isinstance(action, ActionAuthority) for action in actions):
        raise ValueError("actions must use ActionAuthority values")
    start = _time(approved_at)
    end = _time(valid_until)
    expiry = _time(expires_at)
    if end <= start or (end - start).total_seconds() > MAX_SCOPE_SECONDS:
        raise ValueError("scope validity must be positive and at most 30 days")
    if expiry <= start or expiry > end or (expiry - start).total_seconds() > MAX_APPROVAL_SECONDS:
        raise ValueError("approval expiry must be positive, within scope, and at most 7 days")
    normalized_endpoints = tuple(normalize_endpoint(value) for value in endpoints)
    if len(set(normalized_endpoints)) != len(normalized_endpoints):
        raise ValueError("endpoint allowlist must not contain duplicates")
    has_network = ActionAuthority.NETWORK_ACCESS in actions
    if has_network != bool(normalized_endpoints):
        raise ValueError("network authority and an exact endpoint allowlist must be supplied together")
    if any(not _ACCOUNT_REF.fullmatch(value) for value in account_refs):
        raise ValueError("account references must be opaque account: identifiers")
    account_actions = {
        ActionAuthority.ENROLL_CHALLENGE,
        ActionAuthority.SUBMIT_CHALLENGE,
        ActionAuthority.THIRD_PARTY_MESSAGE,
    }
    if set(actions).intersection(account_actions) and not account_refs:
        raise ValueError("account-scoped actions require an opaque account reference")
    if credential_use and not account_refs:
        raise ValueError("credential use requires an opaque account reference")
    prohibited = tuple(action for action in DEFAULT_PROHIBITED_ACTIONS if action not in actions)
    envelope = ScopeEnvelope(
        scope_id=scope_id,
        revision=revision,
        valid_from=approved_at,
        valid_until=valid_until,
        targets=(TargetGrant.from_path(target),),
        endpoints=normalized_endpoints,
        account_refs=account_refs,
        actions=actions,
        network_mode=(NetworkMode.EXACT_ENDPOINTS if normalized_endpoints else NetworkMode.NONE),
        credential_use=credential_use,
        prohibited_actions=prohibited,
    )
    approval = ScopeApproval(
        scope_id=scope_id,
        revision=revision,
        content_digest=envelope.content_digest,
        approved_by=approved_by,
        approved_at=approved_at,
        expires_at=expires_at,
        rationale=rationale,
    )
    authorized = AuthorizedScope(envelope, approval)
    authorized.validate(now=approved_at)
    return authorized


def normalize_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("endpoint must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("endpoint cannot contain credentials, query, or fragment")
    host = parsed.hostname.lower()
    if "*" in host:
        raise ValueError("wildcard endpoints are forbidden")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and address.is_unspecified:
        raise ValueError("unspecified-address endpoints are forbidden")
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if not path.startswith("/"):
        raise ValueError("endpoint path must be absolute")
    return urlunsplit((parsed.scheme, f"{host}:{port}", path, "", ""))


def hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    elif path.is_dir():
        children = sorted(path.rglob("*"))
        if any(child.is_symlink() for child in children):
            raise ValueError("authorized target directories cannot contain symbolic links")
        for child in (item for item in children if item.is_file()):
            digest.update(child.relative_to(path).as_posix().encode())
            with child.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
    else:
        raise FileNotFoundError(path)
    return digest.hexdigest()


def opaque_ref(kind: str, value: str) -> str:
    return f"{kind}:{hashlib.sha256(value.encode()).hexdigest()[:16]}"


def _time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamps must be ISO 8601 UTC values")
    try:
        parsed = datetime.fromisoformat(
            value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else "")
        )
    except ValueError as exc:
        raise ValueError("invalid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamps must use UTC")
    return parsed


def _digest(value: str, name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
