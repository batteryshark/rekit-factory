"""E2.3 — orchestrator-level fan-out: N invocations concurrent, one ledger, no races.

Proves the acceptance of the fan-out slice:

- ``fan_out`` runs N independent brain invocations **concurrently** (bounded by
  ``max_concurrency``) and folds **all** N results into one temp-``$REKIT_HOME``
  project ledger — every finding lands, and the ledger is internally consistent
  (folded event count == on-disk ``ledger.jsonl`` event count) and lossless on
  reload (replay reconstructs identical state → proves no write races);
- a worker that raises is reported as ``failed`` without losing the others;
- ``max_concurrency`` is respected — observed peak in-flight workers ≤ the cap.

Plain-python style (runnable via ``python tests/test_fanout.py``) and
pytest-compatible. Uses a temp ``REKIT_HOME`` via the env var so nothing touches
``~/.rekit``; no network (a threadsafe scripted mock adapter is the brain).
"""

import contextlib
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.harness.base import HarnessAdapter, HarnessResult  # noqa: E402
from rekit.ledger import open_project  # noqa: E402
from rekit.ledger.ledger import read_events  # noqa: E402
from rekit.loop import fan_out  # noqa: E402


@contextlib.contextmanager
def temp_home():
    """A temp ``REKIT_HOME`` (restored afterwards) + a temp workspace. Yields
    ``(home_path, work_path)``."""
    saved = os.environ.get("REKIT_HOME")
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as work:
        os.environ["REKIT_HOME"] = home
        try:
            yield Path(home), Path(work)
        finally:
            if saved is None:
                os.environ.pop("REKIT_HOME", None)
            else:
                os.environ["REKIT_HOME"] = saved


def _make_target(work: Path) -> Path:
    """A small target tree to open a project against."""
    target = work / "app"
    target.mkdir()
    (target / "main.py").write_text("print('hello')\n", encoding="utf-8")
    return target


class ConcurrentMock(HarnessAdapter):
    """A thread-safe scripted adapter that also measures real concurrency.

    ``responses`` maps a per-item ``user_input`` to the reply text. Each invoke
    increments a live-worker counter (under a lock), records the running peak,
    sleeps briefly so workers genuinely overlap, then returns. ``fail_on`` names
    inputs whose worker should raise, to exercise failure isolation.
    """

    name = "concurrent-mock"

    def __init__(self, responses, *, fail_on=(), hold: float = 0.02):
        self._responses = dict(responses)
        self._fail_on = set(fail_on)
        self._hold = hold
        self._lock = threading.Lock()
        self.live = 0
        self.peak = 0
        self.calls = []  # (system_prompt, user_input, tier) per invoke, thread-safe append

    def invoke(self, system_prompt, user_input, *, tools=None, context=None, tier="cheap"):
        with self._lock:
            self.live += 1
            if self.live > self.peak:
                self.peak = self.live
            self.calls.append((system_prompt, user_input, tier))
        try:
            # Hold the slot so concurrent workers genuinely overlap; without this
            # a fast mock could serialise and never reveal true peak concurrency.
            time.sleep(self._hold)
            if user_input in self._fail_on:
                raise RuntimeError(f"boom on {user_input!r}")
            return HarnessResult(
                text=self._responses.get(user_input, ""),
                tier=tier,
                provider="concurrent-mock",
                model="concurrent-mock",
            )
        finally:
            with self._lock:
                self.live -= 1


def _count_events(project) -> int:
    """The number of events physically on disk in ``ledger.jsonl``."""
    return len(read_events(project.ledger_path))


def test_fanout_folds_all_results_into_one_ledger_losslessly():
    """N=5 items run concurrently; all 5 folds land on one ledger; the ledger is
    internally consistent and replays identically (no write races)."""
    with temp_home() as (_home, work):
        target = _make_target(work)
        project = open_project(str(target))
        # Seed the root artifact so findings/derivations have something to attach to.
        project.add_artifact(project.root_artifact(), is_tree=True)
        events_before = _count_events(project)

        items = [f"tree{i}" for i in range(5)]
        responses = {
            f"analyze {it}": (
                f"FINDING: {it} has an entrypoint\n"
                f"LEAD: decompile for binary/native\n"
                # Distinct transform per item so each derivation records (the
                # content-addressed cache keys on (transform, input hash)).
                f"DERIVED: unpack-{it} -> out/{it}.txt\n"
            )
            for it in items
        }
        adapter = ConcurrentMock(responses)

        summary = fan_out(
            project,
            items,
            adapter,
            goal="triage every tree",
            build_input=lambda it: f"analyze {it}",
            tier="cheap",
            max_concurrency=5,
        )

        # Every item succeeded and every fold landed.
        assert summary.count == 5, summary.as_dict()
        assert summary.ok == 5 and summary.failed == 0, summary.as_dict()
        assert all(r.ok for r in summary.items)
        # 5 items × (1 finding + 1 lead + 1 derivation) = 15 events folded.
        assert summary.findings == 15, summary.as_dict()

        # Findings all landed on the one ledger.
        assert len(project.ledger.findings()) == 5

        # Internal consistency: the ledger's fold counter tracks exactly the events
        # physically on disk — no event was dropped or double-applied by a race.
        # (Each of 5 items folds a finding + a lead + a derivation; a derivation
        # also adds its output artifact, so the physical count exceeds the 15
        # protocol items — but seq and the on-disk line count must agree exactly.)
        events_after = _count_events(project)
        assert events_after > events_before, (events_before, events_after)
        assert project.ledger.seq == events_after, (project.ledger.seq, events_after)

        # Lossless / no-write-race proof: reload replays the log to identical state.
        live_snapshot = project.ledger.snapshot()
        reloaded = project.reload().snapshot()
        assert reloaded == live_snapshot, "reload must replay to byte-identical state"

        # The concurrent primitive was exercised: N invocations, N calls recorded.
        assert len(adapter.calls) == 5
        # All got the goal as their system prompt.
        assert {c[0] for c in adapter.calls} == {"triage every tree"}


