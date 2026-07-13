from __future__ import annotations

from dataclasses import replace
import json

import pytest

from rekit_factory.campaign_contracts import (
    CampaignChangeRequest,
    CampaignCheckpoint,
    CampaignContract,
    CheckpointSource,
    CompletionCriteria,
    ComponentVersion,
    EpochContract,
    OperatorPolicy,
    ResourceBudget,
    ResourceLimit,
    ResourceUsage,
    ScopeBinding,
    TerminalOutcome,
)
from rekit_factory.campaign_persistence import (
    CampaignPersistence,
    CampaignPersistenceError,
    CampaignWriteInterrupted,
)


DIGEST = "a" * 64


def limit(value, unit):
    return ResourceLimit(value, unit)


def budget(*, work=8, cost=100):
    return ResourceBudget(
        limit(work, "items"), limit(2, "workers"), limit(2, "attempts"),
        limit(10_000, "tokens"), limit(4_000, "tokens"),
        limit(cost, "cost-units"), limit(3600, "seconds"), limit(10, "calls"),
        limit(0, "calls"), limit(1_000_000, "bytes"),
    )


def contract(project="project-a"):
    return CampaignContract(
        project, "Prove the bounded fixture", ScopeBinding("scope-a", 1, DIGEST),
        budget(), budget(work=32, cost=400),
        CompletionCriteria(8000, 1, 1, ("artifact-proof",)),
        OperatorPolicy(risk_threshold=60),
        (ComponentVersion("factory", "0.2.0", DIGEST),),
    )


def checkpoint(parent, epoch, sequence=1, cost=10):
    return CampaignCheckpoint(
        parent.campaign_id, epoch.epoch_id, sequence,
        (CheckpointSource("factory-ledger", sequence, DIGEST),),
        ResourceUsage(work_items=sequence, cost_units=cost),
    )


class FailAt:
    def __init__(self, boundary):
        self.boundary = boundary

    def __call__(self, boundary):
        if boundary == self.boundary:
            raise CampaignWriteInterrupted(boundary)


def authority_dump(store):
    tables = [row[0] for row in store.conn.execute(
        "select name from sqlite_master where type='table' and name like 'factory_campaign%' "
        "order by name"
    )]
    return tuple((table, tuple(tuple(row) for row in store.conn.execute(
        f"select * from {table} order by rowid"
    ))) for table in tables)


def setup_leased(store, parent=None, suffix="a"):
    parent = parent or contract()
    store.create_campaign(parent, operation_id=f"create-{suffix}")
    store.transition_campaign(parent.campaign_id, "running",
                              authority="factory-scheduler", operation_id=f"run-{suffix}")
    epoch = EpochContract(parent.campaign_id, 1, (f"work-{suffix}",), budget())
    store.publish_epoch(epoch, operation_id=f"publish-{suffix}")
    store.acquire_epoch_lease(parent.campaign_id, epoch.epoch_id, f"worker-{suffix}",
                              operation_id=f"lease-{suffix}")
    return parent, epoch


@pytest.mark.parametrize("boundary", ["event-appended", "campaign-projected"])
def test_campaign_creation_crash_is_wholly_absent_and_exact_retry_succeeds(tmp_path, boundary):
    path = tmp_path / "factory.db"
    parent = contract()
    store = CampaignPersistence(path)
    with pytest.raises(CampaignWriteInterrupted):
        store.create_campaign(parent, operation_id="create", failure_injector=FailAt(boundary))
    store.close()

    restarted = CampaignPersistence(path)
    assert restarted.conn.execute("select count(*) from factory_campaign_events").fetchone()[0] == 0
    assert restarted.conn.execute("select count(*) from factory_campaigns").fetchone()[0] == 0
    projection = restarted.create_campaign(parent, operation_id="create")
    assert projection.status == "requested"
    assert restarted.create_campaign(parent, operation_id="create") == projection


