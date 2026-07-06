"""E5 tests: the skill runner — gate, sandbox, execute, fold (card T-052).

Proves a skill actually RUNS and its outputs re-enter the ledger:

* a real fixture skill (SKILL.md + scripts/run.sh) executes against a target
  (sandboxed on macOS), records a derivation, and the output artifact it wrote
  into its ``$2`` out_dir appears in the ledger — revealed content re-enters;
* the **tier-gate** routes a ``network``-tier skill through a
  ``ScriptedHumanChannel``: denied → ``skipped`` and nothing recorded; allowed →
  it runs and records;
* the **host-gate** on a skill requiring a missing tool records an "install X"
  lead and returns ``unavailable`` — no crash;
* the run contract: ``run: scripts/run.sh`` invoked as ``<run> <input> <out_dir>``,
  defaulting to ``scripts/run.sh`` when ``run:`` is omitted.

Plain-python style (runnable via ``python tests/test_runner.py``) and
pytest-compatible. Pure stdlib; hermetic (temp ``$REKIT_HOME``, no network).
"""

import contextlib
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.human import ScriptedHumanChannel  # noqa: E402
from rekit.ledger import open_project  # noqa: E402
from rekit.skills import Policy, Registry, load_skill, run_skill  # noqa: E402
from rekit.skills import sandbox as _sandbox  # noqa: E402


# --------------------------------------------------------------------------------
# Fixture skills. Each is a SKILL.md + scripts/run.sh that writes an output file
# into its $2 out_dir (the run contract: `<run> <input> <out_dir>`).
# --------------------------------------------------------------------------------

# An executes-untrusted skill (the sandboxed case): copies the input's name and a
# marker into the out_dir. Writes ONLY under $2, so it works inside the sandbox.
UNPACK_MD = """---
name: unpack-fixture
capability: unpack
accepts: ['*']
emits: [text]
tier: executes-untrusted
run: scripts/run.sh
description: A fixture skill that emits an output file into its out_dir.
---
# unpack-fixture
"""

# Default-run variant: no `run:` field -> the runner defaults to scripts/run.sh.
DEFAULTRUN_MD = """---
name: defaultrun-fixture
capability: unpack
accepts: ['*']
tier: executes-untrusted
description: Same, but relies on the default run command (scripts/run.sh).
---
# defaultrun-fixture
"""

# A network-tier skill for the gate test (read-only would auto-run without asking).
NET_MD = """---
name: net-fixture
capability: unpack
accepts: ['*']
tier: network
run: scripts/run.sh
description: A network-tier skill; each run must clear the human channel.
---
# net-fixture
"""

# A skill that requires a host tool that does not exist -> host-gate -> lead.
HOSTGATED_MD = """---
name: hostgated-fixture
capability: unpack
accepts: ['*']
tier: executes-untrusted
host: definitely-not-a-real-tool-xyz
env: NO_SUCH_TOOL_HOME
run: scripts/run.sh
description: Needs a tool that is not installed.
---
# hostgated-fixture
"""

# A run.sh that writes a single output file NAMED after nothing external — pure,
# hermetic, sandbox-safe (writes only under "$2").
RUN_SH = """#!/bin/sh
# Run contract: $1 = input path, $2 = out_dir (the only writable location).
set -e
input="$1"
out="$2"
mkdir -p "$out"
printf 'unpacked from %s\\n' "$input" > "$out/result.txt"
mkdir -p "$out/revealed"
printf 'nested\\n' > "$out/revealed/inner.txt"
"""


def _write_skill(skills_root: Path, folder: str, skill_md: str, run_sh: str | None = RUN_SH) -> Path:
    d = skills_root / folder
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if run_sh is not None:
        run_path = d / "scripts" / "run.sh"
        run_path.write_text(run_sh, encoding="utf-8")
        run_path.chmod(0o755)
    return d


@contextlib.contextmanager
def temp_home():
    """A temp ``$REKIT_HOME`` (restored after) + a temp workspace. Yields
    ``(home, skills_root, work)``."""
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
    t.write_bytes(b"\x00\x01\x02 hello target\n")
    return t


def _load(skills_root: Path, folder: str):
    return load_skill(skills_root / folder / "SKILL.md", environ=dict(os.environ))


def _always_yes_channel() -> ScriptedHumanChannel:
    return ScriptedHumanChannel([True] * 8)


# --------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------

