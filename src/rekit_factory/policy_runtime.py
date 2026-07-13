"""Controller-owned resolution and enforcement for durable safety policies."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .policy_contracts import NamedSafetyPolicy, ScopePolicyBinding
from .strategies import RunCeilings


DEFAULT_POLICY_CEILINGS = RunCeilings(
    concurrency=4, retries_per_worker=3, cost_units=100, max_workers=8,
)


@dataclass(frozen=True)
class SafetyPolicyCatalog:
    """An immutable ID-addressed set of controller-configured policies."""

    policies: tuple[NamedSafetyPolicy, ...]
    default_policy_id: str

    def __post_init__(self) -> None:
        if not self.policies:
            raise ValueError("at least one safety policy is required")
        ids = tuple(policy.policy_id for policy in self.policies)
        if len(ids) != len(set(ids)):
            raise ValueError("safety policy identities must be unique")
        if self.default_policy_id not in ids:
            raise ValueError("default safety policy identity is not configured")

    def resolve(self, policy_id: str | None) -> NamedSafetyPolicy:
        selected = policy_id or self.default_policy_id
        for policy in self.policies:
            if policy.policy_id == selected:
                return policy
        raise ValueError("unknown or stale safety policy identity")

    def public_dicts(self) -> list[dict[str, object]]:
        return [policy_record(policy) for policy in self.policies]


def builtin_policy_catalog(
    manifests: Iterable[Any], *, ceilings: RunCeilings = DEFAULT_POLICY_CEILINGS,
) -> SafetyPolicyCatalog:
    """Build deterministic supervised and automatic-only policies from Rekit."""

    by_id: dict[str, Any] = {}
    for manifest in manifests:
        tool_id = getattr(manifest, "id", None)
        if not isinstance(tool_id, str) or not tool_id:
            raise ValueError("Rekit manifest has no valid tool identity")
        if tool_id in by_id:
            raise ValueError(f"duplicate Rekit tool identity {tool_id!r}")
        by_id[tool_id] = manifest
    allowed = tuple(sorted(by_id))
    gated = tuple(sorted(
        tool_id for tool_id, manifest in by_id.items()
        if bool(getattr(manifest, "requires_permission", False))
    ))
    automatic = tuple(tool_id for tool_id in allowed if tool_id not in set(gated))
    supervised = NamedSafetyPolicy(
        name="supervised", revision=1, allowed_tool_ids=allowed,
        approval_mode="operator-gated" if gated else "automatic-only",
        approval_required_tool_ids=gated, ceilings=ceilings,
        scope_binding=ScopePolicyBinding.unbound(),
    )
    automatic_only = NamedSafetyPolicy(
        name="automatic-only", revision=1, allowed_tool_ids=automatic,
        approval_mode="automatic-only", approval_required_tool_ids=(),
        ceilings=ceilings, scope_binding=ScopePolicyBinding.unbound(),
    )
    return SafetyPolicyCatalog(
        policies=(supervised, automatic_only),
        default_policy_id=supervised.policy_id,
    )


def policy_record(policy: NamedSafetyPolicy) -> dict[str, object]:
    return {"policyId": policy.policy_id, "document": policy.to_dict()}


def policy_from_record(value: object) -> NamedSafetyPolicy:
    if not isinstance(value, Mapping) or set(value) != {"policyId", "document"}:
        raise ValueError("persisted safety policy record is malformed")
    policy_id = value["policyId"]
    if not isinstance(policy_id, str):
        raise ValueError("persisted safety policy identity is malformed")
    policy = NamedSafetyPolicy.from_dict(value["document"])
    if policy.policy_id != policy_id:
        raise ValueError("persisted safety policy identity does not match its document")
    return policy


def policy_from_meta(meta: Mapping[str, Any], ceilings: RunCeilings) -> NamedSafetyPolicy:
    value = meta.get("safetyPolicy")
    if value is None:
        return NamedSafetyPolicy.legacy_compatibility(ceilings=ceilings)
    return policy_from_record(value)


def validate_policy_authority(
    policy: NamedSafetyPolicy,
    *,
    requested_tool_ids: Iterable[str],
    manifests: Mapping[str, Any],
    ceilings: RunCeilings,
    scope: Any | None,
) -> None:
    """Fail closed unless policy covers the exact requested runtime authority."""

    requested = tuple(dict.fromkeys(requested_tool_ids))
    unknown = sorted(set(requested) - set(policy.allowed_tool_ids))
    if unknown:
        raise PermissionError(
            "selected safety policy does not allow requested tools: " + ", ".join(unknown)
        )
    for tool_id in requested:
        manifest = manifests.get(tool_id)
        if manifest is None:
            raise PermissionError(f"selected safety policy references unavailable tool {tool_id!r}")
        gated = bool(getattr(manifest, "requires_permission", False))
        requires_approval = tool_id in policy.approval_required_tool_ids
        if gated and (policy.approval_mode != "operator-gated" or not requires_approval):
            raise PermissionError(
                f"selected safety policy cannot authorize gated tool {tool_id!r}"
            )
        if not gated and requires_approval:
            raise PermissionError(
                f"selected safety policy approval contract changed for tool {tool_id!r}"
            )
    for name in ("concurrency", "retries_per_worker", "cost_units", "max_workers"):
        if getattr(ceilings, name) > getattr(policy.ceilings, name):
            raise PermissionError(f"run {name} exceeds selected safety policy ceiling")
    binding = policy.scope_binding
    if binding.mode == "authorized-scope":
        if scope is None:
            raise PermissionError("selected safety policy requires its bound engagement scope")
        envelope = scope.envelope
        if (
            envelope.scope_id != binding.scope_id
            or envelope.revision != binding.revision
            or envelope.content_digest != binding.content_digest
        ):
            raise PermissionError("selected safety policy scope binding does not match the run")
