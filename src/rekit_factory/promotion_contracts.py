"""Immutable promotion candidate identity without evaluation or install authority."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
CAPABILITY_KINDS = frozenset({
    "reusable-knowledge",
    "behavioral-skill",
    "deterministic-tool-adapter",
    "strategy-policy-change",
    "benchmark-fixture",
    "target-specific-evidence",
})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _identifier(value: Any, name: str) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a safe stable identifier")
    return value


def _digest(value: Any, name: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} must be a sha256 digest")
    return value


def _identifiers(values: Iterable[str], name: str, *, required: bool = False) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a sequence")
    result = tuple(_identifier(value, name) for value in values)
    if required and not result:
        raise ValueError(f"{name} must not be empty")
    if len(result) > 64 or len(result) != len(set(result)):
        raise ValueError(f"{name} must be unique and bounded")
    return tuple(sorted(result))


@dataclass(frozen=True, order=True)
class EvidenceReference:
    """An opaque exact reference; its owner remains responsible for semantic truth."""

    owner: str
    record_id: str
    record_digest: str

    def __post_init__(self) -> None:
        _identifier(self.owner, "evidence owner")
        _identifier(self.record_id, "evidence record id")
        _digest(self.record_digest, "evidence record digest")

    def to_dict(self) -> dict[str, str]:
        return {
            "owner": self.owner,
            "recordId": self.record_id,
            "recordDigest": self.record_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> "EvidenceReference":
        if not isinstance(value, Mapping) or set(value) != {
            "owner", "recordId", "recordDigest",
        }:
            raise ValueError("evidence reference is malformed")
        return cls(
            owner=value["owner"], record_id=value["recordId"],
            record_digest=value["recordDigest"],
        )


def _references(values: Iterable[EvidenceReference], name: str,
                *, required: bool = False) -> tuple[EvidenceReference, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a sequence")
    result = tuple(values)
    if any(type(value) is not EvidenceReference for value in result):
        raise ValueError(f"{name} must contain exact evidence references")
    if required and not result:
        raise ValueError(f"{name} must not be empty")
    if len(result) > 64 or len(result) != len(set(result)):
        raise ValueError(f"{name} must be unique and bounded")
    return tuple(sorted(result))


@dataclass(frozen=True)
class PromotionCandidate:
    """Content-addressed proposal; deliberately carries no eligibility or approval state."""

    capability_kind: str
    capability_name: str
    capability_version: str
    capability_content_digest: str
    origin_runs: tuple[EvidenceReference, ...]
    proof_bundles: tuple[EvidenceReference, ...]
    evaluation_results: tuple[EvidenceReference, ...] = ()
    scope_ids: tuple[str, ...] = ()
    prerequisite_ids: tuple[str, ...] = ()
    risk_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.capability_kind) is not str \
                or self.capability_kind not in CAPABILITY_KINDS:
            raise ValueError("capability kind is unsupported")
        _identifier(self.capability_name, "capability name")
        if type(self.capability_version) is not str \
                or _VERSION.fullmatch(self.capability_version) is None:
            raise ValueError("capability version is invalid")
        _digest(self.capability_content_digest, "capability content digest")
        object.__setattr__(self, "origin_runs", _references(
            self.origin_runs, "origin runs", required=True,
        ))
        object.__setattr__(self, "proof_bundles", _references(
            self.proof_bundles, "proof bundles", required=True,
        ))
        object.__setattr__(self, "evaluation_results", _references(
            self.evaluation_results, "evaluation results",
        ))
        object.__setattr__(self, "scope_ids", _identifiers(self.scope_ids, "scope ids"))
        object.__setattr__(self, "prerequisite_ids", _identifiers(
            self.prerequisite_ids, "prerequisite ids",
        ))
        object.__setattr__(self, "risk_ids", _identifiers(self.risk_ids, "risk ids"))

    def _subject(self) -> dict[str, Any]:
        return {
            "capabilityKind": self.capability_kind,
            "capabilityName": self.capability_name,
            "capabilityVersion": self.capability_version,
            "capabilityContentDigest": self.capability_content_digest,
            "scopeIds": list(self.scope_ids),
            "prerequisiteIds": list(self.prerequisite_ids),
            "riskIds": list(self.risk_ids),
        }

    @property
    def subject_hash(self) -> str:
        return _hash(self._subject())

    @property
    def candidate_id(self) -> str:
        # Concurrent campaigns proposing identical content converge on one subject identity.
        return "promotion-" + self.subject_hash.removeprefix("sha256:")

    def _content(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            **self._subject(),
            "originRuns": [value.to_dict() for value in self.origin_runs],
            "proofBundles": [value.to_dict() for value in self.proof_bundles],
            "evaluationResults": [value.to_dict() for value in self.evaluation_results],
        }

    @property
    def record_hash(self) -> str:
        return _hash(self._content())

    def to_record(self) -> dict[str, Any]:
        return {
            **self._content(),
            "candidateId": self.candidate_id,
            "subjectHash": self.subject_hash,
            "recordHash": self.record_hash,
        }

    @classmethod
    def from_record(cls, value: object) -> "PromotionCandidate":
        exact = {
            "schemaVersion", "candidateId", "subjectHash", "recordHash",
            "capabilityKind", "capabilityName", "capabilityVersion",
            "capabilityContentDigest", "originRuns", "proofBundles",
            "evaluationResults", "scopeIds", "prerequisiteIds", "riskIds",
        }
        if not isinstance(value, Mapping) or set(value) != exact \
                or type(value.get("schemaVersion")) is not int \
                or value.get("schemaVersion") != SCHEMA_VERSION:
            raise ValueError("promotion candidate record is malformed")
        candidate = cls(
            capability_kind=value["capabilityKind"],
            capability_name=value["capabilityName"],
            capability_version=value["capabilityVersion"],
            capability_content_digest=value["capabilityContentDigest"],
            origin_runs=tuple(EvidenceReference.from_dict(item)
                              for item in _list(value["originRuns"], "originRuns")),
            proof_bundles=tuple(EvidenceReference.from_dict(item)
                                for item in _list(value["proofBundles"], "proofBundles")),
            evaluation_results=tuple(EvidenceReference.from_dict(item)
                                     for item in _list(value["evaluationResults"],
                                                       "evaluationResults")),
            scope_ids=tuple(_list(value["scopeIds"], "scopeIds")),
            prerequisite_ids=tuple(_list(value["prerequisiteIds"], "prerequisiteIds")),
            risk_ids=tuple(_list(value["riskIds"], "riskIds")),
        )
        if candidate.to_record() != dict(value):
            raise ValueError("promotion candidate identity or canonical order is invalid")
        return candidate


def _list(value: Any, name: str) -> list[Any]:
    if type(value) is not list:
        raise ValueError(f"{name} must be a JSON array")
    return value


def _hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