@pytest.mark.parametrize("operation,boundary", [
    ("publish", "event-appended"), ("publish", "epoch-projected"),
    ("lease", "event-appended"), ("lease", "lease-projected"),
    ("checkpoint", "event-appended"), ("checkpoint", "checkpoint-projected"),
    ("terminal", "event-appended"), ("terminal", "terminal-projected"),
])
def test_epoch_write_boundaries_restart_exactly_once(tmp_path, operation, boundary):
    path = tmp_path / "factory.db"
    parent = contract()
    store = CampaignPersistence(path)
    store.create_campaign(parent, operation_id="create")
    store.transition_campaign(parent.campaign_id, "running",
                              authority="factory-scheduler", operation_id="run")
    epoch = EpochContract(parent.campaign_id, 1, ("work-a",), budget())
    cp = checkpoint(parent, epoch)

    if operation == "publish":
        invoke = lambda s, fault=None: s.publish_epoch(
            epoch, operation_id="publish", failure_injector=fault)
    elif operation == "lease":
        store.publish_epoch(epoch, operation_id="publish")
        invoke = lambda s, fault=None: s.acquire_epoch_lease(
            parent.campaign_id, epoch.epoch_id, "worker-a", operation_id="lease",
            failure_injector=fault)
    elif operation == "checkpoint":
        store.publish_epoch(epoch, operation_id="publish")
        store.acquire_epoch_lease(parent.campaign_id, epoch.epoch_id, "worker-a",
                                  operation_id="lease")
        invoke = lambda s, fault=None: s.record_checkpoint(
            cp, operation_id="checkpoint", failure_injector=fault)
    else:
        store.publish_epoch(epoch, operation_id="publish")
        store.acquire_epoch_lease(parent.campaign_id, epoch.epoch_id, "worker-a",
                                  operation_id="lease")
        store.record_checkpoint(cp, operation_id="checkpoint")
        outcome = TerminalOutcome(parent.campaign_id, "completed", "proof-complete",
                                  ("artifact-proof",), cp.checkpoint_id)
        invoke = lambda s, fault=None: s.transition_campaign(
            parent.campaign_id, "completed", authority="factory-scheduler",
            operation_id="terminal", terminal=outcome, failure_injector=fault)

    before = authority_dump(store)
    with pytest.raises(CampaignWriteInterrupted):
        invoke(store, FailAt(boundary))
    store.close()
    restarted = CampaignPersistence(path)
    assert authority_dump(restarted) == before
    assert restarted.rebuild_projection(parent.campaign_id).matches_live
    invoke(restarted)
    rebuilt = restarted.rebuild_projection(parent.campaign_id)
    assert rebuilt.matches_live and not rebuilt.degraded
    operations = restarted.conn.execute(
        "select operation_id from factory_campaign_events where campaign_id=?",
        (parent.campaign_id,),
    ).fetchall()
    assert len({row[0] for row in operations}) == len(operations)


@pytest.mark.parametrize("operation,boundary", [
    ("decision", "event-appended"), ("decision", "decision-projected"),
    ("recover", "event-appended"), ("recover", "lease-projected"),
])
def test_decision_and_recovery_write_boundaries_are_atomic(tmp_path, operation, boundary):
    path = tmp_path / "factory.db"
    store = CampaignPersistence(path)
    parent, epoch = setup_leased(store)
    proposed = replace(parent, scope=ScopeBinding("scope-a", 2, "b" * 64))
    request = CampaignChangeRequest(parent.campaign_id, proposed, "Expand exact scope")
    if operation == "decision":
        invoke = lambda s, fault=None: s.record_operator_decision(
            parent.campaign_id, request, approved=True, decided_by="operator-a",
            operation_id="decision", failure_injector=fault)
    else:
        invoke = lambda s, fault=None: s.recover(
            parent.campaign_id, operation_id="recover", failure_injector=fault)
    before = authority_dump(store)
    with pytest.raises(CampaignWriteInterrupted):
        invoke(store, FailAt(boundary))
    store.close()
    restarted = CampaignPersistence(path)
    assert authority_dump(restarted) == before
    invoke(restarted)
    assert restarted.rebuild_projection(parent.campaign_id).matches_live


