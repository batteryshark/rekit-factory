from __future__ import annotations

import json
import os

import pytest

from rekit_factory.range_supervisor import RangeCleanupSupervisor, RangeSupervisorStore
from rekit_factory.ranges import (
    DeterministicFakeRangeAdapter,
    InjectedRangeFailure,
    RangeStateError,
    benign_two_node_fixture,
)


def _ready(*, range_id="range-supervised"):
    template, spec = benign_two_node_fixture(range_id=range_id)
    adapter = DeterministicFakeRangeAdapter(now=spec.requested_at)
    adapter.provision(f"provision-{range_id}", template, spec)
    return adapter, spec


def _supervisor(tmp_path, spec):
    store = RangeSupervisorStore(tmp_path / "supervisor")
    supervisor = RangeCleanupSupervisor(store)
    supervisor.register(spec.range_id, spec.expires_at)
    return store, supervisor


def test_before_expiry_is_a_durable_noop(tmp_path):
    adapter, spec = _ready()
    store, supervisor = _supervisor(tmp_path, spec)

    before = store.path.read_bytes()
    snapshot = supervisor.reconcile(adapter, spec.requested_at)

    assert store.path.read_bytes() == before
    assert adapter.state(spec.range_id).status == "ready"
    assert snapshot["records"][spec.range_id]["audit"] == []


def test_overdue_range_is_expired_then_destroyed_with_audited_effects(tmp_path):
    adapter, spec = _ready()
    _, supervisor = _supervisor(tmp_path, spec)
    adapter.advance(3600)

    snapshot = supervisor.reconcile(adapter, spec.expires_at)
    record = snapshot["records"][spec.range_id]

    assert adapter.state(spec.range_id).status == "destroyed"
    assert record["terminal"] is True
    assert [item["kind"] for item in record["audit"]] == ["expire", "destroy"]
    assert [item["status"] for item in record["audit"]] == ["expired", "destroyed"]
    assert record["pending"] is None


def test_explicit_cleanup_and_failed_lease_converge_without_waiting_for_expiry(tmp_path):
    adapter, spec = _ready()
    _, supervisor = _supervisor(tmp_path, spec)
    supervisor.request_cleanup(spec.range_id, "operator cancellation")

    supervisor.reconcile(adapter, spec.requested_at)

    assert adapter.state(spec.range_id).status == "destroyed"
    assert adapter.state(spec.range_id).terminal_reason == "operator cancellation"

    failed_adapter, failed_spec = _ready(range_id="range-failed-cleanup")
    failed_adapter.inject_failure("resetting")
    with pytest.raises(InjectedRangeFailure):
        failed_adapter.reset("fail-reset", failed_spec.range_id)
    _, failed_supervisor = _supervisor(tmp_path / "failed", failed_spec)
    failed_supervisor.reconcile(failed_adapter, failed_spec.requested_at)
    assert failed_adapter.state(failed_spec.range_id).status == "destroyed"


def test_retryable_cleanup_failure_is_durable_and_next_attempt_has_distinct_id(tmp_path):
    adapter, spec = _ready()
    _, supervisor = _supervisor(tmp_path, spec)
    supervisor.request_cleanup(spec.range_id, "retry fixture")
    adapter.inject_failure("destroyed")

    first = supervisor.reconcile(adapter, spec.requested_at)["records"][spec.range_id]
    assert first["last_error"]["retryable"] is True
    assert first["last_error"]["attempt"] == 1
    assert first["next_attempt"] == 2
    assert first["pending"] is None

    second = supervisor.reconcile(adapter, spec.requested_at)["records"][spec.range_id]
    assert second["terminal"] is True
    assert second["audit"][0]["attempt"] == 2
    operations = json.loads(adapter.checkpoint())["operations"]
    cleanup_ids = sorted(key for key in operations if key.startswith("range-supervisor:destroy:"))
    assert len(cleanup_ids) == 2
    assert cleanup_ids[0].endswith(":1")
    assert cleanup_ids[1].endswith(":2")


