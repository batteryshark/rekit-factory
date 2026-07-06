"""E7.4/E7.5 tests: the lab catalog read-model (``rekit.lab.catalog``).

Proves the composer's two folds are pure and honest:

* :func:`skills_catalog` groups every discoverable skill **by capability**, sorts
  the groups and their members, counts the total, and reports a skill whose host
  tool cannot resolve as ``available: False`` (with a ``host`` hint);
* :func:`harnesses` lists the known brains — ``mock`` always available, ``pi``
  present — each a ``{"name", "status", "description"}`` row.

The "missing" fixture skill requires a host tool that cannot exist, so its
``available`` is deterministic. Fixtures are built like ``test_find_skill.py``: a
temp dir of ``<name>/SKILL.md`` folders, discovered via ``extra_roots``. Pure
stdlib.
"""

import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.lab.catalog import harnesses, skills_catalog  # noqa: E402


# -- fixture skills: two capabilities, one host that cannot resolve ----------

_UNPACK_SKILL = """---
name: fixture-unpack
capability: unpack
accepts: [archive/zip]
emits: [tree]
tier: read-only
run: scripts/run.sh
keywords: [unpack, extract, archive, zip]
description: Unpack a zip archive into a tree of members.
---
# fixture-unpack
"""

# A decompiler gated on a host tool that cannot exist -> available is False.
_DECOMPILE_SKILL = """---
name: fixture-decompile
capability: decompile
accepts: [binary/native]
emits: [source/c]
tier: executes-untrusted
host: no-such-tool-xyz-9000
run: scripts/run.sh
keywords: [decompile, native, binary, reverse]
description: Decompile a native binary to C source.
---
# fixture-decompile
"""

# A second unpack skill, so a capability group holds more than one member.
_ASAR_SKILL = """---
name: aardvark-asar
capability: unpack
accepts: [archive/asar]
emits: [tree]
tier: read-only
run: scripts/run.sh
keywords: [asar, electron, unpack]
description: Unpack an Electron asar archive.
---
# aardvark-asar
"""


def _fixture_roots():
    """A temp skills dir with three SKILL.md folders across two capabilities."""
    d = Path(tempfile.mkdtemp(prefix="rekit-catalog-"))
    for name, body in [
        ("fixture-unpack", _UNPACK_SKILL),
        ("fixture-decompile", _DECOMPILE_SKILL),
        ("aardvark-asar", _ASAR_SKILL),
    ]:
        (d / name).mkdir()
        (d / name / "SKILL.md").write_text(body, encoding="utf-8")
    return d


def _empty_home_environ():
    """An environ pointing REKIT_HOME at an empty dir, so only extra_roots discover."""
    home = tempfile.mkdtemp(prefix="rekit-home-")
    return {"REKIT_HOME": home}


def test_catalog_groups_by_capability():
    cat = skills_catalog(root=_fixture_roots())

    caps = [c["capability"] for c in cat["capabilities"]]
    # Alphabetical: decompile before unpack.
    assert caps == ["decompile", "unpack"]

    unpack = next(c for c in cat["capabilities"] if c["capability"] == "unpack")
    names = [s["name"] for s in unpack["skills"]]
    # Two members, sorted by name.
    assert names == ["aardvark-asar", "fixture-unpack"]


def test_catalog_total_counts_every_skill():
    cat = skills_catalog(root=_fixture_roots())
    counted = sum(len(c["skills"]) for c in cat["capabilities"])
    assert cat["total"] == 3 == counted


def test_missing_host_tool_is_unavailable():
    cat = skills_catalog(root=_fixture_roots())
    decompile = next(c for c in cat["capabilities"] if c["capability"] == "decompile")
    row = decompile["skills"][0]
    assert row["name"] == "fixture-decompile"
    assert row["available"] is False
    # The host hint is present so the composer can surface an "install X" lead.
    assert row["host"] == "no-such-tool-xyz-9000"


def test_available_skill_has_no_host_key():
    cat = skills_catalog(root=_fixture_roots())
    unpack = next(c for c in cat["capabilities"] if c["capability"] == "unpack")
    row = next(s for s in unpack["skills"] if s["name"] == "fixture-unpack")
    # No host declared -> available, and the host key is omitted entirely.
    assert row["available"] is True
    assert "host" not in row
    assert row["tier"] == "read-only"
    assert row["accepts"] == ["archive/zip"]


def test_harnesses_shape():
    hs = harnesses()
    assert isinstance(hs, list) and hs
    for h in hs:
        assert set(h) == {"name", "status", "description"}
        assert isinstance(h["description"], str) and h["description"]

    by_name = {h["name"]: h for h in hs}
    # mock is always available; pi is present (status best-effort).
    assert by_name["mock"]["status"] == "available"
    assert "pi" in by_name
    assert by_name["pi"]["status"] in {"available", "unconfigured"}


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
    print(f"\nall {len(ALL_TESTS)} catalog tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
