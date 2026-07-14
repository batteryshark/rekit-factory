"""Content-addressed provider-neutral metadata for range proof and comparisons."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Self

from rekit_factory.range_attachments import RangeAttachmentAuditV1
from rekit_factory.ranges import (
    RangeContract, RangeLeaseStateV1, RangeSpecV1, RangeTemplateV1, canonical_sha256,
)


SCHEMA_VERSION = 1
MAX_TOOLS_PER_NODE = 128
MAX_ATTACHMENTS = 32
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _id(value: Any, name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None \
            or value.lower().startswith("credential:"):
        raise ValueError(f"{name} must be a bounded stable identifier")
    return value


def _digest(value: Any, name: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True)
class RangeToolIdentityV1(RangeContract):
    tool_id: str
    version: str
    artifact_sha256: str

    def __post_init__(self) -> None:
        _id(self.tool_id, "tool_id")
        _id(self.version, "tool version")
        _digest(self.artifact_sha256, "tool artifact_sha256")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        if type(value) is not dict or set(value) != {"tool_id", "version", "artifact_sha256"}:
            raise ValueError("RangeToolIdentityV1 has an invalid schema")
        return cls(**value)


@dataclass(frozen=True)
class RangeNodeRuntimeV1(RangeContract):
    node_id: str
    image_sha256: str
    environment_sha256: str
    tools: tuple[RangeToolIdentityV1, ...]

    def __post_init__(self) -> None:
        _id(self.node_id, "node_id")
        _digest(self.image_sha256, "image_sha256")
        _digest(self.environment_sha256, "environment_sha256")
        tools = tuple(sorted(self.tools, key=lambda item: item.tool_id))
        if len(tools) > MAX_TOOLS_PER_NODE or any(
                type(item) is not RangeToolIdentityV1 for item in tools) \
                or len({item.tool_id for item in tools}) != len(tools):
            raise ValueError("node tools must be bounded unique typed identities")
        object.__setattr__(self, "tools", tools)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = {"node_id", "image_sha256", "environment_sha256", "tools"}
        if type(value) is not dict or set(value) != fields or type(value["tools"]) is not list:
            raise ValueError("RangeNodeRuntimeV1 has an invalid schema")
        return cls(
            value["node_id"], value["image_sha256"], value["environment_sha256"],
            tuple(RangeToolIdentityV1.from_dict(item) for item in value["tools"]),
        )


@dataclass(frozen=True)
class RangeExecutionIdentityV1(RangeContract):
    schema_version: int
    adapter_id: str
    adapter_version: str
    template_sha256: str
    topology_sha256: str
    spec_sha256: str
    scope_sha256: str
    generation: int
    nodes: tuple[RangeNodeRuntimeV1, ...]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("schema_version must be 1")
        _id(self.adapter_id, "adapter_id")
        _id(self.adapter_version, "adapter_version")
        for name in ("template_sha256", "topology_sha256", "spec_sha256", "scope_sha256"):
            _digest(getattr(self, name), name)
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("generation must be positive")
        nodes = tuple(sorted(self.nodes, key=lambda item: item.node_id))
        if not nodes or any(type(item) is not RangeNodeRuntimeV1 for item in nodes) \
                or len({item.node_id for item in nodes}) != len(nodes):
            raise ValueError("execution nodes must be non-empty unique typed identities")
        object.__setattr__(self, "nodes", nodes)


def bind_range_execution_identity(
    template: RangeTemplateV1,
    spec: RangeSpecV1,
    lease: RangeLeaseStateV1,
    *,
    adapter_id: str,
    adapter_version: str,
    nodes: tuple[RangeNodeRuntimeV1, ...],
) -> RangeExecutionIdentityV1:
    """Bind provider-reported runtime facts to one exact committed lease generation."""
    if template.digest != spec.template_sha256 or lease.spec_sha256 != spec.digest \
            or lease.range_id != spec.range_id or lease.status not in {"ready", "in-use"}:
        raise ValueError("runtime identity does not bind an active exact lease")
    expected = {item.node_id: item.image_sha256 for item in template.nodes}
    supplied = {item.node_id: item.image_sha256 for item in nodes}
    if supplied != expected:
        raise ValueError("runtime nodes or image identities conflict with the template")
    topology = {
        "templateSha256": template.digest,
        "nodes": [item.node_id for item in template.nodes],
        "links": [item.to_dict() for item in template.links],
    }
    return RangeExecutionIdentityV1(
        SCHEMA_VERSION, adapter_id, adapter_version, template.digest,
        canonical_sha256(topology), spec.digest, spec.scope.digest, lease.generation, nodes,
    )


def range_proof_metadata(identity: RangeExecutionIdentityV1) -> dict[str, Any]:
    """Return the exact bounded metadata block embedded by a future proof producer."""
    if type(identity) is not RangeExecutionIdentityV1:
        raise TypeError("proof metadata requires an exact range execution identity")
    return {
        "schemaVersion": 1,
        "rangeExecutionSha256": identity.digest,
        "adapter": {"id": identity.adapter_id, "version": identity.adapter_version},
        "templateSha256": identity.template_sha256,
        "topologySha256": identity.topology_sha256,
        "specSha256": identity.spec_sha256,
        "scopeSha256": identity.scope_sha256,
        "generation": identity.generation,
        "nodes": [{
            "nodeId": node.node_id,
            "imageSha256": node.image_sha256,
            "environmentSha256": node.environment_sha256,
            "tools": [{"toolId": tool.tool_id, "version": tool.version,
                       "artifactSha256": tool.artifact_sha256} for tool in node.tools],
        } for node in identity.nodes],
    }


def range_benchmark_comparison_key(identity: RangeExecutionIdentityV1) -> str:
    """Return the stable comparison-key component shared by equivalent trials."""
    metadata = range_proof_metadata(identity)
    return "sha256:" + canonical_sha256({
        "schemaVersion": 1,
        "rangeExecutionSha256": metadata["rangeExecutionSha256"],
    })


def project_range_health(
    template: RangeTemplateV1,
    spec: RangeSpecV1,
    lease: RangeLeaseStateV1,
    *,
    identity: RangeExecutionIdentityV1 | None = None,
    attachments: tuple[RangeAttachmentAuditV1, ...] = (),
) -> dict[str, Any]:
    """Project a bounded redacted Mission Control record from canonical contracts."""
    if template.digest != spec.template_sha256 or lease.range_id != spec.range_id \
            or lease.spec_sha256 != spec.digest:
        raise ValueError("range health inputs do not share one exact identity")
    if identity is not None and (
            type(identity) is not RangeExecutionIdentityV1
            or identity.template_sha256 != template.digest
            or identity.spec_sha256 != spec.digest
            or identity.scope_sha256 != spec.scope.digest
            or identity.generation != lease.generation):
        raise ValueError("range execution identity conflicts with lease health")
    if len(attachments) > MAX_ATTACHMENTS or any(
            type(item) is not RangeAttachmentAuditV1 for item in attachments):
        raise ValueError("attachment audit projection is invalid or unbounded")
    ordered = tuple(sorted(attachments, key=lambda item: (item.created_at, item.audit_id)))
    if any(item.range_id != spec.range_id or item.generation != lease.generation
           for item in ordered):
        raise ValueError("attachment audit belongs to another lease generation")
    runtime = {item.node_id: item for item in identity.nodes} if identity else {}
    return {
        "schemaVersion": 1,
        "rangeId": spec.range_id,
        "status": lease.status,
        "revision": lease.revision,
        "generation": lease.generation,
        "updatedAt": lease.updated_at,
        "expiresAt": spec.expires_at,
        "specSha256": spec.digest,
        "scopeSha256": spec.scope.digest,
        "templateSha256": template.digest,
        "topologySha256": identity.topology_sha256 if identity else None,
        "executionSha256": identity.digest if identity else None,
        "benchmarkComparisonKey": range_benchmark_comparison_key(identity) if identity else None,
        "nodes": [{
            "nodeId": node.node_id,
            "platform": node.platform,
            "architecture": node.architecture,
            "imageSha256": node.image_sha256,
            "environmentSha256": runtime[node.node_id].environment_sha256
                if node.node_id in runtime else None,
            "toolCount": len(runtime[node.node_id].tools) if node.node_id in runtime else 0,
        } for node in template.nodes],
        "attachments": [{
            "auditId": item.audit_id,
            "nodeId": item.node_id,
            "action": item.action,
            "requestedBy": item.requested_by,
            "disposition": item.disposition,
            "reasonCode": item.reason_code,
            "createdAt": item.created_at,
            "expiresAt": item.expires_at,
        } for item in ordered],
        "failure": None if lease.failure is None else {
            "code": lease.failure.code,
            "transition": lease.failure.transition,
            "retryable": lease.failure.retryable,
        },
    }
