"""The run supervisor — launch and stop rekit runs for Mission Control (E7.4).

The lab UI reads state (``rekit.lab.readmodel``) but never *drives* a run; this is
the write half. A :class:`Supervisor` starts the ralph loop for a target in a
**background daemon thread** so the UI's request returns immediately, and holds a
handle on each live run so the operator's Stop button can end it.

Two properties fall out of the loop's own contract and this module leans on both:

* **launch is non-blocking** — :meth:`Supervisor.launch` opens the project, wires
  the run/liveness log (``run.jsonl``), the UI-routed human channel
  (``inbox.jsonl``), and the skill registry, spawns the loop on a daemon thread,
  records the ``(thread, cancel)`` handle, and returns the project id at once. The
  run's progress is observable entirely through the logs the loop writes — the
  supervisor keeps *no* run state of its own beyond the liveness handle.

* **stop is cooperative** — :meth:`Supervisor.stop` sets the run's
  :class:`threading.Event`; :func:`rekit.loop.run` checks it at each round boundary
  and terminates with reason ``"stopped by operator"``. Stop therefore takes effect
  at the *next* round, not mid-round — the loop never kills a running skill under
  the operator's feet.

Every mutation of the run registry (the ``project_id -> (thread, cancel)`` map) is
guarded by a :class:`threading.Lock`, so the UI thread launching/stopping and the
run threads unregistering themselves never race. Pure stdlib + rekit imports; a
leaf that imports the loop, never the reverse.
"""

from __future__ import annotations

import threading

from ..goalpacks import load_goalpack, run_goalpack
from ..harness import MockAdapter, MockTurn
from ..harness.base import HarnessAdapter
from ..human import LedgerHumanChannel
from ..ledger import open_project
from ..ledger.runlog import RunLog
from ..loop import run as _run
from ..skills.registry import Registry

#: The demo script a ``mock`` launch replays: it does something visible then
#: finishes, so a run kicked off from the UI produces findings and a DONE rather
#: than an empty log.
_MOCK_DEMO_SCRIPT = [
    MockTurn(
        text=(
            "FINDING: launched via Mission Control\n"
            "FINDING: (demo) inspected the target\n"
            "DONE\n"
        )
    )
]

#: What a ``pi`` launch falls back to when the real pi adapter cannot be built
#: (binary/import missing): a single lead explaining the gap, then DONE, so the
#: run still completes and is visible instead of crashing the launch.
_PI_UNAVAILABLE_SCRIPT = [
    MockTurn(
        text=(
            "LEAD: install-pi for harness/pi\n"
            "FINDING: pi harness unavailable — launched a stub run instead\n"
            "DONE\n"
        )
    )
]


