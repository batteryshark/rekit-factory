"""Deterministic owned inputs for a real ReverseBench isolation probe.

This module prepares host-side records only.  It does not stage files, invoke an
adapter, expose canary values to a worker, or claim that an environment is isolated.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
from typing import Mapping

from rekit_factory.isolation_probe import (
    REQUIRED_DENIAL_CHANNELS,
    CanaryRefV1,
    IsolationBindingV1,
    IsolationProbePlanV1,
    PackageMemberV1,
    ProbeSpecV1,
    SealedPublicPackageV1,
)
from rekit_factory.parallels_candidate import StagedFile


BRANCH_SIGNAL_PACKAGE_ID = "branch-signal-public-v1"
BRANCH_SIGNAL_PACKAGE_SHA256 = "c4f020b1fc4704f07645ccbd7f87058a4604e96b1dc97f0ac9060c8bd4d11886"
BRANCH_SIGNAL_PACKAGE_SIZE = 10_240
BRANCH_SIGNAL_MEMBERS = (
    PackageMemberV1(
        "target/branch-signal.rbvm",
        "a1e1f9f039413c58b73f6e960e77e190a918aaef6415766b8fa5ddd99e1a0502",
        26,
    ),
    PackageMemberV1(
        "task.json",
        "4c5b81a1ead94b8c6f2c3a92a068eebba0af31e7a110c19ca5187efe8f7a3773",
        1_448,
    ),
)
BRANCH_SIGNAL_PACKAGE = SealedPublicPackageV1(
    BRANCH_SIGNAL_PACKAGE_ID,
    BRANCH_SIGNAL_PACKAGE_SHA256,
    BRANCH_SIGNAL_PACKAGE_SIZE,
    BRANCH_SIGNAL_MEMBERS,
)
STAGED_PACKAGE_NAME = f"sha256-{BRANCH_SIGNAL_PACKAGE_SHA256}.tar"
TRIAL_IDS = ("branch-signal-trial-a", "branch-signal-trial-b")
CANARY_KINDS = (
    "source", "truth", "private-test", "dossier", "credential", "sibling", "residue",
)
BRANCH_SIGNAL_CANARY_SHA256 = {
    "source": "957a9fe7de3d143c6964cdf031e5d37236edc2cdcb4bbdd1b1b55da8016a29e1",
    "truth": "78decb6aee063c42f8434a526f2b1e8f077da1bc42bee7daae8ff48ed5c72a1d",
    "private-test": "de292c284ba7a71c7e6ad61beae15710b97725c3f0f8b4dc6d9bdd5164845809",
    "dossier": "a66c50a3b57e80ceb7e686d1fb57c76ec0b6e979fb1aca514e0be4a1170c5983",
    "credential": "1d17901117d6dfbee61eb12f983b745f7c015eacdf83f98f982d5d2b832698b5",
    "sibling": "4148559652fc81df8817f1cabd2089c61f40c6fa4a4f2289e24421e08521fece",
    "residue": "3501da5b44cd38bbc6d7484968f66286b6f87b2232224a69f8d989fa15b86b8b",
}
BRANCH_SIGNAL_INPUTS_SHA256 = "c22575940eef6aee361cada0d34e1ce83e721c1905f988ab2cfd18fdba82370b"
_STAGING_TEMP_NAME = ".branch-signal-public-v1.staging"


@dataclass(frozen=True)
class OwnedProbeInputs:
    """Exact public package and opaque host-side probe records, before adapter binding."""

    package: SealedPublicPackageV1
    staged_package_name: str
    canaries: tuple[CanaryRefV1, ...]
    probes: tuple[ProbeSpecV1, ...]

    @property
    def digest(self) -> str:
        value = {
            "canaries": [item.to_dict() for item in self.canaries],
            "package": self.package.to_dict(),
            "probes": [item.to_dict() for item in self.probes],
            "schema_version": 1,
            "staged_package_name": self.staged_package_name,
        }
        encoded = (json.dumps(value, allow_nan=False, ensure_ascii=False,
                              separators=(",", ":"), sort_keys=True) + "\n").encode()
        return hashlib.sha256(encoded).hexdigest()

    def bind(self, binding: IsolationBindingV1) -> IsolationProbePlanV1:
        """Build the existing strict plan after a real adapter identity is known."""
        if binding.package_sha256 != self.package.archive_sha256:
            raise ValueError("adapter binding does not identify the prepared public package")
        if PurePosixPath(binding.input_mount).name != self.staged_package_name:
            raise ValueError("adapter binding does not mount the exact staged package name")
        return IsolationProbePlanV1(binding, self.package, self.canaries, self.probes)


def _inspect_branch_signal_archive(archive_bytes: bytes) -> None:
    if type(archive_bytes) is not bytes or len(archive_bytes) != BRANCH_SIGNAL_PACKAGE_SIZE:
        raise ValueError("archive size does not match the owned branch-signal package")
    if hashlib.sha256(archive_bytes).hexdigest() != BRANCH_SIGNAL_PACKAGE_SHA256:
        raise ValueError("archive digest does not match the owned branch-signal package")
    try:
        with tarfile.open(fileobj=BytesIO(archive_bytes), mode="r:") as archive:
            members = archive.getmembers()
            actual = []
            for member in members:
                if not member.isfile() or member.linkname:
                    raise ValueError("sealed package members must be regular files")
                opened = archive.extractfile(member)
                if opened is None:
                    raise ValueError("sealed package member cannot be read")
                content = opened.read()
                actual.append(PackageMemberV1(
                    member.name, hashlib.sha256(content).hexdigest(), len(content),
                ))
                expected_mode = 0o555 if member.name.startswith("target/") else 0o444
                if (member.mode, member.mtime, member.uid, member.gid, member.uname, member.gname) != (
                    expected_mode, 0, 0, 0, "", "",
                ):
                    raise ValueError("sealed package metadata is not canonical")
    except tarfile.TarError as error:
        raise ValueError("sealed package must be a readable canonical tar") from error
    if tuple(actual) != BRANCH_SIGNAL_MEMBERS:
        raise ValueError("sealed package member allowlist or identity does not match")


def prepare_branch_signal_probe_inputs(
    archive_bytes: bytes, private_canary_values: Mapping[str, bytes],
) -> OwnedProbeInputs:
    """Verify and prepare the exact two-trial W-0050 owned probe inputs.

    Canary values remain caller-owned.  Only their hashes enter the returned records.
    Every denial observation checks all canaries in both trials; the worker receives only
    the sealed public archive and never this input mapping.
    """
    _inspect_branch_signal_archive(archive_bytes)
    if set(private_canary_values) != set(CANARY_KINDS):
        raise ValueError(f"private canary kinds must be exactly {list(CANARY_KINDS)}")
    values: list[tuple[str, bytes]] = []
    for kind in CANARY_KINDS:
        value = private_canary_values[kind]
        if type(value) is not bytes or not value:
            raise ValueError("private canary values must be non-empty bytes")
        if value in archive_bytes:
            raise ValueError(f"private {kind} canary bytes are present in the public archive")
        if hashlib.sha256(value).hexdigest() != BRANCH_SIGNAL_CANARY_SHA256[kind]:
            raise ValueError(f"private {kind} canary identity does not match the owned fixture")
        values.append((kind, value))
    if len({value for _, value in values}) != len(values):
        raise ValueError("private canary values must be distinct")

    canaries = tuple(
        CanaryRefV1(
            f"branch-signal-{kind}-v1", kind,
            hashlib.sha256(value).hexdigest(),
        )
        for kind, value in values
    )
    canary_ids = tuple(item.canary_id for item in canaries)
    probes: list[ProbeSpecV1] = []
    for trial_id in TRIAL_IDS:
        for channel in sorted(REQUIRED_DENIAL_CHANNELS):
            probes.append(ProbeSpecV1(
                f"{trial_id}-{channel}-deny", trial_id, channel, "unreachable", canary_ids,
            ))
        probes.append(ProbeSpecV1(
            f"{trial_id}-public-read", trial_id, "path", "public-readable", (),
        ))
    probes.append(ProbeSpecV1(
        "branch-signal-trial-b-post-reset-empty", TRIAL_IDS[1], "post-reset", "empty", (),
    ))
    prepared = OwnedProbeInputs(
        BRANCH_SIGNAL_PACKAGE, STAGED_PACKAGE_NAME, canaries, tuple(probes),
    )
    if prepared.digest != BRANCH_SIGNAL_INPUTS_SHA256:
        raise ValueError("prepared input identity does not match the owned probe matrix")
    return prepared


def _verify_materialized_file(directory_fd: int, name: str) -> StagedFile:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        raise ValueError("materialized public package is not a no-follow regular file") from error
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o444
                or metadata.st_size != BRANCH_SIGNAL_PACKAGE_SIZE):
            raise ValueError("materialized public package metadata does not match")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        if digest.hexdigest() != BRANCH_SIGNAL_PACKAGE_SHA256:
            raise ValueError("materialized public package digest does not match")
    finally:
        os.close(descriptor)
    return StagedFile(name, BRANCH_SIGNAL_PACKAGE_SHA256, BRANCH_SIGNAL_PACKAGE_SIZE)


def materialize_branch_signal_package(
    archive_bytes: bytes, destination: str | Path,
) -> StagedFile:
    """Atomically publish the exact package into one already-authorized empty directory.

    The function never creates the destination directory and never replaces an entry. The
    caller owns authorization and any later external share/VM operation. A temporary inode is
    written exclusively, fsynced, chmodded read-only, and atomically linked at the final name;
    the directory is fsynced after publication and temporary-name removal.
    """
    _inspect_branch_signal_archive(archive_bytes)
    if os.name != "posix" or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("secure package materialization requires POSIX no-follow operations")
    directory_flags = (
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        directory_fd = os.open(Path(destination), directory_flags)
    except OSError as error:
        raise ValueError("destination must be an existing real directory, not a symlink") from error
    temporary_created = False
    try:
        directory_metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_metadata.st_mode):
            raise ValueError("destination must be a real directory")
        with os.scandir(directory_fd) as iterator:
            existing = tuple(item.name for item in iterator)
        if existing:
            raise ValueError("destination must be empty before exact package publication")

        file_flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            file_fd = os.open(_STAGING_TEMP_NAME, file_flags, 0o600, dir_fd=directory_fd)
            temporary_created = True
        except OSError as error:
            raise ValueError("exclusive temporary package creation failed") from error
        try:
            offset = 0
            while offset < len(archive_bytes):
                written = os.write(file_fd, archive_bytes[offset:])
                if written <= 0:
                    raise OSError("package write made no progress")
                offset += written
            os.fsync(file_fd)
            os.fchmod(file_fd, 0o444)
            os.fsync(file_fd)
            metadata = os.fstat(file_fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                    or metadata.st_size != BRANCH_SIGNAL_PACKAGE_SIZE
                    or stat.S_IMODE(metadata.st_mode) != 0o444):
                raise ValueError("temporary package metadata does not match")
        finally:
            os.close(file_fd)

        try:
            os.link(
                _STAGING_TEMP_NAME, STAGED_PACKAGE_NAME,
                src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False,
            )
        except OSError as error:
            raise ValueError("exclusive final package publication failed") from error
        os.unlink(_STAGING_TEMP_NAME, dir_fd=directory_fd)
        temporary_created = False
        os.fsync(directory_fd)

        with os.scandir(directory_fd) as iterator:
            names = tuple(item.name for item in iterator)
        if names != (STAGED_PACKAGE_NAME,):
            raise ValueError("published staging directory violates the one-file allowlist")
        result = _verify_materialized_file(directory_fd, STAGED_PACKAGE_NAME)
        os.fsync(directory_fd)
        return result
    finally:
        if temporary_created:
            try:
                os.unlink(_STAGING_TEMP_NAME, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError:
                pass
        os.close(directory_fd)
