"""``REKIT_SKILLPATH`` tests: external skill collections discovered via a search path.

Mirrors the ``REKIT_GOALPATH`` behaviour (``test_goalpacks.py``) for skills. Proves:

* **Search-path discovery** — a fixture skill dropped on a ``$REKIT_SKILLPATH`` dir
  is surfaced by ``discover_skills()`` (no install, no ``$REKIT_HOME`` copy). This
  is how rekit finds the ``parallax-goalpacks/skills`` collection at runtime.
* **os.pathsep-separated, multi-dir** — every dir on ``$REKIT_SKILLPATH`` is scanned.
* **Shadowing precedence** — builtin → ``$REKIT_SKILLPATH`` → user: a search-path
  skill shadows a same-named builtin, and a user skill shadows both. Builtins that
  aren't shadowed stay present.
* **Escape hatch intact** — ``discover_skills(root=…)`` still scans *only* that dir
  (no builtin, no search path, no user), so existing fixture-tree tests keep working.

Plain-python style (runnable via ``python tests/test_skillpath.py``) and
pytest-compatible. Pure stdlib; hermetic (temp ``$REKIT_HOME`` + ``$REKIT_SKILLPATH``,
no network).
"""

import contextlib
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.skills.registry import (  # noqa: E402
    SKILLPATH_ENV_VAR,
    builtin_skills_dir,
    discover_skills,
)

#: A couple of committed builtin skills we assert stay present alongside externals.
SOME_BUILTINS = {"extract-archive", "jadx"}


@contextlib.contextmanager
def temp_env():
    """Temp ``REKIT_HOME`` + ``REKIT_SKILLPATH`` (both restored afterwards), a temp
    skillpath dir, and a temp workspace. Yields ``(home, skillpath, work)`` as
    ``Path``s. The home starts empty (no user skills)."""
    saved_home = os.environ.get("REKIT_HOME")
    saved_skillpath = os.environ.get(SKILLPATH_ENV_VAR)
    with tempfile.TemporaryDirectory() as home, \
         tempfile.TemporaryDirectory() as skillpath, \
         tempfile.TemporaryDirectory() as work:
        os.environ["REKIT_HOME"] = home
        os.environ[SKILLPATH_ENV_VAR] = skillpath
        # A user skills dir must exist for $REKIT_HOME scanning, but stays empty.
        (Path(home) / "skills").mkdir()
        try:
            yield Path(home), Path(skillpath), Path(work)
        finally:
            for key, saved in (("REKIT_HOME", saved_home),
                               (SKILLPATH_ENV_VAR, saved_skillpath)):
                if saved is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = saved


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
        f"description: A fixture skill named {name} for REKIT_SKILLPATH tests.\n"
        f"---\n# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


# --------------------------------------------------------------------------------
# search-path discovery
# --------------------------------------------------------------------------------

def test_discover_finds_skillpath_skill():
    """A fixture on ``$REKIT_SKILLPATH`` is surfaced by ``discover_skills()`` — the
    external-collection case (how parallax-goalpacks/skills is found)."""
    with temp_env() as (_home, skillpath, _work):
        _drop_fixture_skill(skillpath, "on-skillpath")
        names = {s.name for s in discover_skills()}
        assert "on-skillpath" in names, f"REKIT_SKILLPATH skill missing: {names}"
        # Builtins still come along.
        assert SOME_BUILTINS <= names, (SOME_BUILTINS - names, names)


def test_skillpath_skill_resolves_from_that_dir():
    """The discovered skill's folder really lives under the ``$REKIT_SKILLPATH`` dir
    (not copied into ``$REKIT_HOME``)."""
    with temp_env() as (_home, skillpath, _work):
        _drop_fixture_skill(skillpath, "external")
        skill = next(s for s in discover_skills() if s.name == "external")
        assert skill.path.resolve().parent == skillpath.resolve(), skill.path


def test_skillpath_multiple_dirs_are_all_scanned():
    """``$REKIT_SKILLPATH`` is ``os.pathsep``-separated (like ``PATH``): every dir on
    it is scanned for ``*/SKILL.md``."""
    with temp_env() as (_home, skillpath, work):
        second = work / "more-skills"
        second.mkdir()
        _drop_fixture_skill(skillpath, "first")
        _drop_fixture_skill(second, "second")
        os.environ[SKILLPATH_ENV_VAR] = os.pathsep.join([str(skillpath), str(second)])
        names = {s.name for s in discover_skills()}
        assert {"first", "second"} <= names, names


def test_skillpath_absent_hides_external_skill():
    """Without ``$REKIT_SKILLPATH``, an external skill is NOT discovered — proving the
    search path (not something else) is what surfaced it, and builtins are unaffected."""
    with temp_env() as (_home, skillpath, _work):
        _drop_fixture_skill(skillpath, "only-on-path")
        os.environ.pop(SKILLPATH_ENV_VAR, None)
        names = {s.name for s in discover_skills()}
        assert "only-on-path" not in names, names
        assert SOME_BUILTINS <= names, "builtins must remain discoverable"


# --------------------------------------------------------------------------------
# shadowing precedence: builtin -> skillpath -> user
# --------------------------------------------------------------------------------

def test_skillpath_shadows_builtin():
    """A ``$REKIT_SKILLPATH`` skill of the same name as a builtin one wins (search
    path scanned after builtin). Other builtins remain present."""
    with temp_env() as (_home, skillpath, _work):
        assert (builtin_skills_dir() / "extract-archive" / "SKILL.md").is_file()
        _drop_fixture_skill(skillpath, "extract-archive", capability="code-understanding")
        chosen = next(s for s in discover_skills() if s.name == "extract-archive")
        # The winner is the search-path one (its folder lives under skillpath).
        assert chosen.path.resolve().parent == skillpath.resolve(), chosen.path
        assert chosen.capability == "code-understanding"  # the fixture, not the builtin
        assert "jadx" in {s.name for s in discover_skills()}  # unshadowed builtin stays


def test_user_shadows_skillpath_and_builtin():
    """A user skill (``$REKIT_HOME/skills``) shadows both a ``$REKIT_SKILLPATH`` skill
    and a builtin of the same name (user scanned last, wins)."""
    with temp_env() as (home, skillpath, _work):
        _drop_fixture_skill(skillpath, "extract-archive", capability="from-skillpath")
        _drop_fixture_skill(home / "skills", "extract-archive", capability="from-user")
        chosen = next(s for s in discover_skills() if s.name == "extract-archive")
        assert chosen.path.resolve().parent == (home / "skills").resolve(), chosen.path
        assert chosen.capability == "from-user"


# --------------------------------------------------------------------------------
# escape hatch stays intact
# --------------------------------------------------------------------------------

def test_explicit_root_ignores_skillpath_and_home():
    """``discover_skills(root=…)`` scans ONLY that dir — no builtin, no search path,
    no user — the fixture-tree escape hatch existing tests rely on."""
    with temp_env() as (_home, skillpath, work):
        fixture_root = work / "isolated"
        fixture_root.mkdir()
        _drop_fixture_skill(fixture_root, "isolated-only")
        # skillpath has a different skill that must NOT leak in.
        _drop_fixture_skill(skillpath, "should-not-appear")
        names = {s.name for s in discover_skills(root=fixture_root)}
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
    print(f"\nall {len(ALL_TESTS)} REKIT_SKILLPATH tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
