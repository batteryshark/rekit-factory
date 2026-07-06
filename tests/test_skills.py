"""E3 tests: filesystem skill discovery + the searchable registry.

Proves the framework parts of card T-050 (skill *relocation* is a follow-up):

* filesystem discovery with **zero install** — a temp ``$REKIT_HOME/skills`` of
  SKILL.md folders is found by convention;
* ``find_skills(intent)`` surfaces a skill by description — the creds-in-a-text-file
  case, which has no special artifact kind;
* ``skills_for_kind`` (with family match) and ``skills_by_capability`` return the
  right skills;
* host-gating: available when env/path present, unavailable otherwise;
* the new ``tier`` field parses.

Plain-python style (runnable via ``python tests/test_skills.py``) and
pytest-compatible. Pure stdlib — no pytest/pyyaml import.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.skills import (  # noqa: E402
    DEFAULT_TIER,
    HostRequirement,
    Registry,
    Skill,
    discover_skills,
    load_skill,
    rekit_home,
    skills_dir,
)


# --------------------------------------------------------------------------------
# Fixtures: build a temp REKIT_HOME/skills with a few SKILL.md folders.
# --------------------------------------------------------------------------------

JADX_SKILL = """---
name: jadx
capability: decompile
accepts: [archive/apk, binary/dex]
emits: [source/java]
tier: executes-untrusted
host: jadx (JVM required)
env: JADX_HOME
keywords: [android, dalvik, apk]
description: >-
  Decompile an Android APK or DEX back to readable Java source so a workflow can
  analyze the recovered code.
---

# jadx — Android APK/DEX -> Java

Body prose that discovery ignores for the description because frontmatter has one.
"""

UNPACK_ASAR_SKILL = """---
name: unpack-asar
capability: unpack
accepts:
  - archive/asar
emits:
  - source/js
tier: read-only
keywords: [electron, asar, bundle]
description: >-
  Unpack an Electron asar archive into its constituent JavaScript files.
---

# unpack-asar

Extracts the contents of an .asar bundle.
"""

# A code-understanding skill with NO special artifact kind — reachable only by
# intent search over its description (the creds-in-a-text-file case).
CREDS_SKILL = """---
name: probe-endpoint
capability: code-understanding
tier: network
keywords: [credentials, secrets, remote, endpoint]
description: >-
  Analyze a remote endpoint and the credentials used to reach it. Use when a text
  file names a remote service plus API keys or credentials and you need to see what
  that endpoint actually serves.
---

# probe-endpoint

