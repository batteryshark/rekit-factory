from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from rekit_factory.parallels_effects import (
    DurableParallelsEffectAdapter,
    InjectedParallelsAdapterCrash,
    ParallelsEffectConflictError,
    ParallelsEffectIntegrityError,
    ParallelsRunnerCaptureV1,
)
import rekit_factory.parallels_effects as effects_module
from rekit_factory.parallels_plan import (
    ParallelsAdapterIdentityV1,
    ParallelsVmLifecycleIdentityV1,
    build_parallels_command_plan,
)


SOURCE_VM = "{2c2e0cd1-5019-4832-9e16-b5b218d6131a}"
SOURCE_SNAPSHOT = "{074287d1-6918-4a01-b3cd-17095f97d76b}"
PROVIDER_VM = "{d4080cf3-d729-488a-ae28-ee0564d6ca91}"


def _plan(operation="clone-op"):
    adapter = ParallelsAdapterIdentityV1(
        "26.4.0-57513", "a" * 64, "b" * 64, SOURCE_VM, SOURCE_SNAPSHOT, "c" * 64,
    )
    target = ParallelsVmLifecycleIdentityV1(
        "range-proof", "analysis-a", 1, "d" * 64, "e" * 64, adapter.digest,
    )
    return build_parallels_command_plan(operation, "clone", adapter, target)


def _envelope(plan, **changes):
    value = {
        "schema_version": 1, "operation_id": plan.operation_id,
        "plan_sha256": plan.digest, "kind": plan.kind,
        "provider_vm_id": PROVIDER_VM, "snapshot_id": None,
    }
    value.update(changes)
    return (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()


class IdempotentRunner:
    def __init__(self):
        self.calls = 0
        self.effects = {}

    def __call__(self, operation_id, plan_sha256, argv):
        self.calls += 1
        key = (operation_id, plan_sha256)
        capture = self.effects.get(key)
        if capture is None:
            plan = self.plan
            capture = ParallelsRunnerCaptureV1(
                operation_id, plan_sha256, argv, 0, _envelope(plan), b"",
            )
            self.effects[key] = capture
        return capture


def test_effect_is_journaled_and_completed_result_replays_without_runner(tmp_path):
    plan, runner = _plan(), IdempotentRunner()
    runner.plan = plan
    path = tmp_path / "effects.json"
    first = DurableParallelsEffectAdapter(path, runner).apply(plan)
    second = DurableParallelsEffectAdapter(path, runner).apply(plan)
    assert first == second
    assert first.provider_vm_id == PROVIDER_VM
    assert first.plan_sha256 == plan.digest
    assert first.digest == second.digest
    assert runner.calls == 1
    assert len(runner.effects) == 1


def test_crash_after_provider_effect_converges_through_idempotent_runner(tmp_path):
    plan, runner = _plan(), IdempotentRunner()
    runner.plan = plan
    path = tmp_path / "effects.json"
    fired = False

    def crash(boundary):
        nonlocal fired
        if boundary == "after-effect-before-result" and not fired:
            fired = True
            raise InjectedParallelsAdapterCrash(boundary)

    with pytest.raises(InjectedParallelsAdapterCrash):
        DurableParallelsEffectAdapter(path, runner, crash).apply(plan)
    result = DurableParallelsEffectAdapter(path, runner).apply(plan)
    assert result.outcome == "succeeded"
    assert runner.calls == 2
    assert len(runner.effects) == 1


def test_operation_id_cannot_be_rebound_to_a_different_plan(tmp_path):
    plan, runner = _plan(), IdempotentRunner()
    runner.plan = plan
    path = tmp_path / "effects.json"
    DurableParallelsEffectAdapter(path, runner).apply(plan)
    changed_target = replace(plan.target, generation=2)
    changed = build_parallels_command_plan(
        plan.operation_id, "clone", plan.adapter, changed_target,
    )
    with pytest.raises(ParallelsEffectConflictError):
        DurableParallelsEffectAdapter(path, runner).apply(changed)


@pytest.mark.parametrize("mutation", [
    lambda value: {**value, "operation_id": "other-op"},
    lambda value: {**value, "plan_sha256": "f" * 64},
    lambda value: {**value, "kind": "delete"},
    lambda value: {**value, "provider_vm_id": "not-a-uuid"},
    lambda value: {**value, "extra": True},
])
def test_result_parser_fails_closed_on_unbound_or_unknown_data(tmp_path, mutation):
    plan = _plan()
    value = json.loads(_envelope(plan))
    stdout = (json.dumps(mutation(value), separators=(",", ":"), sort_keys=True) + "\n").encode()

    def runner(operation_id, plan_sha256, argv):
        return ParallelsRunnerCaptureV1(operation_id, plan_sha256, argv, 0, stdout, b"")

    with pytest.raises(ValueError):
        DurableParallelsEffectAdapter(tmp_path / "effects.json", runner).apply(plan)


def test_failed_capture_requires_canonical_error_and_persists_terminal_result(tmp_path):
    plan = _plan()
    error = {
        "schema_version": 1, "operation_id": plan.operation_id,
        "plan_sha256": plan.digest, "kind": plan.kind, "error_code": "provider-busy",
    }
    stderr = (json.dumps(error, separators=(",", ":"), sort_keys=True) + "\n").encode()
    calls = 0

    def runner(operation_id, plan_sha256, argv):
        nonlocal calls
        calls += 1
        return ParallelsRunnerCaptureV1(operation_id, plan_sha256, argv, 75, b"", stderr)

    path = tmp_path / "effects.json"
    result = DurableParallelsEffectAdapter(path, runner).apply(plan)
    assert result.outcome == "failed"
    assert result.error_code == "provider-busy"
    assert DurableParallelsEffectAdapter(path, runner).apply(plan) == result
    assert calls == 1


def test_capture_argv_mismatch_and_corrupt_journal_are_rejected(tmp_path):
    plan = _plan()

    def runner(operation_id, plan_sha256, argv):
        return ParallelsRunnerCaptureV1(
            operation_id, plan_sha256, argv + ("--force",), 0, _envelope(plan), b"",
        )

    path = tmp_path / "effects.json"
    with pytest.raises(ValueError, match="exact operation, plan, and argv"):
        DurableParallelsEffectAdapter(path, runner).apply(plan)
    path.write_text("not a sqlite database")
    with pytest.raises(ParallelsEffectIntegrityError, match="SQLite database"):
        DurableParallelsEffectAdapter(path, runner)


def test_module_has_no_subprocess_or_real_provider_runner():
    source = (Path(__file__).parents[1] / "src/rekit_factory/parallels_effects.py").read_text()
    assert "import subprocess" not in source
    assert "subprocess." not in source
    assert "Popen(" not in source


def test_journal_rejects_symlink_database_and_sidecars(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"")
    path = tmp_path / "effects.sqlite3"
    path.symlink_to(target)
    with pytest.raises(ParallelsEffectIntegrityError, match="symlink"):
        DurableParallelsEffectAdapter(path, lambda *_: None)

    path.unlink()
    plan, runner = _plan(), IdempotentRunner()
    runner.plan = plan
    adapter = DurableParallelsEffectAdapter(path, runner)
    os.symlink(target, str(path) + "-wal")
    with pytest.raises(ParallelsEffectIntegrityError, match="sidecar"):
        adapter.apply(plan)
    adapter.close()


def test_journal_rejects_database_inode_and_parent_replacement(tmp_path):
    plan, runner = _plan(), IdempotentRunner()
    runner.plan = plan
    directory = tmp_path / "journal-parent"
    path = directory / "effects.sqlite3"
    adapter = DurableParallelsEffectAdapter(path, runner)
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)
    with pytest.raises(ParallelsEffectIntegrityError, match="inode changed"):
        adapter.apply(plan)
    adapter.close()


