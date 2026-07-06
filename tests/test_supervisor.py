"""E7.4 tests: the run supervisor (``rekit.lab.supervisor``).

Proves the write half of Mission Control — launch and stop:

* :meth:`Supervisor.launch` starts a run on a background daemon thread and returns
  the project id at once (non-blocking); the run drives the real ralph loop, so a
  faithful ``run.jsonl`` lands under the project dir and its folded
  :class:`~rekit.ledger.runlog.RunState` shows ``done`` with findings folded;
* :meth:`Supervisor.stop` is cooperative and handle-based: it returns True for a
  tracked run (setting its cancel event) and False for an unknown id;
* :meth:`Supervisor.running_ids` / :meth:`Supervisor.is_running` track live runs
  and are asserted in a race-free way (poll to completion, then check the settled
  state).

Hermetic via a temp ``REKIT_HOME``. Plain-python style (runnable via
``python tests/test_supervisor.py``) and pytest-compatible. Pure stdlib.
"""

import contextlib
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.lab.supervisor import Supervisor  # noqa: E402
from rekit.ledger import open_project  # noqa: E402
from rekit.ledger.runlog import RUN_LOG_FILENAME, load_run_state  # noqa: E402


@contextlib.contextmanager
def _temp_home():
    """A temp ``REKIT_HOME`` (restored afterwards) so ``open_project`` is hermetic."""
    saved = os.environ.get("REKIT_HOME")
    os.environ["REKIT_HOME"] = tempfile.mkdtemp(prefix="rekit-home-")
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("REKIT_HOME", None)
        else:
            os.environ["REKIT_HOME"] = saved


def _target(name: str = "bin.dat") -> Path:
    """A real temp file the loop can seed as the root artifact."""
    ws = tempfile.mkdtemp(prefix="rekit-target-")
    target = Path(ws) / name
    target.write_bytes(b"\x7fELF fake target bytes")
    return target


def _await_idle(sup: Supervisor, pid: str, timeout: float = 5.0) -> None:
    """Poll ``is_running`` until the run settles (or the timeout trips)."""
    deadline = time.monotonic() + timeout
    while sup.is_running(pid) and time.monotonic() < deadline:
        time.sleep(0.02)


def test_launch_runs_to_completion_and_writes_run_log():
    """A mock launch runs on a background thread to a DONE, writing run.jsonl."""
    with _temp_home():
        target = _target()
        sup = Supervisor()
        pid = sup.launch(str(target), "explain the binary", harness="mock")

        # launch is non-blocking: it handed back the project id immediately.
        assert pid == open_project(str(target)).id

        _await_idle(sup, pid)
        assert not sup.is_running(pid), "run should have finished within the timeout"

        project = open_project(str(target))
        run_path = Path(project.dir) / RUN_LOG_FILENAME
        assert run_path.exists(), "the loop should have written run.jsonl"

        state = load_run_state(run_path)
        assert state.done is True
        assert state.findings >= 1, "the demo script folds at least one FINDING"


def test_stop_returns_true_for_tracked_and_false_for_unknown():
    """stop signals a tracked run's cancel event (True) and no-ops an unknown id."""
    with _temp_home():
        target = _target("stopme.dat")
        sup = Supervisor()
        # A high max_rounds so we exercise stop's tracking without racing the loop.
        pid = sup.launch(str(target), "long goal", harness="mock", max_rounds=1000)

        assert sup.stop(pid) is True, "a freshly launched run is tracked"
        assert sup.stop("nonexistent-000000000000") is False

        _await_idle(sup, pid)  # let the (now-cancelled) thread wind down cleanly


def test_running_ids_tracks_then_clears():
    """running_ids holds a fresh id, and is empty once the run completes."""
    with _temp_home():
        target = _target("track.dat")
        sup = Supervisor()
        pid = sup.launch(str(target), "quick goal", harness="mock")

        _await_idle(sup, pid)
        # After completion the thread unregistered itself: not running, not listed.
        assert not sup.is_running(pid)
        assert pid not in sup.running_ids()


def test_pi_harness_launch_never_crashes():
    """harness='pi' launches without crashing whether or not pi is installed.

    We assert launch robustness (a tracked project id comes back) rather than the
    run's terminal state: on a box with real pi installed the run makes a live model
    call we must not block the suite on, so we stop it right after launch. The
    unavailable-pi *fallback* (stub run → DONE) is proven by the mock path in
    :func:`test_launch_runs_to_completion_and_writes_run_log`.
    """
    with _temp_home():
        target = _target("pi.dat")
        sup = Supervisor()
        pid = sup.launch(str(target), "drive with pi", harness="pi")

        assert pid == open_project(str(target)).id
        # Cooperative stop, then let the thread wind down — never wait on live pi.
        assert sup.stop(pid) is True
        _await_idle(sup, pid, timeout=1.0)


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    failures = []
    for test in ALL_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures.append((test.__name__, exc))
            print(f"FAIL {test.__name__}: {exc}")
        else:
            print(f"ok   {test.__name__}")
    if failures:
        print(f"\n{len(failures)} failed, {len(ALL_TESTS) - len(failures)} passed")
        return 1
    print(f"\nall {len(ALL_TESTS)} supervisor tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
