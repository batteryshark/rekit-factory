"""Pure Parallels inventory reconciliation for exact command plans.

No inventory is collected here and no command can run.  Callers supply bounded, exact
observations.  The state machine distinguishes a safe initial execution from an observed
completed effect; after an effect boundary, an unchanged inventory is ``unknown`` rather
than permission to blindly repeat a provider command.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Literal, Self

from .parallels_plan import ParallelsCommandPlanV1


SCHEMA_VERSION = 1
MAX_VMS = 128
MAX_SNAPSHOTS_PER_VM = 256
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_UUID = re.compile(r"^\{[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}$")
VmState = Literal["stopped", "running", "transitional", "unknown"]
Decision = Literal["execute", "already-applied", "conflict", "unknown"]


def _canonical(value: Any) -> bytes:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    return (json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"),
                       sort_keys=True) + "\n").encode()


def _strict(value: Any, name: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} must contain exactly {sorted(fields)}")
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


def _bounded_text(
    value: Any, name: str, maximum: int = 256, *, allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > maximum \
            or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{name} must be bounded printable text")
    return value


class ReconciliationContract:
    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical(self)).hexdigest()


@dataclass(frozen=True)
class ParallelsSnapshotObservationV1(ReconciliationContract):
    snapshot_id: str
    name: str
    description: str

    def __post_init__(self) -> None:
        _uuid(self.snapshot_id, "snapshot_id")
        _bounded_text(self.name, "snapshot name")
        _bounded_text(self.description, "snapshot description", allow_empty=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION, "snapshot_id": self.snapshot_id,
            "name": self.name, "description": self.description,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        value = _strict(value, "snapshot observation", {
            "schema_version", "snapshot_id", "name", "description",
        })
        _version(value["schema_version"], "snapshot observation")
        return cls(value["snapshot_id"], value["name"], value["description"])


@dataclass(frozen=True)
class ParallelsVmObservationV1(ReconciliationContract):
    provider_vm_id: str
    name: str
    source_vm_id: str | None
    source_snapshot_id: str | None
    state: VmState
    current_snapshot_id: str | None
    snapshots: tuple[ParallelsSnapshotObservationV1, ...]

    def __post_init__(self) -> None:
        _uuid(self.provider_vm_id, "provider_vm_id")
        _bounded_text(self.name, "VM name")
        for name in ("source_vm_id", "source_snapshot_id", "current_snapshot_id"):
            value = getattr(self, name)
            if value is not None:
                _uuid(value, name)
        if not isinstance(self.state, str) or self.state not in {
            "stopped", "running", "transitional", "unknown",
        }:
            raise ValueError("VM state is unsupported")
        if not isinstance(self.snapshots, tuple) or len(self.snapshots) > MAX_SNAPSHOTS_PER_VM \
                or any(type(item) is not ParallelsSnapshotObservationV1 for item in self.snapshots):
            raise ValueError("VM snapshots must be a bounded exact tuple")
        if len({item.snapshot_id for item in self.snapshots}) != len(self.snapshots):
            raise ValueError("VM snapshot UUIDs must be unique")
        if self.current_snapshot_id is not None \
                and self.current_snapshot_id not in {item.snapshot_id for item in self.snapshots}:
            raise ValueError("current snapshot must occur in the exact snapshot inventory")
        object.__setattr__(self, "snapshots", tuple(sorted(
            self.snapshots, key=lambda item: item.snapshot_id,
        )))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION, "provider_vm_id": self.provider_vm_id,
            "name": self.name, "source_vm_id": self.source_vm_id,
            "source_snapshot_id": self.source_snapshot_id, "state": self.state,
            "current_snapshot_id": self.current_snapshot_id,
            "snapshots": [item.to_dict() for item in self.snapshots],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = {
            "schema_version", "provider_vm_id", "name", "source_vm_id",
            "source_snapshot_id", "state", "current_snapshot_id", "snapshots",
        }
        value = _strict(value, "VM observation", fields)
        _version(value["schema_version"], "VM observation")
        if not isinstance(value["snapshots"], list):
            raise ValueError("VM snapshots must be an array")
        return cls(
            value["provider_vm_id"], value["name"], value["source_vm_id"],
            value["source_snapshot_id"], value["state"], value["current_snapshot_id"],
            tuple(ParallelsSnapshotObservationV1.from_dict(item)
                  for item in value["snapshots"]),
        )


@dataclass(frozen=True)
class ParallelsInventoryObservationV1(ReconciliationContract):
    schema_version: int
    observation_id: str
    sequence: int
    adapter_sha256: str
    previous_observation_sha256: str | None
    vms: tuple[ParallelsVmObservationV1, ...]

    def __post_init__(self) -> None:
        _version(self.schema_version, "inventory observation")
        _identifier(self.observation_id, "observation_id")
        if type(self.sequence) is not int or not 1 <= self.sequence <= 2**63 - 1:
            raise ValueError("observation sequence must be a positive integer")
        _digest(self.adapter_sha256, "inventory adapter_sha256")
        if self.previous_observation_sha256 is not None:
            _digest(self.previous_observation_sha256, "previous_observation_sha256")
        if not isinstance(self.vms, tuple) or len(self.vms) > MAX_VMS \
                or any(type(item) is not ParallelsVmObservationV1 for item in self.vms):
            raise ValueError("inventory VMs must be a bounded exact tuple")
        if len({item.provider_vm_id for item in self.vms}) != len(self.vms):
            raise ValueError("inventory provider VM UUIDs must be unique")
        object.__setattr__(self, "vms", tuple(sorted(
            self.vms, key=lambda item: item.provider_vm_id,
        )))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "observation_id": self.observation_id,
            "sequence": self.sequence, "adapter_sha256": self.adapter_sha256,
            "previous_observation_sha256": self.previous_observation_sha256,
            "vms": [item.to_dict() for item in self.vms],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = {
            "schema_version", "observation_id", "sequence", "adapter_sha256",
            "previous_observation_sha256", "vms",
        }
        value = _strict(value, "inventory observation", fields)
        if not isinstance(value["vms"], list):
            raise ValueError("inventory VMs must be an array")
        return cls(
            value["schema_version"], value["observation_id"], value["sequence"],
            value["adapter_sha256"], value["previous_observation_sha256"],
            tuple(ParallelsVmObservationV1.from_dict(item) for item in value["vms"]),
        )


@dataclass(frozen=True)
class ParallelsReconciliationDecisionV1(ReconciliationContract):
    schema_version: int
    operation_id: str
    plan_sha256: str
    before_observation_sha256: str
    effective_observation_sha256: str
    decision: Decision
    reason_code: str
    provider_vm_id: str | None
    snapshot_id: str | None

    def __post_init__(self) -> None:
        _version(self.schema_version, "reconciliation decision")
        _identifier(self.operation_id, "decision operation_id")
        for name in ("plan_sha256", "before_observation_sha256", "effective_observation_sha256"):
            _digest(getattr(self, name), name)
        if not isinstance(self.decision, str) or self.decision not in {
            "execute", "already-applied", "conflict", "unknown",
        }:
            raise ValueError("reconciliation decision is unsupported")
        _identifier(self.reason_code, "reconciliation reason_code")
        if self.provider_vm_id is not None:
            _uuid(self.provider_vm_id, "decision provider_vm_id")
        if self.snapshot_id is not None:
            _uuid(self.snapshot_id, "decision snapshot_id")
        if self.decision != "already-applied" \
                and (self.provider_vm_id is not None or self.snapshot_id is not None):
            raise ValueError("only completed reconciliation may discover provider identities")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "operation_id": self.operation_id,
            "plan_sha256": self.plan_sha256,
            "before_observation_sha256": self.before_observation_sha256,
            "effective_observation_sha256": self.effective_observation_sha256,
            "decision": self.decision, "reason_code": self.reason_code,
            "provider_vm_id": self.provider_vm_id, "snapshot_id": self.snapshot_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = {
            "schema_version", "operation_id", "plan_sha256",
            "before_observation_sha256", "effective_observation_sha256",
            "decision", "reason_code", "provider_vm_id", "snapshot_id",
        }
        return cls(**_strict(value, "reconciliation decision", fields))

    def success_envelope(self, plan: ParallelsCommandPlanV1) -> bytes:
        if self.decision != "already-applied" or self.operation_id != plan.operation_id \
                or self.plan_sha256 != plan.digest:
            raise ValueError("only an exact completed reconciliation has a success envelope")
        return _canonical({
            "schema_version": SCHEMA_VERSION, "operation_id": plan.operation_id,
            "plan_sha256": plan.digest, "kind": plan.kind,
            "provider_vm_id": self.provider_vm_id, "snapshot_id": self.snapshot_id,
        })


def _raw_decision(
    plan: ParallelsCommandPlanV1, observation: ParallelsInventoryObservationV1,
) -> tuple[Decision, str, str | None, str | None]:
    vms = observation.vms
    if plan.kind == "clone":
        named = tuple(item for item in vms if item.name == plan.target.clone_name)
        exact = tuple(item for item in named if (
            item.source_vm_id == plan.adapter.source_vm_id
            and item.source_snapshot_id == plan.adapter.source_snapshot_id
        ))
        if len(exact) == 1 and len(named) == 1:
            return "already-applied", "clone-observed", exact[0].provider_vm_id, None
        if named:
            return "conflict", "clone-name-collision", None, None
        return "execute", "clone-absent", None, None

    target = next((item for item in vms if item.provider_vm_id == plan.provider_vm_id), None)
    if target is None:
        if plan.kind == "delete":
            return "already-applied", "delete-observed", None, None
        return "conflict", "provider-vm-missing", None, None
    if target.name != plan.target.clone_name \
            or target.source_vm_id != plan.adapter.source_vm_id \
            or target.source_snapshot_id != plan.adapter.source_snapshot_id:
        return "conflict", "provider-vm-identity-mismatch", None, None
    if target.state in {"transitional", "unknown"}:
        return "unknown", "provider-state-unstable", None, None
    if plan.kind == "start":
        return (("already-applied", "running-observed", None, None)
                if target.state == "running"
                else ("execute", "stopped-before-start", None, None))
    if plan.kind == "stop":
        return (("already-applied", "stopped-observed", None, None)
                if target.state == "stopped"
                else ("execute", "running-before-stop", None, None))
    if plan.kind == "delete":
        return "execute", "provider-vm-present", None, None
    if plan.kind == "snapshot-create":
        name = "reset-" + plan.target.digest[:16]
        description = "operation:" + plan.operation_id
        named = tuple(item for item in target.snapshots if item.name == name)
        exact = tuple(item for item in named if item.description == description)
        if len(exact) == 1 and len(named) == 1:
            return "already-applied", "snapshot-observed", None, exact[0].snapshot_id
        if named:
            return "conflict", "snapshot-name-collision", None, None
        return "execute", "snapshot-absent", None, None
    if plan.kind == "snapshot-switch":
        if target.current_snapshot_id == plan.snapshot_id:
            return "already-applied", "snapshot-current", None, None
        if plan.snapshot_id not in {item.snapshot_id for item in target.snapshots}:
            return "conflict", "snapshot-missing", None, None
        return "execute", "snapshot-not-current", None, None
    raise ValueError("unsupported Parallels plan kind")


def reconcile_parallels_plan(
    plan: ParallelsCommandPlanV1,
    before: ParallelsInventoryObservationV1,
    after: ParallelsInventoryObservationV1 | None = None,
) -> ParallelsReconciliationDecisionV1:
    """Reconcile an initial inventory or a chained post-effect observation."""
    if type(plan) is not ParallelsCommandPlanV1 \
            or type(before) is not ParallelsInventoryObservationV1 \
            or (after is not None and type(after) is not ParallelsInventoryObservationV1):
        raise ValueError("reconciliation requires exact plan and inventory contracts")
    if before.adapter_sha256 != plan.adapter.digest:
        raise ValueError("before inventory does not bind the plan adapter")
    effective = before
    if after is not None:
        before_decision = _raw_decision(plan, before)[0]
        if before_decision != "execute":
            raise ValueError("before inventory did not authorize this effect boundary")
        if after.adapter_sha256 != plan.adapter.digest \
                or after.sequence != before.sequence + 1 \
                or after.previous_observation_sha256 != before.digest \
                or after.observation_id == before.observation_id:
            raise ValueError("after inventory is not the exact chained observation")
        effective = after
    decision, reason, provider_vm_id, snapshot_id = _raw_decision(plan, effective)
    if after is not None and decision == "execute":
        decision, reason = "unknown", "effect-not-observed"
    return ParallelsReconciliationDecisionV1(
        SCHEMA_VERSION, plan.operation_id, plan.digest, before.digest, effective.digest,
        decision, reason, provider_vm_id, snapshot_id,
    )