class Supervisor:
    """Launches rekit runs on background threads and stops them cooperatively.

    One :class:`Supervisor` backs the lab server: it owns the live-run registry
    (``project_id -> (thread, cancel)``) behind a lock. A launched run is a daemon
    thread driving :func:`rekit.loop.run`; the supervisor holds only the thread and
    its cancel :class:`threading.Event`, because everything else the UI needs is a
    fold over the project's on-disk logs.
    """

    def __init__(self) -> None:
        #: project_id -> (thread, cancel Event) for every live run, guarded by _lock.
        self._runs: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self._lock = threading.Lock()

    # -- launch (non-blocking) -------------------------------------------------

    def launch(
        self,
        target: str,
        goal: str = "",
        *,
        harness: str = "mock",
        tier: str = "cheap",
        max_rounds: int = 8,
        tools: list[str] | None = None,
        goalpack: str | None = None,
    ) -> str:
        """Start a run for ``goal`` against ``target`` and return its project id now.

        Opens (or resumes) the project for ``target``, wires the run/liveness log,
        the UI-routed human channel, and the skill registry (with ``tools`` as extra
        skill roots), builds the adapter named by ``harness``, and spawns a daemon
        thread that calls :func:`rekit.loop.run`. The thread records itself in the
        registry under ``project.id`` and removes itself when done. Returns
        immediately — the caller polls the logs (or :meth:`is_running`) for progress.

        ``harness`` selects the brain: ``"mock"`` replays a short demo script,
        ``"pi"`` drives the real pi adapter (falling back to a visible stub run if
        pi cannot be constructed). Any exception raised while *building* the adapter
        is caught so a bad launch degrades to the stub rather than crashing.
        """
        project = open_project(target)
        runlog = RunLog(project.dir)
        channel = LedgerHumanChannel(project.dir)
        adapter = self._build_adapter(harness)
        cancel = threading.Event()
        # A goalpack launch loads the pack up front (a bad name fails the launch
        # cleanly); its own goal / capabilities / bundled skills drive the loop, so
        # no ad-hoc registry is needed. An ad-hoc launch builds the rack from tools.
        pack = load_goalpack(goalpack) if goalpack else None
        registry = None if pack is not None else Registry.from_home(extra_roots=tools)

        def _drive() -> None:
            # The loop already writes run.jsonl for observability; the thread's only
            # extra job is to never let an exception escape and to unregister itself.
            try:
                if pack is not None:
                    run_goalpack(
                        project, pack, adapter,
                        channel=channel, runlog=runlog, cancel=cancel,
                        tier=tier, max_rounds=max_rounds,
                    )
                else:
                    _run(
                        project,
                        goal,
                        adapter,
                        registry=registry,
                        channel=channel,
                        runlog=runlog,
                        tier=tier,
                        max_rounds=max_rounds,
                        cancel=cancel,
                    )
            except Exception:  # noqa: BLE001 — a run thread must never crash the process.
                pass
            finally:
                with self._lock:
                    # Only drop our own handle: a relaunch may already have replaced it.
                    current = self._runs.get(project.id)
                    if current is not None and current[1] is cancel:
                        del self._runs[project.id]

        thread = threading.Thread(
            target=_drive, name=f"rekit-run-{project.id}", daemon=True
        )
        with self._lock:
            self._runs[project.id] = (thread, cancel)
        thread.start()
        return project.id

    def _build_adapter(self, harness: str) -> HarnessAdapter:
        """Build the harness adapter for a launch, degrading gracefully.

        ``"mock"`` → a :class:`MockAdapter` replaying the demo script (a launched run
        visibly does something then DONEs). ``"pi"`` → the real
        :class:`~rekit.harness.pi.PiAdapter`; if importing/constructing it raises for
        any reason, fall back to a :class:`MockAdapter` that emits a single lead
        explaining pi is unavailable then DONE, so launch never crashes.
        """
        if harness == "pi":
            try:
                from ..harness.pi import PiAdapter

                return PiAdapter()
            except Exception:  # noqa: BLE001 — never let a bad pi build crash launch.
                return MockAdapter(list(_PI_UNAVAILABLE_SCRIPT))
        # Default / "mock": the deterministic demo brain.
        return MockAdapter(list(_MOCK_DEMO_SCRIPT))

    # -- stop (cooperative) ----------------------------------------------------

    def stop(self, project_id: str) -> bool:
        """Signal a run to stop at its next round boundary.

        Sets the run's cancel :class:`threading.Event`; :func:`rekit.loop.run` reads
        it between rounds and terminates with reason ``"stopped by operator"``.
        Returns True if the run was tracked (and was signalled), False if no such run
        is registered. Cooperative: the currently-executing round finishes first.
        """
        with self._lock:
            entry = self._runs.get(project_id)
        if entry is None:
            return False
        entry[1].set()
        return True

    # -- observability ---------------------------------------------------------

    def is_running(self, project_id: str) -> bool:
        """Whether a run for ``project_id`` is currently tracked and its thread alive."""
        with self._lock:
            entry = self._runs.get(project_id)
        return entry is not None and entry[0].is_alive()

    def running_ids(self) -> list[str]:
        """The project ids of every currently-tracked run, sorted for stability."""
        with self._lock:
            return sorted(self._runs)
