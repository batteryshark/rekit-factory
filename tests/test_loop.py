"""E2 — the ralph loop drives a goal to termination over the ledger.

Proves the first-slice acceptance of card T-049:

- the loop runs a goal to termination via the :class:`HarnessAdapter` interface —
  no pydantic-ai in the path (a deterministic :class:`MockAdapter` is the brain);
- structured outcomes the brain reports (FINDING / LEAD / DERIVED) are folded into
  the persistent ledger as durable events;
- the loop terminates on the brain's DONE signal, and — separately — always halts
  at ``max_rounds`` even when DONE never arrives (bounded, deterministic);
- the loop chooses the model tier per round (cheap floor → beefy judgment).

Plain-python style (runnable via ``python tests/test_loop.py``) and
pytest-compatible. Each test uses a temp ``REKIT_HOME`` via the env var so nothing
touches ``~/.rekit`` and there is no network.
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

from rekit.harness import MockAdapter, MockTurn  # noqa: E402
from rekit.harness.base import HarnessAdapter, HarnessResult  # noqa: E402
from rekit.harness.tiers import BEEFY, CHEAP  # noqa: E402
from rekit.ledger import open_project  # noqa: E402
from rekit.loop import run  # noqa: E402


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
    (target / "config.json").write_text('{"k": 1}\n', encoding="utf-8")
    return target


def test_loop_runs_to_termination_and_folds_outcomes():
    """MockAdapter drives a goal to a DONE signal; findings/leads/derivations land
    in the ledger; the loop stops before the bound."""
    with temp_home() as (_home, work):
        target = _make_target(work)
        project = open_project(str(target))

        # A scripted brain: two working rounds then DONE.
        script = [
            MockTurn(
                text=(
                    "Looking at the tree.\n"
                    "FINDING: main.py prints a greeting\n"
                    "LEAD: decompile for binary/native\n"
                )
            ),
            MockTurn(
                text=(
                    "DERIVED: unpack -> extracted/payload.txt\n"
                    "FINDING: config.json holds a single key\n"
                )
            ),
            MockTurn(text="Nothing left to do.\nDONE\n"),
        ]
        adapter = MockAdapter(script)

        summary = run(project, "unpack + analyze this app", adapter, max_rounds=8)

        assert summary.done, "loop should terminate on the brain's DONE signal"
        assert summary.reason == "brain signaled DONE"
        assert summary.round_count == 3, f"expected 3 rounds, got {summary.round_count}"

        # Outcomes folded into the ledger.
        assert summary.total_findings == 2, summary.as_dict()
        assert summary.total_leads == 1, summary.as_dict()
        assert summary.total_derivations == 1, summary.as_dict()

        # Durable in the ledger — and lossless on reload.
        ledger = project.reload()
        assert len(ledger.findings()) == 2
        assert ("decompile", "binary/native") in ledger.leads
        assert any(
            d.transform == "unpack" for d in ledger.derivations.values()
        ), "the DERIVED line should have recorded a derivation"

        # The adapter interface was exercised (no pydantic-ai anywhere).
        assert len(adapter.calls) == 3
        assert adapter.name == "mock"


def test_loop_is_bounded_when_no_done_signal():
    """With a brain that never says DONE, the loop still halts at max_rounds —
    determinism is guaranteed by the bound, not by the brain."""
    with temp_home() as (_home, work):
        target = _make_target(work)
        project = open_project(str(target))

        # Every round just reports a finding, never DONE.
        adapter = MockAdapter([], terminal=MockTurn(text="FINDING: still working\n"))

        summary = run(project, "endless goal", adapter, max_rounds=4)

        assert not summary.done
        assert summary.reason == "reached max_rounds (4)"
        assert summary.round_count == 4
        assert len(adapter.calls) == 4


def test_loop_context_reflects_ledger_and_tools_are_scoped_names():
    """The context handed to the brain reflects ledger state, and the tool
    allowlist is a list of names (empty with no registry)."""
    with temp_home() as (_home, work):
        target = _make_target(work)
        project = open_project(str(target))

        adapter = MockAdapter([MockTurn(text="DONE\n")])
        run(project, "inspect the app", adapter, max_rounds=3)

        call = adapter.calls[0]
        assert "Project ledger" in (call.context or "")
        assert "Target:" in (call.context or "")
        # No registry passed → no tools handed to the brain.
        assert call.tools == []
        # System prompt defaults to the goal.
        assert call.system_prompt == "inspect the app"


def test_loop_escalates_tier_for_synthesis():
    """Tier is a loop decision: the cheap floor triages while nothing is found yet,
    escalating to the beefy judgment tier once findings exist to synthesize over."""
    with temp_home() as (_home, work):
        target = _make_target(work)
        project = open_project(str(target))

        script = [
            MockTurn(text="FINDING: something notable\n"),  # round 0: cheap triage
            MockTurn(text="DONE\n"),  # round 1: a finding now exists → escalate
        ]
        adapter = MockAdapter(script)
        summary = run(project, "analyze", adapter, max_rounds=4, tier=CHEAP)

        tiers_used = [c.tier for c in adapter.calls]
        assert tiers_used[0] == CHEAP, tiers_used
        # Round 0 recorded a finding, so round 1 is synthesis/judgment → beefy.
        assert tiers_used[1] == BEEFY, tiers_used
        assert summary.done


class _CancellableSlowAdapter(HarnessAdapter):
    """A brain whose turn blocks — but polls ``cancel`` like the pi adapter, so a
    Stop interrupts it mid-turn. Never emits DONE, so only cancel ends the loop."""

    name = "slow"

    def invoke(self, system_prompt, user_input, *, tools=None, context=None,
               tier="cheap", cancel=None):
        for _ in range(500):
            if cancel is not None and cancel.is_set():
                return HarnessResult(text="", ok=False)   # soft cancel, like pi
            time.sleep(0.01)
        return HarnessResult(text="FINDING: still working\n")


def test_loop_cancel_stops_a_blocked_turn_promptly():
    """Stop fires during a long brain call: the adapter aborts and the loop ends
    with reason 'stopped by operator', not by running out max_rounds."""
    with temp_home() as (_home, work):
        target = _make_target(work)
        project = open_project(str(target))
        adapter = _CancellableSlowAdapter()
        cancel = threading.Event()
        box: dict = {}

        def go():
            box["summary"] = run(project, "analyze", adapter, max_rounds=50, cancel=cancel)

        t = threading.Thread(target=go)
        t.start()
        time.sleep(0.15)          # let it enter the (blocking) first turn
        cancel.set()              # operator hits Stop
        t.join(timeout=3)
        assert not t.is_alive(), "loop did not stop after cancel"
        assert box["summary"].reason == "stopped by operator"


if __name__ == "__main__":
    test_loop_runs_to_termination_and_folds_outcomes()
    test_loop_is_bounded_when_no_done_signal()
    test_loop_context_reflects_ledger_and_tools_are_scoped_names()
    test_loop_escalates_tier_for_synthesis()
    test_loop_cancel_stops_a_blocked_turn_promptly()
    print("rekit loop tests passed")
