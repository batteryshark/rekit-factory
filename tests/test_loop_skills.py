"""E5 loop integration: the ralph loop scopes, gates, and RUNS skills (card T-052).

Proves the wired seam end-to-end with a deterministic MockAdapter (no network):

* the loop scopes the skill set by ``(kinds present) ∩ (capabilities requested)``
  filtered by policy, and surfaces the available ones into the brain's context as
  ``Available skills: <name> — <capability> — accepts <kinds>``;
* when the brain emits ``RUN_SKILL: <fixture> on <path>``, the loop actually runs
  the skill (gating + sandboxing) and folds the resulting derivation into the
  ledger — the revealed output re-enters;
* the run's result is fed back into the next round's context, and the brain then
  emits ``DONE`` and the loop terminates;
* a **gated** (executes-untrusted) fixture routes through a ``ScriptedHumanChannel``:
  denied → the loop records no derivation; allowed → it runs.

Plain-python style (runnable via ``python tests/test_loop_skills.py``) and
pytest-compatible. Pure stdlib; hermetic.
"""

import contextlib
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.harness import MockAdapter, MockInvocation, MockTurn  # noqa: E402
from rekit.human import ScriptedHumanChannel  # noqa: E402
from rekit.ledger import open_project  # noqa: E402
from rekit.loop import run  # noqa: E402
from rekit.skills import Policy, Registry  # noqa: E402


RUN_SH = """#!/bin/sh
set -e
input="$1"
out="$2"
mkdir -p "$out"
printf 'unpacked %s\\n' "$input" > "$out/result.txt"
"""

READONLY_MD = """---
name: unpack-fixture
capability: unpack
accepts: ['*']
emits: [text]
tier: read-only
run: scripts/run.sh
description: Fixture unpack skill (read-only, auto-runs).
---
# unpack-fixture
"""

UNTRUSTED_MD = """---
name: unpack-fixture
capability: unpack
accepts: ['*']
emits: [text]
tier: executes-untrusted
run: scripts/run.sh
description: Fixture unpack skill (executes-untrusted, gated).
---
# unpack-fixture
"""


def _write_skill(skills_root: Path, folder: str, skill_md: str):
    d = skills_root / folder
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(skill_md, encoding="utf-8")
    run_path = d / "scripts" / "run.sh"
    run_path.write_text(RUN_SH, encoding="utf-8")
    run_path.chmod(0o755)


@contextlib.contextmanager
def temp_home():
    saved = os.environ.get("REKIT_HOME")
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as work:
        os.environ["REKIT_HOME"] = home
        skills_root = Path(home) / "skills"
        skills_root.mkdir()
        try:
            yield Path(home), skills_root, Path(work)
        finally:
            if saved is None:
                os.environ.pop("REKIT_HOME", None)
            else:
                os.environ["REKIT_HOME"] = saved


def _make_target(work: Path) -> Path:
    t = work / "payload.bin"
    t.write_bytes(b"\x00\x01\x02 target bytes\n")
    return t


def test_loop_scopes_surfaces_and_runs_a_skill():
    """The loop surfaces the scoped skill, runs it on RUN_SKILL, folds the
    derivation, feeds the result forward, then terminates on DONE."""
    with temp_home() as (_home, skills_root, work):
        _write_skill(skills_root, "unpack-fixture", READONLY_MD)
        registry = Registry.from_home(environ=dict(os.environ))
        target = _make_target(work)
        project = open_project(str(target))

        # The brain: round 0 asks to run the fixture; round 1, having seen the
        # result in its context, says DONE.
        script = [
            MockTurn(text="Let's unpack it.\nRUN_SKILL: unpack-fixture on " + str(target) + "\n"),
            MockTurn(text="Saw the unpack result.\nDONE\n"),
        ]
        adapter = MockAdapter(script)

        summary = run(
            project, "unpack this payload", adapter,
            registry=registry, max_rounds=6,
            channel=ScriptedHumanChannel([]),  # read-only -> never asked
            policy=Policy.default(),
            requested_capabilities=["unpack"],
        )

        assert summary.done, summary.as_dict()
        assert summary.reason == "brain signaled DONE"
        assert summary.total_skill_runs == 1, summary.as_dict()

        # The skill actually ran and recorded a derivation.
        runs = summary.skill_runs
        assert runs[0].status == "ok", runs[0].detail
        assert runs[0].recorded_derivation
        assert summary.total_derivations == 1, summary.as_dict()

        # Round 0's context surfaced the available skill to the brain.
        ctx0 = adapter.calls[0].context or ""
        assert "Available skills:" in ctx0, ctx0
        assert "unpack-fixture — unpack — accepts *" in ctx0, ctx0
        # The scoped tool allowlist carried the name.
        assert adapter.calls[0].tools == ["unpack-fixture"], adapter.calls[0].tools

        # Round 1's context fed the run result forward so the brain could react.
        ctx1 = adapter.calls[1].context or ""
        assert "Skill run results from the last round:" in ctx1, ctx1
        assert "unpack-fixture" in ctx1 and "ok" in ctx1, ctx1

        # Durable + lossless: the revealed output re-entered the ledger.
        ledger = project.reload()
        assert any(d.capability == "unpack" for d in ledger.derivations.values())
        out_paths = [
            o.path
            for d in ledger.derivations.values()
            for o in d.outputs
        ]
        assert any(Path(p).name == "result.txt" for p in out_paths), out_paths


