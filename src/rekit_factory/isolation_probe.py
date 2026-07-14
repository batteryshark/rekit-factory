"""Strict, adapter-neutral contracts for disposable-worker isolation probes.

These contracts bind a future real probe to exact inputs and observations.  They do
not provision an environment and an instance of them is never isolation evidence by
itself.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any, ClassVar, Literal, Self


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
ProbeChannel = Literal[
    "path", "environment", "process", "retrieval", "log", "cache", "artifact",
    "credential", "socket", "network", "sibling", "post-reset",
]
ProbeExpectation = Literal["public-readable", "unreachable", "empty"]
ProbeOutcome = Literal["passed", "failed", "not-run"]

REQUIRED_DENIAL_CHANNELS = frozenset({
    "path", "environment", "process", "retrieval", "log", "cache", "artifact",
    "credential", "socket", "network", "sibling",
})
MAX_PACKAGE_MEMBERS = 4096
MAX_CANARIES = 128
MAX_PROBES = 256
MAX_PATH_LENGTH = 1024
MAX_INTEGER = 2**63 - 1


def _canonical(value: Any) -> bytes:
    if isinstance(value, IsolationContract):
        value = value.to_dict()
    return (json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"),
                       sort_keys=True) + "\n").encode("utf-8")


def _json_value(value: Any) -> Any:
    if isinstance(value, IsolationContract):
        return value.to_dict()
    if is_dataclass(value):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _digest(value: str, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a bounded stable identifier")
    return value


def _positive(value: int, name: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise ValueError(f"{name} must be a positive 64-bit integer")
    return value


def _relative(value: str, name: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_PATH_LENGTH:
        raise ValueError(f"{name} must be a normalized relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise ValueError(f"{name} must be a normalized relative POSIX path")
    return value


def _strict(value: Any, cls: type, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{cls.__name__} must contain exactly {sorted(fields)}")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    return {key: item for key, item in value.items() if key != "schema_version"}


def _bounded_array(value: Any, name: str, maximum: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{name} must be a JSON array with at most {maximum} items")
    return value


class IsolationContract:
    schema_version: ClassVar[int] = 1

    def to_dict(self) -> dict[str, Any]:
        value = {item.name: _json_value(getattr(self, item.name)) for item in fields(self)}
        value["schema_version"] = self.schema_version
        return value

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical(self)).hexdigest()


@dataclass(frozen=True)
class PackageMemberV1(IsolationContract):
    path: str
    sha256: str
    size: int

    def __post_init__(self) -> None:
        _relative(self.path, "package member path")
        _digest(self.sha256, "package member sha256")
        _positive(self.size, "package member size")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        return cls(**_strict(value, cls, {"schema_version", "path", "sha256", "size"}))


@dataclass(frozen=True)
class SealedPublicPackageV1(IsolationContract):
    package_id: str
    archive_sha256: str
    archive_size: int
    members: tuple[PackageMemberV1, ...]

    def __post_init__(self) -> None:
        _identifier(self.package_id, "package_id")
        _digest(self.archive_sha256, "archive_sha256")
        _positive(self.archive_size, "archive_size")
        members = tuple(self.members)
        if not members or any(type(item) is not PackageMemberV1 for item in members):
            raise ValueError("members must contain PackageMemberV1 records")
        if len(members) > MAX_PACKAGE_MEMBERS:
            raise ValueError(f"members must contain at most {MAX_PACKAGE_MEMBERS} records")
        if tuple(sorted(members, key=lambda item: item.path)) != members:
            raise ValueError("members must be sorted by path")
        if len({item.path for item in members}) != len(members):
            raise ValueError("package member paths must be unique")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = _strict(
            value, cls,
            {"schema_version", "package_id", "archive_sha256", "archive_size", "members"},
        )
        members = _bounded_array(fields["members"], "members", MAX_PACKAGE_MEMBERS)
        fields["members"] = tuple(PackageMemberV1.from_dict(item) for item in members)
        return cls(**fields)


@dataclass(frozen=True)
class CanaryRefV1(IsolationContract):
    canary_id: str
    kind: Literal["source", "truth", "private-test", "dossier", "credential", "sibling", "residue"]
    value_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.canary_id, "canary_id")
        if not isinstance(self.kind, str) or self.kind not in {
            "source", "truth", "private-test", "dossier", "credential", "sibling", "residue",
        }:
            raise ValueError("unsupported canary kind")
        _digest(self.value_sha256, "value_sha256")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        return cls(**_strict(
            value, cls, {"schema_version", "canary_id", "kind", "value_sha256"},
        ))


@dataclass(frozen=True)
class IsolationBindingV1(IsolationContract):
    binding_id: str
    adapter_id: str
    adapter_version: str
    image_digest: str
    worker_sha256: str
    scope_sha256: str
    evidence_policy_sha256: str
    package_sha256: str
    network_policy: Literal["none"]
    input_mount: str
    scratch_mount: str
    output_mount: str
    reset_policy_id: str

    def __post_init__(self) -> None:
        for name in ("binding_id", "adapter_id", "adapter_version", "reset_policy_id"):
            _identifier(getattr(self, name), name)
        if not isinstance(self.image_digest, str) or not _IMAGE.fullmatch(self.image_digest):
            raise ValueError("image_digest must be a pinned sha256 image digest")
        for name in ("worker_sha256", "scope_sha256", "evidence_policy_sha256", "package_sha256"):
            _digest(getattr(self, name), name)
        if self.network_policy != "none":
            raise ValueError("held-out isolation qualification requires network_policy none")
        mounts = (self.input_mount, self.scratch_mount, self.output_mount)
        if any(
            not isinstance(item, str) or len(item) > MAX_PATH_LENGTH
            or not item.startswith("/") or item.startswith("//")
            or not PurePosixPath(item).is_absolute()
            or PurePosixPath(item).as_posix() != item
            or ".." in PurePosixPath(item).parts
            for item in mounts
        ):
            raise ValueError("worker mounts must be normalized absolute POSIX paths")
        if len(set(mounts)) != 3:
            raise ValueError("input, scratch, and output mounts must be distinct")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = set(cls.__dataclass_fields__) | {"schema_version"}
        return cls(**_strict(value, cls, fields))


@dataclass(frozen=True)
class ProbeSpecV1(IsolationContract):
    probe_id: str
    trial_id: str
    channel: ProbeChannel
    expectation: ProbeExpectation
    canary_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.probe_id, "probe_id")
        _identifier(self.trial_id, "trial_id")
        if not isinstance(self.channel, str) or self.channel not in REQUIRED_DENIAL_CHANNELS | {"post-reset"}:
            raise ValueError("unsupported probe channel")
        if not isinstance(self.expectation, str) or self.expectation not in {"public-readable", "unreachable", "empty"}:
            raise ValueError("unsupported probe expectation")
        values = tuple(self.canary_ids)
        if len(values) > MAX_CANARIES:
            raise ValueError(f"canary_ids must contain at most {MAX_CANARIES} identifiers")
        if any(_identifier(item, "canary_id") != item for item in values) or len(set(values)) != len(values):
            raise ValueError("canary_ids must contain unique stable identifiers")
        if self.expectation == "unreachable" and not values:
            raise ValueError("unreachable probes must reference at least one opaque canary")
        if self.expectation != "unreachable" and values:
            raise ValueError("only unreachable probes may reference canaries")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = _strict(
            value, cls,
            {"schema_version", "probe_id", "trial_id", "channel", "expectation", "canary_ids"},
        )
        fields["canary_ids"] = tuple(
            _bounded_array(fields["canary_ids"], "canary_ids", MAX_CANARIES)
        )
        return cls(**fields)


@dataclass(frozen=True)
class IsolationProbePlanV1(IsolationContract):
    binding: IsolationBindingV1
    package: SealedPublicPackageV1
    canaries: tuple[CanaryRefV1, ...]
    probes: tuple[ProbeSpecV1, ...]

    def __post_init__(self) -> None:
        if type(self.binding) is not IsolationBindingV1 or type(self.package) is not SealedPublicPackageV1:
            raise ValueError("plan requires exact binding and package records")
        if self.binding.package_sha256 != self.package.archive_sha256:
            raise ValueError("binding package digest does not match sealed package")
        canaries, probes = tuple(self.canaries), tuple(self.probes)
        if not canaries or not probes:
            raise ValueError("plan must contain canaries and probes")
        if len(canaries) > MAX_CANARIES or len(probes) > MAX_PROBES:
            raise ValueError(
                f"plan permits at most {MAX_CANARIES} canaries and {MAX_PROBES} probes"
            )
        if any(type(item) is not CanaryRefV1 for item in canaries) or any(
            type(item) is not ProbeSpecV1 for item in probes
        ):
            raise ValueError("plan requires exact canary and probe records")
        canary_ids = {item.canary_id for item in canaries}
        if len(canary_ids) != len(canaries) or any(
            not set(probe.canary_ids) <= canary_ids for probe in probes
        ):
            raise ValueError("probe canary references must resolve exactly")
        if len({probe.probe_id for probe in probes}) != len(probes):
            raise ValueError("probe IDs must be unique")
        channels = {probe.channel for probe in probes if probe.expectation == "unreachable"}
        if not REQUIRED_DENIAL_CHANNELS <= channels:
            raise ValueError("plan is missing required denial channels")
        trials = {probe.trial_id for probe in probes}
        if len(trials) < 2 or not any(probe.channel == "sibling" for probe in probes):
            raise ValueError("plan must exercise at least two trials and sibling isolation")
        if not any(probe.channel == "post-reset" and probe.expectation == "empty" for probe in probes):
            raise ValueError("plan must include a post-reset empty-state probe")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = _strict(
            value, cls, {"schema_version", "binding", "package", "canaries", "probes"},
        )
        canaries = _bounded_array(fields["canaries"], "canaries", MAX_CANARIES)
        probes = _bounded_array(fields["probes"], "probes", MAX_PROBES)
        fields["binding"] = IsolationBindingV1.from_dict(fields["binding"])
        fields["package"] = SealedPublicPackageV1.from_dict(fields["package"])
        fields["canaries"] = tuple(CanaryRefV1.from_dict(item) for item in canaries)
        fields["probes"] = tuple(ProbeSpecV1.from_dict(item) for item in probes)
        return cls(**fields)


@dataclass(frozen=True)
class ProbeResultV1(IsolationContract):
    plan_sha256: str
    probe_id: str
    outcome: ProbeOutcome
    evidence_sha256: str | None
    leaked_canary_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _digest(self.plan_sha256, "plan_sha256")
        _identifier(self.probe_id, "probe_id")
        if not isinstance(self.outcome, str) or self.outcome not in {"passed", "failed", "not-run"}:
            raise ValueError("unsupported probe outcome")
        if (self.outcome == "not-run") != (self.evidence_sha256 is None):
            raise ValueError("run probes require evidence and not-run probes forbid it")
        if self.evidence_sha256 is not None:
            _digest(self.evidence_sha256, "evidence_sha256")
        leaked = tuple(self.leaked_canary_ids)
        if len(set(leaked)) != len(leaked) or any(_identifier(item, "leaked canary ID") != item for item in leaked):
            raise ValueError("leaked_canary_ids must contain unique stable identifiers")
        if leaked and self.outcome != "failed":
            raise ValueError("a canary leak must fail its probe")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        fields = _strict(
            value, cls,
            {"schema_version", "plan_sha256", "probe_id", "outcome", "evidence_sha256",
             "leaked_canary_ids"},
        )
        if not isinstance(fields["leaked_canary_ids"], list):
            raise ValueError("leaked_canary_ids must be a JSON array")
        fields["leaked_canary_ids"] = tuple(fields["leaked_canary_ids"])
        return cls(**fields)


def assess_probe_results(
    plan: IsolationProbePlanV1, results: tuple[ProbeResultV1, ...],
) -> tuple[str, ...]:
    """Return stable blocking reasons; an empty tuple is necessary, not sufficient, proof.

    Environmental evidence still needs independent review.  This function only checks
    completeness, identity, pass/fail state, and that no opaque canary ID was reported.
    """
    expected = {probe.probe_id for probe in plan.probes}
    result_ids = [item.probe_id for item in results]
    blockers: list[str] = []
    if any(item.plan_sha256 != plan.digest for item in results):
        blockers.append("plan-mismatch")
    if len(set(result_ids)) != len(result_ids):
        blockers.append("duplicate-result")
    if set(result_ids) != expected:
        blockers.append("incomplete-results")
    known_canaries = {item.canary_id for item in plan.canaries}
    if any(not set(item.leaked_canary_ids) <= known_canaries for item in results):
        blockers.append("unknown-canary-reference")
    if any(item.leaked_canary_ids for item in results):
        blockers.append("canary-leak")
    if any(item.outcome == "failed" for item in results):
        blockers.append("probe-failed")
    if any(item.outcome == "not-run" for item in results):
        blockers.append("probe-not-run")
    return tuple(blockers)
