"""E7.0 tests: the run/liveness log (``run.jsonl``).

Proves the read-model spine that drives Mission Control's fleet card:

* a :class:`RunLog` appends one typed event per lifecycle transition;
* the live :class:`RunState` is a deterministic fold — round index, tier, model,
  and the findings/leads/derivations/skill-run counters accumulate;
* reload = replay: :func:`load_run_state` off disk equals the writer's own state;
* an unknown event type replays as a no-op (forward-compatible).

Plain-python style (runnable via ``python tests/test_runlog.py``) and
pytest-compatible. Pure stdlib.
"""

import contextlib
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.harness import MockAdapter, MockTurn  # noqa: E402
from rekit.ledger import open_project  # noqa: E402
from rekit.ledger.runlog import (  # noqa: E402
    DONE,
    IDLE,
    RUNNING,
    RunLog,
    RunState,
    load_run_state,
)
from rekit.loop import run  # noqa: E402


def _dir():
    return tempfile.mkdtemp(prefix="rekit-runlog-")


def test_empty_log_folds_to_idle():
    rl = RunLog(_dir())
    state = rl.state()
    assert isinstance(state, RunState)
    assert state.status == IDLE
    assert state.round == 0 and state.findings == 0 and state.seq == 0


def test_run_started_sets_running_and_meta():
    rl = RunLog(_dir())
    rl.run_started(goal="explain the pipeline", harness="pi", tier="cheap", max_rounds=8)
    s = rl.state()
    assert s.status == RUNNING
    assert s.goal == "explain the pipeline"
    assert s.harness == "pi" and s.tier == "cheap" and s.max_rounds == 8
    assert s.started_at  # stamped


def test_rounds_accumulate_counters_and_model():
    rl = RunLog(_dir())
    rl.run_started(goal="g", harness="pi", tier="cheap", max_rounds=8)
    rl.round_started(0, "cheap", ["code-understanding"])
    rl.round_ended(0, findings=3, leads=1, derivations=0, skill_runs=1)
    rl.round_started(1, "beefy", ["ghidra"])
    rl.round_ended(1, findings=2, derivations=1, model="zai/glm-5.2", provider="zai")
    s = rl.state()
    assert s.round == 1
    assert s.tier == "beefy"                    # last round's tier
    assert s.findings == 5 and s.leads == 1 and s.derivations == 1 and s.skill_runs == 1
    assert s.model == "zai/glm-5.2" and s.provider == "zai"


def test_cost_accumulates():
    rl = RunLog(_dir())
    rl.run_started(goal="g", harness="pi", tier="cheap")
    rl.round_ended(0, cost={"usd": 0.07, "tokensIn": 40, "tokensOut": 10})
    rl.round_ended(1, cost={"usd": 0.34, "tokensIn": 88, "tokensOut": 26})
    s = rl.state()
    assert round(s.cost_usd, 2) == 0.41
    assert s.tokens_in == 128 and s.tokens_out == 36


def test_run_ended_done_marks_done():
    rl = RunLog(_dir())
    rl.run_started(goal="g", harness="pi", tier="cheap")
    rl.run_ended(done=True, reason="brain signaled DONE")
    s = rl.state()
    assert s.done is True and s.status == DONE
    assert s.reason == "brain signaled DONE" and s.ended_at


def test_run_ended_not_done_is_idle():
    rl = RunLog(_dir())
    rl.run_started(goal="g", harness="pi", tier="cheap")
    rl.run_ended(done=False, reason="reached max_rounds (8)")
    assert rl.state().status == IDLE


def test_status_changed_overrides():
    rl = RunLog(_dir())
    rl.run_started(goal="g", harness="pi", tier="cheap")
    rl.status("failed", reason="harness error")
    s = rl.state()
    assert s.status == "failed" and s.reason == "harness error"


def test_reload_equals_live_state():
    d = _dir()
    rl = RunLog(d)
    rl.run_started(goal="g", harness="pi", tier="cheap", max_rounds=4)
    rl.round_started(0, "cheap", [])
    rl.round_ended(0, findings=2)
    rl.run_ended(done=True, reason="done")
    # A fresh replay off disk must equal the writer's folded state (lossless).
    replayed = load_run_state(rl.path)
    assert replayed.to_dict() == rl.state().to_dict()


def test_unknown_event_type_is_ignored():
    d = _dir()
    rl = RunLog(d)
    rl.run_started(goal="g", harness="pi", tier="cheap")
    # Append a raw line with a future/unknown type — must replay as a no-op.
    with open(rl.path, "a", encoding="utf-8") as fh:
        fh.write('{"seq": 99, "type": "future_thing", "ts": "2026-01-01T00:00:00+00:00", "payload": {}}\n')
    s = rl.state()
    assert s.status == RUNNING            # unaffected by the unknown event
    assert s.seq == 99                    # but seq still advances (forward-compat)


def test_to_dict_shape():
    rl = RunLog(_dir())
    rl.run_started(goal="g", harness="pi", tier="cheap", max_rounds=8)
    rl.round_ended(0, findings=1, cost={"usd": 0.02})
    d = rl.state().to_dict()
    assert d["status"] == RUNNING
    assert d["counters"] == {"findings": 1, "leads": 0, "derivations": 0, "skillRuns": 0}
    assert d["cost"]["usd"] == 0.02
    assert d["maxRounds"] == 8


@contextlib.contextmanager
def _temp_home():
    """A temp ``REKIT_HOME`` (restored afterwards) so ``open_project`` is hermetic."""
    saved = os.environ.get("REKIT_HOME")
    home = tempfile.mkdtemp(prefix="rekit-home-")
    os.environ["REKIT_HOME"] = home
    try:
        yield home
    finally:
        if saved is None:
            os.environ.pop("REKIT_HOME", None)
        else:
            os.environ["REKIT_HOME"] = saved


def test_loop_run_emits_run_log_end_to_end():
    """The real loop, driven by a MockAdapter, writes a faithful run.jsonl."""
    with _temp_home():
        ws = tempfile.mkdtemp(prefix="rekit-target-")
        target = Path(ws) / "bin.dat"
        target.write_bytes(b"\x7fELF fake target bytes")
        project = open_project(str(target))
        adapter = MockAdapter([MockTurn(text="FINDING: found a sink\nDONE\n")])
        rl = RunLog(project.dir)

        summary = run(project, "explain the binary", adapter, max_rounds=4, runlog=rl)

        assert summary.done is True
        # The run log, replayed off disk, reflects what the loop did.
        s = load_run_state(rl.path)
        assert s.status == DONE and s.done is True
        assert s.goal == "explain the binary" and s.harness == "mock"
        assert s.findings == 1          # the brain's FINDING folded through
        assert s.round == 0             # DONE on the first round


def test_loop_run_without_runlog_is_unchanged():
    """runlog=None must leave loop behaviour (and the project dir) untouched."""
    with _temp_home():
        ws = tempfile.mkdtemp(prefix="rekit-target-")
        target = Path(ws) / "bin.dat"
        target.write_bytes(b"data")
        project = open_project(str(target))
        adapter = MockAdapter([MockTurn(text="DONE\n")])
        summary = run(project, "g", adapter, max_rounds=2)  # no runlog
        assert summary.done is True
        assert not (Path(project.dir) / "run.jsonl").exists()


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
    print(f"\nall {len(ALL_TESTS)} run-log tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
