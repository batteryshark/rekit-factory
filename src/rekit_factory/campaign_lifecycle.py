"""Strict durable source contracts for campaign coverage and archival lifecycle.

This module deliberately stops before outcome projection, HTTP, or UI integration.  It owns
only canonical source records, their transition authorities, deterministic serialization, and
small atomic POSIX persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import errno
import json
import os
from pathlib import Path
import re
import stat
import uuid
from typing import Any, Literal, Mapping, Self


SCHEMA_VERSION = 1
SOURCE_VERSION = "factory-campaign-lifecycle/v1"
MAX_STATE_BYTES = 1_048_576

CAMPAIGN_AUTHORITY = "factory-scheduler"
COVERAGE_AUTHORITY = "muster"
ARCHIVE_AUTHORITY = "operator"

CampaignState = Literal["planned", "active", "completed", "cancelled"]
ArchiveState = Literal["unarchived", "archived"]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_CAMPAIGN_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"active", "cancelled"}),
    "active": frozenset({"completed", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}
_ARCHIVE_TRANSITIONS: dict[str, frozenset[str]] = {
    "unarchived": frozenset({"archived"}),
    "archived": frozenset(),
}


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a stable identifier")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer of at least {minimum}")
    return value


def _exact(value: object, label: str, fields: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ValueError(f"{label} must be a JSON object with string fields")
    unknown, missing = set(value) - fields, fields - set(value)
    if unknown:
        raise ValueError(f"{label} has unknown fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"{label} is missing fields: {sorted(missing)}")
    return value


def _authority(value: object, expected: str, label: str) -> str:
    if value != expected:
        raise ValueError(f"{label} authority must be {expected}")
    return expected


@dataclass(frozen=True)
class CoverageRecord:
    """Muster-owned scope coverage; deliberately independent of campaign completion."""

    completed_units: int = 0
    total_units: int = 0
    authority: str = COVERAGE_AUTHORITY

    def __post_init__(self) -> None:
        _integer(self.completed_units, "completed_units")
        _integer(self.total_units, "total_units")
        if self.completed_units > self.total_units:
            raise ValueError("completed_units must not exceed total_units")
        _authority(self.authority, COVERAGE_AUTHORITY, "coverage")

    @property
    def state(self) -> Literal["uncovered", "partial", "covered"]:
        if self.completed_units == 0:
            return "uncovered"
        if self.completed_units == self.total_units:
            return "covered"
        return "partial"

    def to_dict(self) -> dict[str, object]:
        return {
            "authority": self.authority,
            "completedUnits": self.completed_units,
            "state": self.state,
            "totalUnits": self.total_units,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _exact(
            value, "coverage record", {"authority", "completedUnits", "state", "totalUnits"},
        )
        record = cls(
            completed_units=_integer(raw["completedUnits"], "completedUnits"),
            total_units=_integer(raw["totalUnits"], "totalUnits"),
            authority=_authority(raw["authority"], COVERAGE_AUTHORITY, "coverage"),
        )
        if raw["state"] != record.state:
            raise ValueError("coverage state does not match its canonical unit counts")
        return record


@dataclass(frozen=True)
class CampaignRecord:
    campaign_id: str
    revision: int = 1
    state: CampaignState = "planned"
    coverage: CoverageRecord = CoverageRecord()
    authority: str = CAMPAIGN_AUTHORITY

    def __post_init__(self) -> None:
        _identifier(self.campaign_id, "campaign_id")
        _integer(self.revision, "campaign revision", minimum=1)
        if self.state not in _CAMPAIGN_TRANSITIONS:
            raise ValueError("unsupported campaign state")
        if not isinstance(self.coverage, CoverageRecord):
            raise ValueError("coverage must be a CoverageRecord")
        _authority(self.authority, CAMPAIGN_AUTHORITY, "campaign")

    def to_dict(self) -> dict[str, object]:
        return {
            "authority": self.authority,
            "campaignId": self.campaign_id,
            "coverage": self.coverage.to_dict(),
            "revision": self.revision,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _exact(
            value, "campaign record",
            {"authority", "campaignId", "coverage", "revision", "state"},
        )
        return cls(
            campaign_id=_identifier(raw["campaignId"], "campaignId"),
            revision=_integer(raw["revision"], "campaign revision", minimum=1),
            state=raw["state"],
            coverage=CoverageRecord.from_dict(raw["coverage"]),
            authority=_authority(raw["authority"], CAMPAIGN_AUTHORITY, "campaign"),
        )


@dataclass(frozen=True)
class ArchiveRecord:
    archive_id: str
    campaign_id: str
    revision: int = 1
    state: ArchiveState = "unarchived"
    authority: str = ARCHIVE_AUTHORITY

    def __post_init__(self) -> None:
        _identifier(self.archive_id, "archive_id")
        _identifier(self.campaign_id, "campaign_id")
        _integer(self.revision, "archive revision", minimum=1)
        if self.state not in _ARCHIVE_TRANSITIONS:
            raise ValueError("unsupported archive state")
        _authority(self.authority, ARCHIVE_AUTHORITY, "archive")

    def to_dict(self) -> dict[str, object]:
        return {
            "archiveId": self.archive_id,
            "authority": self.authority,
            "campaignId": self.campaign_id,
            "revision": self.revision,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _exact(
            value, "archive record",
            {"archiveId", "authority", "campaignId", "revision", "state"},
        )
        return cls(
            archive_id=_identifier(raw["archiveId"], "archiveId"),
            campaign_id=_identifier(raw["campaignId"], "campaignId"),
            revision=_integer(raw["revision"], "archive revision", minimum=1),
            state=raw["state"],
            authority=_authority(raw["authority"], ARCHIVE_AUTHORITY, "archive"),
        )


@dataclass(frozen=True)
class CampaignLifecycleState:
    campaigns: tuple[CampaignRecord, ...] = ()
    archives: tuple[ArchiveRecord, ...] = ()

    def __post_init__(self) -> None:
        campaigns = tuple(sorted(self.campaigns, key=lambda item: item.campaign_id))
        archives = tuple(sorted(self.archives, key=lambda item: item.archive_id))
        if any(not isinstance(item, CampaignRecord) for item in campaigns):
            raise ValueError("campaigns must contain CampaignRecord values")
        if any(not isinstance(item, ArchiveRecord) for item in archives):
            raise ValueError("archives must contain ArchiveRecord values")
        campaign_ids = [item.campaign_id for item in campaigns]
        archive_ids = [item.archive_id for item in archives]
        if len(set(campaign_ids)) != len(campaign_ids):
            raise ValueError("campaign identities must be unique")
        if len(set(archive_ids)) != len(archive_ids):
            raise ValueError("archive identities must be unique")
        known_campaigns = set(campaign_ids)
        if any(item.campaign_id not in known_campaigns for item in archives):
            raise ValueError("archive parent campaign must exist")
        object.__setattr__(self, "campaigns", campaigns)
        object.__setattr__(self, "archives", archives)

    def _campaign(self, campaign_id: str) -> CampaignRecord:
        identifier = _identifier(campaign_id, "campaign_id")
        try:
            return next(item for item in self.campaigns if item.campaign_id == identifier)
        except StopIteration as exc:
            raise ValueError("campaign does not exist") from exc

    def _archive(self, archive_id: str) -> ArchiveRecord:
        identifier = _identifier(archive_id, "archive_id")
        try:
            return next(item for item in self.archives if item.archive_id == identifier)
        except StopIteration as exc:
            raise ValueError("archive does not exist") from exc

    @staticmethod
    def _expected(actual: int, expected: object) -> None:
        if _integer(expected, "expected_revision", minimum=1) != actual:
            raise ValueError("stale lifecycle revision")

    def create_campaign(self, campaign_id: str, *, authority: str) -> Self:
        identifier = _identifier(campaign_id, "campaign_id")
        _authority(authority, CAMPAIGN_AUTHORITY, "campaign")
        if any(item.campaign_id == identifier for item in self.campaigns):
            raise ValueError("campaign already exists")
        return replace(self, campaigns=(*self.campaigns, CampaignRecord(identifier)))

    def transition_campaign(
        self, campaign_id: str, to_state: CampaignState, *, expected_revision: int,
        authority: str,
    ) -> Self:
        current = self._campaign(campaign_id)
        self._expected(current.revision, expected_revision)
        _authority(authority, CAMPAIGN_AUTHORITY, "campaign")
        if to_state not in _CAMPAIGN_TRANSITIONS[current.state]:
            raise ValueError(f"invalid campaign transition {current.state} -> {to_state}")
        updated = replace(current, revision=current.revision + 1, state=to_state)
        return replace(self, campaigns=tuple(
            updated if item.campaign_id == current.campaign_id else item
            for item in self.campaigns
        ))

    def record_coverage(
        self, campaign_id: str, *, completed_units: int, total_units: int,
        expected_revision: int, authority: str,
    ) -> Self:
        current = self._campaign(campaign_id)
        self._expected(current.revision, expected_revision)
        _authority(authority, COVERAGE_AUTHORITY, "coverage")
        coverage = CoverageRecord(completed_units, total_units, authority)
        if coverage.completed_units < current.coverage.completed_units:
            raise ValueError("coverage completed_units must not decrease")
        if coverage.total_units < current.coverage.total_units:
            raise ValueError("coverage total_units must not decrease")
        updated = replace(current, revision=current.revision + 1, coverage=coverage)
        return replace(self, campaigns=tuple(
            updated if item.campaign_id == current.campaign_id else item
            for item in self.campaigns
        ))

    def create_archive(self, archive_id: str, campaign_id: str, *, authority: str) -> Self:
        identifier = _identifier(archive_id, "archive_id")
        parent = self._campaign(campaign_id)
        _authority(authority, ARCHIVE_AUTHORITY, "archive")
        if any(item.archive_id == identifier for item in self.archives):
            raise ValueError("archive already exists")
        return replace(
            self,
            archives=(*self.archives, ArchiveRecord(identifier, parent.campaign_id)),
        )

    def transition_archive(
        self, archive_id: str, to_state: ArchiveState, *, expected_revision: int,
        authority: str,
    ) -> Self:
        current = self._archive(archive_id)
        self._expected(current.revision, expected_revision)
        _authority(authority, ARCHIVE_AUTHORITY, "archive")
        if to_state not in _ARCHIVE_TRANSITIONS[current.state]:
            raise ValueError(f"invalid archive transition {current.state} -> {to_state}")
        updated = replace(current, revision=current.revision + 1, state=to_state)
        return replace(self, archives=tuple(
            updated if item.archive_id == current.archive_id else item
            for item in self.archives
        ))

    def to_dict(self) -> dict[str, object]:
        return {
            "archives": [item.to_dict() for item in self.archives],
            "campaigns": [item.to_dict() for item in self.campaigns],
            "schemaVersion": SCHEMA_VERSION,
            "sourceVersion": SOURCE_VERSION,
        }

    def canonical_bytes(self) -> bytes:
        return (json.dumps(
            self.to_dict(), allow_nan=False, ensure_ascii=False,
            separators=(",", ":"), sort_keys=True,
        ) + "\n").encode("utf-8")

    @classmethod
    def from_dict(cls, value: object) -> Self:
        raw = _exact(
            value, "campaign lifecycle state",
            {"archives", "campaigns", "schemaVersion", "sourceVersion"},
        )
        if raw["schemaVersion"] != SCHEMA_VERSION or raw["sourceVersion"] != SOURCE_VERSION:
            raise ValueError("unsupported campaign lifecycle source version")
        if type(raw["campaigns"]) is not list or type(raw["archives"]) is not list:
            raise ValueError("campaigns and archives must be arrays")
        return cls(
            campaigns=tuple(CampaignRecord.from_dict(item) for item in raw["campaigns"]),
            archives=tuple(ArchiveRecord.from_dict(item) for item in raw["archives"]),
        )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


class CampaignLifecycleStore:
    """Bounded atomic POSIX store for the canonical lifecycle source document."""

    def __init__(self, root: str | Path, *, max_bytes: int = MAX_STATE_BYTES) -> None:
        if os.name != "posix":
            raise NotImplementedError("campaign lifecycle storage currently requires POSIX")
        _integer(max_bytes, "max_bytes", minimum=1)
        raw = Path(root).expanduser()
        if raw.is_symlink():
            raise ValueError("campaign lifecycle root must not be a symlink")
        raw.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw.chmod(0o700)
        self.root = raw.resolve()
        info = self.root.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("campaign lifecycle root must be a directory")
        self._identity = (info.st_dev, info.st_ino)
        self.path = self.root / "campaign-lifecycle.json"
        self.max_bytes = max_bytes

    def _open_root(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.root, flags)
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != self._identity:
            os.close(descriptor)
            raise ValueError("campaign lifecycle root identity changed")
        return descriptor

    def load(self) -> CampaignLifecycleState:
        root_fd = self._open_root()
        try:
            try:
                state_fd = os.open(
                    self.path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd,
                )
            except FileNotFoundError:
                return CampaignLifecycleState()
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise ValueError("campaign lifecycle state must not be a symlink") from exc
                raise
            try:
                info = os.fstat(state_fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("campaign lifecycle state must be a regular file")
                if info.st_size > self.max_bytes:
                    raise ValueError("campaign lifecycle state exceeds the size limit")
                raw = os.read(state_fd, self.max_bytes + 1)
            finally:
                os.close(state_fd)
        finally:
            os.close(root_fd)
        if len(raw) > self.max_bytes:
            raise ValueError("campaign lifecycle state exceeds the size limit")
        try:
            decoded = json.loads(
                raw.decode("utf-8", "strict"), object_pairs_hook=_reject_duplicate_keys,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("campaign lifecycle state must be valid UTF-8 JSON") from exc
        return CampaignLifecycleState.from_dict(decoded)

    def save(self, state: CampaignLifecycleState) -> None:
        if not isinstance(state, CampaignLifecycleState):
            raise ValueError("state must be a CampaignLifecycleState")
        encoded = state.canonical_bytes()
        if len(encoded) > self.max_bytes:
            raise ValueError("campaign lifecycle state exceeds the size limit")
        root_fd = self._open_root()
        temporary = f".campaign-lifecycle.{uuid.uuid4().hex}.tmp"
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            temporary_fd = os.open(temporary, flags, 0o600, dir_fd=root_fd)
            try:
                with os.fdopen(temporary_fd, "wb", closefd=False) as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(temporary_fd)
            os.replace(temporary, self.path.name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.fsync(root_fd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=root_fd)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(root_fd)

