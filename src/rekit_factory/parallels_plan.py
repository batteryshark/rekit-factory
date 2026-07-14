"""Pure fixed-argument Parallels lifecycle plans with no provider effects.

The records in this module bind a future Parallels adapter to exact identities and argv.
There is intentionally no subprocess import, command runner, output parser, retry loop, or
claim that a provider accepted an operation.  Constructing a plan never touches VM state.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, ClassVar, Literal, Self


SCHEMA_VERSION = 1
PRLCTL = "/Applications/Parallels Desktop.app/Contents/MacOS/prlctl"
MAX_ARGV = 10
MAX_ARG = 256
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_UUID = re.compile(r"^\{[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}$")
PlanKind = Literal[
    "clone", "start", "stop", "snapshot-create", "snapshot-switch", "delete",
]


def _canonical(value: Any) -> bytes:
    if isinstance(value, PlanContract):
        value = value.to_dict()
    return (json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"),
                       sort_keys=True) + "\n").encode("utf-8")


def _strict(value: Any, name: str, names: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != names:
        raise ValueError(f"{name} must contain exactly {sorted(names)}")
    return value


def _version(value: Any, name: str) -> int:
    if type(value) is not int or value != SCHEMA_VERSION:
        raise ValueError(f"{name} schema_version must be 1")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a bounded stable identifier")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _uuid(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase braced UUID")
    return value


class PlanContract:
    schema_version: ClassVar[int] = SCHEMA_VERSION

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical(self)).hexdigest()


@dataclass(frozen=True)
class ParallelsAdapterIdentityV1(PlanContract):
    adapter_version: str
    binary_sha256: str
    candidate_config_sha256: str
    source_vm_id: str
    source_snapshot_id: str
    base_image_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.adapter_version, "adapter_version")
        for name in ("binary_sha256", "candidate_config_sha256", "base_image_sha256"):
            _digest(getattr(self, name), name)
        _uuid(self.source_vm_id, "source_vm_id")
        _uuid(self.source_snapshot_id, "source_snapshot_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION, "adapter_version": self.adapter_version,
            "binary_sha256": self.binary_sha256,
            "candidate_config_sha256": self.candidate_config_sha256,
            "source_vm_id": self.source_vm_id,
            "source_snapshot_id": self.source_snapshot_id,
            "base_image_sha256": self.base_image_sha256,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        value = _strict(value, "adapter identity", {
            "schema_version", "adapter_version", "binary_sha256",
            "candidate_config_sha256", "source_vm_id", "source_snapshot_id",
            "base_image_sha256",
        })
        _version(value["schema_version"], "adapter identity")
        return cls(**{key: item for key, item in value.items() if key != "schema_version"})


@dataclass(frozen=True)
class ParallelsVmLifecycleIdentityV1(PlanContract):
    range_id: str
    node_id: str
    generation: int
    spec_sha256: str
    scope_sha256: str
    adapter_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.range_id, "range_id")
        _identifier(self.node_id, "node_id")
        if type(self.generation) is not int or not 1 <= self.generation <= 2**63 - 1:
            raise ValueError("generation must be a positive 64-bit integer")
        for name in ("spec_sha256", "scope_sha256", "adapter_sha256"):
            _digest(getattr(self, name), name)

    @property
    def logical_vm_id(self) -> str:
        return "prl-vm:" + self.digest[:32]

    @property
    def clone_name(self) -> str:
        return "rekit-" + self.digest[:24]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION, "range_id": self.range_id,
            "node_id": self.node_id, "generation": self.generation,
            "spec_sha256": self.spec_sha256, "scope_sha256": self.scope_sha256,
            "adapter_sha256": self.adapter_sha256,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        value = _strict(value, "VM lifecycle identity", {
            "schema_version", "range_id", "node_id", "generation", "spec_sha256",
            "scope_sha256", "adapter_sha256",
        })
        _version(value["schema_version"], "VM lifecycle identity")
        return cls(**{key: item for key, item in value.items() if key != "schema_version"})


@dataclass(frozen=True)
class ParallelsCommandPlanV1(PlanContract):
    operation_id: str
    kind: PlanKind
    adapter: ParallelsAdapterIdentityV1
    target: ParallelsVmLifecycleIdentityV1
    provider_vm_id: str | None
    snapshot_id: str | None
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "operation_id")
        if not isinstance(self.kind, str) or self.kind not in {
            "clone", "start", "stop", "snapshot-create", "snapshot-switch", "delete",
        }:
            raise ValueError("unsupported Parallels plan kind")
        if type(self.adapter) is not ParallelsAdapterIdentityV1 \
                or type(self.target) is not ParallelsVmLifecycleIdentityV1:
            raise ValueError("plan requires exact adapter and lifecycle identities")
        if self.target.adapter_sha256 != self.adapter.digest:
            raise ValueError("lifecycle identity does not bind the adapter")
        if self.provider_vm_id is not None:
            _uuid(self.provider_vm_id, "provider_vm_id")
        if self.snapshot_id is not None:
            _uuid(self.snapshot_id, "snapshot_id")
        expected = _plan_argv(
            self.operation_id, self.kind, self.adapter, self.target,
            self.provider_vm_id, self.snapshot_id,
        )
        if self.argv != expected:
            raise ValueError("argv is not the exact fixed plan for these identities")
        if len(self.argv) > MAX_ARGV or any(
            not isinstance(arg, str) or not arg or len(arg) > MAX_ARG
            or any(ord(char) < 32 or ord(char) == 127 for char in arg)
            for arg in self.argv
        ):
            raise ValueError("argv contains an invalid or unbounded argument")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION, "operation_id": self.operation_id,
            "kind": self.kind, "adapter": self.adapter.to_dict(),
            "target": self.target.to_dict(), "provider_vm_id": self.provider_vm_id,
            "snapshot_id": self.snapshot_id, "argv": list(self.argv),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        value = _strict(value, "Parallels command plan", {
            "schema_version", "operation_id", "kind", "adapter", "target",
            "provider_vm_id", "snapshot_id", "argv",
        })
        _version(value["schema_version"], "Parallels command plan")
        if not isinstance(value["argv"], list):
            raise ValueError("Parallels command plan schema is invalid")
        return cls(
            operation_id=value["operation_id"], kind=value["kind"],
            adapter=ParallelsAdapterIdentityV1.from_dict(value["adapter"]),
            target=ParallelsVmLifecycleIdentityV1.from_dict(value["target"]),
            provider_vm_id=value["provider_vm_id"], snapshot_id=value["snapshot_id"],
            argv=tuple(value["argv"]),
        )


def _plan_argv(
    operation_id: str,
    kind: PlanKind,
    adapter: ParallelsAdapterIdentityV1,
    target: ParallelsVmLifecycleIdentityV1,
    provider_vm_id: str | None,
    snapshot_id: str | None,
) -> tuple[str, ...]:
    if kind == "clone":
        if provider_vm_id is not None or snapshot_id is not None:
            raise ValueError("clone plans cannot claim provider-created identities")
        return (
            PRLCTL, "clone", adapter.source_vm_id, "--name", target.clone_name,
            "--linked", "--id", adapter.source_snapshot_id,
        )
    if provider_vm_id is None:
        raise ValueError("post-clone plans require an exact provider VM UUID")
    if kind == "start":
        if snapshot_id is not None:
            raise ValueError("start plans do not accept snapshot identity")
        return (PRLCTL, "start", provider_vm_id)
    if kind == "stop":
        if snapshot_id is not None:
            raise ValueError("stop plans do not accept snapshot identity")
        return (PRLCTL, "stop", provider_vm_id, "--acpi")
    if kind == "snapshot-create":
        if snapshot_id is not None:
            raise ValueError("snapshot creation cannot claim an unobserved snapshot UUID")
        return (
            PRLCTL, "snapshot", provider_vm_id, "--name",
            "reset-" + target.digest[:16], "--description", "operation:" + operation_id,
        )
    if kind == "snapshot-switch":
        if snapshot_id is None:
            raise ValueError("snapshot switch requires an exact observed snapshot UUID")
        return (PRLCTL, "snapshot-switch", provider_vm_id, "--id", snapshot_id, "--skip-resume")
    if kind == "delete":
        if snapshot_id is not None:
            raise ValueError("delete plans do not accept snapshot identity")
        return (PRLCTL, "delete", provider_vm_id)
    raise ValueError("unsupported Parallels plan kind")


def build_parallels_command_plan(
    operation_id: str,
    kind: PlanKind,
    adapter: ParallelsAdapterIdentityV1,
    target: ParallelsVmLifecycleIdentityV1,
    *,
    provider_vm_id: str | None = None,
    snapshot_id: str | None = None,
) -> ParallelsCommandPlanV1:
    """Build exact argv only; this function has no execution capability."""
    argv = _plan_argv(operation_id, kind, adapter, target, provider_vm_id, snapshot_id)
    return ParallelsCommandPlanV1(
        operation_id, kind, adapter, target, provider_vm_id, snapshot_id, argv,
    )
