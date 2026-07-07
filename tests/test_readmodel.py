"""E7.0 tests: the lab read-model (``rekit.lab``).

Proves Mission Control is a pure fold over ``$REKIT_HOME``:

* :func:`project_view` folds a project's ledger + run + inbox logs into one view;
* the read-model **join** — a pending inbox question makes a run ``blocked``, and a
  pending *tool* question makes it ``suspended`` — the derivation no single log
  records;
* :func:`fleet` lists every project needs-you first;
* :func:`health` counts statuses for the ring.

Hermetic via a temp ``REKIT_HOME``. Pure stdlib.
"""

import contextlib
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.harness import MockAdapter, MockTurn  # noqa: E402
from rekit.human import post_question  # noqa: E402
from rekit.lab import (  # noqa: E402
    BLOCKED,
    SUSPENDED,
    fleet,
    health,
    project_detail,
    project_view,
    reap_stale,
)
from rekit.ledger import open_project  # noqa: E402
from rekit.ledger.events import Event, utc_now  # noqa: E402
from rekit.ledger.runlog import RunLog  # noqa: E402
from rekit.loop import run  # noqa: E402


@contextlib.contextmanager
def _temp_home():
    saved = os.environ.get("REKIT_HOME")
    os.environ["REKIT_HOME"] = tempfile.mkdtemp(prefix="rekit-home-")
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("REKIT_HOME", None)
        else:
            os.environ["REKIT_HOME"] = saved


def _project(name, *, findings=0, done=False):
    """Create a project with a run log; return the Project."""
    ws = tempfile.mkdtemp(prefix="rekit-target-")
    target = Path(ws) / name
    target.write_bytes(b"bytes-" + name.encode())
    p = open_project(str(target))
    rl = RunLog(p.dir)
    rl.run_started(goal="goal " + name, harness="mock", tier="cheap", max_rounds=4)
    rl.round_started(0, "cheap", [])
    rl.round_ended(0, findings=findings)
    if done:
        rl.run_ended(done=True, reason="done")
    return p


def test_empty_home_has_no_fleet():
    with _temp_home():
        assert fleet() == []


def test_running_project_view():
    with _temp_home():
        p = _project("app.apk", findings=3)
        v = project_view(p.dir)
        assert v["status"] == "running"
        assert v["needsYou"] is False
        assert v["run"]["counters"]["findings"] == 3
        assert v["target"].endswith("app.apk")


def test_done_project_status():
    with _temp_home():
        p = _project("firmware.bin", findings=5, done=True)
        assert project_view(p.dir)["status"] == "done"


def test_pending_confirm_blocks():
    with _temp_home():
        p = _project("libx.so")
        post_question(p.dir, "confirm", "allow ghidra?")
        v = project_view(p.dir)
        assert v["status"] == BLOCKED and v["needsYou"] is True
        assert len(v["pending"]) == 1 and v["pending"][0]["kind"] == "confirm"


def test_pending_tool_suspends():
    with _temp_home():
        p = _project("PaywallCore.dll")
        post_question(p.dir, "tool", "install ilspy?", ["install", "manual", "skip"],
                      extra={"tool": "ilspy", "capability": "decompile"})
        v = project_view(p.dir)
        assert v["status"] == SUSPENDED
        assert v["pending"][0]["extra"]["tool"] == "ilspy"


def test_fleet_orders_needs_you_first():
    with _temp_home():
        _project("done1.bin", done=True)      # done -> last
        _project("run1.apk")                   # running -> middle
        blocked = _project("blk.so")
        post_question(blocked.dir, "confirm", "allow?")  # blocked -> first
        statuses = [v["status"] for v in fleet()]
        assert statuses[0] == BLOCKED
        assert statuses.index("running") < statuses.index("done")


def test_project_detail_has_ledger_and_events():
    with _temp_home():
        ws = tempfile.mkdtemp(prefix="rekit-target-")
        target = Path(ws) / "app.bin"
        target.write_bytes(b"\x7fELF fake")
        project = open_project(str(target))
        rl = RunLog(project.dir)
        adapter = MockAdapter([MockTurn(
            text="FINDING: a shell sink\nLEAD: decompile for binary/native\nDONE\n")])
        run(project, "understand it", adapter, max_rounds=3, runlog=rl)

        d = project_detail(project.dir)
        # full ledger contents, not just the summary
        assert len(d["findings"]) >= 1 and d["findings"][0].get("note") == "a shell sink"
        assert len(d["leads"]) >= 1 and d["leads"][0]["capability"] == "decompile"
        assert len(d["artifacts"]) >= 1                     # root artifact seeded
        # merged activity feed spans both logs
        assert d["events"]
        assert any(e["source"] == "run" for e in d["events"])
        assert any(e["source"] == "ledger" for e in d["events"])


def test_health_counts():
    with _temp_home():
        _project("a.bin", done=True)
        _project("b.bin")
        blocked = _project("c.bin")
        post_question(blocked.dir, "confirm", "q?")
        h = health(fleet())
        assert h["total"] == 3
        assert h.get("running") == 1 and h.get("done") == 1 and h.get(BLOCKED) == 1


def _running_project(name, pid, *, pending=None):
    """A project frozen mid-run: a lone run_started with a chosen owning pid."""
    ws = tempfile.mkdtemp(prefix="rekit-target-")
    target = Path(ws) / name
    target.write_bytes(b"bytes-" + name.encode())
    p = open_project(str(target))
    ev = Event(seq=1, type="run_started", ts=utc_now(),
               payload={"goal": "g " + name, "harness": "pi", "tier": "cheap",
                        "maxRounds": 8, "pid": pid})
    (p.dir / "run.jsonl").write_text(ev.to_json_line(), encoding="utf-8")
    if pending:
        post_question(p.dir, *pending)
    return p


def test_reap_stale_marks_dead_pid_run_idle():
    with _temp_home():
        p = _running_project("zombie.bin", 0)          # no owning process
        assert project_view(p.dir)["status"] == "running"
        reaped = reap_stale()
        assert p.id in reaped
        assert project_view(p.dir)["status"] == "idle"


def test_reap_stale_leaves_live_pid_run():
    with _temp_home():
        p = _running_project("live.bin", os.getpid())  # this test process is alive
        assert reap_stale() == []
        assert project_view(p.dir)["status"] == "running"


def test_reap_clears_pending_of_dead_run():
    with _temp_home():
        p = _running_project("blk.so", 0, pending=("confirm", "allow ghidra?"))
        assert project_view(p.dir)["status"] == BLOCKED   # pending + in flight
        reap_stale()
        v = project_view(p.dir)
        assert v["status"] == "idle" and v["pending"] == []   # reaped + inbox cleared


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
    print(f"\nall {len(ALL_TESTS)} read-model tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