def test_crash_after_adapter_success_replays_exact_pending_operation(tmp_path, monkeypatch):
    adapter, spec = _ready()
    store, supervisor = _supervisor(tmp_path, spec)
    supervisor.request_cleanup(spec.range_id, "ambiguous crash fixture")
    real_save = store.save
    calls = 0

    def fail_ack(value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected crash before acknowledgement")
        real_save(value)

    monkeypatch.setattr(store, "save", fail_ack)
    with pytest.raises(OSError, match="before acknowledgement"):
        supervisor.reconcile(adapter, spec.requested_at)
    assert adapter.state(spec.range_id).status == "destroyed"
    durable = RangeSupervisorStore(store.root).load()["records"][spec.range_id]
    pending_id = durable["pending"]["operation_id"]

    restarted_adapter = DeterministicFakeRangeAdapter.from_checkpoint(adapter.checkpoint())
    restarted = RangeCleanupSupervisor(RangeSupervisorStore(store.root))
    finished = restarted.reconcile(restarted_adapter, spec.requested_at)["records"][spec.range_id]

    assert finished["terminal"] is True
    assert finished["audit"] == [{
        "kind": "destroy", "operation_id": pending_id, "attempt": 1,
        "status": "destroyed",
    }]
    operations = json.loads(restarted_adapter.checkpoint())["operations"]
    assert list(key for key in operations if key == pending_id) == [pending_id]


def test_registration_and_cleanup_identity_conflicts_fail_closed(tmp_path):
    _, spec = _ready()
    _, supervisor = _supervisor(tmp_path, spec)

    supervisor.register(spec.range_id, spec.expires_at)
    with pytest.raises(ValueError, match="another expiry"):
        supervisor.register(spec.range_id, "2026-07-14T12:00:00Z")
    supervisor.request_cleanup(spec.range_id, "first reason")
    with pytest.raises(ValueError, match="already bound"):
        supervisor.request_cleanup(spec.range_id, "different reason")
    with pytest.raises(KeyError, match="not registered"):
        supervisor.request_cleanup("foreign-range", "not permitted")


def test_nonretryable_adapter_failure_is_durably_blocked(tmp_path):
    adapter, spec = _ready()
    _, supervisor = _supervisor(tmp_path, spec)
    supervisor.request_cleanup(spec.range_id, "fixture")

    class RejectingAdapter:
        def state(self, range_id):
            return adapter.state(range_id)

        def destroy(self, operation_id, range_id, *, reason):
            raise RangeStateError("provider rejected cleanup")

        def expire(self, operation_id, range_id):
            raise AssertionError

    record = supervisor.reconcile(RejectingAdapter(), spec.requested_at)["records"][spec.range_id]
    assert record["blocked"] is True
    assert record["last_error"]["retryable"] is False
    assert record["next_attempt"] == 1


def test_store_rejects_duplicate_keys_invalid_utf8_and_oversized_state(tmp_path):
    store = RangeSupervisorStore(tmp_path / "strict", max_bytes=512)
    store.path.write_text('{"schema_version":1,"schema_version":1,"records":{}}\n')
    with pytest.raises(ValueError, match="duplicate key"):
        store.load()

    store.path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8 JSON"):
        store.load()

    store.path.write_bytes(b" " * 513)
    with pytest.raises(ValueError, match="size limit"):
        store.load()


def test_store_rejects_symlink_root_and_state(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="root must not be a symlink"):
        RangeSupervisorStore(root_link)

    store = RangeSupervisorStore(tmp_path / "safe")
    target = tmp_path / "outside.json"
    target.write_text('{}')
    store.path.symlink_to(target)
    with pytest.raises(ValueError, match="must not be a symlink"):
        store.load()


def test_failed_atomic_replace_preserves_last_good_checkpoint(tmp_path, monkeypatch):
    adapter, spec = _ready()
    store, supervisor = _supervisor(tmp_path, spec)
    before = store.path.read_bytes()
    real_replace = os.replace

    def fail_replace(*args, **kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        supervisor.request_cleanup(spec.range_id, "must not partially persist")
    monkeypatch.setattr(os, "replace", real_replace)

    assert store.path.read_bytes() == before
    restored = RangeCleanupSupervisor(RangeSupervisorStore(store.root)).snapshot()
    assert restored["records"][spec.range_id]["cleanup_reason"] is None
