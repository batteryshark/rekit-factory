"""Bounded, read-only qualification of an existing Parallels VM candidate.

This module parses evidence captured by an operator or a read-only inventory command.  It
does not invoke Parallels, start a VM, provision shares, or claim environmental isolation.
An empty blocker list means only that the supplied candidate metadata is ready for a real
probe plan; the resulting probe evidence still requires independent review.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath
import re
import xml.etree.ElementTree as ET


MAX_CONFIG_BYTES = 1_048_576
MAX_STAGED_FILES = 16
MAX_SNAPSHOTS = 64
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:-]{0,127}$")
_UUID = re.compile(r"^\{[0-9a-fA-F-]{36}\}$")


@dataclass(frozen=True)
class StagedFile:
    """Content identity only; host paths are deliberately outside the report."""

    name: str
    sha256: str
    size: int

    def __post_init__(self) -> None:
        path = PurePosixPath(self.name)
        if (not self.name or len(self.name) > 255 or path.is_absolute()
                or path.as_posix() != self.name or len(path.parts) != 1
                or path.parts[0] in {".", ".."}):
            raise ValueError("staged file name must be one bounded basename")
        if not _DIGEST.fullmatch(self.sha256):
            raise ValueError("staged file sha256 must be a lowercase digest")
        if type(self.size) is not int or not 0 <= self.size <= 2**63 - 1:
            raise ValueError("staged file size must be a non-negative 64-bit integer")


@dataclass(frozen=True)
class ParallelsCandidateAssessment:
    adapter_version: str
    vm_id: str
    vm_name: str
    config_sha256: str
    source_snapshot_id: str | None
    base_image_sha256: str | None
    staged_files: tuple[StagedFile, ...]
    blockers: tuple[str, ...]

    @property
    def ready_for_probe(self) -> bool:
        """Necessary metadata/config readiness, never environmental proof."""
        return not self.blockers


def _text(root: ET.Element, path: str) -> str | None:
    value = root.findtext(path)
    return value.strip() if isinstance(value, str) else None


def _unsafe_setting(
    root: ET.Element, path: str, expected: str, blocker: str, blockers: list[str],
) -> None:
    if _text(root, path) != expected:
        blockers.append(blocker)


def assess_parallels_candidate(
    config: bytes,
    *,
    adapter_version: str,
    vm_state: str,
    snapshot_ids: tuple[str, ...],
    base_image_sha256: str | None,
    staged_files: tuple[StagedFile, ...],
    expected_package_sha256: str,
    reset_adapter_available: bool,
    worker_adapter_available: bool,
    host_defined_sharing_verified_disabled: bool,
) -> ParallelsCandidateAssessment:
    """Fail closed over bounded, already-captured Parallels candidate metadata."""
    if not isinstance(config, bytes) or not config or len(config) > MAX_CONFIG_BYTES:
        raise ValueError(f"config must contain 1..{MAX_CONFIG_BYTES} bytes")
    if b"<!DOCTYPE" in config.upper() or b"<!ENTITY" in config.upper():
        raise ValueError("config must not contain declarations or entities")
    try:
        config.decode("utf-8", errors="strict")
        root = ET.fromstring(config)
    except (UnicodeDecodeError, ET.ParseError) as error:
        raise ValueError("config must be well-formed UTF-8 XML") from error
    if root.tag != "ParallelsVirtualMachine":
        raise ValueError("config root must be ParallelsVirtualMachine")
    if not _IDENTIFIER.fullmatch(adapter_version):
        raise ValueError("adapter_version must be bounded display text")
    if vm_state not in {"stopped", "running", "paused", "suspended", "unknown"}:
        raise ValueError("unsupported vm_state")
    if not _DIGEST.fullmatch(expected_package_sha256):
        raise ValueError("expected_package_sha256 must be a lowercase digest")
    if len(snapshot_ids) > MAX_SNAPSHOTS or any(not _UUID.fullmatch(item) for item in snapshot_ids):
        raise ValueError(f"snapshot_ids must contain at most {MAX_SNAPSHOTS} UUIDs")
    if len(staged_files) > MAX_STAGED_FILES or any(type(item) is not StagedFile for item in staged_files):
        raise ValueError(f"staged_files must contain at most {MAX_STAGED_FILES} StagedFile values")
    if base_image_sha256 is not None and not _DIGEST.fullmatch(base_image_sha256):
        raise ValueError("base_image_sha256 must be a lowercase digest")
    if (type(reset_adapter_available) is not bool or type(worker_adapter_available) is not bool
            or type(host_defined_sharing_verified_disabled) is not bool):
        raise ValueError("adapter availability flags must be booleans")

    vm_id = _text(root, "./Identification/VmUuid")
    vm_name = _text(root, "./Identification/VmName")
    source_snapshot_id = _text(root, "./Identification/LinkedSnapshotUuid") or None
    if not isinstance(vm_id, str) or not _UUID.fullmatch(vm_id):
        raise ValueError("config VM UUID is missing or malformed")
    if not isinstance(vm_name, str) or not _IDENTIFIER.fullmatch(vm_name):
        raise ValueError("config VM name is missing or malformed")

    blockers: list[str] = []
    if _text(root, "./AppVersion") != adapter_version:
        blockers.append("adapter-version-mismatch")
    if vm_state != "stopped":
        blockers.append("vm-not-stopped")
    if root.findall("./Hardware/NetworkAdapter"):
        blockers.append("network-adapter-present")

    expected_settings = (
        ("./Settings/Tools/IsolatedVm", "1", "parallels-isolated-mode-disabled"),
        ("./Settings/Tools/SharedFolders/HostSharing/ShareAllMacDisks", "0", "all-host-disks-shared"),
        ("./Settings/Tools/SharedFolders/HostSharing/ShareUserHomeDir", "0", "host-home-shared"),
        ("./Settings/Tools/SharedFolders/HostSharing/SharedCloud", "0", "host-cloud-shared"),
        ("./Settings/Tools/SharedFolders/GuestSharing/Enabled", "0", "guest-sharing-enabled"),
        ("./Settings/Tools/SharedProfile/Enabled", "0", "shared-profile-enabled"),
        ("./Settings/Tools/SharedApplications/FromWinToMac", "0", "guest-app-sharing-enabled"),
        ("./Settings/Tools/SharedApplications/FromMacToWin", "0", "host-app-sharing-enabled"),
        ("./Settings/Tools/ClipboardSync/Enabled", "0", "clipboard-enabled"),
        ("./Settings/Tools/DragAndDrop/Enabled", "0", "drag-and-drop-enabled"),
        ("./Settings/Tools/SharedGamepad/Enabled", "0", "shared-gamepad-enabled"),
        ("./Settings/Tools/RemoteControl/Enabled", "0", "remote-control-enabled"),
        ("./Hardware/VirtIOVsock/ToolgateEnabled", "0", "toolgate-enabled"),
    )
    for path, expected, blocker in expected_settings:
        _unsafe_setting(root, path, expected, blocker, blockers)
    usb = root.find("./Settings/Tools/UsbController")
    if usb is None or any(_text(usb, name) != "0" for name in ("UhcEnabled", "EhcEnabled", "XhcEnabled")):
        blockers.append("usb-controller-enabled")

    shares = root.findall("./Settings/Tools/SharedFolders/HostSharing/SharedFolder")
    share_facts = sorted(
        (_text(item, "Name"), _text(item, "ReadOnly"), _text(item, "Enabled"))
        for item in shares
    )
    if share_facts != [("rekit-input", "1", "1"), ("rekit-output", "0", "1")]:
        blockers.append("host-share-policy-mismatch")
    if not snapshot_ids:
        blockers.append("reset-snapshot-missing")
    if source_snapshot_id is None or not _UUID.fullmatch(source_snapshot_id):
        blockers.append("source-snapshot-unpinned")
    if base_image_sha256 is None:
        blockers.append("base-image-content-digest-missing")
    if tuple(item.sha256 for item in staged_files) != (expected_package_sha256,):
        blockers.append("sealed-package-not-staged-alone")
    if not reset_adapter_available:
        blockers.append("reset-adapter-unavailable")
    if not worker_adapter_available:
        blockers.append("worker-adapter-unavailable")
    if not host_defined_sharing_verified_disabled:
        blockers.append("host-defined-sharing-unverified")

    return ParallelsCandidateAssessment(
        adapter_version=adapter_version,
        vm_id=vm_id,
        vm_name=vm_name,
        config_sha256=hashlib.sha256(config).hexdigest(),
        source_snapshot_id=source_snapshot_id,
        base_image_sha256=base_image_sha256,
        staged_files=tuple(staged_files),
        blockers=tuple(blockers),
    )
