from __future__ import annotations

from dataclasses import replace

import pytest

from rekit_factory.parallels_candidate import StagedFile, assess_parallels_candidate


PACKAGE = "a" * 64
VM = "{d4080cf3-d729-488a-ae28-ee0564d6ca91}"
SOURCE = "{074287d1-6918-4a01-b3cd-17095f97d76b}"
RESET = "{174287d1-6918-4a01-b3cd-17095f97d76b}"


def _config(
    *, network: str = "", isolated: str = "1", drag: str = "0",
    gamepad: str = "0", remote: str = "0", ehc: str = "0", xhc: str = "0",
) -> bytes:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<ParallelsVirtualMachine><AppVersion>26.4.0-57513</AppVersion><Identification>
<VmUuid>{VM}</VmUuid><VmName>Rekit Worker Proof</VmName><LinkedSnapshotUuid>{SOURCE}</LinkedSnapshotUuid>
</Identification><Settings><Tools><IsolatedVm>{isolated}</IsolatedVm><SharedFolders><HostSharing><ShareAllMacDisks>0</ShareAllMacDisks>
<ShareUserHomeDir>0</ShareUserHomeDir><SharedCloud>0</SharedCloud>
<SharedFolder><Name>rekit-input</Name><ReadOnly>1</ReadOnly><Enabled>1</Enabled></SharedFolder>
<SharedFolder><Name>rekit-output</Name><ReadOnly>0</ReadOnly><Enabled>1</Enabled></SharedFolder>
</HostSharing><GuestSharing><Enabled>0</Enabled></GuestSharing></SharedFolders>
<SharedProfile><Enabled>0</Enabled></SharedProfile><SharedApplications><FromWinToMac>0</FromWinToMac>
<FromMacToWin>0</FromMacToWin></SharedApplications><ClipboardSync><Enabled>0</Enabled></ClipboardSync>
<DragAndDrop><Enabled>{drag}</Enabled></DragAndDrop><SharedGamepad><Enabled>{gamepad}</Enabled></SharedGamepad>
<RemoteControl><Enabled>{remote}</Enabled></RemoteControl>
<UsbController><UhcEnabled>0</UhcEnabled><EhcEnabled>{ehc}</EhcEnabled><XhcEnabled>{xhc}</XhcEnabled></UsbController>
</Tools></Settings><Hardware>{network}<VirtIOVsock><ToolgateEnabled>0</ToolgateEnabled></VirtIOVsock></Hardware>
</ParallelsVirtualMachine>'''.encode()


def _assess(config: bytes | None = None, **overrides):
    values = dict(
        adapter_version="26.4.0-57513", vm_state="stopped", snapshot_ids=(RESET,),
        base_image_sha256="b" * 64,
        staged_files=(StagedFile("public-package.tar", PACKAGE, 10240),),
        expected_package_sha256=PACKAGE, reset_adapter_available=True,
        worker_adapter_available=True, host_defined_sharing_verified_disabled=True,
    )
    values.update(overrides)
    return assess_parallels_candidate(config or _config(), **values)


def test_safe_metadata_is_ready_for_probe_but_never_claimed_as_proof():
    result = _assess()
    assert result.ready_for_probe
    assert result.blockers == ()
    assert result.vm_id == VM and result.source_snapshot_id == SOURCE
    assert "/Users/" not in repr(result) and "/private/" not in repr(result)
    assert "never environmental proof" in (type(result).ready_for_probe.fget.__doc__ or "")


def test_current_host_shape_fails_closed_on_exact_known_gaps():
    result = _assess(
        _config(
            isolated="0", drag="1", gamepad="1", remote="1", ehc="1", xhc="1",
        ),
        snapshot_ids=(), base_image_sha256=None,
        staged_files=(StagedFile("benign-proof.exe", "c" * 64, 1024),),
        reset_adapter_available=False, worker_adapter_available=False,
        host_defined_sharing_verified_disabled=False,
    )
    assert result.ready_for_probe is False
    assert result.blockers == (
        "parallels-isolated-mode-disabled", "drag-and-drop-enabled",
        "shared-gamepad-enabled", "remote-control-enabled", "usb-controller-enabled",
        "reset-snapshot-missing", "base-image-content-digest-missing",
        "sealed-package-not-staged-alone", "reset-adapter-unavailable",
        "worker-adapter-unavailable", "host-defined-sharing-unverified",
    )


def test_network_and_share_expansion_fail_closed():
    network = "<NetworkAdapter><Enabled>0</Enabled></NetworkAdapter>"
    assert "network-adapter-present" in _assess(_config(network=network)).blockers
    unsafe = _config().replace(b"<ShareUserHomeDir>0", b"<ShareUserHomeDir>1")
    assert "host-home-shared" in _assess(unsafe).blockers
    missing = _config().replace(
        b"<SharedFolder><Name>rekit-output</Name><ReadOnly>0</ReadOnly><Enabled>1</Enabled></SharedFolder>",
        b"",
    )
    assert "host-share-policy-mismatch" in _assess(missing).blockers


def test_parser_is_bounded_and_rejects_entities_malformed_identity_and_host_paths():
    with pytest.raises(ValueError, match="declarations or entities"):
        _assess(b'<!DOCTYPE x [<!ENTITY y "z">]><ParallelsVirtualMachine/>')
    with pytest.raises(ValueError, match="1.."):
        _assess(b"x" * 1_048_577)
    with pytest.raises(ValueError, match="UUID"):
        _assess(_config().replace(VM.encode(), b"not-a-uuid"))
    with pytest.raises(ValueError, match="basename"):
        StagedFile("/private/tmp/public-package.tar", PACKAGE, 1)


def test_content_and_adapter_identity_changes_are_visible():
    result = _assess()
    changed = _assess(_config().replace(b"Rekit Worker Proof", b"Rekit Worker Proof 2"))
    assert result.config_sha256 != changed.config_sha256
    assert replace(result, base_image_sha256="d" * 64) != result