def test_fanout_isolates_a_failing_worker():
    """One worker that raises is reported failed; the others still fold cleanly."""
    with temp_home() as (_home, work):
        target = _make_target(work)
        project = open_project(str(target))
        project.add_artifact(project.root_artifact(), is_tree=True)

        items = [f"tree{i}" for i in range(4)]
        responses = {f"analyze {it}": f"FINDING: {it} ok\n" for it in items}
        # tree2's worker raises.
        adapter = ConcurrentMock(responses, fail_on={"analyze tree2"})

        summary = fan_out(
            project,
            items,
            adapter,
            goal="triage",
            build_input=lambda it: f"analyze {it}",
            max_concurrency=4,
        )

        assert summary.count == 4, summary.as_dict()
        assert summary.ok == 3 and summary.failed == 1, summary.as_dict()

        # The failed item is flagged and carries an error; the batch is not sunk.
        failed = [r for r in summary.items if not r.ok]
        assert len(failed) == 1
        assert failed[0].item == "tree2"
        assert failed[0].error and "boom" in failed[0].error
        assert failed[0].result is None

        # The three successful items each folded one finding.
        assert summary.findings == 3
        assert len(project.ledger.findings()) == 3

        # Still lossless despite the failure.
        assert project.reload().snapshot() == project.ledger.snapshot()


def test_fanout_respects_max_concurrency():
    """Observed peak in-flight workers never exceeds the cap, even with more items
    than the cap. The mock counts live workers under a lock; fan_out counts too."""
    with temp_home() as (_home, work):
        target = _make_target(work)
        project = open_project(str(target))
        project.add_artifact(project.root_artifact(), is_tree=True)

        items = [f"tree{i}" for i in range(10)]
        responses = {f"analyze {it}": f"FINDING: {it}\n" for it in items}
        adapter = ConcurrentMock(responses, hold=0.03)

        cap = 3
        summary = fan_out(
            project,
            items,
            adapter,
            goal="triage",
            build_input=lambda it: f"analyze {it}",
            max_concurrency=cap,
        )

        # Both the adapter's own counter and fan_out's instrumentation must honour
        # the cap. Adapter is the ground truth (it counts real overlapping work).
        assert adapter.peak <= cap, f"adapter peak {adapter.peak} > cap {cap}"
        assert summary.peak_concurrency <= cap, summary.peak_concurrency
        # And it actually parallelised (10 items, cap 3, workers overlapped).
        assert adapter.peak > 1, "expected genuine concurrency, saw serial execution"

        # All ten still folded onto the one ledger.
        assert summary.ok == 10 and summary.failed == 0
        assert summary.findings == 10
        assert len(project.ledger.findings()) == 10


def test_fanout_custom_fold_callback():
    """A custom fold callback is used instead of the default tagged-line parser and
    is invoked exactly once per successful item, sequentially in the parent."""
    with temp_home() as (_home, work):
        target = _make_target(work)
        project = open_project(str(target))
        root = project.root_artifact()
        project.add_artifact(root, is_tree=True)

        items = ["a", "b", "c"]
        adapter = ConcurrentMock({f"do {it}": f"result for {it}" for it in items})

        folded_items = []
        fold_lock = threading.Lock()

        def custom_fold(proj, item, result):
            # Records to the real ledger; must be safe because fan_out serialises us.
            with fold_lock:
                folded_items.append(item)
            proj.record_finding(root, {"note": result.text, "item": item})
            return 1

        summary = fan_out(
            project,
            items,
            adapter,
            goal="g",
            build_input=lambda it: f"do {it}",
            fold=custom_fold,
        )

        assert summary.ok == 3 and summary.findings == 3
        assert sorted(folded_items) == ["a", "b", "c"]
        assert len(project.ledger.findings()) == 3
        assert project.reload().snapshot() == project.ledger.snapshot()


def test_fanout_empty_items_is_a_noop():
    """No items → an empty summary, no invocations, no ledger writes."""
    with temp_home() as (_home, work):
        target = _make_target(work)
        project = open_project(str(target))
        project.add_artifact(project.root_artifact(), is_tree=True)
        before = _count_events(project)

        adapter = ConcurrentMock({})
        summary = fan_out(
            project, [], adapter, goal="g", build_input=lambda it: str(it)
        )

        assert summary.count == 0 and summary.ok == 0 and summary.failed == 0
        assert len(adapter.calls) == 0
        assert _count_events(project) == before


if __name__ == "__main__":
    test_fanout_folds_all_results_into_one_ledger_losslessly()
    test_fanout_isolates_a_failing_worker()
    test_fanout_respects_max_concurrency()
    test_fanout_custom_fold_callback()
    test_fanout_empty_items_is_a_noop()
    print("rekit fanout tests passed")