Reaches out to a named remote service to characterize it.
"""


def _write_skill(root: Path, folder: str, content: str, *, with_scripts: bool = True):
    d = root / folder
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8")
    if with_scripts:
        (d / "scripts").mkdir()
    return d


def _build_home() -> str:
    tmp = tempfile.mkdtemp(prefix="rekit-home-")
    sk = Path(tmp) / "skills"
    sk.mkdir()
    _write_skill(sk, "jadx", JADX_SKILL)
    _write_skill(sk, "unpack-asar", UNPACK_ASAR_SKILL)
    _write_skill(sk, "probe-endpoint", CREDS_SKILL)
    # A junk folder with no SKILL.md must be silently ignored.
    (sk / "not-a-skill").mkdir()
    return tmp


# --------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------

def test_home_resolution():
    # Explicit env override wins and is expanded/absolute.
    home = rekit_home({"REKIT_HOME": "/tmp/rekit-x"})
    assert home == Path("/tmp/rekit-x").resolve()
    assert skills_dir({"REKIT_HOME": "/tmp/rekit-x"}) == Path("/tmp/rekit-x").resolve() / "skills"
    # Default falls back to ~/.rekit when unset.
    default = rekit_home({})
    assert default == (Path.home() / ".rekit").resolve()


def test_discovery_finds_dropped_folders():
    home = _build_home()
    try:
        environ = {"REKIT_HOME": home}
        found = discover_skills(environ=environ)
        names = {s.name for s in found}
        # Three real skills; the junk folder without a SKILL.md is ignored.
        assert names == {"jadx", "unpack-asar", "probe-endpoint"}, names
        # Discovery is by convention off REKIT_HOME — zero install.
        assert all(isinstance(s, Skill) for s in found)
        jadx = next(s for s in found if s.name == "jadx")
        # scripts_dir sits under the (resolved) home skills dir; compare against
        # the resolved root, since rekit_home canonicalizes symlinks (e.g. macOS
        # /var -> /private/var).
        assert jadx.scripts_dir == skills_dir(environ) / "jadx" / "scripts"
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_discovery_missing_home_is_empty():
    # A missing REKIT_HOME must not crash — just no skills.
    assert discover_skills(environ={"REKIT_HOME": "/nonexistent/rekit/home/xyz"}) == []


def test_tier_parses():
    home = _build_home()
    try:
        reg = Registry.from_home(environ={"REKIT_HOME": home})
        assert reg.get("jadx").tier == "executes-untrusted"
        assert reg.get("unpack-asar").tier == "read-only"
        assert reg.get("probe-endpoint").tier == "network"
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_tier_defaults_safe():
    # A skill that omits tier defaults to the safe read-only tier.
    tmp = tempfile.mkdtemp(prefix="rekit-home-")
    try:
        sk = Path(tmp) / "skills"
        sk.mkdir()
        _write_skill(sk, "plain", "---\nname: plain\ncapability: noop\n---\n\n# plain\n")
        reg = Registry.from_home(environ={"REKIT_HOME": tmp})
        assert reg.get("plain").tier == DEFAULT_TIER == "read-only"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_find_skills_by_intent_surfaces_creds_skill():
    home = _build_home()
    try:
        reg = Registry.from_home(environ={"REKIT_HOME": home})
        ranked = reg.find_skills("analyze remote endpoint credentials")
        assert ranked, "expected at least one match"
        # The creds skill (no special artifact kind) must rank first, purely by
        # description/keywords — proving intent search, not kind routing.
        assert ranked[0].name == "probe-endpoint", [s.name for s in ranked]
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_find_skills_ranks_decompile_by_intent():
    home = _build_home()
    try:
        reg = Registry.from_home(environ={"REKIT_HOME": home})
        ranked = reg.find_skills("decompile an android apk to java")
        assert ranked[0].name == "jadx", [s.name for s in ranked]
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_skills_for_kind_exact_and_family():
    home = _build_home()
    try:
        reg = Registry.from_home(environ={"REKIT_HOME": home})

        # Exact kind: only unpack-asar accepts archive/asar.
        asar = reg.skills_for_kind("archive/asar")
        assert {s.name for s in asar} == {"unpack-asar"}, [s.name for s in asar]

        # Exact kind: jadx accepts binary/dex.
        dex = reg.skills_for_kind("binary/dex")
        assert {s.name for s in dex} == {"jadx"}, [s.name for s in dex]

        # Family query "archive" matches any archive/* member (jadx: archive/apk,
        # unpack-asar: archive/asar).
        archive = reg.skills_for_kind("archive")
        assert {s.name for s in archive} == {"jadx", "unpack-asar"}, [s.name for s in archive]

        # Nothing accepts a source kind.
        assert reg.skills_for_kind("source/python") == []
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_skills_by_capability():
    home = _build_home()
    try:
        reg = Registry.from_home(environ={"REKIT_HOME": home})
        assert {s.name for s in reg.skills_by_capability("decompile")} == {"jadx"}
        assert {s.name for s in reg.skills_by_capability("unpack")} == {"unpack-asar"}
        assert {s.name for s in reg.skills_by_capability("code-understanding")} == {"probe-endpoint"}
        assert reg.skills_by_capability("nonesuch") == []
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_host_gating_env_var():
    home = _build_home()
    try:
        skill = load_skill(Path(home) / "skills" / "jadx" / "SKILL.md",
                           environ={"REKIT_HOME": home})
        assert skill.host is not None
        # `which` that never finds anything on PATH.
        no_path = lambda name: None  # noqa: E731

        # Unavailable when neither JADX_HOME set, nor on PATH, nor in bin/.
        assert not skill.available(environ={}, which=no_path)

        # Available when the declared env var is set (non-empty).
        assert skill.available(environ={"JADX_HOME": "/opt/jadx"}, which=no_path)

        # An empty env var does not count as satisfied.
        assert not skill.available(environ={"JADX_HOME": ""}, which=no_path)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_host_gating_path_lookup():
    home = _build_home()
    try:
        skill = load_skill(Path(home) / "skills" / "jadx" / "SKILL.md",
                           environ={"REKIT_HOME": home})
        # `which` that resolves the tool: available even with no env var.
        found = lambda name: f"/usr/local/bin/{name}"  # noqa: E731
        assert skill.available(environ={}, which=found)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_host_gating_shared_bin_dir():
    # A shared REKIT_HOME/bin containing the tool satisfies the gate (proves the
    # shared-bin resolution path from the epic's REKIT_HOME layout).
    home = _build_home()
    try:
        bin_d = Path(home) / "bin"
        bin_d.mkdir()
        (bin_d / "jadx").write_text("#!/bin/sh\n", encoding="utf-8")
        skill = load_skill(Path(home) / "skills" / "jadx" / "SKILL.md",
                           environ={"REKIT_HOME": home})
        no_path = lambda name: None  # noqa: E731
        assert skill.available(environ={"REKIT_HOME": home}, which=no_path)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_registry_available_only_filter():
    home = _build_home()
    try:
        reg = Registry.from_home(environ={"REKIT_HOME": home})
        no_path = lambda name: None  # noqa: E731
        # jadx has a host gate; with nothing present it is unavailable.
        decompilers = reg.skills_by_capability(
            "decompile", available_only=True, environ={}, which=no_path)
        assert decompilers == []
        # unpack-asar declares no host -> always available.
        unpackers = reg.skills_by_capability(
            "unpack", available_only=True, environ={}, which=no_path)
        assert {s.name for s in unpackers} == {"unpack-asar"}
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_host_requirement_reimplements_satisfied():
    req = HostRequirement(name="ghidra", env="GHIDRA_HOME", paths=("/opt/ghidra",))
    no_path = lambda name: None  # noqa: E731
    assert req.satisfied(environ={"GHIDRA_HOME": "/opt/g"}, which=no_path)
    assert not req.satisfied(environ={}, which=no_path)
    assert req.satisfied(environ={}, which=lambda n: f"/bin/{n}")


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
    print(f"\nall {len(ALL_TESTS)} skill tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
