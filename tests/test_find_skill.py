"""E3/E4 tests: the ``FIND_SKILL`` runtime discovery action (loop).

The brain never sees the whole rack; it *searches* it by intent at runtime. This
proves the loop's half of that:

* :func:`_handle_find_skill` decides an outcome per match — an **available** skill's
  capability is discovered (widens scope), an **uninstalled** one becomes an install
  **lead**, a **policy-forbidden** one is reported and unreachable;
* through the real :func:`rekit.loop.run`, a ``FIND_SKILL`` for an uninstalled
  capability records an install lead in the ledger, while one that resolves to an
  available skill records none.

Deterministic: the "missing" fixture skill requires a host tool that cannot exist.
Pure stdlib.
"""

import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.harness import MockAdapter, MockTurn  # noqa: E402
from rekit.ledger import open_project  # noqa: E402
from rekit.loop import run  # noqa: E402
from rekit.loop.loop import _handle_find_skill  # noqa: E402
from rekit.skills import Policy  # noqa: E402
from rekit.skills.registry import Registry  # noqa: E402


# -- unit: the outcome decision (duck-typed fakes) --------------------------

@dataclass
class FakeSkill:
    name: str
    capability: str
    tier: str
    accepts: list = field(default_factory=list)
    avail: bool = True

    def available(self, environ=None, which=None):
        return self.avail


class FakeRegistry:
    def __init__(self, skills):
        self._skills = skills

    def find_skills(self, intent, limit=None, **kw):
        q = intent.lower()
        hits = [s for s in self._skills
                if any(tok in (s.name + " " + s.capability).lower() for tok in q.split())]
        return hits[:limit] if limit else hits


class FakeProject:
    def __init__(self):
        self.leads = []

    def record_lead(self, capability, kind, requires=None, **kw):
        self.leads.append((capability, kind, requires))


def test_available_match_discovers_capability():
    reg = FakeRegistry([FakeSkill("extract", "unpack", "read-only", ["archive"], True)])
    fp = FakeProject()
    fb, caps, leads = _handle_find_skill("FIND_SKILL: unpack archive", reg, fp, Policy.default())
    assert caps == {"unpack"} and leads == 0 and fp.leads == []
    assert "now in scope" in fb


def test_unavailable_match_records_lead():
    reg = FakeRegistry([FakeSkill("ghidra", "decompile", "executes-untrusted", ["binary"], False)])
    fp = FakeProject()
    fb, caps, leads = _handle_find_skill("FIND_SKILL: decompile binary", reg, fp, Policy.default())
    assert caps == set() and leads == 1
    assert fp.leads[0][0] == "decompile" and fp.leads[0][2] == ["ghidra"]
    assert "install lead" in fb


def test_forbidden_tier_reported_not_reached():
    reg = FakeRegistry([FakeSkill("wipe", "destroy", "destructive", ["file"], True)])
    fp = FakeProject()
    # read-only policy forbids the destructive tier.
    fb, caps, leads = _handle_find_skill("FIND_SKILL: destroy", reg, fp, Policy.read_only())
    assert caps == set() and leads == 0 and "forbidden" in fb


def test_no_match_reported():
    reg = FakeRegistry([FakeSkill("x", "unpack", "read-only")])
    fb, caps, leads = _handle_find_skill("FIND_SKILL: quantum teleportation", reg, FakeProject(), Policy.default())
    assert "no match" in fb and caps == set() and leads == 0


def test_no_find_skill_lines_is_noop():
    reg = FakeRegistry([FakeSkill("x", "unpack", "read-only")])
    assert _handle_find_skill("FINDING: x\nDONE", reg, FakeProject(), Policy.default()) == ("", set(), 0)


def test_none_registry_is_noop():
    assert _handle_find_skill("FIND_SKILL: anything", None, FakeProject(), Policy.default()) == ("", set(), 0)


# -- integration: FIND_SKILL through the real loop --------------------------

_AVAIL_SKILL = """---
name: avail-unpack
capability: unpack
accepts: [archive/zip]
emits: [tree]
tier: read-only
run: scripts/run.sh
keywords: [unpack, extract, archive, zip]
description: Unpack a zip archive into a tree of members.
---
# avail-unpack
"""

_MISSING_SKILL = """---
name: miss-decompile
capability: decompile
accepts: [binary/native]
emits: [source/c]
tier: executes-untrusted
host: no-such-tool-xyz-9000
run: scripts/run.sh
keywords: [decompile, native, binary, reverse]
description: Decompile a native binary to C source.
---
# miss-decompile
"""


def _fixture_registry():
    d = Path(tempfile.mkdtemp(prefix="rekit-skills-"))
    (d / "avail-unpack").mkdir()
    (d / "avail-unpack" / "SKILL.md").write_text(_AVAIL_SKILL, encoding="utf-8")
    (d / "miss-decompile").mkdir()
    (d / "miss-decompile" / "SKILL.md").write_text(_MISSING_SKILL, encoding="utf-8")
    return Registry.from_home(root=d)


def _temp_home():
    os.environ["REKIT_HOME"] = tempfile.mkdtemp(prefix="rekit-home-")


def _target():
    ws = tempfile.mkdtemp(prefix="rekit-target-")
    t = Path(ws) / "bin.dat"
    t.write_bytes(b"\x7fELF fake")
    return str(t)


def test_loop_find_skill_uninstalled_records_lead():
    _temp_home()
    registry = _fixture_registry()
    project = open_project(_target())
    adapter = MockAdapter([
        MockTurn(text="FIND_SKILL: decompile native binary\n"),
        MockTurn(text="DONE\n"),
    ])
    summary = run(project, "understand it", adapter, registry=registry, max_rounds=3)
    assert summary.done is True
    # The uninstalled decompiler became an install lead the operator can act on.
    assert any(cap == "decompile" for (cap, _kind) in project.ledger.leads)


def test_loop_find_skill_available_records_no_lead():
    _temp_home()
    registry = _fixture_registry()
    project = open_project(_target())
    adapter = MockAdapter([
        MockTurn(text="FIND_SKILL: unpack archive\n"),
        MockTurn(text="DONE\n"),
    ])
    run(project, "understand it", adapter, registry=registry, max_rounds=3)
    # An available match is discovered, not turned into a lead.
    assert not project.ledger.leads


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
    print(f"\nall {len(ALL_TESTS)} find-skill tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