def test_runs_records_derivation_and_reveals_outputs():
    """A skill executes (sandboxed on macOS), records a derivation, and the output
    it wrote into $2 re-enters the ledger as first-class artifacts."""
    with temp_home() as (_home, skills_root, work):
        _write_skill(skills_root, "unpack-fixture", UNPACK_MD)
        target = _make_target(work)
        project = open_project(str(target))
        skill = _load(skills_root, "unpack-fixture")

        result = run_skill(
            skill, str(target), project,
            channel=_always_yes_channel(), policy=Policy.default(),
        )

        assert result.status == "ok", result.detail
        assert result.recorded_derivation, "a new derivation should have been recorded"
        # The run.sh writes result.txt (a file) + revealed/ (a tree) into out_dir.
        out_names = {Path(a.path).name for a in result.outputs}
        assert "result.txt" in out_names, out_names
        assert "revealed" in out_names, out_names
        # Provenance stamp reflects the sandbox state of this host.
        assert result.provenance.get("sandboxed") == _sandbox.available()

        # Durable + lossless: the derivation and the revealed artifacts are in the
        # reloaded ledger.
        ledger = project.reload()
        assert any(d.capability == "unpack" for d in ledger.derivations.values()), \
            "derivation recorded under the skill's capability"
        # Every output artifact re-entered the ledger (revealed content is first-class).
        for art in result.outputs:
            assert ledger.has_artifact(art.content_hash), art.path
        # The revealed subtree registered as a tree kind.
        assert "tree" in ledger.kinds, ledger.kinds

        # Content-addressed cache: a second identical run is a no-op derivation.
        again = run_skill(
            skill, str(target), project,
            channel=_always_yes_channel(), policy=Policy.default(),
        )
        assert again.status == "ok"
        assert not again.recorded_derivation, "same bytes -> derivation is a cache hit"


def test_default_run_command_when_run_field_omitted():
    """With no ``run:`` field, the runner defaults to scripts/run.sh."""
    with temp_home() as (_home, skills_root, work):
        _write_skill(skills_root, "defaultrun-fixture", DEFAULTRUN_MD)
        target = _make_target(work)
        project = open_project(str(target))
        skill = _load(skills_root, "defaultrun-fixture")

        result = run_skill(
            skill, str(target), project,
            channel=_always_yes_channel(), policy=Policy.default(),
        )
        assert result.status == "ok", result.detail
        assert {Path(a.path).name for a in result.outputs} >= {"result.txt"}


def test_tier_gate_denied_skips_and_records_nothing():
    """A network-tier skill routed through a scripted channel that says NO is
    skipped — nothing destructive recorded, no derivation."""
    with temp_home() as (_home, skills_root, work):
        _write_skill(skills_root, "net-fixture", NET_MD)
        target = _make_target(work)
        project = open_project(str(target))
        skill = _load(skills_root, "net-fixture")

        before = len(project.ledger.derivations)
        channel = ScriptedHumanChannel([False])  # deny the gate
        result = run_skill(skill, str(target), project, channel=channel, policy=Policy.default())

        assert result.status == "skipped", result.detail
        assert not result.recorded_derivation
        assert len(project.ledger.derivations) == before, "denied gate records no derivation"
        # The gate actually asked (one confirm question).
        assert channel.asked and channel.asked[-1][1] == "confirm"


def test_tier_gate_allowed_runs():
    """The same network-tier skill runs when the human channel approves."""
    with temp_home() as (_home, skills_root, work):
        _write_skill(skills_root, "net-fixture", NET_MD)
        target = _make_target(work)
        project = open_project(str(target))
        skill = _load(skills_root, "net-fixture")

        channel = ScriptedHumanChannel([True])  # allow the gate
        result = run_skill(skill, str(target), project, channel=channel, policy=Policy.default())

        assert result.status == "ok", result.detail
        assert result.recorded_derivation
        assert channel.asked[-1][1] == "confirm"


def test_host_gate_missing_tool_records_lead_no_crash():
    """A skill requiring a missing host tool records an 'install X' lead and
    returns unavailable — it does not crash or run anything."""
    with temp_home() as (_home, skills_root, work):
        _write_skill(skills_root, "hostgated-fixture", HOSTGATED_MD)
        target = _make_target(work)
        project = open_project(str(target))
        skill = _load(skills_root, "hostgated-fixture")

        result = run_skill(
            skill, str(target), project,
            channel=_always_yes_channel(), policy=Policy.default(),
            environ={},  # empty env -> the declared env var is unset, tool off PATH
        )

        assert result.status == "unavailable", result.detail
        assert not result.recorded_derivation
        # A lead was recorded naming the missing tool.
        ledger = project.reload()
        assert any(
            "definitely-not-a-real-tool-xyz" in (lead.get("requires") or [])
            for lead in ledger.leads.values()
        ), ledger.leads
        assert not ledger.derivations, "unavailable skill records no derivation"


def test_read_only_skill_auto_runs_without_asking():
    """A read-only skill under the default policy auto-runs — the channel is never
    consulted (a scripted channel with zero answers would raise if asked)."""
    with temp_home() as (_home, skills_root, work):
        ro_md = UNPACK_MD.replace("tier: executes-untrusted", "tier: read-only")
        _write_skill(skills_root, "ro-fixture", ro_md.replace("unpack-fixture", "ro-fixture"))
        target = _make_target(work)
        project = open_project(str(target))
        skill = _load(skills_root, "ro-fixture")

        # Zero scripted answers: if the gate tried to ask, ScriptedHumanChannel raises.
        result = run_skill(
            skill, str(target), project,
            channel=ScriptedHumanChannel([]), policy=Policy.default(),
        )
        assert result.status == "ok", result.detail
        assert result.recorded_derivation


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
    print(f"\nall {len(ALL_TESTS)} runner tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
