"""Skill discovery tests: builtin + ``$REKIT_HOME/skills`` + the ``extra_roots`` param.

``REKIT_HOME`` is the only env var. ``discover_skills()`` scans two roots — the
committed builtins (``rekit/skills``) and ``$REKIT_HOME/skills`` — and a caller may
pass ``extra_roots`` (a direct-path parameter, not an env search path) for extra
skill dirs scanned last. Proves:

* **user discovery** — a fixture skill dropped in ``$REKIT_HOME/skills`` is surfaced
  by ``discover_skills()`` (zero install), alongside the builtins.
* **extra_roots** — a fixture in a dir passed via ``extra_roots=[...]`` is surfaced;
  without passing it, it is NOT discovered (proving the param, not an env var, is
  what surfaced it). Multiple ``extra_roots`` are all scanned.
* **shadowing precedence** — builtin → user → extra_roots: a user skill shadows a
  same-named builtin, and an ``extra_roots`` skill shadows both. Unshadowed builtins
  stay present.
* **escape hatch intact** — ``discover_skills(root=…)`` still scans *only* that dir
  (no builtin, no user, no extra_roots).

Plain-python style (runnable via ``python tests/test_skill_discovery.py``) and
pytest-compatible. Pure stdlib; hermetic (temp ``$REKIT_HOME``, no network).
"""

import contextlib
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.skills.registry import (  # noqa: E402
    builtin_skills_dir,
    discover_skills,
)

#: A couple of committed builtin skills we assert stay present alongside externals.
SOME_BUILTINS = {"extract-archive", "jadx"}


@contextlib.contextmanager
def temp_home():
    """Temp ``REKIT_HOME`` (restored afterwards) with an empty ``skills/`` dir, plus a
    temp workspace. Yields ``(home, work)`` as ``Path``s."""
    saved_home = os.environ.get("REKIT_HOME")
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as work:
        os.environ["REKIT_HOME"] = home
        (Path(home) / "skills").mkdir()
        try:
            yield Path(home), Path(work)
        finally:
            if saved_home is None:
                os.environ.pop("REKIT_HOME", None)
            else:
                os.environ["REKIT_HOME"] = saved_home


def _drop_fixture_skill(root: Path, name: str, *, capability: str = "code-understanding",
                        tier: str = "read-only") -> Path:
    """Write a minimal valid ``<root>/<name>/SKILL.md`` (+ scripts/) fixture."""
    skill_dir = root / name
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\n"
        f"name: {name}\n"
        f"capability: {capability}\n"
        f"tier: {tier}\n"
        f"accepts: [source/python]\n"
        f"emits: [analysis/observations]\n"
        f"run: scripts/run.sh\n"
        f"description: A fixture skill named {name} for skill-discovery tests.\n"
        f"---\n# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


# --------------------------------------------------------------------------------
# builtin + $REKIT_HOME/skills discovery
# --------------------------------------------------------------------------------

def test_discover_finds_builtin_skills():
    """The committed builtins under ``rekit/skills`` are surfaced by default."""
    with temp_home():
        names = {s.name for s in discover_skills()}
        assert SOME_BUILTINS <= names, (SOME_BUILTINS - names, names)


def test_discover_finds_user_skill():
    """A fixture in ``$REKIT_HOME/skills`` is surfaced (zero install) alongside the
    builtins."""
    with temp_home() as (home, _work):
        _drop_fixture_skill(home / "skills", "user-skill")
        names = {s.name for s in discover_skills()}
        assert "user-skill" in names, f"user skill missing: {names}"
        assert SOME_BUILTINS <= names, "builtins must remain discoverable"


# --------------------------------------------------------------------------------
# extra_roots — the direct-path parameter (replaces the old env search path)
# --------------------------------------------------------------------------------

def test_extra_roots_surfaces_skill():
    """A fixture in a dir passed via ``extra_roots`` is surfaced — the direct-path
    mechanism for a goalpack's bundled skills / ad-hoc ``--tools`` dirs."""
    with temp_home() as (_home, work):
        extra = work / "tools"
        extra.mkdir()
        _drop_fixture_skill(extra, "from-extra")
        names = {s.name for s in discover_skills(extra_roots=[extra])}
        assert "from-extra" in names, f"extra_roots skill missing: {names}"
        # Builtins still come along.
        assert SOME_BUILTINS <= names, (SOME_BUILTINS - names, names)


