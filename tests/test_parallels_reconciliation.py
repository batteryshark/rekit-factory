from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from rekit_factory.parallels_effects import (
    DurableParallelsEffectAdapter, ParallelsRunnerCaptureV1,
)
from rekit_factory.parallels_plan import (
    ParallelsAdapterIdentityV1, ParallelsVmLifecycleIdentityV1,
    build_parallels_command_plan,
)
from rekit_factory.parallels_reconciliation import (
    ParallelsInventoryObservationV1, ParallelsSnapshotObservationV1,
    ParallelsReconciliationDecisionV1, ParallelsVmObservationV1,
    reconcile_parallels_plan,
)


SOURCE_VM = "{2c2e0cd1-5019-4832-9e16-b5b218d6131a}"
SOURCE_SNAPSHOT = "{074287d1-6918-4a01-b3cd-17095f97d76b}"
PROVIDER_VM = "{d4080cf3-d729-488a-ae28-ee0564d6ca91}"
RESET = "{174287d1-6918-4a01-b3cd-17095f97d76b}"


def identities():
    adapter = ParallelsAdapterIdentityV1(
        "26.4.0-57513", "a" * 64, "b" * 64, SOURCE_VM, SOURCE_SNAPSHOT, "c" * 64,
    )
    target = ParallelsVmLifecycleIdentityV1(
        "range-proof", "analysis-a", 1, "d" * 64, "e" * 64, adapter.digest,
    )
    return adapter, target


def vm(target, *, state="stopped", snapshots=(), current=None, name=None,
       source_vm=SOURCE_VM, source_snapshot=SOURCE_SNAPSHOT):
    return ParallelsVmObservationV1(
        PROVIDER_VM, name or target.clone_name, source_vm, source_snapshot,
        state, current, tuple(snapshots),
    )


def inventory(adapter, vms=(), *, sequence=1, previous=None, observation_id="inventory-1"):
    return ParallelsInventoryObservationV1(
        1, observation_id, sequence, adapter.digest, previous, tuple(vms),
    )


def chained(adapter, before, vms):
    return inventory(
        adapter, vms, sequence=before.sequence + 1, previous=before.digest,
        observation_id="inventory-2",
    )


def test_clone_executes_when_absent_and_discovers_exact_uuid_when_observed():
    adapter, target = identities()
    plan = build_parallels_command_plan("clone-op", "clone", adapter, target)
    before = inventory(adapter)
    assert reconcile_parallels_plan(plan, before).decision == "execute"
    result = reconcile_parallels_plan(plan, before, chained(adapter, before, (vm(target),)))
    assert result.decision == "already-applied"
    assert result.provider_vm_id == PROVIDER_VM
    envelope = json.loads(result.success_envelope(plan))
    assert envelope["provider_vm_id"] == PROVIDER_VM
    assert envelope["plan_sha256"] == plan.digest


def test_clone_name_collision_conflicts_and_unchanged_post_effect_is_unknown():
    adapter, target = identities()
    plan = build_parallels_command_plan("clone-op", "clone", adapter, target)
    collision = vm(target, source_snapshot=RESET)
    assert reconcile_parallels_plan(plan, inventory(adapter, (collision,))).decision == "conflict"
    before = inventory(adapter)
    after = chained(adapter, before, ())
    decision = reconcile_parallels_plan(plan, before, after)
    assert (decision.decision, decision.reason_code) == ("unknown", "effect-not-observed")
    with pytest.raises(ValueError, match="completed"):
        decision.success_envelope(plan)


@pytest.mark.parametrize("kind,before_state,after_state", [
    ("start", "stopped", "running"),
    ("stop", "running", "stopped"),
])
def test_start_and_stop_reconcile_exact_provider_state(kind, before_state, after_state):
    adapter, target = identities()
    plan = build_parallels_command_plan(
        f"{kind}-op", kind, adapter, target, provider_vm_id=PROVIDER_VM,
    )
    before = inventory(adapter, (vm(target, state=before_state),))
    assert reconcile_parallels_plan(plan, before).decision == "execute"
    result = reconcile_parallels_plan(
        plan, before, chained(adapter, before, (vm(target, state=after_state),)),
    )
    assert result.decision == "already-applied"
    assert json.loads(result.success_envelope(plan))["provider_vm_id"] is None


def test_snapshot_create_discovers_uuid_and_rejects_name_collision():
    adapter, target = identities()
    plan = build_parallels_command_plan(
        "snapshot-op", "snapshot-create", adapter, target, provider_vm_id=PROVIDER_VM,
    )
    before = inventory(adapter, (vm(target),))
    snapshot = ParallelsSnapshotObservationV1(
        RESET, "reset-" + target.digest[:16], "operation:snapshot-op",
    )
    result = reconcile_parallels_plan(
        plan, before, chained(adapter, before, (vm(target, snapshots=(snapshot,)),)),
    )
    assert result.decision == "already-applied"
    assert result.snapshot_id == RESET
    collision = replace(snapshot, description="operation:other")
    assert reconcile_parallels_plan(
        plan, inventory(adapter, (vm(target, snapshots=(collision,)),)),
    ).decision == "conflict"


