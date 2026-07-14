from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from rekit_factory.parallels_plan import (
    PRLCTL,
    ParallelsAdapterIdentityV1,
    ParallelsCommandPlanV1,
    ParallelsVmLifecycleIdentityV1,
    build_parallels_command_plan,
)


SOURCE_VM = "{2c2e0cd1-5019-4832-9e16-b5b218d6131a}"
SOURCE_SNAPSHOT = "{074287d1-6918-4a01-b3cd-17095f97d76b}"
PROVIDER_VM = "{d4080cf3-d729-488a-ae28-ee0564d6ca91}"
RESET_SNAPSHOT = "{174287d1-6918-4a01-b3cd-17095f97d76b}"


def _adapter() -> ParallelsAdapterIdentityV1:
    return ParallelsAdapterIdentityV1(
        "26.4.0-57513", "a" * 64, "b" * 64, SOURCE_VM, SOURCE_SNAPSHOT, "c" * 64,
    )


def _target(adapter=None, *, node_id="analysis-a", generation=1):
    adapter = adapter or _adapter()
    return ParallelsVmLifecycleIdentityV1(
        "range-proof", node_id, generation, "d" * 64, "e" * 64, adapter.digest,
    )


def test_clone_plan_is_fixed_to_pinned_source_snapshot_and_derived_name():
    adapter, target = _adapter(), _target()
    plan = build_parallels_command_plan("provision-range-proof-a", "clone", adapter, target)
    assert plan.argv == (
        PRLCTL, "clone", SOURCE_VM, "--name", target.clone_name,
        "--linked", "--id", SOURCE_SNAPSHOT,
    )
    assert target.range_id not in target.clone_name
    assert ParallelsCommandPlanV1.from_dict(plan.to_dict()) == plan
    assert ParallelsCommandPlanV1.from_dict(plan.to_dict()).digest == plan.digest


def test_post_clone_lifecycle_uses_provider_uuid_and_installed_cli_syntax():
    adapter, target = _adapter(), _target()
    expected = {
        "start": (PRLCTL, "start", PROVIDER_VM),
        "stop": (PRLCTL, "stop", PROVIDER_VM, "--acpi"),
        "snapshot-create": (
            PRLCTL, "snapshot", PROVIDER_VM, "--name", "reset-" + target.digest[:16],
            "--description", "operation:snapshot-op",
        ),
        "snapshot-switch": (
            PRLCTL, "snapshot-switch", PROVIDER_VM, "--id", RESET_SNAPSHOT,
            "--skip-resume",
        ),
        "delete": (PRLCTL, "delete", PROVIDER_VM),
    }
    for kind, argv in expected.items():
        snapshot = RESET_SNAPSHOT if kind == "snapshot-switch" else None
        operation = "snapshot-op" if kind == "snapshot-create" else f"{kind}-op"
        assert build_parallels_command_plan(
            operation, kind, adapter, target,
            provider_vm_id=PROVIDER_VM, snapshot_id=snapshot,
        ).argv == argv


def test_arbitrary_argv_provider_names_and_shell_text_cannot_be_admitted():
    adapter, target = _adapter(), _target()
    plan = build_parallels_command_plan("start-op", "start", adapter, target,
                                       provider_vm_id=PROVIDER_VM)
    with pytest.raises(ValueError, match="exact fixed plan"):
        replace(plan, argv=(PRLCTL, "exec", PROVIDER_VM, "sh", "-c", "id"))
    with pytest.raises(ValueError, match="provider_vm_id"):
        build_parallels_command_plan("start-op", "start", adapter, target,
                                     provider_vm_id="Rekit Worker Proof")
    with pytest.raises(ValueError, match="stable identifier"):
        build_parallels_command_plan("start;rm", "start", adapter, target,
                                     provider_vm_id=PROVIDER_VM)
    with pytest.raises(ValueError, match="cannot claim"):
        build_parallels_command_plan("clone-op", "clone", adapter, target,
                                     provider_vm_id=PROVIDER_VM)


def test_lifecycle_identity_splits_generation_node_scope_and_adapter_changes():
    adapter, target = _adapter(), _target()
    variants = (
        replace(target, generation=2), replace(target, node_id="analysis-b"),
        replace(target, scope_sha256="f" * 64),
    )
    assert len({target.digest, *(item.digest for item in variants)}) == 4
    changed_adapter = replace(adapter, binary_sha256="9" * 64)
    changed_target = replace(target, adapter_sha256=changed_adapter.digest)
    assert changed_target.digest != target.digest
    assert changed_target.clone_name != target.clone_name


def test_decoder_rejects_unknown_fields_and_identity_or_argv_forgery():
    plan = build_parallels_command_plan("delete-op", "delete", _adapter(), _target(),
                                       provider_vm_id=PROVIDER_VM)
    value = plan.to_dict()
    value["credential"] = "secret"
    with pytest.raises(ValueError, match="exactly"):
        ParallelsCommandPlanV1.from_dict(value)
    value = plan.to_dict()
    value["target"]["adapter_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="bind the adapter"):
        ParallelsCommandPlanV1.from_dict(value)
    value = plan.to_dict()
    value["argv"].append("--force")
    with pytest.raises(ValueError, match="exact fixed plan"):
        ParallelsCommandPlanV1.from_dict(value)


def test_decoder_rejects_boolean_versions_and_unhashable_kinds_as_validation_errors():
    plan = build_parallels_command_plan("delete-op", "delete", _adapter(), _target(),
                                       provider_vm_id=PROVIDER_VM)
    value = plan.to_dict()
    value["schema_version"] = True
    with pytest.raises(ValueError, match="schema_version"):
        ParallelsCommandPlanV1.from_dict(value)
    value = plan.to_dict()
    value["adapter"]["schema_version"] = True
    with pytest.raises(ValueError, match="schema_version"):
        ParallelsCommandPlanV1.from_dict(value)
    value = plan.to_dict()
    value["target"]["schema_version"] = True
    with pytest.raises(ValueError, match="schema_version"):
        ParallelsCommandPlanV1.from_dict(value)
    value = plan.to_dict()
    value["kind"] = []
    with pytest.raises(ValueError, match="unsupported"):
        ParallelsCommandPlanV1.from_dict(value)


def test_module_has_no_execution_surface_or_subprocess_dependency():
    source = (Path(__file__).parents[1] / "src/rekit_factory/parallels_plan.py").read_text()
    assert "import subprocess" not in source
    assert "subprocess." not in source
    assert "Popen(" not in source and "run(" not in source
    assert "def execute" not in source and "def invoke" not in source
