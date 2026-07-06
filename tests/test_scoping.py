"""E4 tests: the scoping resolver + trust-tier policy (card T-051).

Proves the exposure authority:

* ``scope_skills`` returns exactly ``(skills matching a present kind) ∩ (skills
  whose capability was requested)`` — nothing broader, nothing narrower;
* a **read-only goalpack policy** never yields a destructive/network skill (the
  headline acceptance criterion);
* a broader (default) policy includes the gated tiers, and each gated skill is
  flagged ``requires_gate`` while read-only skills are not.

Builds a temp ``$REKIT_HOME/skills`` of fixture skills spanning tiers,
capabilities, and kinds, then discovers a real :class:`Registry` over it.

Plain-python style (runnable via ``python tests/test_scoping.py``) and
pytest-compatible. Pure stdlib.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.skills import (  # noqa: E402
    Policy,
    Registry,
    scope_scoped_skills,
    scope_skills,
)


# --------------------------------------------------------------------------------
# Fixtures: skills across tiers / capabilities / kinds.
#   unpack-asar    read-only            unpack             accepts archive/asar
#   inspect-apk    read-only            code-understanding accepts archive/apk
#   jadx           executes-untrusted   decompile          accepts archive/apk, binary/dex
#   probe-endpoint network              code-understanding accepts source/js
#   wipe-target    destructive          cleanup            accepts archive/asar
# --------------------------------------------------------------------------------

UNPACK_ASAR = """---
name: unpack-asar
capability: unpack
accepts: [archive/asar]
emits: [source/js]
tier: read-only
description: Unpack an Electron asar archive.
---
# unpack-asar
"""

INSPECT_APK = """---
name: inspect-apk
capability: code-understanding
accepts: [archive/apk]
tier: read-only
description: Statically inspect an APK's manifest without decompiling.
---
# inspect-apk
"""

JADX = """---
name: jadx
capability: decompile
accepts: [archive/apk, binary/dex]
emits: [source/java]
tier: executes-untrusted
description: Decompile an APK/DEX to Java.
---
# jadx
"""

PROBE_ENDPOINT = """---
name: probe-endpoint
capability: code-understanding
accepts: [source/js]
tier: network
description: Reach out to a remote endpoint referenced in recovered source.
---
# probe-endpoint
"""

WIPE_TARGET = """---
name: wipe-target
capability: cleanup
accepts: [archive/asar]
tier: destructive
description: Delete extracted artifacts from disk.
---
# wipe-target
"""


def _write_skill(root: Path, folder: str, content: str):
    d = root / folder
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8")
    (d / "scripts").mkdir()
    return d


def _build_home() -> str:
    tmp = tempfile.mkdtemp(prefix="rekit-scope-")
    sk = Path(tmp) / "skills"
    sk.mkdir()
    _write_skill(sk, "unpack-asar", UNPACK_ASAR)
    _write_skill(sk, "inspect-apk", INSPECT_APK)
    _write_skill(sk, "jadx", JADX)
    _write_skill(sk, "probe-endpoint", PROBE_ENDPOINT)
    _write_skill(sk, "wipe-target", WIPE_TARGET)
    return tmp


def _registry(home: str) -> Registry:
    return Registry.from_home(environ={"REKIT_HOME": home})


# --------------------------------------------------------------------------------
# Policy unit tests
# --------------------------------------------------------------------------------

def test_default_policy_dispositions():
    p = Policy.default()
    assert p.is_auto("read-only")
    assert p.is_gated("network")
    assert p.is_gated("executes-untrusted")
    assert p.is_gated("destructive")
    assert p.allows("network") and not p.is_auto("network")


def test_read_only_policy_forbids_everything_else():
    p = Policy.read_only()
    assert p.is_auto("read-only")
    for tier in ("network", "executes-untrusted", "destructive"):
        assert not p.allows(tier), tier
        assert p.disposition(tier) == "forbid"


def test_paranoid_policy_forbids_destructive():
    p = Policy.paranoid()
    assert p.is_gated("network")
    assert not p.allows("destructive")


def test_policy_rejects_unknown_tier_or_disposition():
    for bad in ({"nope": "auto"}, {"read-only": "maybe"}):
        try:
            Policy(tiers=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Policy({bad}) should have raised")


def test_unlisted_tier_fails_closed():
    # A policy that lists only read-only forbids any tier it doesn't mention.
    p = Policy(tiers={"read-only": "auto"})
    assert p.disposition("destructive") == "forbid"


# --------------------------------------------------------------------------------
# scope_skills: the intersection
# --------------------------------------------------------------------------------

def test_intersection_of_kinds_and_capabilities():
    home = _build_home()
    try:
        reg = _registry(home)
        # An asar is present; the goalpack wants unpack + code-understanding.
        # Only unpack-asar matches BOTH a present kind (archive/asar) AND a
        # requested capability. inspect-apk's capability is requested but its kind
        # (archive/apk) is absent; wipe-target's kind matches but 'cleanup' was
        # not requested.
        scoped = scope_skills(
            reg,
            present_kinds=["archive/asar"],
            requested_capabilities=["unpack", "code-understanding"],
            policy=Policy.default(),
        )
        assert {s.name for s in scoped} == {"unpack-asar"}, [s.name for s in scoped]
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_kind_present_but_capability_not_requested_is_excluded():
    home = _build_home()
    try:
        reg = _registry(home)
        # apk present, but only 'unpack' requested -> jadx(decompile) and
        # inspect-apk(code-understanding) both excluded on capability.
        scoped = scope_skills(
            reg,
            present_kinds=["archive/apk"],
            requested_capabilities=["unpack"],
            policy=Policy.default(),
        )
        assert scoped == [], [s.name for s in scoped]
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_capability_requested_but_kind_absent_is_excluded():
    home = _build_home()
    try:
        reg = _registry(home)
        # code-understanding requested, but no kind present that any
        # code-understanding skill accepts.
        scoped = scope_skills(
            reg,
            present_kinds=["binary/pe"],
            requested_capabilities=["code-understanding"],
            policy=Policy.default(),
        )
        assert scoped == [], [s.name for s in scoped]
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_family_kind_matches_members():
    home = _build_home()
    try:
        reg = _registry(home)
        # The 'archive' family present -> both archive/asar and archive/apk
        # skills are kind-relevant; requesting all their capabilities yields them.
        scoped = scope_skills(
            reg,
            present_kinds=["archive"],
            requested_capabilities=["unpack", "code-understanding", "decompile", "cleanup"],
            policy=Policy.default(),
        )
        assert {s.name for s in scoped} == {
            "unpack-asar", "inspect-apk", "jadx", "wipe-target"
        }, [s.name for s in scoped]
    finally:
        shutil.rmtree(home, ignore_errors=True)


# --------------------------------------------------------------------------------
# Tier filtering: the headline acceptance
# --------------------------------------------------------------------------------

def test_read_only_goalpack_never_sees_network_or_destructive():
    home = _build_home()
    try:
        reg = _registry(home)
        # Every kind present and every capability requested — the widest surface.
        # A read-only policy must still yield ONLY read-only skills.
        scoped = scope_skills(
            reg,
            present_kinds=["archive/asar", "archive/apk", "binary/dex", "source/js"],
            requested_capabilities=["unpack", "code-understanding", "decompile", "cleanup"],
            policy=Policy.read_only(),
        )
        names = {s.name for s in scoped}
        assert names == {"unpack-asar", "inspect-apk"}, names
        # Explicitly: no network / executes-untrusted / destructive skill leaked.
        tiers = {s.tier for s in scoped}
        assert tiers == {"read-only"}, tiers
        assert "probe-endpoint" not in names  # network
        assert "jadx" not in names             # executes-untrusted
        assert "wipe-target" not in names      # destructive
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_broader_policy_includes_gated_and_flags_them():
    home = _build_home()
    try:
        reg = _registry(home)
        scoped = scope_scoped_skills(
            reg,
            present_kinds=["archive/asar", "archive/apk", "binary/dex", "source/js"],
            requested_capabilities=["unpack", "code-understanding", "decompile", "cleanup"],
            policy=Policy.default(),
        )
        by_name = {s.name: s for s in scoped}
        # Default policy exposes all four requested capabilities across present
        # kinds, including the gated tiers.
        assert set(by_name) == {
            "unpack-asar", "inspect-apk", "probe-endpoint", "jadx", "wipe-target"
        }, set(by_name)
        # Read-only skills auto-run; gated tiers are flagged for the human channel.
        assert by_name["unpack-asar"].requires_gate is False
        assert by_name["inspect-apk"].requires_gate is False
        assert by_name["probe-endpoint"].requires_gate is True   # network
        assert by_name["jadx"].requires_gate is True             # executes-untrusted
        assert by_name["wipe-target"].requires_gate is True      # destructive
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_paranoid_policy_drops_destructive_keeps_gated():
    home = _build_home()
    try:
        reg = _registry(home)
        scoped = scope_skills(
            reg,
            present_kinds=["archive/asar", "archive/apk", "source/js"],
            requested_capabilities=["unpack", "code-understanding", "cleanup"],
            policy=Policy.paranoid(),
        )
        names = {s.name for s in scoped}
        # wipe-target (destructive) forbidden; probe-endpoint (network) still in.
        assert "wipe-target" not in names, names
        assert "probe-endpoint" in names, names
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_available_only_drops_unresolved_host():
    # A skill with an unresolved host tool is dropped when available_only=True.
    tmp = tempfile.mkdtemp(prefix="rekit-scope-host-")
    try:
        sk = Path(tmp) / "skills"
        sk.mkdir()
        _write_skill(sk, "unpack-asar", UNPACK_ASAR)  # no host -> always available
        _write_skill(sk, "needs-tool", """---
name: needs-tool
capability: unpack
accepts: [archive/asar]
tier: read-only
host: ghidra
env: GHIDRA_HOME
description: Needs an unresolved host tool.
---
# needs-tool
""")
        reg = Registry.from_home(environ={"REKIT_HOME": tmp})
        no_path = lambda name: None  # noqa: E731
        scoped = scope_skills(
            reg,
            present_kinds=["archive/asar"],
            requested_capabilities=["unpack"],
            policy=Policy.default(),
            available_only=True,
            environ={},
            which=no_path,
        )
        # needs-tool dropped (host unresolved); unpack-asar survives.
        assert {s.name for s in scoped} == {"unpack-asar"}, [s.name for s in scoped]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_empty_inputs_yield_empty_scope():
    home = _build_home()
    try:
        reg = _registry(home)
        assert scope_skills(reg, [], ["unpack"], Policy.default()) == []
        assert scope_skills(reg, ["archive/asar"], [], Policy.default()) == []
    finally:
        shutil.rmtree(home, ignore_errors=True)


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
    print(f"\nall {len(ALL_TESTS)} scoping tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