def test_snapshot_switch_and_delete_reconcile_idempotently():
    adapter, target = identities()
    snapshot = ParallelsSnapshotObservationV1(RESET, "reset-owned", "operation:owned")
    switch = build_parallels_command_plan(
        "switch-op", "snapshot-switch", adapter, target,
        provider_vm_id=PROVIDER_VM, snapshot_id=RESET,
    )
    before = inventory(adapter, (vm(target, snapshots=(snapshot,)),))
    assert reconcile_parallels_plan(switch, before).decision == "execute"
    assert reconcile_parallels_plan(
        switch, before, chained(adapter, before, (vm(
            target, snapshots=(snapshot,), current=RESET,
        ),)),
    ).decision == "already-applied"
    delete = build_parallels_command_plan(
        "delete-op", "delete", adapter, target, provider_vm_id=PROVIDER_VM,
    )
    assert reconcile_parallels_plan(delete, inventory(adapter)).decision == "already-applied"


def test_missing_vm_unstable_state_and_missing_snapshot_fail_closed():
    adapter, target = identities()
    start = build_parallels_command_plan(
        "start-op", "start", adapter, target, provider_vm_id=PROVIDER_VM,
    )
    assert reconcile_parallels_plan(start, inventory(adapter)).decision == "conflict"
    assert reconcile_parallels_plan(
        start, inventory(adapter, (vm(target, state="unknown"),)),
    ).decision == "unknown"
    switch = build_parallels_command_plan(
        "switch-op", "snapshot-switch", adapter, target,
        provider_vm_id=PROVIDER_VM, snapshot_id=RESET,
    )
    assert reconcile_parallels_plan(
        switch, inventory(adapter, (vm(target),)),
    ).decision == "conflict"


@pytest.mark.parametrize("changes", [
    {"name": "unrelated-vm"},
    {"source_vm": RESET},
    {"source_snapshot": RESET},
])
def test_lifecycle_operations_reject_reused_uuid_with_wrong_vm_identity(changes):
    adapter, target = identities()
    plan = build_parallels_command_plan(
        "start-op", "start", adapter, target, provider_vm_id=PROVIDER_VM,
    )
    decision = reconcile_parallels_plan(
        plan, inventory(adapter, (vm(target, **changes),)),
    )
    assert (decision.decision, decision.reason_code) == (
        "conflict", "provider-vm-identity-mismatch",
    )


def test_exact_inventory_can_represent_snapshots_with_empty_descriptions():
    adapter, target = identities()
    snapshot = ParallelsSnapshotObservationV1(RESET, "ordinary-snapshot", "")
    observed = inventory(adapter, (vm(target, snapshots=(snapshot,)),))
    assert ParallelsInventoryObservationV1.from_dict(observed.to_dict()) == observed


def test_observation_chain_adapter_and_decoders_fail_closed():
    adapter, target = identities()
    plan = build_parallels_command_plan("clone-op", "clone", adapter, target)
    before = inventory(adapter)
    bad_after = inventory(adapter, sequence=2, previous="f" * 64, observation_id="bad")
    with pytest.raises(ValueError, match="chained"):
        reconcile_parallels_plan(plan, before, bad_after)
    value = vm(target).to_dict()
    value["state"] = []
    with pytest.raises(ValueError, match="state"):
        ParallelsVmObservationV1.from_dict(value)
    value = before.to_dict()
    value["schema_version"] = True
    with pytest.raises(ValueError, match="schema_version"):
        ParallelsInventoryObservationV1.from_dict(value)
    decision = reconcile_parallels_plan(plan, before)
    value = decision.to_dict()
    value["decision"] = []
    with pytest.raises(ValueError, match="decision"):
        ParallelsReconciliationDecisionV1.from_dict(value)


def test_post_effect_chain_requires_a_prior_execute_decision_and_new_observation_id():
    adapter, target = identities()
    plan = build_parallels_command_plan("clone-op", "clone", adapter, target)
    completed = inventory(adapter, (vm(target),))
    with pytest.raises(ValueError, match="did not authorize"):
        reconcile_parallels_plan(
            plan, completed, chained(adapter, completed, (vm(target),)),
        )
    before = inventory(adapter)
    same_id = inventory(
        adapter, (vm(target),), sequence=2, previous=before.digest,
        observation_id=before.observation_id,
    )
    with pytest.raises(ValueError, match="chained"):
        reconcile_parallels_plan(plan, before, same_id)


def test_inventory_bounds_unknown_fields_and_no_effect_surface():
    adapter, target = identities()
    with pytest.raises(ValueError, match="bounded"):
        inventory(adapter, (vm(target),) * 129)
    value = inventory(adapter).to_dict()
    value["credential"] = "secret"
    with pytest.raises(ValueError, match="exactly"):
        ParallelsInventoryObservationV1.from_dict(value)
    source = (
        Path(__file__).parents[1] / "src/rekit_factory/parallels_reconciliation.py"
    ).read_text()
    assert "import subprocess" not in source
    assert "subprocess." not in source
    assert "Popen(" not in source


def test_reconciled_success_envelope_is_accepted_by_durable_effect_boundary(tmp_path):
    adapter_identity, target = identities()
    plan = build_parallels_command_plan("clone-op", "clone", adapter_identity, target)
    before = inventory(adapter_identity)
    completed = reconcile_parallels_plan(
        plan, before, chained(adapter_identity, before, (vm(target),)),
    )

    def runner(operation_id, plan_sha256, argv):
        return ParallelsRunnerCaptureV1(
            operation_id, plan_sha256, argv, 0, completed.success_envelope(plan), b"",
        )

    with DurableParallelsEffectAdapter(tmp_path / "effects.sqlite3", runner) as effects:
        result = effects.apply(plan)
    assert result.outcome == "succeeded"
    assert result.provider_vm_id == PROVIDER_VM
