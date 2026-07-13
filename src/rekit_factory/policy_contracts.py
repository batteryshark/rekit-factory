"""Strict, immutable contracts for durable safety policy and strategy metadata.

These value objects deliberately have no controller, API, or persistence behavior.
They define the canonical documents those layers can bind to in a later integration.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Literal, Mapping

from .strategies import RunCeilings


POLICY_SCHEMA_VERSION = 1
STRATEGY_METADATA_SCHEMA_VERSION = 1
_DIGEST = re.compile(r"[0-9a-f]{64}")
_POLICY_ID = re.compile(r"safety-policy-v1-[0-9a-f]{64}")


def _strict_text(value: object, label: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty, trimmed string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} must not contain control characters")
    if len(value) > max_length:
        raise ValueError(f"{label} must not exceed {max_length} characters")
    return value


def _strict_int(value: object, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer of at least {minimum}")
    return value


def _strict_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be an array")
    result = tuple(_strict_text(item, f"{label} item") for item in value)
    if result != tuple(sorted(set(result))):
        raise ValueError(f"{label} must be sorted and contain no duplicates")
    return result


def _object(value: object, label: str, fields: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} field names must be strings")
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown:
        raise ValueError(f"{label} has unknown fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"{label} is missing fields: {sorted(missing)}")
    return value


def _ceilings_from_dict(value: object) -> RunCeilings:
    fields = {"concurrency", "retries_per_worker", "cost_units", "max_workers"}
    raw = _object(value, "ceilings", fields)
    return RunCeilings(
        concurrency=_strict_int(raw["concurrency"], "concurrency", minimum=1),
        retries_per_worker=_strict_int(
            raw["retries_per_worker"], "retries_per_worker", minimum=0
        ),
        cost_units=_strict_int(raw["cost_units"], "cost_units", minimum=1),
        max_workers=_strict_int(raw["max_workers"], "max_workers", minimum=1),
    )


def _ceilings_dict(value: RunCeilings) -> dict[str, int]:
    return {
        "concurrency": value.concurrency,
        "retries_per_worker": value.retries_per_worker,
        "cost_units": value.cost_units,
        "max_workers": value.max_workers,
    }


def _validate_ceilings(value: object) -> RunCeilings:
    if not isinstance(value, RunCeilings):
        raise ValueError("ceilings must be a RunCeilings contract")
    for name, minimum in (
        ("concurrency", 1), ("retries_per_worker", 0),
        ("cost_units", 1), ("max_workers", 1),
    ):
        _strict_int(getattr(value, name), name, minimum=minimum)
    return value


@dataclass(frozen=True)
class ScopePolicyBinding:
    """Exact authorized-scope revision, or an explicit absence of scope authority."""

    mode: Literal["unbound", "authorized-scope"]
    scope_id: str | None
    revision: int | None
    content_digest: str | None

    def __post_init__(self) -> None:
        if self.mode == "unbound":
            if any(value is not None for value in (
                self.scope_id, self.revision, self.content_digest
            )):
                raise ValueError("unbound scope must not contain binding fields")
            return
        if self.mode != "authorized-scope":
            raise ValueError("unsupported scope binding mode")
        _strict_text(self.scope_id, "scope_id")
        _strict_int(self.revision, "scope revision", minimum=1)
        if not isinstance(self.content_digest, str) or not _DIGEST.fullmatch(
            self.content_digest
        ):
            raise ValueError("scope content_digest must be 64 lowercase hex characters")

    @classmethod
    def unbound(cls) -> "ScopePolicyBinding":
        return cls("unbound", None, None, None)

    @classmethod
    def from_dict(cls, value: object) -> "ScopePolicyBinding":
        raw = _object(
            value, "scope_binding", {"mode", "scope_id", "revision", "content_digest"}
        )
        return cls(raw["mode"], raw["scope_id"], raw["revision"], raw["content_digest"])

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "scope_id": self.scope_id,
            "revision": self.revision,
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True)
class NamedSafetyPolicy:
    """A resolved authority document whose ID binds every permission-bearing field."""

    name: str
    revision: int
    allowed_tool_ids: tuple[str, ...]
    approval_mode: Literal["deny-all", "automatic-only", "operator-gated"]
    approval_required_tool_ids: tuple[str, ...]
    ceilings: RunCeilings
    scope_binding: ScopePolicyBinding
    compatibility: Literal["native-v1", "legacy-deny-all-v1"] = "native-v1"
    schema_version: int = POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _strict_text(self.name, "policy name")
        _strict_int(self.revision, "policy revision", minimum=1)
        if self.schema_version != POLICY_SCHEMA_VERSION:
            raise ValueError("unsupported safety-policy schema_version")
        object.__setattr__(
            self, "allowed_tool_ids", _strict_tuple(self.allowed_tool_ids, "allowed_tool_ids")
        )
        object.__setattr__(
            self, "approval_required_tool_ids",
            _strict_tuple(self.approval_required_tool_ids, "approval_required_tool_ids"),
        )
        _validate_ceilings(self.ceilings)
        if not isinstance(self.scope_binding, ScopePolicyBinding):
            raise ValueError("scope_binding must be a ScopePolicyBinding contract")
        if self.approval_mode not in {"deny-all", "automatic-only", "operator-gated"}:
            raise ValueError("unsupported approval_mode")
        if not set(self.approval_required_tool_ids).issubset(self.allowed_tool_ids):
            raise ValueError("approval-required tools must be allowed by the policy")
        if self.approval_mode != "operator-gated" and self.approval_required_tool_ids:
            raise ValueError("only operator-gated policies may require approvals")
        if self.approval_mode == "operator-gated" and not self.approval_required_tool_ids:
            raise ValueError("operator-gated policy must name approval-required tools")
        if self.approval_mode == "deny-all" and self.allowed_tool_ids:
            raise ValueError("deny-all policy must not allow tools")
        if self.compatibility not in {"native-v1", "legacy-deny-all-v1"}:
            raise ValueError("unsupported compatibility mode")
        if self.compatibility == "legacy-deny-all-v1" and (
            self.approval_mode != "deny-all"
            or self.allowed_tool_ids
            or self.scope_binding.mode != "unbound"
        ):
            raise ValueError("legacy compatibility policy must carry no tool or scope authority")

    @property
    def policy_id(self) -> str:
        canonical = json.dumps(
            self.to_dict(), allow_nan=False, ensure_ascii=False,
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        return f"safety-policy-v1-{hashlib.sha256(canonical).hexdigest()}"

    @classmethod
    def legacy_compatibility(
        cls, *, ceilings: RunCeilings, name: str = "legacy-compatibility", revision: int = 1
    ) -> "NamedSafetyPolicy":
        """Represent a pre-policy run without inferring any executable permission."""

        return cls(
            name=name, revision=revision, allowed_tool_ids=(), approval_mode="deny-all",
            approval_required_tool_ids=(), ceilings=ceilings,
            scope_binding=ScopePolicyBinding.unbound(), compatibility="legacy-deny-all-v1",
        )

    @classmethod
    def from_dict(cls, value: object) -> "NamedSafetyPolicy":
        fields = {
            "schema_version", "name", "revision", "allowed_tool_ids", "approval_mode",
            "approval_required_tool_ids", "ceilings", "scope_binding", "compatibility",
        }
        raw = _object(value, "safety policy", fields)
        return cls(
            schema_version=_strict_int(raw["schema_version"], "schema_version", minimum=1),
            name=raw["name"],
            revision=_strict_int(raw["revision"], "policy revision", minimum=1),
            allowed_tool_ids=_strict_tuple(raw["allowed_tool_ids"], "allowed_tool_ids"),
            approval_mode=raw["approval_mode"],
            approval_required_tool_ids=_strict_tuple(
                raw["approval_required_tool_ids"], "approval_required_tool_ids"
            ),
            ceilings=_ceilings_from_dict(raw["ceilings"]),
            scope_binding=ScopePolicyBinding.from_dict(raw["scope_binding"]),
            compatibility=raw["compatibility"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "revision": self.revision,
            "allowed_tool_ids": list(self.allowed_tool_ids),
            "approval_mode": self.approval_mode,
            "approval_required_tool_ids": list(self.approval_required_tool_ids),
            "ceilings": _ceilings_dict(self.ceilings),
            "scope_binding": self.scope_binding.to_dict(),
            "compatibility": self.compatibility,
        }


@dataclass(frozen=True)
class StrategyRoleMetadata:
    role: str
    objective: str
    depends_on_roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _strict_text(self.role, "strategy role")
        _strict_text(self.objective, "strategy role objective", max_length=2048)
        object.__setattr__(
            self, "depends_on_roles", _strict_tuple(self.depends_on_roles, "depends_on_roles")
        )

    @classmethod
    def from_dict(cls, value: object) -> "StrategyRoleMetadata":
        raw = _object(value, "strategy role", {"role", "objective", "depends_on_roles"})
        return cls(
            raw["role"], raw["objective"],
            _strict_tuple(raw["depends_on_roles"], "depends_on_roles"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role, "objective": self.objective,
            "depends_on_roles": list(self.depends_on_roles),
        }


@dataclass(frozen=True)
class StrategyPolicyConstraints:
    """Compatibility claims only; this metadata never creates policy authority."""

    compatible_policy_ids: tuple[str, ...]
    required_tool_ids: tuple[str, ...] = ()
    requires_scope_binding: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "compatible_policy_ids",
            _strict_tuple(self.compatible_policy_ids, "compatible_policy_ids"),
        )
        if not self.compatible_policy_ids:
            raise ValueError("strategy must name at least one compatible policy identity")
        if any(not _POLICY_ID.fullmatch(item) for item in self.compatible_policy_ids):
            raise ValueError("compatible_policy_ids must contain safety-policy-v1 identities")
        object.__setattr__(
            self, "required_tool_ids", _strict_tuple(self.required_tool_ids, "required_tool_ids")
        )
        if not isinstance(self.requires_scope_binding, bool):
            raise ValueError("requires_scope_binding must be a boolean")

    @classmethod
    def from_dict(cls, value: object) -> "StrategyPolicyConstraints":
        raw = _object(
            value, "policy_constraints",
            {"compatible_policy_ids", "required_tool_ids", "requires_scope_binding"},
        )
        return cls(
            _strict_tuple(raw["compatible_policy_ids"], "compatible_policy_ids"),
            _strict_tuple(raw["required_tool_ids"], "required_tool_ids"),
            raw["requires_scope_binding"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "compatible_policy_ids": list(self.compatible_policy_ids),
            "required_tool_ids": list(self.required_tool_ids),
            "requires_scope_binding": self.requires_scope_binding,
        }


@dataclass(frozen=True)
class StrategyMetadata:
    name: str
    description: str
    roles: tuple[StrategyRoleMetadata, ...]
    default_ceilings: RunCeilings
    compatible_profile_names: tuple[str, ...]
    policy_constraints: StrategyPolicyConstraints
    schema_version: int = STRATEGY_METADATA_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _strict_text(self.name, "strategy name")
        _strict_text(self.description, "strategy description", max_length=2048)
        if self.schema_version != STRATEGY_METADATA_SCHEMA_VERSION:
            raise ValueError("unsupported strategy-metadata schema_version")
        if not isinstance(self.roles, (list, tuple)):
            raise ValueError("roles must be an array")
        object.__setattr__(self, "roles", tuple(self.roles))
        if not self.roles:
            raise ValueError("strategy metadata must contain at least one role")
        if any(not isinstance(role, StrategyRoleMetadata) for role in self.roles):
            raise ValueError("roles must contain StrategyRoleMetadata contracts")
        _validate_ceilings(self.default_ceilings)
        if not isinstance(self.policy_constraints, StrategyPolicyConstraints):
            raise ValueError("policy_constraints must be a StrategyPolicyConstraints contract")
        names = tuple(role.role for role in self.roles)
        if len(names) != len(set(names)):
            raise ValueError("strategy role names must be unique")
        known = set(names)
        for role in self.roles:
            unknown = set(role.depends_on_roles) - known
            if unknown:
                raise ValueError(f"unknown dependency roles: {sorted(unknown)}")
            if role.role in role.depends_on_roles:
                raise ValueError("a strategy role cannot depend on itself")
        _assert_acyclic(self.roles)
        if len(self.roles) > self.default_ceilings.max_workers:
            raise ValueError("strategy roles exceed default max_workers")
        object.__setattr__(
            self, "compatible_profile_names",
            _strict_tuple(self.compatible_profile_names, "compatible_profile_names"),
        )
        if not self.compatible_profile_names:
            raise ValueError("strategy must name at least one compatible profile")

    @property
    def strategy_id(self) -> str:
        canonical = json.dumps(
            self.to_dict(), allow_nan=False, ensure_ascii=False,
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        return f"strategy-metadata-v1-{hashlib.sha256(canonical).hexdigest()}"

    @classmethod
    def from_dict(cls, value: object) -> "StrategyMetadata":
        fields = {
            "schema_version", "name", "description", "roles", "default_ceilings",
            "compatible_profile_names", "policy_constraints",
        }
        raw = _object(value, "strategy metadata", fields)
        if not isinstance(raw["roles"], (list, tuple)):
            raise ValueError("roles must be an array")
        return cls(
            schema_version=_strict_int(raw["schema_version"], "schema_version", minimum=1),
            name=raw["name"], description=raw["description"],
            roles=tuple(StrategyRoleMetadata.from_dict(item) for item in raw["roles"]),
            default_ceilings=_ceilings_from_dict(raw["default_ceilings"]),
            compatible_profile_names=_strict_tuple(
                raw["compatible_profile_names"], "compatible_profile_names"
            ),
            policy_constraints=StrategyPolicyConstraints.from_dict(raw["policy_constraints"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "description": self.description,
            "roles": [role.to_dict() for role in self.roles],
            "default_ceilings": _ceilings_dict(self.default_ceilings),
            "compatible_profile_names": list(self.compatible_profile_names),
            "policy_constraints": self.policy_constraints.to_dict(),
        }


def _assert_acyclic(roles: tuple[StrategyRoleMetadata, ...]) -> None:
    dependencies = {role.role: set(role.depends_on_roles) for role in roles}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(role: str) -> None:
        if role in visiting:
            raise ValueError("strategy role graph must be acyclic")
        if role in visited:
            return
        visiting.add(role)
        for dependency in dependencies[role]:
            visit(dependency)
        visiting.remove(role)
        visited.add(role)

    for role in dependencies:
        visit(role)