def test_idempotency_conflicts_fail_closed_for_operations_and_decisions(tmp_path):
    store = CampaignPersistence(tmp_path / "factory.db")
    parent = contract()
    store.create_campaign(parent, operation_id="create")
    with pytest.raises(CampaignPersistenceError, match="conflicting reuse"):
        store.transition_campaign(parent.campaign_id, "running",
                                  authority="factory-scheduler", operation_id="create")

    proposed = replace(parent, scope=ScopeBinding("scope-a", 2, "b" * 64))
    request = CampaignChangeRequest(parent.campaign_id, proposed, "Expand exact scope")
    decision = store.record_operator_decision(
        parent.campaign_id, request, approved=True, decided_by="operator-a",
        operation_id="decision-a",
    )
    assert store.record_operator_decision(
        parent.campaign_id, request, approved=True, decided_by="operator-a",
        operation_id="decision-a",
    ) == decision
    with pytest.raises(CampaignPersistenceError, match="conflicting reuse"):
        store.record_operator_decision(
            parent.campaign_id, request, approved=False, decided_by="operator-a",
            operation_id="decision-a",
        )
    with pytest.raises(CampaignPersistenceError, match="exactly boolean"):
        store.record_operator_decision(
            parent.campaign_id, request, approved=1, decided_by="operator-a",
            operation_id="decision-b",
        )
    with pytest.raises(CampaignPersistenceError, match="bounded stable identifier"):
        store.record_operator_decision(
            parent.campaign_id, request, approved=True, decided_by="operator with spaces",
            operation_id="decision-b",
        )


def test_epoch_publication_leases_and_recovery_require_live_campaign_authority(tmp_path):
    store = CampaignPersistence(tmp_path / "factory.db")
    parent = contract()
    store.create_campaign(parent, operation_id="create")
    epoch = EpochContract(parent.campaign_id, 1, ("work-a",), budget())
    with pytest.raises(CampaignPersistenceError, match="running campaign"):
        store.publish_epoch(epoch, operation_id="publish-requested")
    with pytest.raises(CampaignPersistenceError, match="running or waiting"):
        store.recover(parent.campaign_id, operation_id="recover-requested")

    store.transition_campaign(parent.campaign_id, "running",
                              authority="factory-scheduler", operation_id="run")
    store.publish_epoch(epoch, operation_id="publish")
    with pytest.raises(CampaignPersistenceError, match="bounded stable identifier"):
        store.acquire_epoch_lease(parent.campaign_id, epoch.epoch_id, "bad owner",
                                  operation_id="lease-bad-owner")


def test_checkpoint_delta_must_fit_exact_epoch_budget(tmp_path):
    store = CampaignPersistence(tmp_path / "factory.db")
    parent = contract()
    store.create_campaign(parent, operation_id="create")
    store.transition_campaign(parent.campaign_id, "running",
                              authority="factory-scheduler", operation_id="run")
    small = replace(budget(), cost_units=limit(5, "cost-units"))
    epoch = EpochContract(parent.campaign_id, 1, ("work-a",), small)
    store.publish_epoch(epoch, operation_id="publish")
    store.acquire_epoch_lease(parent.campaign_id, epoch.epoch_id, "worker-a",
                              operation_id="lease")
    too_large = checkpoint(parent, epoch, cost=6)
    with pytest.raises(CampaignPersistenceError, match="delta exceeds epoch budget"):
        store.record_checkpoint(too_large, operation_id="checkpoint")
    assert store.campaign(parent.campaign_id).latest_checkpoint_id is None
    assert store.conn.execute(
        "select count(*) from factory_campaign_events where operation_id='checkpoint'"
    ).fetchone()[0] == 0


def test_operator_can_stop_before_first_epoch_without_inventing_checkpoint(tmp_path):
    store = CampaignPersistence(tmp_path / "factory.db")
    parent = contract()
    store.create_campaign(parent, operation_id="create")
    outcome = TerminalOutcome(
        parent.campaign_id, "stopped", "operator-before-start", ("decision-stop",), None,
    )
    projection = store.transition_campaign(
        parent.campaign_id, "stopped", authority="operator",
        operation_id="stop-before-start", terminal=outcome,
    )
    assert projection.status == "stopped"
    assert projection.latest_checkpoint_id is None
    assert store.rebuild_projection(parent.campaign_id).matches_live


def test_restart_never_inferrs_completion_and_blocks_orphaned_lease_once(tmp_path):
    path = tmp_path / "factory.db"
    store = CampaignPersistence(path)
    parent, epoch = setup_leased(store)
    store.close()

    restarted = CampaignPersistence(path)
    recovered = restarted.recover(parent.campaign_id, operation_id="recover-boot-1")
    assert recovered.status == "waiting"
    assert recovered.latest_checkpoint_id is None
    lease = restarted.conn.execute(
        "select status from factory_campaign_leases where epoch_id=?", (epoch.epoch_id,),
    ).fetchone()
    assert lease["status"] == "recovery-required"
    assert restarted.recover(parent.campaign_id, operation_id="recover-boot-1") == recovered
    assert restarted.conn.execute(
        "select count(*) from factory_campaign_events where operation_id='recover-boot-1'"
    ).fetchone()[0] == 1


