from __future__ import annotations

import hashlib
from io import BytesIO
import os
from pathlib import Path
import sys
import tarfile

import pytest

from rekit_factory.isolation_probe import IsolationBindingV1, IsolationProbePlanV1
from rekit_factory.owned_probe_inputs import (
    BRANCH_SIGNAL_PACKAGE_SHA256,
    BRANCH_SIGNAL_PACKAGE_SIZE,
    BRANCH_SIGNAL_INPUTS_SHA256,
    CANARY_KINDS,
    STAGED_PACKAGE_NAME,
    TRIAL_IDS,
    materialize_branch_signal_package,
    prepare_branch_signal_probe_inputs,
)


REVERSEBENCH = Path(__file__).parents[2] / "reversebench"
FIXTURE = REVERSEBENCH / "fixtures/development/branch-signal"
PRIVATE_FILES = {
    "source": "source.rbvm.asm",
    "truth": "ground-truth.json",
    "private-test": "private-test-vector.txt",
    "dossier": "prior-dossier.txt",
    "credential": "heldout-identifier.txt",
    "sibling": "reset-recipe.txt",
    "residue": "build-recipe.txt",
}


def _archive_and_values():
    sys.path.insert(0, str(REVERSEBENCH / "src"))
    try:
        from reversebench import build_public_package
        from reversebench.contracts import TaskDefinitionV1
        task = TaskDefinitionV1.from_json((FIXTURE / "task.json").read_text())
        archive = build_public_package(task, FIXTURE / "public").archive_bytes
    finally:
        sys.path.remove(str(REVERSEBENCH / "src"))
        for name in tuple(sys.modules):
            if name == "reversebench" or name.startswith("reversebench."):
                sys.modules.pop(name)
    values = {
        kind: (FIXTURE / "private" / name).read_bytes()
        for kind, name in PRIVATE_FILES.items()
    }
    return archive, values


def test_exact_owned_package_and_opaque_two_trial_matrix_are_deterministic():
    archive, values = _archive_and_values()
    first = prepare_branch_signal_probe_inputs(archive, values)
    second = prepare_branch_signal_probe_inputs(archive, values)
    assert len(archive) == BRANCH_SIGNAL_PACKAGE_SIZE
    assert hashlib.sha256(archive).hexdigest() == BRANCH_SIGNAL_PACKAGE_SHA256
    assert first == second and first.digest == second.digest == BRANCH_SIGNAL_INPUTS_SHA256
    assert first.staged_package_name == STAGED_PACKAGE_NAME
    assert {item.trial_id for item in first.probes} == set(TRIAL_IDS)
    assert len(first.probes) == 25
    assert all(value not in archive for value in values.values())
    encoded = str([item.to_dict() for item in first.canaries])
    assert all(value.decode(errors="ignore") not in encoded for value in values.values())


def test_exact_stage_contains_only_public_member_bytes_and_canonical_metadata():
    archive, values = _archive_and_values()
    prepare_branch_signal_probe_inputs(archive, values)
    with tarfile.open(fileobj=BytesIO(archive), mode="r:") as opened:
        members = opened.getmembers()
        assert [item.name for item in members] == [
            "target/branch-signal.rbvm", "task.json",
        ]
        assert [(item.mode, item.mtime, item.uid, item.gid) for item in members] == [
            (0o555, 0, 0, 0), (0o444, 0, 0, 0),
        ]


def test_every_denial_channel_checks_every_canary_in_both_trials_and_reset_is_empty():
    archive, values = _archive_and_values()
    prepared = prepare_branch_signal_probe_inputs(archive, values)
    canary_ids = tuple(item.canary_id for item in prepared.canaries)
    denied = [item for item in prepared.probes if item.expectation == "unreachable"]
    assert len(denied) == 22
    assert all(item.canary_ids == canary_ids for item in denied)
    assert [item for item in prepared.probes if item.channel == "post-reset"][0].expectation == "empty"


