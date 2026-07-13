"""Strict v1 analysis-range contracts and a deterministic conformance fake.

The fake models lifecycle, identity, scope, and evidence metadata only.  It never
provisions infrastructure, mounts a host path, opens a socket, or consumes a credential.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from typing import Any, ClassVar, Literal, Mapping, Self
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]{0,126}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

RangeStatus = Literal[
    "requested", "provisioning", "ready", "in-use", "resetting", "destroyed",
    "expired", "failed",
]


class RangeError(RuntimeError):
    """Base class for deterministic range failures."""


class RangeConflictError(RangeError):
    """A stable range or operation ID was reused for different canonical content."""


class RangeStateError(RangeError):
    """The requested operation is invalid for the current lifecycle state."""


class RangeAccessError(RangeError, PermissionError):
    """Work intent exceeds its exact lease or scope-derived authority."""


class InjectedRangeFailure(RangeError):
    """A deterministic fake failure was injected before a named transition."""


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def canonical_json(value: Any) -> str:
    if isinstance(value, RangeContract):
        value = value.to_dict()
    else:
        value = _json_value(value)
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ) + "\n"


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class RangeContract:
    schema_version: ClassVar[int] | int

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)

    def to_json(self) -> str:
        return canonical_json(self)

    @property
    def digest(self) -> str:
        return canonical_sha256(self)


def _strict(value: Any, name: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    missing, unknown = fields - set(value), set(value) - fields
    if missing:
        raise ValueError(f"{name} is missing fields: {sorted(missing)}")
    if unknown:
        raise ValueError(f"{name} contains unknown fields: {sorted(unknown)}")
    return value


def _array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    return value


def _version(value: Any) -> int:
    if type(value) is not int or value != SCHEMA_VERSION:
        raise ValueError("schema_version must be 1")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a bounded stable identifier")
    return value


def _text(value: Any, name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be non-empty text of at most {maximum} characters")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _strings(value: Any, name: str, *, identifiers: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be an array")
    result = tuple(
        _identifier(item, f"{name} item") if identifiers else _text(item, f"{name} item", 256)
        for item in value
    )
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must contain unique values")
    return tuple(sorted(result))


def _contracts(value: Any, expected: type, name: str) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be an array")
    result = tuple(value)
    if any(type(item) is not expected for item in result):
        raise ValueError(f"{name} must contain only {expected.__name__}")
    return result


def _timestamp(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValueError(f"{name} must be a UTC whole-second timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid timestamp") from exc
    return value


def _time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _relative_path(value: Any, name: str) -> str:
    text = _text(value, name, 256)
    path = PurePosixPath(text)
    if path.is_absolute() or text != path.as_posix() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{name} must be a normalized relative POSIX path")
    return text


def _endpoint(value: Any, name: str) -> str:
    text = _text(value, name, 256)
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} must be an exact HTTPS origin") from exc
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or host != host.lower()
        or len(host) > 253
        or any(not _HOST_LABEL.fullmatch(label) for label in host.split("."))
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError(f"{name} must be an exact HTTPS origin")
    expected = f"https://{host}" + (f":{port}" if port is not None else "")
    if text != expected:
        raise ValueError(f"{name} must be a canonical exact HTTPS origin")
    return text


@dataclass(frozen=True)
class RangeServiceV1(RangeContract):
    service_id: str
    protocol: Literal["tcp", "udp"]
    port: int

    def __post_init__(self) -> None:
        _identifier(self.service_id, "service_id")
        if self.protocol not in {"tcp", "udp"}:
            raise ValueError("service protocol must be tcp or udp")
        _integer(self.port, "service port", 1, 65535)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        return cls(**_strict(value, cls.__name__, {"service_id", "protocol", "port"}))


@dataclass(frozen=True)
class RangeNodeV1(RangeContract):
    node_id: str
    platform: Literal["linux", "windows"]
    architecture: Literal["x86_64", "arm64"]
    image_sha256: str
    capabilities: tuple[str, ...]
    services: tuple[RangeServiceV1, ...]

    def __post_init__(self) -> None:
        _identifier(self.node_id, "node_id")
        if self.platform not in {"linux", "windows"}:
            raise ValueError("unsupported node platform")
        if self.architecture not in {"x86_64", "arm64"}:
            raise ValueError("unsupported node architecture")
        _digest(self.image_sha256, "image_sha256")
        capabilities = _strings(self.capabilities, "capabilities", identifiers=True)
        services = _contracts(self.services, RangeServiceV1, "services")
        if len({item.service_id for item in services}) != len(services):
            raise ValueError("node services must have unique IDs")
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "services", tuple(sorted(services, key=lambda item: item.service_id)))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = {"node_id", "platform", "architecture", "image_sha256", "capabilities", "services"}
        value = _strict(value, cls.__name__, fields)
        capabilities = _array(value["capabilities"], "capabilities")
        services = _array(value["services"], "services")
        return cls(
            **{key: value[key] for key in fields - {"capabilities", "services"}},
            capabilities=tuple(capabilities),
            services=tuple(RangeServiceV1.from_dict(item) for item in services),
        )


@dataclass(frozen=True)
class RangeLinkV1(RangeContract):
    source_node: str
    destination_node: str
    service_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.source_node, "source_node")
        _identifier(self.destination_node, "destination_node")
        if self.source_node == self.destination_node:
            raise ValueError("range links cannot target the same node")
        services = _strings(self.service_ids, "service_ids", identifiers=True)
        if not services:
            raise ValueError("range links require at least one service")
        object.__setattr__(self, "service_ids", services)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        value = _strict(value, cls.__name__, {"source_node", "destination_node", "service_ids"})
        return cls(
            value["source_node"], value["destination_node"],
            tuple(_array(value["service_ids"], "service_ids")),
        )


@dataclass(frozen=True)
class RangeTemplateV1(RangeContract):
    schema_version: int
    template_id: str
    template_version: str
    nodes: tuple[RangeNodeV1, ...]
    links: tuple[RangeLinkV1, ...]

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _identifier(self.template_id, "template_id")
        _identifier(self.template_version, "template_version")
        nodes = _contracts(self.nodes, RangeNodeV1, "nodes")
        links = _contracts(self.links, RangeLinkV1, "links")
        if not nodes or len({item.node_id for item in nodes}) != len(nodes):
            raise ValueError("range template requires unique nodes")
        node_index = {item.node_id: item for item in nodes}
        keys: set[tuple[str, str, tuple[str, ...]]] = set()
        for link in links:
            if link.source_node not in node_index or link.destination_node not in node_index:
                raise ValueError("range link references an unknown node")
            available = {item.service_id for item in node_index[link.destination_node].services}
            if not set(link.service_ids) <= available:
                raise ValueError("range link references an undeclared destination service")
            key = (link.source_node, link.destination_node, link.service_ids)
            if key in keys:
                raise ValueError("range links must be unique")
            keys.add(key)
        object.__setattr__(self, "nodes", tuple(sorted(nodes, key=lambda item: item.node_id)))
        object.__setattr__(self, "links", tuple(sorted(
            links, key=lambda item: (item.source_node, item.destination_node, item.service_ids),
        )))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = {"schema_version", "template_id", "template_version", "nodes", "links"}
        value = _strict(value, cls.__name__, fields)
        nodes = _array(value["nodes"], "nodes")
        links = _array(value["links"], "links")
        return cls(
            value["schema_version"], value["template_id"], value["template_version"],
            tuple(RangeNodeV1.from_dict(item) for item in nodes),
            tuple(RangeLinkV1.from_dict(item) for item in links),
        )


@dataclass(frozen=True)
class ImmutableInputV1(RangeContract):
    input_id: str
    sha256: str
    size: int
    media_type: str
    mount_path: str
    read_only: bool = True

    def __post_init__(self) -> None:
        _identifier(self.input_id, "input_id")
        _digest(self.sha256, "input sha256")
        _integer(self.size, "input size", 0, 1_000_000_000_000)
        if not isinstance(self.media_type, str) or not _MEDIA_TYPE.fullmatch(self.media_type):
            raise ValueError("media_type must be a bounded lowercase media type")
        _relative_path(self.mount_path, "mount_path")
        if self.read_only is not True:
            raise ValueError("v1 immutable inputs must be read-only")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        return cls(**_strict(
            value, cls.__name__, {"input_id", "sha256", "size", "media_type", "mount_path", "read_only"},
        ))


_SCOPE_ACTIONS = frozenset({"mount_input", "network_access", "credential_use"})


@dataclass(frozen=True)
class RangeScopeV1(RangeContract):
    scope_id: str
    revision: int
    actions: tuple[str, ...]
    endpoints: tuple[str, ...]
    credential_refs: tuple[str, ...]
    input_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.scope_id, "scope_id")
        _integer(self.revision, "scope revision", 1, 1_000_000_000)
        actions = _strings(self.actions, "scope actions", identifiers=True)
        if not set(actions) <= _SCOPE_ACTIONS:
            raise ValueError("scope contains an unsupported range action")
        endpoints = _strings(self.endpoints, "scope endpoints")
        for item in endpoints:
            _endpoint(item, "scope endpoint")
        credentials = _strings(self.credential_refs, "credential_refs", identifiers=True)
        if any(not item.startswith("credential:") for item in credentials):
            raise ValueError("credentials must be opaque credential: references")
        inputs = _strings(self.input_ids, "scope input_ids", identifiers=True)
        if endpoints and "network_access" not in actions:
            raise ValueError("scope endpoints require network_access authority")
        if credentials and "credential_use" not in actions:
            raise ValueError("credential references require credential_use authority")
        if inputs and "mount_input" not in actions:
            raise ValueError("scope input IDs require mount_input authority")
        object.__setattr__(self, "actions", actions)
        object.__setattr__(self, "endpoints", endpoints)
        object.__setattr__(self, "credential_refs", credentials)
        object.__setattr__(self, "input_ids", inputs)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = {"scope_id", "revision", "actions", "endpoints", "credential_refs", "input_ids"}
        value = _strict(value, cls.__name__, fields)
        return cls(
            value["scope_id"], value["revision"],
            tuple(_array(value["actions"], "actions")),
            tuple(_array(value["endpoints"], "endpoints")),
            tuple(_array(value["credential_refs"], "credential_refs")),
            tuple(_array(value["input_ids"], "input_ids")),
        )


@dataclass(frozen=True)
class RangeNetworkV1(RangeContract):
    mode: Literal["isolated"]
    allowed_egress: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode != "isolated":
            raise ValueError("v1 range network mode must be isolated")
        egress = _strings(self.allowed_egress, "allowed_egress")
        for item in egress:
            _endpoint(item, "allowed egress")
        object.__setattr__(self, "allowed_egress", egress)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        value = _strict(value, cls.__name__, {"mode", "allowed_egress"})
        return cls(value["mode"], tuple(_array(value["allowed_egress"], "allowed_egress")))


@dataclass(frozen=True)
class RangeResourcesV1(RangeContract):
    max_nodes: int
    max_vcpus_per_node: int
    max_memory_mb_per_node: int
    max_scratch_bytes: int
    max_output_bytes: int
    max_work_items: int

    def __post_init__(self) -> None:
        _integer(self.max_nodes, "max_nodes", 1, 64)
        _integer(self.max_vcpus_per_node, "max_vcpus_per_node", 1, 256)
        _integer(self.max_memory_mb_per_node, "max_memory_mb_per_node", 64, 1_048_576)
        _integer(self.max_scratch_bytes, "max_scratch_bytes", 1, 10_000_000_000_000)
        _integer(self.max_output_bytes, "max_output_bytes", 1, 10_000_000_000_000)
        _integer(self.max_work_items, "max_work_items", 1, 1_000_000)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = {
            "max_nodes", "max_vcpus_per_node", "max_memory_mb_per_node",
            "max_scratch_bytes", "max_output_bytes", "max_work_items",
        }
        return cls(**_strict(value, cls.__name__, fields))


@dataclass(frozen=True)
class RangeLifecyclePolicyV1(RangeContract):
    max_lifetime_seconds: int
    reset_policy: Literal["recreate-scratch"]
    destroy_policy: Literal["explicit-or-expiry"]

    def __post_init__(self) -> None:
        _integer(self.max_lifetime_seconds, "max_lifetime_seconds", 1, 604_800)
        if self.reset_policy != "recreate-scratch":
            raise ValueError("unsupported reset policy")
        if self.destroy_policy != "explicit-or-expiry":
            raise ValueError("unsupported destroy policy")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        return cls(**_strict(
            value, cls.__name__, {"max_lifetime_seconds", "reset_policy", "destroy_policy"},
        ))


@dataclass(frozen=True)
class RangeSpecV1(RangeContract):
    schema_version: int
    range_id: str
    template_sha256: str
    inputs: tuple[ImmutableInputV1, ...]
    scope: RangeScopeV1
    network: RangeNetworkV1
    resources: RangeResourcesV1
    lifecycle: RangeLifecyclePolicyV1
    requested_at: str
    expires_at: str

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _identifier(self.range_id, "range_id")
        _digest(self.template_sha256, "template_sha256")
        inputs = _contracts(self.inputs, ImmutableInputV1, "inputs")
        if len({item.input_id for item in inputs}) != len(inputs):
            raise ValueError("range inputs must have unique IDs")
        if type(self.scope) is not RangeScopeV1 or type(self.network) is not RangeNetworkV1 \
                or type(self.resources) is not RangeResourcesV1 \
                or type(self.lifecycle) is not RangeLifecyclePolicyV1:
            raise ValueError("range spec contains invalid nested contracts")
        requested = _time(_timestamp(self.requested_at, "requested_at"))
        expires = _time(_timestamp(self.expires_at, "expires_at"))
        if expires <= requested:
            raise ValueError("expires_at must be after requested_at")
        if (expires - requested).total_seconds() > self.lifecycle.max_lifetime_seconds:
            raise ValueError("range lifetime exceeds its lifecycle ceiling")
        input_ids = {item.input_id for item in inputs}
        if not input_ids <= set(self.scope.input_ids):
            raise ValueError("immutable inputs are outside the scope projection")
        if not set(self.network.allowed_egress) <= set(self.scope.endpoints):
            raise ValueError("range egress is outside the scope projection")
        object.__setattr__(self, "inputs", tuple(sorted(inputs, key=lambda item: item.input_id)))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = {
            "schema_version", "range_id", "template_sha256", "inputs", "scope", "network",
            "resources", "lifecycle", "requested_at", "expires_at",
        }
        value = _strict(value, cls.__name__, fields)
        inputs = _array(value["inputs"], "inputs")
        return cls(
            value["schema_version"], value["range_id"], value["template_sha256"],
            tuple(ImmutableInputV1.from_dict(item) for item in inputs),
            RangeScopeV1.from_dict(value["scope"]), RangeNetworkV1.from_dict(value["network"]),
            RangeResourcesV1.from_dict(value["resources"]),
            RangeLifecyclePolicyV1.from_dict(value["lifecycle"]),
            value["requested_at"], value["expires_at"],
        )


@dataclass(frozen=True)
class ProviderHandleV1(RangeContract):
    kind: Literal["range", "node"]
    opaque_id: str

    def __post_init__(self) -> None:
        if self.kind not in {"range", "node"}:
            raise ValueError("provider handle kind must be range or node")
        _identifier(self.opaque_id, "opaque provider handle")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        return cls(**_strict(value, cls.__name__, {"kind", "opaque_id"}))


@dataclass(frozen=True)
class NodeHandleV1(RangeContract):
    node_id: str
    handle: ProviderHandleV1

    def __post_init__(self) -> None:
        _identifier(self.node_id, "node_id")
        if type(self.handle) is not ProviderHandleV1 or self.handle.kind != "node":
            raise ValueError("node handle must contain a node provider handle")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        value = _strict(value, cls.__name__, {"node_id", "handle"})
        return cls(value["node_id"], ProviderHandleV1.from_dict(value["handle"]))


@dataclass(frozen=True)
class RangeFailureV1(RangeContract):
    code: str
    reason: str
    transition: RangeStatus
    retryable: bool

    def __post_init__(self) -> None:
        _identifier(self.code, "failure code")
        _text(self.reason, "failure reason", 512)
        if self.transition not in RANGE_STATUSES:
            raise ValueError("failure transition is unknown")
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable must be a boolean")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        return cls(**_strict(value, cls.__name__, {"code", "reason", "transition", "retryable"}))


@dataclass(frozen=True)
class RangeLeaseStateV1(RangeContract):
    schema_version: int
    range_id: str
    spec_sha256: str
    status: RangeStatus
    revision: int
    generation: int
    range_handle: ProviderHandleV1 | None
    node_handles: tuple[NodeHandleV1, ...]
    failure: RangeFailureV1 | None
    terminal_reason: str | None
    updated_at: str

    def __post_init__(self) -> None:
        _version(self.schema_version)
        _identifier(self.range_id, "range_id")
        _digest(self.spec_sha256, "spec_sha256")
        if self.status not in RANGE_STATUSES:
            raise ValueError("unsupported range status")
        _integer(self.revision, "state revision", 1, 1_000_000_000)
        _integer(self.generation, "lease generation", 1, 1_000_000_000)
        if self.range_handle is not None and (
            type(self.range_handle) is not ProviderHandleV1 or self.range_handle.kind != "range"
        ):
            raise ValueError("range_handle must be an opaque range handle")
        handles = _contracts(self.node_handles, NodeHandleV1, "node_handles")
        if len({item.node_id for item in handles}) != len(handles):
            raise ValueError("node handles must be unique")
        if self.failure is not None and type(self.failure) is not RangeFailureV1:
            raise ValueError("failure must be a RangeFailureV1")
        if self.status == "failed" and self.failure is None:
            raise ValueError("failed ranges require a reasoned failure")
        if self.status != "failed" and self.failure is not None:
            raise ValueError("only failed ranges may carry failure details")
        if self.status in {"ready", "in-use"} and (
            self.range_handle is None or not handles
        ):
            raise ValueError("ready and in-use ranges require provider handles")
        if self.status == "requested" and (self.range_handle is not None or handles):
            raise ValueError("requested ranges cannot carry provider handles")
        if self.status == "destroyed" and (self.range_handle is not None or handles):
            raise ValueError("destroyed ranges cannot carry provider handles")
        if self.terminal_reason is not None:
            _text(self.terminal_reason, "terminal_reason", 256)
        if self.status in {"destroyed", "expired"} and self.terminal_reason is None:
            raise ValueError("destroyed and expired ranges require a terminal reason")
        if self.status not in {"destroyed", "expired"} and self.terminal_reason is not None:
            raise ValueError("only destroyed and expired ranges may carry a terminal reason")
        _timestamp(self.updated_at, "updated_at")
        object.__setattr__(self, "node_handles", tuple(sorted(handles, key=lambda item: item.node_id)))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = {
            "schema_version", "range_id", "spec_sha256", "status", "revision", "generation",
            "range_handle", "node_handles", "failure", "terminal_reason", "updated_at",
        }
        value = _strict(value, cls.__name__, fields)
        handles = _array(value["node_handles"], "node_handles")
        return cls(
            **{key: value[key] for key in fields - {"range_handle", "node_handles", "failure"}},
            range_handle=(None if value["range_handle"] is None
                          else ProviderHandleV1.from_dict(value["range_handle"])),
            node_handles=tuple(NodeHandleV1.from_dict(item) for item in handles),
            failure=(None if value["failure"] is None
                     else RangeFailureV1.from_dict(value["failure"])),
        )


@dataclass(frozen=True)
class RangeTransitionRule:
    owner: Literal["requester", "adapter", "scheduler", "clock"]
    allowed_predecessors: tuple[RangeStatus | None, ...]
    terminal: bool


RANGE_STATUSES = frozenset({
    "requested", "provisioning", "ready", "in-use", "resetting", "destroyed",
    "expired", "failed",
})
RANGE_TRANSITIONS: Mapping[RangeStatus, RangeTransitionRule] = MappingProxyType({
    "requested": RangeTransitionRule("requester", (None,), False),
    "provisioning": RangeTransitionRule("adapter", ("requested",), False),
    "ready": RangeTransitionRule("adapter", ("provisioning", "resetting"), False),
    "in-use": RangeTransitionRule("scheduler", ("ready",), False),
    "resetting": RangeTransitionRule("adapter", ("ready", "in-use", "failed"), False),
    "expired": RangeTransitionRule(
        "clock", ("requested", "provisioning", "ready", "in-use", "resetting", "failed"),
        False,
    ),
    "failed": RangeTransitionRule(
        "adapter", ("requested", "provisioning", "ready", "in-use", "resetting", "expired"),
        False,
    ),
    "destroyed": RangeTransitionRule(
        "adapter",
        ("requested", "provisioning", "ready", "in-use", "resetting", "expired", "failed"),
        True,
    ),
})


def require_range_transition(
    predecessor: RangeStatus | None, target: RangeStatus, owner: str,
) -> None:
    rule = RANGE_TRANSITIONS.get(target)
    if rule is None or owner != rule.owner or predecessor not in rule.allowed_predecessors:
        raise RangeStateError(
            f"transition {predecessor or 'none'} -> {target} is not owned by {owner}"
        )


@dataclass(frozen=True)
class RangeWorkIntentV1(RangeContract):
    network_endpoints: tuple[str, ...] = ()
    credential_refs: tuple[str, ...] = ()
    input_mounts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        endpoints = _strings(self.network_endpoints, "network_endpoints")
        for item in endpoints:
            _endpoint(item, "network endpoint")
        credentials = _strings(self.credential_refs, "credential_refs", identifiers=True)
        if any(not item.startswith("credential:") for item in credentials):
            raise ValueError("credential intent requires opaque credential: references")
        mounts = _strings(self.input_mounts, "input_mounts", identifiers=True)
        object.__setattr__(self, "network_endpoints", endpoints)
        object.__setattr__(self, "credential_refs", credentials)
        object.__setattr__(self, "input_mounts", mounts)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        value = _strict(value, cls.__name__, {"network_endpoints", "credential_refs", "input_mounts"})
        return cls(
            tuple(_array(value["network_endpoints"], "network_endpoints")),
            tuple(_array(value["credential_refs"], "credential_refs")),
            tuple(_array(value["input_mounts"], "input_mounts")),
        )


@dataclass(frozen=True)
class RangeWorkRequestV1(RangeContract):
    schema_version: int
    operation_id: str
    range_id: str
    node_id: str
    node_handle: ProviderHandleV1
    input_ids: tuple[str, ...]
    action: Literal["inspect-inputs", "summarize-topology"]
    output_name: str
    intent: RangeWorkIntentV1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        for name in ("operation_id", "range_id", "node_id"):
            _identifier(getattr(self, name), name)
        if type(self.node_handle) is not ProviderHandleV1 or self.node_handle.kind != "node":
            raise ValueError("work requires an opaque node handle")
        inputs = _strings(self.input_ids, "input_ids", identifiers=True)
        if self.action not in {"inspect-inputs", "summarize-topology"}:
            raise ValueError("unsupported fake range action")
        _relative_path(self.output_name, "output_name")
        if type(self.intent) is not RangeWorkIntentV1:
            raise ValueError("work intent must be RangeWorkIntentV1")
        if set(self.intent.input_mounts) != set(inputs):
            raise ValueError("work input mounts must exactly match declared input IDs")
        object.__setattr__(self, "input_ids", inputs)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = {
            "schema_version", "operation_id", "range_id", "node_id", "node_handle",
            "input_ids", "action", "output_name", "intent",
        }
        value = _strict(value, cls.__name__, fields)
        return cls(
            **{key: value[key] for key in fields - {"node_handle", "input_ids", "intent"}},
            node_handle=ProviderHandleV1.from_dict(value["node_handle"]),
            input_ids=tuple(_array(value["input_ids"], "input_ids")),
            intent=RangeWorkIntentV1.from_dict(value["intent"]),
        )


@dataclass(frozen=True)
class RangeScratchV1(RangeContract):
    schema_version: int
    scratch_id: str
    operation_id: str
    range_id: str
    node_id: str
    generation: int
    sha256: str
    size: int

    def __post_init__(self) -> None:
        _version(self.schema_version)
        for name in ("scratch_id", "operation_id", "range_id", "node_id"):
            _identifier(getattr(self, name), name)
        _integer(self.generation, "scratch generation", 1, 1_000_000_000)
        _digest(self.sha256, "scratch sha256")
        _integer(self.size, "scratch size", 0, 10_000_000_000_000)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = {
            "schema_version", "scratch_id", "operation_id", "range_id", "node_id",
            "generation", "sha256", "size",
        }
        return cls(**_strict(value, cls.__name__, fields))


@dataclass(frozen=True)
class RangeOutputV1(RangeContract):
    schema_version: int
    output_id: str
    operation_id: str
    range_id: str
    node_id: str
    generation: int
    logical_path: str
    sha256: str
    size: int
    media_type: str
    verified: bool
    input_sha256: tuple[str, ...]

    def __post_init__(self) -> None:
        _version(self.schema_version)
        for name in ("output_id", "operation_id", "range_id", "node_id"):
            _identifier(getattr(self, name), name)
        _integer(self.generation, "output generation", 1, 1_000_000_000)
        _relative_path(self.logical_path, "logical_path")
        _digest(self.sha256, "output sha256")
        _integer(self.size, "output size", 0, 10_000_000_000_000)
        if not isinstance(self.media_type, str) or not _MEDIA_TYPE.fullmatch(self.media_type):
            raise ValueError("output media_type is invalid")
        if self.verified is not True:
            raise ValueError("fake outputs must carry verified metadata")
        digests = _strings(self.input_sha256, "input_sha256")
        if any(not _DIGEST.fullmatch(item) for item in digests):
            raise ValueError("input_sha256 contains an invalid digest")
        object.__setattr__(self, "input_sha256", digests)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = {
            "schema_version", "output_id", "operation_id", "range_id", "node_id",
            "generation", "logical_path", "sha256", "size", "media_type", "verified",
            "input_sha256",
        }
        value = _strict(value, cls.__name__, fields)
        return cls(**{
            **value, "input_sha256": tuple(_array(value["input_sha256"], "input_sha256")),
        })


@dataclass
class _RangeRecord:
    template: RangeTemplateV1
    spec: RangeSpecV1
    state: RangeLeaseStateV1
    history: list[RangeLeaseStateV1]
    scratch: dict[str, RangeScratchV1]
    outputs: dict[str, RangeOutputV1]
    evidence: dict[str, RangeOutputV1]
    work_count: int = 0


_ERRORS: dict[str, type[RangeError]] = {
    cls.__name__: cls for cls in (
        RangeError, RangeConflictError, RangeStateError, RangeAccessError,
        InjectedRangeFailure,
    )
}


def _validate_history(
    history: list[RangeLeaseStateV1], range_id: str, spec_sha256: str,
) -> None:
    predecessor: RangeStatus | None = None
    previous_revision = 0
    previous_generation = 1
    for state in history:
        if state.range_id != range_id or state.spec_sha256 != spec_sha256:
            raise ValueError("checkpoint transition history changes range identity")
        if state.revision != previous_revision + 1:
            raise ValueError("checkpoint transition revisions are not contiguous")
        rule = RANGE_TRANSITIONS[state.status]
        require_range_transition(predecessor, state.status, rule.owner)
        if state.generation < previous_generation or state.generation > previous_generation + 1:
            raise ValueError("checkpoint lease generations are not contiguous")
        if state.generation > previous_generation and state.status != "ready":
            raise ValueError("checkpoint generation changed outside a ready transition")
        predecessor = state.status
        previous_revision = state.revision
        previous_generation = state.generation


def _validate_checkpoint_result(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("checkpoint operation result must be a contract object")
    if "output_id" in value:
        RangeOutputV1.from_dict(value)
    elif "status" in value:
        RangeLeaseStateV1.from_dict(value)
    else:
        raise ValueError("checkpoint operation result has an unknown contract shape")


class DeterministicFakeRangeAdapter:
    """Serializable two-node range lifecycle fake with no infrastructure effects."""

    def __init__(self, *, now: str = "2026-07-13T12:00:00Z") -> None:
        self._now = _timestamp(now, "now")
        self._ranges: dict[str, _RangeRecord] = {}
        self._operations: dict[str, dict[str, Any]] = {}
        self._fail_next: set[str] = set()

    @property
    def now(self) -> str:
        return self._now

    def advance(self, seconds: int) -> None:
        _integer(seconds, "advance seconds", 1, 31_536_000)
        self._now = _format_time(_time(self._now) + timedelta(seconds=seconds))

    def inject_failure(self, transition: RangeStatus) -> None:
        if transition not in RANGE_STATUSES:
            raise ValueError("unknown failure-injection transition")
        self._fail_next.add(transition)

    def state(self, range_id: str) -> RangeLeaseStateV1:
        return self._record(range_id).state

    def history(self, range_id: str) -> tuple[RangeLeaseStateV1, ...]:
        return tuple(self._record(range_id).history)

    def outputs(self, range_id: str) -> tuple[RangeOutputV1, ...]:
        return tuple(self._record(range_id).outputs[key] for key in sorted(self._record(range_id).outputs))

    def scratch(self, range_id: str) -> tuple[RangeScratchV1, ...]:
        record = self._record(range_id)
        return tuple(record.scratch[key] for key in sorted(record.scratch))

    def evidence(self, range_id: str) -> tuple[RangeOutputV1, ...]:
        return tuple(self._record(range_id).evidence[key] for key in sorted(self._record(range_id).evidence))

    def output(self, range_id: str, output_id: str) -> RangeOutputV1:
        record = self._record(range_id)
        try:
            return record.evidence[output_id]
        except KeyError as exc:
            raise RangeAccessError("output is not owned by the requested range") from exc

    def provision(
        self, operation_id: str, template: RangeTemplateV1, spec: RangeSpecV1,
    ) -> RangeLeaseStateV1:
        _identifier(operation_id, "operation_id")
        if type(template) is not RangeTemplateV1 or type(spec) is not RangeSpecV1:
            raise ValueError("provision requires exact v1 template and spec contracts")
        request = {"kind": "provision", "template": template.to_dict(), "spec": spec.to_dict()}
        replay = self._operation_replay(operation_id, request, RangeLeaseStateV1.from_dict)
        if replay is not None:
            return replay
        try:
            if template.digest != spec.template_sha256:
                raise RangeConflictError("range spec does not bind the supplied template")
            if len(template.nodes) > spec.resources.max_nodes:
                raise RangeStateError("template node count exceeds the resource ceiling")
            if _time(self._now) < _time(spec.requested_at):
                raise RangeStateError("range lifetime has not started")
            prior = self._ranges.get(spec.range_id)
            if prior is not None:
                if prior.template.digest != template.digest or prior.spec.digest != spec.digest:
                    raise RangeConflictError("range ID is already bound to different content")
                result = prior.state
            else:
                state = self._new_state(spec, "requested")
                record = _RangeRecord(template, spec, state, [state], {}, {}, {})
                self._ranges[spec.range_id] = record
                self._maybe_fail(record, "requested")
                if _time(self._now) >= _time(spec.expires_at):
                    self._maybe_fail(record, "expired")
                    self._transition(
                        record, "expired", "clock", terminal_reason="lifetime elapsed",
                    )
                    result = record.state
                    self._operation_success(operation_id, request, result)
                    return result
                self._maybe_fail(record, "provisioning")
                self._transition(record, "provisioning", "adapter")
                self._maybe_fail(record, "ready")
                range_handle, node_handles = self._handles(record, 1)
                self._transition(
                    record, "ready", "adapter", range_handle=range_handle,
                    node_handles=node_handles,
                )
                result = record.state
            self._operation_success(operation_id, request, result)
            return result
        except RangeError as exc:
            self._operation_error(operation_id, request, exc)
            raise

    def execute(self, request: RangeWorkRequestV1) -> RangeOutputV1:
        if type(request) is not RangeWorkRequestV1:
            raise ValueError("execute requires an exact RangeWorkRequestV1")
        replay = self._operation_replay(request.operation_id, request.to_dict(), RangeOutputV1.from_dict)
        if replay is not None:
            return replay
        try:
            record = self._record(request.range_id)
            self._expire_if_needed(record)
            if record.state.status != "ready":
                raise RangeStateError(f"range is {record.state.status}, not ready")
            self._authorize_work(record, request)
            if record.work_count >= record.spec.resources.max_work_items:
                raise RangeStateError("range work-item ceiling is exhausted")
            self._maybe_fail(record, "in-use")
            self._transition(record, "in-use", "scheduler")
            inputs = {item.input_id: item for item in record.spec.inputs}
            payload = canonical_json({
                "action": request.action,
                "range_id": request.range_id,
                "node_id": request.node_id,
                "generation": record.state.generation,
                "inputs": [inputs[item].sha256 for item in request.input_ids],
                "topology": record.template.to_dict(),
            }).encode("utf-8")
            if len(payload) > record.spec.resources.max_scratch_bytes:
                raise RangeStateError("deterministic scratch exceeds the scratch ceiling")
            if len(payload) > record.spec.resources.max_output_bytes:
                raise RangeStateError("deterministic output exceeds the output ceiling")
            scratch = RangeScratchV1(
                SCHEMA_VERSION,
                f"scratch-{canonical_sha256({'operation_id': request.operation_id})[:24]}",
                request.operation_id, request.range_id, request.node_id,
                record.state.generation, hashlib.sha256(payload).hexdigest(), len(payload),
            )
            output = RangeOutputV1(
                SCHEMA_VERSION,
                f"output-{canonical_sha256({'operation_id': request.operation_id})[:24]}",
                request.operation_id, request.range_id, request.node_id,
                record.state.generation, request.output_name, hashlib.sha256(payload).hexdigest(),
                len(payload), "application/json", True,
                tuple(inputs[item].sha256 for item in request.input_ids),
            )
            record.scratch[scratch.scratch_id] = scratch
            record.outputs[output.output_id] = output
            record.evidence[output.output_id] = output
            record.work_count += 1
            self._operation_success(request.operation_id, request.to_dict(), output)
            return output
        except RangeError as exc:
            self._operation_error(request.operation_id, request.to_dict(), exc)
            raise

    def reset(self, operation_id: str, range_id: str) -> RangeLeaseStateV1:
        return self._lifecycle(operation_id, range_id, "reset")

    def destroy(
        self, operation_id: str, range_id: str, *, reason: str = "explicit cleanup",
    ) -> RangeLeaseStateV1:
        return self._lifecycle(operation_id, range_id, "destroy", reason=reason)

    def cancel(self, operation_id: str, range_id: str) -> RangeLeaseStateV1:
        return self._lifecycle(operation_id, range_id, "cancel", reason="cancelled")

    def expire(self, operation_id: str, range_id: str) -> RangeLeaseStateV1:
        return self._lifecycle(operation_id, range_id, "expire")

    def _lifecycle(
        self, operation_id: str, range_id: str, kind: str, *, reason: str | None = None,
    ) -> RangeLeaseStateV1:
        _identifier(operation_id, "operation_id")
        _identifier(range_id, "range_id")
        request = {"kind": kind, "range_id": range_id, "reason": reason}
        replay = self._operation_replay(operation_id, request, RangeLeaseStateV1.from_dict)
        if replay is not None:
            return replay
        try:
            record = self._record(range_id)
            if kind == "reset":
                self._expire_if_needed(record)
                if record.state.status not in {"ready", "in-use", "failed"}:
                    article = "an" if record.state.status == "expired" else "a"
                    raise RangeStateError(
                        f"cannot reset {article} {record.state.status} range"
                    )
                self._maybe_fail(record, "resetting")
                self._transition(record, "resetting", "adapter")
                next_generation = record.state.generation + 1
                self._maybe_fail(record, "ready")
                range_handle, node_handles = self._handles(record, next_generation)
                self._transition(
                    record, "ready", "adapter", generation=next_generation,
                    range_handle=range_handle, node_handles=node_handles,
                )
                record.scratch.clear()
                record.outputs.clear()
            elif kind in {"destroy", "cancel"}:
                if record.state.status == "destroyed":
                    result = record.state
                    self._operation_success(operation_id, request, result)
                    return result
                self._maybe_fail(record, "destroyed")
                self._transition(
                    record, "destroyed", "adapter", terminal_reason=reason or "explicit cleanup",
                    range_handle=None, node_handles=(),
                )
                record.scratch.clear()
                record.outputs.clear()
            elif kind == "expire":
                if record.state.status == "expired":
                    result = record.state
                    self._operation_success(operation_id, request, result)
                    return result
                if _time(self._now) < _time(record.spec.expires_at):
                    raise RangeStateError("range lifetime has not expired")
                if record.state.status == "destroyed":
                    raise RangeStateError(f"cannot expire a {record.state.status} range")
                self._maybe_fail(record, "expired")
                self._transition(
                    record, "expired", "clock", terminal_reason="lifetime elapsed",
                )
                record.scratch.clear()
                record.outputs.clear()
            else:  # pragma: no cover - private callers use the fixed methods above
                raise ValueError("unknown lifecycle operation")
            result = record.state
            self._operation_success(operation_id, request, result)
            return result
        except RangeError as exc:
            self._operation_error(operation_id, request, exc)
            raise

    def _authorize_work(self, record: _RangeRecord, request: RangeWorkRequestV1) -> None:
        handles = {item.node_id: item.handle for item in record.state.node_handles}
        if request.node_id not in handles or handles[request.node_id] != request.node_handle:
            raise RangeAccessError("node handle is not owned by this range generation")
        declared_inputs = {item.input_id for item in record.spec.inputs}
        if not set(request.input_ids) <= declared_inputs:
            raise RangeAccessError("work references an input outside this range")
        scope = record.spec.scope
        if not set(request.intent.input_mounts) <= set(scope.input_ids):
            raise RangeAccessError("input mount intent is outside the scope revision")
        if request.intent.network_endpoints:
            if "network_access" not in scope.actions:
                raise RangeAccessError("network intent lacks scope authority")
            if not set(request.intent.network_endpoints) <= set(record.spec.network.allowed_egress):
                raise RangeAccessError("network intent is outside the exact range allowlist")
        if request.intent.credential_refs:
            if "credential_use" not in scope.actions:
                raise RangeAccessError("credential intent lacks scope authority")
            if not set(request.intent.credential_refs) <= set(scope.credential_refs):
                raise RangeAccessError("credential intent is outside the scope revision")

    def _expire_if_needed(self, record: _RangeRecord) -> None:
        if record.state.status in {
            "requested", "provisioning", "ready", "in-use", "resetting", "failed",
        } \
                and _time(self._now) >= _time(record.spec.expires_at):
            self._transition(
                record, "expired", "clock", terminal_reason="lifetime elapsed",
            )
            record.scratch.clear()
            record.outputs.clear()

    def _new_state(self, spec: RangeSpecV1, status: RangeStatus) -> RangeLeaseStateV1:
        require_range_transition(None, status, "requester")
        return RangeLeaseStateV1(
            SCHEMA_VERSION, spec.range_id, spec.digest, status, 1, 1,
            None, (), None, None, self._now,
        )

    def _transition(
        self, record: _RangeRecord, target: RangeStatus, owner: str, *,
        generation: int | None = None,
        range_handle: ProviderHandleV1 | None | object = ...,
        node_handles: tuple[NodeHandleV1, ...] | object = ...,
        terminal_reason: str | None = None,
        failure: RangeFailureV1 | None = None,
    ) -> None:
        require_range_transition(record.state.status, target, owner)
        next_state = replace(
            record.state,
            status=target,
            revision=record.state.revision + 1,
            generation=generation or record.state.generation,
            range_handle=(record.state.range_handle if range_handle is ... else range_handle),
            node_handles=(record.state.node_handles if node_handles is ... else node_handles),
            failure=failure,
            terminal_reason=terminal_reason,
            updated_at=self._now,
        )
        record.state = next_state
        record.history.append(next_state)

    def _maybe_fail(self, record: _RangeRecord | None, target: RangeStatus) -> None:
        if target not in self._fail_next:
            return
        self._fail_next.remove(target)
        message = f"injected failure before {target} transition"
        if record is not None and record.state.status != "destroyed":
            if record.state.status == "failed":
                raise InjectedRangeFailure(message)
            failure = RangeFailureV1("injected-failure", message, target, True)
            require_range_transition(record.state.status, "failed", "adapter")
            failed = replace(
                record.state, status="failed", revision=record.state.revision + 1,
                failure=failure, terminal_reason=None, updated_at=self._now,
            )
            record.state = failed
            record.history.append(failed)
        raise InjectedRangeFailure(message)

    def _handles(
        self, record: _RangeRecord, generation: int,
    ) -> tuple[ProviderHandleV1, tuple[NodeHandleV1, ...]]:
        root = canonical_sha256({
            "range_id": record.spec.range_id,
            "spec_sha256": record.spec.digest,
            "generation": generation,
        })
        range_handle = ProviderHandleV1("range", f"fake-range:{root[:32]}")
        nodes = tuple(NodeHandleV1(
            node.node_id,
            ProviderHandleV1("node", f"fake-node:{canonical_sha256({'root': root, 'node': node.node_id})[:32]}"),
        ) for node in record.template.nodes)
        return range_handle, nodes

    def _record(self, range_id: str) -> _RangeRecord:
        _identifier(range_id, "range_id")
        try:
            return self._ranges[range_id]
        except KeyError as exc:
            raise RangeAccessError("unknown range identity") from exc

    def _operation_replay(self, operation_id: str, request: Any, decoder):
        _identifier(operation_id, "operation_id")
        digest = canonical_sha256(request)
        prior = self._operations.get(operation_id)
        if prior is None:
            return None
        if prior["request_sha256"] != digest:
            raise RangeConflictError("operation ID is already bound to different content")
        if prior["error"] is not None:
            error = prior["error"]
            raise _ERRORS[error["type"]](error["message"])
        return decoder(prior["result"])

    def _operation_success(self, operation_id: str, request: Any, result: RangeContract) -> None:
        self._operations[operation_id] = {
            "request_sha256": canonical_sha256(request),
            "result": result.to_dict(),
            "error": None,
        }

    def _operation_error(self, operation_id: str, request: Any, error: RangeError) -> None:
        self._operations[operation_id] = {
            "request_sha256": canonical_sha256(request),
            "result": None,
            "error": {"type": type(error).__name__, "message": str(error)},
        }

    def checkpoint(self) -> str:
        value = {
            "schema_version": SCHEMA_VERSION,
            "now": self._now,
            "fail_next": sorted(self._fail_next),
            "operations": {key: self._operations[key] for key in sorted(self._operations)},
            "ranges": {
                key: {
                    "template": record.template.to_dict(),
                    "spec": record.spec.to_dict(),
                    "state": record.state.to_dict(),
                    "history": [item.to_dict() for item in record.history],
                    "scratch": {item: record.scratch[item].to_dict() for item in sorted(record.scratch)},
                    "outputs": {item: record.outputs[item].to_dict() for item in sorted(record.outputs)},
                    "evidence": {item: record.evidence[item].to_dict() for item in sorted(record.evidence)},
                    "work_count": record.work_count,
                }
                for key, record in sorted(self._ranges.items())
            },
        }
        return canonical_json(value)

    @classmethod
    def from_checkpoint(cls, raw: str | bytes) -> Self:
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("range checkpoint must be valid UTF-8 JSON") from exc
        value = _strict(
            value, "range checkpoint",
            {"schema_version", "now", "fail_next", "operations", "ranges"},
        )
        _version(value["schema_version"])
        adapter = cls(now=value["now"])
        adapter._fail_next = set(_array(value["fail_next"], "checkpoint fail_next"))
        if not adapter._fail_next <= RANGE_STATUSES:
            raise ValueError("checkpoint contains an unknown injected transition")
        if not isinstance(value["operations"], dict) or not isinstance(value["ranges"], dict):
            raise ValueError("checkpoint maps are invalid")
        adapter._operations = {}
        for operation_id, item in value["operations"].items():
            _identifier(operation_id, "checkpoint operation ID")
            item = _strict(item, "checkpoint operation", {"request_sha256", "result", "error"})
            _digest(item["request_sha256"], "checkpoint request digest")
            if item["error"] is not None:
                if item["result"] is not None:
                    raise ValueError("checkpoint failed operation also carries a result")
                error = _strict(item["error"], "checkpoint error", {"type", "message"})
                if error["type"] not in _ERRORS:
                    raise ValueError("checkpoint error type is unsupported")
                _text(error["message"], "checkpoint error message", 512)
            else:
                _validate_checkpoint_result(item["result"])
            adapter._operations[operation_id] = item
        for range_id, item in value["ranges"].items():
            _identifier(range_id, "checkpoint range ID")
            item = _strict(item, "checkpoint range", {
                "template", "spec", "state", "history", "scratch", "outputs", "evidence",
                "work_count",
            })
            template = RangeTemplateV1.from_dict(item["template"])
            spec = RangeSpecV1.from_dict(item["spec"])
            state = RangeLeaseStateV1.from_dict(item["state"])
            history = [
                RangeLeaseStateV1.from_dict(entry)
                for entry in _array(item["history"], "checkpoint history")
            ]
            if range_id != spec.range_id or state.range_id != range_id or state.spec_sha256 != spec.digest:
                raise ValueError("checkpoint range identity is inconsistent")
            if template.digest != spec.template_sha256:
                raise ValueError("checkpoint template identity is inconsistent")
            if not history or history[-1] != state:
                raise ValueError("checkpoint state does not match transition history")
            _validate_history(history, range_id, spec.digest)
            for name in ("scratch", "outputs", "evidence"):
                if not isinstance(item[name], dict):
                    raise ValueError(f"checkpoint {name} must be an object")
            scratch = {
                key: RangeScratchV1.from_dict(entry) for key, entry in item["scratch"].items()
            }
            outputs = {
                key: RangeOutputV1.from_dict(entry) for key, entry in item["outputs"].items()
            }
            evidence = {
                key: RangeOutputV1.from_dict(entry) for key, entry in item["evidence"].items()
            }
            if any(key != output.output_id or output.range_id != range_id
                   for key, output in {**outputs, **evidence}.items()):
                raise ValueError("checkpoint output identity is inconsistent")
            if any(key != value.scratch_id or value.range_id != range_id
                   for key, value in scratch.items()):
                raise ValueError("checkpoint scratch identity is inconsistent")
            if not set(outputs) <= set(evidence):
                raise ValueError("checkpoint current outputs are absent from evidence history")
            if state.status in {"ready", "in-use", "resetting"} and (
                state.range_handle is None
                or {item.node_id for item in state.node_handles}
                != {item.node_id for item in template.nodes}
            ):
                raise ValueError("checkpoint ready lease handles are incomplete")
            if state.status in {"destroyed", "expired"} and (scratch or outputs):
                raise ValueError("checkpoint closed range retains lease-local data")
            work_count = _integer(item["work_count"], "checkpoint work_count", 0, 1_000_000)
            if work_count != len(evidence):
                raise ValueError("checkpoint work count does not match evidence history")
            adapter._ranges[range_id] = _RangeRecord(
                template, spec, state, history, scratch, outputs, evidence, work_count,
            )
        # Round-tripping through the canonical encoder also rejects unserializable values.
        canonical_json(value)
        return adapter


def benign_two_node_fixture(
    *, range_id: str = "range-benign-two-node",
    requested_at: str = "2026-07-13T12:00:00Z",
) -> tuple[RangeTemplateV1, RangeSpecV1]:
    """Return the inert offline fixture used by the v1 conformance tests."""
    template = RangeTemplateV1(
        SCHEMA_VERSION, "benign-two-node", "v1",
        (
            RangeNodeV1(
                "analyzer", "linux", "x86_64", "a" * 64,
                ("static-inspection",), (),
            ),
            RangeNodeV1(
                "helper", "linux", "x86_64", "b" * 64,
                ("artifact-index",),
                (RangeServiceV1("artifact-index", "tcp", 8443),),
            ),
        ),
        (RangeLinkV1("analyzer", "helper", ("artifact-index",)),),
    )
    expires_at = _format_time(_time(_timestamp(requested_at, "requested_at")) + timedelta(hours=1))
    spec = RangeSpecV1(
        SCHEMA_VERSION, range_id, template.digest,
        (ImmutableInputV1(
            "sample", "c" * 64, 12, "application/octet-stream", "input/sample.bin",
        ),),
        RangeScopeV1(
            "scope-benign", 1, ("mount_input",), (), (), ("sample",),
        ),
        RangeNetworkV1("isolated", ()),
        RangeResourcesV1(2, 2, 2048, 1_000_000, 1_000_000, 8),
        RangeLifecyclePolicyV1(3600, "recreate-scratch", "explicit-or-expiry"),
        requested_at, expires_at,
    )
    return template, spec