def test_extra_roots_not_passed_hides_skill():
    """Without passing ``extra_roots``, that dir's skill is NOT discovered — proving
    the parameter (not an env var) is what surfaced it."""
    with temp_home() as (_home, work):
        extra = work / "tools"
        extra.mkdir()
        _drop_fixture_skill(extra, "only-in-extra")
        names = {s.name for s in discover_skills()}
        assert "only-in-extra" not in names, names
        assert SOME_BUILTINS <= names, "builtins must remain discoverable"


def test_extra_roots_multiple_all_scanned():
    """Every dir in ``extra_roots`` is scanned, in order."""
    with temp_home() as (_home, work):
        first = work / "tools-a"
        second = work / "tools-b"
        first.mkdir()
        second.mkdir()
        _drop_fixture_skill(first, "first")
        _drop_fixture_skill(second, "second")
        names = {s.name for s in discover_skills(extra_roots=[first, second])}
        assert {"first", "second"} <= names, names


def test_extra_roots_resolves_from_that_dir():
    """The discovered skill's folder really lives under the ``extra_roots`` dir."""
    with temp_home() as (_home, work):
        extra = work / "tools"
        extra.mkdir()
        _drop_fixture_skill(extra, "external")
        skill = next(s for s in discover_skills(extra_roots=[extra]) if s.name == "external")
        assert skill.path.resolve().parent == extra.resolve(), skill.path


# --------------------------------------------------------------------------------
# shadowing precedence: builtin -> user -> extra_roots
# --------------------------------------------------------------------------------

def test_user_shadows_builtin():
    """A user skill (``$REKIT_HOME/skills``) of the same name as a builtin wins
    (user scanned after builtin). Other builtins remain present."""
    with temp_home() as (home, _work):
        assert (builtin_skills_dir() / "extract-archive" / "SKILL.md").is_file()
        _drop_fixture_skill(home / "skills", "extract-archive", capability="from-user")
        chosen = next(s for s in discover_skills() if s.name == "extract-archive")
        assert chosen.path.resolve().parent == (home / "skills").resolve(), chosen.path
        assert chosen.capability == "from-user"  # the fixture, not the builtin
        assert "jadx" in {s.name for s in discover_skills()}  # unshadowed builtin stays


def test_extra_roots_shadows_user_and_builtin():
    """An ``extra_roots`` skill shadows both a user skill and a builtin of the same
    name (extra_roots scanned last, wins)."""
    with temp_home() as (home, work):
        extra = work / "tools"
        extra.mkdir()
        _drop_fixture_skill(home / "skills", "extract-archive", capability="from-user")
        _drop_fixture_skill(extra, "extract-archive", capability="from-extra")
        chosen = next(
            s for s in discover_skills(extra_roots=[extra]) if s.name == "extract-archive"
        )
        assert chosen.path.resolve().parent == extra.resolve(), chosen.path
        assert chosen.capability == "from-extra"


# --------------------------------------------------------------------------------
# escape hatch stays intact
# --------------------------------------------------------------------------------

def test_explicit_root_ignores_home_and_extra_roots():
    """``discover_skills(root=…)`` scans ONLY that dir — no builtin, no user, no
    extra_roots — the fixture-tree escape hatch existing tests rely on."""
    with temp_home() as (home, work):
        fixture_root = work / "isolated"
        fixture_root.mkdir()
        _drop_fixture_skill(fixture_root, "isolated-only")
        # A user skill and an extra_roots skill that must NOT leak in.
        _drop_fixture_skill(home / "skills", "should-not-appear")
        extra = work / "tools"
        extra.mkdir()
        _drop_fixture_skill(extra, "also-not-appear")
        names = {s.name for s in discover_skills(root=fixture_root, extra_roots=[extra])}
        assert names == {"isolated-only"}, names


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
    print(f"\nall {len(ALL_TESTS)} skill-discovery tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