def test_verified_orphan_lease_reconciliation_is_atomic_idempotent_and_rebuildable(tmp_path):
    store = CampaignPersistence(tmp_path / "factory.db")
    parent, epoch = setup_leased(store)
    store.recover(parent.campaign_id, operation_id="recover")

    def interrupt(boundary):
        if boundary == "lease-projected":
            raise CampaignWriteInterrupted(boundary)

    with pytest.raises(CampaignWriteInterrupted):
        store.reconcile_epoch_lease(
            parent.campaign_id, epoch.epoch_id, "worker-a",
            operation_id="reconcile", failure_injector=interrupt,
        )
    assert store.campaign(parent.campaign_id).status == "waiting"
    lease_id = store.reconcile_epoch_lease(
        parent.campaign_id, epoch.epoch_id, "worker-a", operation_id="reconcile",
    )
    assert lease_id.startswith("campaign-lease-")
    assert store.reconcile_epoch_lease(
        parent.campaign_id, epoch.epoch_id, "worker-a", operation_id="reconcile",
    ) == lease_id
    assert store.campaign(parent.campaign_id).status == "running"
    assert store.rebuild_projection(parent.campaign_id).matches_live


def test_concurrent_campaigns_cannot_cross_read_or_mutate_authority(tmp_path):
    store = CampaignPersistence(tmp_path / "factory.db")
    first, first_epoch = setup_leased(store, contract("project-a"), "a")
    second, second_epoch = setup_leased(store, contract("project-b"), "b")
    first_before = store.campaign(first.campaign_id)

    with pytest.raises(CampaignPersistenceError, match="does not belong"):
        store.acquire_epoch_lease(first.campaign_id, second_epoch.epoch_id, "intruder",
                                  operation_id="cross-lease")
    alien = CampaignCheckpoint(first.campaign_id, second_epoch.epoch_id, 1,
                               (CheckpointSource("factory-ledger", 1, DIGEST),),
                               ResourceUsage(work_items=1))
    with pytest.raises(CampaignPersistenceError, match="does not belong"):
        store.record_checkpoint(alien, operation_id="cross-checkpoint")
    store.record_checkpoint(checkpoint(second, second_epoch), operation_id="checkpoint-b")
    assert store.campaign(first.campaign_id) == first_before
    assert store.campaign(second.campaign_id).latest_checkpoint_id is not None
    assert store.conn.execute(
        "select count(*) from factory_campaign_events where campaign_id=?", (first.campaign_id,),
    ).fetchone()[0] == 4


def test_rebuild_detects_gap_tamper_dangling_reference_and_live_drift(tmp_path):
    store = CampaignPersistence(tmp_path / "factory.db")
    parent, epoch = setup_leased(store)
    cp = checkpoint(parent, epoch)
    store.record_checkpoint(cp, operation_id="checkpoint-a")
    assert store.rebuild_projection(parent.campaign_id).matches_live

    store.conn.execute(
        "update factory_campaign_events set payload_json=? where campaign_id=? "
        "and operation_id='checkpoint-a'",
        (json.dumps({"checkpointId": "dangling"}), parent.campaign_id),
    )
    store.conn.execute(
        "update factory_campaigns set latest_checkpoint_id='stale' where campaign_id=?",
        (parent.campaign_id,),
    )
    store.conn.execute(
        "delete from factory_campaign_events where campaign_id=? and operation_id='lease-a'",
        (parent.campaign_id,),
    )
    store.conn.commit()
    rebuilt = store.rebuild_projection(parent.campaign_id)
    assert rebuilt.degraded and not rebuilt.matches_live
    assert any("corrupt hash chain" in item for item in rebuilt.problems)
    assert any("history gap" in item for item in rebuilt.problems)
    assert any("impossible event" in item for item in rebuilt.problems)
    assert any("differs" in item for item in rebuilt.problems)