def test_loop_gated_skill_denied_records_no_derivation():
    """An executes-untrusted fixture is gated: the loop routes the run through the
    human channel and, when denied, records no derivation."""
    with temp_home() as (_home, skills_root, work):
        _write_skill(skills_root, "unpack-fixture", UNTRUSTED_MD)
        registry = Registry.from_home(environ=dict(os.environ))
        target = _make_target(work)
        project = open_project(str(target))

        script = [
            MockTurn(text="RUN_SKILL: unpack-fixture on " + str(target) + "\n"),
            MockTurn(text="DONE\n"),
        ]
        adapter = MockAdapter(script)
        channel = ScriptedHumanChannel([False])  # deny the gate

        summary = run(
            project, "unpack this payload", adapter,
            registry=registry, max_rounds=6,
            channel=channel, policy=Policy.default(),
            requested_capabilities=["unpack"],
        )

        assert summary.done
        assert summary.total_skill_runs == 1
        assert summary.skill_runs[0].status == "skipped", summary.skill_runs[0].detail
        assert summary.total_derivations == 0, summary.as_dict()
        assert channel.asked and channel.asked[-1][1] == "confirm"

        ledger = project.reload()
        assert not ledger.derivations, "denied gate leaves no derivation in the ledger"


def test_loop_gated_skill_allowed_runs():
    """The same gated fixture runs when the human channel approves the run."""
    with temp_home() as (_home, skills_root, work):
        _write_skill(skills_root, "unpack-fixture", UNTRUSTED_MD)
        registry = Registry.from_home(environ=dict(os.environ))
        target = _make_target(work)
        project = open_project(str(target))

        script = [
            MockTurn(text="RUN_SKILL: unpack-fixture on " + str(target) + "\n"),
            MockTurn(text="DONE\n"),
        ]
        adapter = MockAdapter(script)
        channel = ScriptedHumanChannel([True])  # approve the gate

        summary = run(
            project, "unpack this payload", adapter,
            registry=registry, max_rounds=6,
            channel=channel, policy=Policy.default(),
            requested_capabilities=["unpack"],
        )

        assert summary.done
        assert summary.skill_runs[0].status == "ok", summary.skill_runs[0].detail
        assert summary.total_derivations == 1
        assert channel.asked[-1][1] == "confirm"


def test_loop_read_only_policy_hides_gated_skill():
    """Under a read-only policy the executes-untrusted fixture is never scoped —
    the brain never sees it, so a RUN_SKILL for it is refused as out-of-scope."""
    with temp_home() as (_home, skills_root, work):
        _write_skill(skills_root, "unpack-fixture", UNTRUSTED_MD)
        registry = Registry.from_home(environ=dict(os.environ))
        target = _make_target(work)
        project = open_project(str(target))

        script = [
            MockTurn(text="RUN_SKILL: unpack-fixture on " + str(target) + "\n"),
            MockTurn(text="DONE\n"),
        ]
        adapter = MockAdapter(script)

        summary = run(
            project, "unpack this payload", adapter,
            registry=registry, max_rounds=6,
            channel=ScriptedHumanChannel([]),
            policy=Policy.read_only(),  # forbids executes-untrusted
            requested_capabilities=["unpack"],
        )

        assert summary.done
        # The skill was never in scope: no "Available skills" line names it.
        ctx0 = adapter.calls[0].context or ""
        assert "unpack-fixture" not in ctx0, ctx0
        # The RUN_SKILL was refused as out-of-scope; nothing ran/recorded.
        assert summary.total_skill_runs == 1
        assert summary.skill_runs[0].status == "skipped"
        assert "not in the scoped skill set" in summary.skill_runs[0].detail
        assert summary.total_derivations == 0


def test_loop_without_registry_surfaces_no_skills():
    """No registry -> no scoped skills, no Available-skills block (back-compat)."""
    with temp_home() as (_home, _skills_root, work):
        target = _make_target(work)
        project = open_project(str(target))
        adapter = MockAdapter([MockTurn(text="DONE\n")])

        summary = run(project, "inspect", adapter, max_rounds=3)

        assert summary.done
        assert adapter.calls[0].tools == []
        assert "Available skills:" not in (adapter.calls[0].context or "")
        assert summary.total_skill_runs == 0


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    failures = []
    for test in ALL_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            import traceback
            failures.append((test.__name__, exc))
            print(f"FAIL {test.__name__}: {exc}")
            traceback.print_exc()
        else:
            print(f"ok   {test.__name__}")
    if failures:
        print(f"\n{len(failures)} failed, {len(ALL_TESTS) - len(failures)} passed")
        return 1
    print(f"\nall {len(ALL_TESTS)} loop-skill tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