def test_open_adapter_rechecks_exact_schema_before_each_operation(tmp_path):
    plan, runner = _plan(), IdempotentRunner()
    runner.plan = plan
    path = tmp_path / "effects.sqlite3"
    adapter = DurableParallelsEffectAdapter(path, runner)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE injected(value TEXT)")
    with pytest.raises(ParallelsEffectIntegrityError, match="table definition"):
        adapter.apply(plan)
    assert runner.calls == 0
    adapter.close()

    other_path = tmp_path / "other-parent" / "effects.sqlite3"
    adapter = DurableParallelsEffectAdapter(other_path, runner)
    old_parent = tmp_path / "old-parent"
    other_path.parent.rename(old_parent)
    other_path.parent.mkdir()
    with pytest.raises(ParallelsEffectIntegrityError, match="parent identity changed"):
        adapter.apply(plan)
    adapter.close()


def test_closed_adapter_fails_closed_and_context_manager_closes(tmp_path):
    plan, runner = _plan(), IdempotentRunner()
    runner.plan = plan
    with DurableParallelsEffectAdapter(tmp_path / "effects.sqlite3", runner) as adapter:
        assert adapter.apply(plan).outcome == "succeeded"
    adapter.close()
    with pytest.raises(ParallelsEffectIntegrityError, match="closed"):
        adapter.apply(plan)


def test_separate_concurrent_adapters_merge_different_operations(tmp_path):
    first, second = _plan("clone-a"), _plan("clone-b")
    plans = {first.digest: first, second.digest: second}
    calls = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def runner(operation_id, plan_sha256, argv):
        plan = plans[plan_sha256]
        with lock:
            calls.append(operation_id)
        barrier.wait(timeout=5)
        return ParallelsRunnerCaptureV1(
            operation_id, plan_sha256, argv, 0, _envelope(plan), b"",
        )

    path = tmp_path / "effects.sqlite3"
    left = DurableParallelsEffectAdapter(path, runner)
    right = DurableParallelsEffectAdapter(path, runner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda pair: pair[0].apply(pair[1]), ((left, first), (right, second))))
    left.close()
    right.close()
    assert {item.operation_id for item in results} == {"clone-a", "clone-b"}
    assert set(calls) == {"clone-a", "clone-b"}
    replay = DurableParallelsEffectAdapter(path, lambda *_: pytest.fail("unexpected runner"))
    assert replay.apply(first).operation_id == "clone-a"
    assert replay.apply(second).operation_id == "clone-b"
    replay.close()


def test_journal_enforces_file_and_record_count_bounds(tmp_path, monkeypatch):
    oversized = tmp_path / "oversized.sqlite3"
    with oversized.open("wb") as stream:
        stream.truncate(effects_module.MAX_JOURNAL_BYTES + 1)
    with pytest.raises(ParallelsEffectIntegrityError, match="size limit"):
        DurableParallelsEffectAdapter(oversized, lambda *_: None)

    first, runner = _plan("clone-a"), IdempotentRunner()
    runner.plan = first
    path = tmp_path / "bounded.sqlite3"
    adapter = DurableParallelsEffectAdapter(path, runner)
    adapter.apply(first)
    monkeypatch.setattr(effects_module, "MAX_RECORDS", 1)
    with pytest.raises(ParallelsEffectIntegrityError, match="record limit"):
        adapter.apply(_plan("clone-b"))
    adapter.close()