def test_binding_produces_strict_existing_plan_and_rejects_other_package():
    archive, values = _archive_and_values()
    prepared = prepare_branch_signal_probe_inputs(archive, values)
    binding = IsolationBindingV1(
        "branch-signal-parallels-v1", "parallels", "26.4.0-57513",
        "sha256:" + "1" * 64, "2" * 64, "3" * 64, "4" * 64,
        BRANCH_SIGNAL_PACKAGE_SHA256, "none", "/input/" + STAGED_PACKAGE_NAME,
        "/scratch", "/output", "snapshot-switch-v1",
    )
    plan = prepared.bind(binding)
    assert IsolationProbePlanV1.from_dict(plan.to_dict()) == plan
    with pytest.raises(ValueError, match="prepared public package"):
        prepared.bind(IsolationBindingV1(
            binding.binding_id, binding.adapter_id, binding.adapter_version,
            binding.image_digest, binding.worker_sha256, binding.scope_sha256,
            binding.evidence_policy_sha256, "9" * 64, binding.network_policy,
            binding.input_mount, binding.scratch_mount, binding.output_mount,
            binding.reset_policy_id,
        ))
    with pytest.raises(ValueError, match="exact staged package name"):
        prepared.bind(IsolationBindingV1(
            binding.binding_id, binding.adapter_id, binding.adapter_version,
            binding.image_digest, binding.worker_sha256, binding.scope_sha256,
            binding.evidence_policy_sha256, binding.package_sha256, binding.network_policy,
            "/input/unbound-name.tar", binding.scratch_mount, binding.output_mount,
            binding.reset_policy_id,
        ))


@pytest.mark.parametrize("kind", CANARY_KINDS)
def test_private_bytes_or_incomplete_canary_sets_fail_closed(kind):
    archive, values = _archive_and_values()
    with pytest.raises(ValueError, match="present in the public archive"):
        prepare_branch_signal_probe_inputs(archive, {**values, kind: archive[:32]})
    incomplete = dict(values)
    incomplete.pop(kind)
    with pytest.raises(ValueError, match="exactly"):
        prepare_branch_signal_probe_inputs(archive, incomplete)
    altered = dict(values)
    altered[kind] += b"changed"
    with pytest.raises(ValueError, match="identity does not match"):
        prepare_branch_signal_probe_inputs(archive, altered)


def test_materializer_publishes_one_exact_read_only_file_and_never_creates_destination(tmp_path):
    archive, _ = _archive_and_values()
    destination = tmp_path / "authorized-stage"
    with pytest.raises(ValueError, match="existing real directory"):
        materialize_branch_signal_package(archive, destination)
    destination.mkdir()
    staged = materialize_branch_signal_package(archive, destination)
    final = destination / STAGED_PACKAGE_NAME
    assert staged.name == STAGED_PACKAGE_NAME
    assert staged.sha256 == BRANCH_SIGNAL_PACKAGE_SHA256
    assert tuple(path.name for path in destination.iterdir()) == (STAGED_PACKAGE_NAME,)
    assert hashlib.sha256(final.read_bytes()).hexdigest() == BRANCH_SIGNAL_PACKAGE_SHA256
    assert final.stat().st_mode & 0o777 == 0o444
    assert final.stat().st_nlink == 1


def test_materializer_rejects_symlink_destination_and_every_preexisting_entry(tmp_path):
    archive, _ = _archive_and_values()
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="not a symlink"):
        materialize_branch_signal_package(archive, alias)
    for name, make in (
        ("extra", lambda path: path.write_bytes(b"extra")),
        (STAGED_PACKAGE_NAME, lambda path: path.write_bytes(archive)),
        ("link", lambda path: path.symlink_to(tmp_path / "outside")),
    ):
        destination = tmp_path / ("stage-" + hashlib.sha256(name.encode()).hexdigest()[:8])
        destination.mkdir()
        make(destination / name)
        with pytest.raises(ValueError, match="must be empty"):
            materialize_branch_signal_package(archive, destination)
        assert tuple(item.name for item in destination.iterdir()) == (name,)


def test_materializer_rejects_wrong_bytes_without_touching_empty_destination(tmp_path):
    archive, _ = _archive_and_values()
    destination = tmp_path / "stage"
    destination.mkdir()
    changed = archive[:-1] + bytes([archive[-1] ^ 1])
    with pytest.raises(ValueError, match="digest"):
        materialize_branch_signal_package(changed, destination)
    assert tuple(destination.iterdir()) == ()


def test_materializer_cleans_its_temporary_inode_when_exclusive_publish_fails(
    tmp_path, monkeypatch,
):
    archive, _ = _archive_and_values()
    destination = tmp_path / "stage"
    destination.mkdir()

    def fail_link(*args, **kwargs):
        raise FileExistsError("simulated final-name race")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(ValueError, match="exclusive final package publication failed"):
        materialize_branch_signal_package(archive, destination)
    assert tuple(destination.iterdir()) == ()
