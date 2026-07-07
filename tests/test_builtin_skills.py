"""Builtin-skill relocation tests: the transform skills now ship in the rekit repo.

Proves the three things the relocation had to deliver:

* **Builtin discovery** — ``discover_skills()`` (with an empty temp ``$REKIT_HOME``)
  finds the skills committed at the repo root ``rekit/skills/*/SKILL.md``:
  ``unpack-asar``, ``extract-archive``, ``jadx``, ``ghidra``, ``dex2jar``, ``ilspy``.
  A **user** skill of the same name shadows the builtin one.
* **End-to-end (real, no mocks)** — a hand-built asar is fed to
  ``run_skill(unpack-asar, …)``: it extracts (sandboxed where available, pure
  Python), records a derivation, and the revealed tree re-enters the ledger.
  ``unpack-asar`` is tier ``read-only`` → it auto-runs with no human gate.
* **Graceful host-gating** — the decompile skills (``jadx``/``ghidra``/…) are
  discovered but, with their host tool absent, report ``available()==False`` (the
  runner would degrade them to an "install X" lead), never a crash.

Plain-python style (runnable via ``python tests/test_builtin_skills.py``) and
pytest-compatible. Pure stdlib; hermetic (temp ``$REKIT_HOME``, no network).
"""

import contextlib
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.human import ScriptedHumanChannel  # noqa: E402
from rekit.ledger import open_project  # noqa: E402
from rekit.skills import (  # noqa: E402
    Policy,
    Registry,
    builtin_skills_dir,
    discover_skills,
    run_skill,
)
from rekit.skills import sandbox as _sandbox  # noqa: E402

#: The skills committed at the repo root that discovery must surface.
BUILTIN_NAMES = {"unpack-asar", "extract-archive", "jadx", "ghidra", "dex2jar", "ilspy"}
#: Of those, the host-gated (decompile-family) skills that degrade without a tool.
HOST_GATED_NAMES = {"jadx", "ghidra", "dex2jar", "ilspy"}


@contextlib.contextmanager
def temp_home():
    """A temp ``$REKIT_HOME`` (restored after) + a temp workspace. Yields
    ``(home, skills_root, work)``. The home starts EMPTY — no user skills — so
    whatever discovery finds is the committed builtin set."""
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


def _make_asar(files: dict) -> bytes:
    """Pack ``{relpath: bytes}`` into asar bytes matching the extractor's parser.

    The asar is built here (not shipped as a binary fixture) so the parser is
    exercised against bytes this test controls.
    Framing: ``[uint32=4][uint32 header_size][uint32 json_len][json…][data…]``.
    """
    blob = b""
    tree: dict = {"files": {}}
    for relpath, content in files.items():
        parts = relpath.split("/")
        node = tree
        for part in parts[:-1]:
            node = node["files"].setdefault(part, {"files": {}})
        node["files"][parts[-1]] = {"offset": str(len(blob)), "size": len(content)}
        blob += content
    json_bytes = json.dumps(tree).encode("utf-8")
    pad = (4 - (len(json_bytes) % 4)) % 4
    json_padded = json_bytes + b"\x00" * pad
    payload = struct.pack("<I", len(json_bytes)) + json_padded   # [json_len][json]
    header_buf = struct.pack("<I", len(payload)) + payload        # headerPickle.toBuffer
    size_buf = struct.pack("<I", 4) + struct.pack("<I", len(header_buf))
    return size_buf + header_buf + blob


# --------------------------------------------------------------------------------
# (1) discovery
# --------------------------------------------------------------------------------

def test_discovery_finds_builtin_skills():
    """With an empty ``$REKIT_HOME``, ``discover_skills()`` surfaces the committed
    builtin skills — no install, resolved relative to the package."""
    with temp_home():
        found = {s.name for s in discover_skills()}
        assert BUILTIN_NAMES <= found, (BUILTIN_NAMES - found, found)
        # The builtin root really is the repo-root skills/ (parents[2]/"skills").
        assert builtin_skills_dir().is_dir(), builtin_skills_dir()
        assert (builtin_skills_dir() / "unpack-asar" / "SKILL.md").is_file()


def test_builtin_skill_metadata_parses():
    """The relocated SKILL.md files carry the rekit fields: tier, capability,
    accepts/emits, and (for host tools) the host gate + env var."""
    with temp_home():
        reg = Registry.from_home()

        asar = reg.get("unpack-asar")
        assert asar is not None and asar.tier == "read-only"
        assert asar.capability == "unpack" and asar.accepts == ("archive/asar",)
        assert asar.emits == ("tree",) and asar.host is None  # pure python, no host

        extract = reg.get("extract-archive")
        assert extract is not None and extract.tier == "read-only"
        assert extract.capability == "unpack" and extract.host is None
        assert extract.accepts_kind("archive/zip") and extract.accepts_kind("archive/apk")

        jadx = reg.get("jadx")
        assert jadx.tier == "executes-untrusted" and jadx.capability == "decompile"
        assert jadx.host is not None and jadx.host.name == "jadx"
        assert jadx.host.env == "JADX_HOME"

        dex2jar = reg.get("dex2jar")
        assert dex2jar.capability == "dex-to-jar" and dex2jar.emits == ("archive/jar",)

        ilspy = reg.get("ilspy")
        assert ilspy.capability == "decompile" and ilspy.emits == ("source/csharp",)


def test_user_skill_shadows_builtin():
    """A user skill of the same name as a builtin one wins (user scanned last)."""
    with temp_home() as (_home, skills_root, _work):
        shadow = skills_root / "unpack-asar"
        (shadow / "scripts").mkdir(parents=True)
        (shadow / "SKILL.md").write_text(
            "---\nname: unpack-asar\ncapability: unpack\ntier: read-only\n"
            "description: A user override of the builtin unpack-asar.\n---\n# override\n",
            encoding="utf-8",
        )
        reg = Registry.from_home()
        chosen = reg.get("unpack-asar")
        assert chosen is not None
        # The shadowing skill is the user one (its folder lives under REKIT_HOME).
        assert str(shadow.resolve()) == str(chosen.path.resolve()), chosen.path
        # The builtins that were not shadowed are still present.
        names = {s.name for s in reg.skills}
        assert {"jadx", "ghidra", "dex2jar", "ilspy", "extract-archive"} <= names


# --------------------------------------------------------------------------------
# (2) end-to-end: unpack-asar runs for real and the tree re-enters the ledger
# --------------------------------------------------------------------------------

def test_unpack_asar_runs_end_to_end():
    """A hand-built asar → run_skill(unpack-asar) → extracted (sandboxed, pure
    python), a derivation recorded, and the revealed tree back in the ledger.
    Read-only tier → auto-runs, so a zero-answer channel is never consulted."""
    with temp_home() as (_home, _skills_root, work):
        asar_bytes = _make_asar({
            "index.js": b"console.log('hi')\n",
            "pkg/lib.js": b"module.exports = 42\n",
        })
        target = work / "app.asar"
        target.write_bytes(asar_bytes)

        project = open_project(str(target))
        skill = Registry.from_home().get("unpack-asar")
        assert skill is not None and skill.tier == "read-only"

        result = run_skill(
            skill, str(target), project,
            channel=ScriptedHumanChannel([]),  # read-only auto-runs; asking would raise
            policy=Policy.default(),
        )

        assert result.status == "ok", result.detail
        assert result.recorded_derivation, "a new derivation should have been recorded"
        # The extractor wrote a tree (the top-level entries of the asar) into out_dir.
        out_names = {Path(a.path).name for a in result.outputs}
        assert {"index.js", "pkg"} <= out_names, out_names
        # Provenance reflects this host's sandbox state (true on macOS w/ seatbelt).
        assert result.provenance.get("sandboxed") == _sandbox.available()

        # Durable + lossless: derivation + revealed artifacts survive a reload.
        ledger = project.reload()
        assert any(d.capability == "unpack" for d in ledger.derivations.values()), \
            "derivation recorded under the unpack capability"
        for art in result.outputs:
            assert ledger.has_artifact(art.content_hash), art.path
        # The revealed subdir (pkg/) registered as a tree kind — content re-entered.
        assert "tree" in ledger.kinds, ledger.kinds

        # Content-addressed cache: a second identical run is a no-op derivation.
        again = run_skill(
            skill, str(target), project,
            channel=ScriptedHumanChannel([]), policy=Policy.default(),
        )
        assert again.status == "ok"
        assert not again.recorded_derivation, "same bytes -> derivation is a cache hit"


# --------------------------------------------------------------------------------
# (3) decompile skills host-gate cleanly (graceful degrade)
# --------------------------------------------------------------------------------

def test_decompile_skills_host_gate_without_tool():
    """Each decompile skill is discovered but reports unavailable when its host
    tool is absent (empty env + nothing on PATH) — the runner would turn that into
    an install-lead, proving graceful degrade rather than a crash."""
    with temp_home():
        reg = Registry.from_home()
        no_path = lambda name: None  # noqa: E731  (which() that finds nothing)
        for name in HOST_GATED_NAMES:
            skill = reg.get(name)
            assert skill is not None, f"{name} not discovered"
            assert skill.host is not None, f"{name} should declare a host gate"
            assert not skill.available(environ={}, which=no_path), \
                f"{name} should be unavailable with no tool present"


def test_host_gate_records_lead_no_crash():
    """Running a host-gated decompile skill with no tool records an 'install X'
    lead and returns ``unavailable`` — it never runs or raises."""
    with temp_home() as (_home, _skills_root, work):
        target = work / "classes.dex"
        target.write_bytes(b"dex\n035\x00fake dalvik payload")

        project = open_project(str(target))
        skill = Registry.from_home().get("jadx")

        result = run_skill(
            skill, str(target), project,
            channel=ScriptedHumanChannel([True] * 4), policy=Policy.default(),
            environ={},  # empty env -> JADX_HOME unset, jadx off PATH
        )

        assert result.status == "unavailable", result.detail
        assert not result.recorded_derivation
        ledger = project.reload()
        assert any(
            "jadx" in (lead.get("requires") or [])
            for lead in ledger.leads.values()
        ), ledger.leads
        assert not ledger.derivations, "unavailable skill records no derivation"


# --------------------------------------------------------------------------------
# (4) tree-summary surveys an arbitrary source tree (pure stdlib, read-only)
# --------------------------------------------------------------------------------

def test_tree_summary_surveys_source_tree():
    """A fixture source tree fed to the ``tree-summary`` builtin via its ``run.sh``
    yields both a JSON survey and a Markdown report: the JSON carries the right test
    count and doc map, and the .md's ordered outline lists the docs and tests.

    Runs the real ``scripts/run.sh <input> <out_dir>`` in a subprocess (with this
    interpreter's dir on PATH so its ``python3`` resolves), the way the runner does —
    pure stdlib, read-only, no host tool."""
    import subprocess

    skill_dir = builtin_skills_dir() / "tree-summary"
    run_sh = skill_dir / "scripts" / "run.sh"
    assert run_sh.is_file(), run_sh

    with temp_home() as (_home, _skills_root, work):
        # Build a small mixed source tree, plus noise dirs that must be skipped.
        src = work / "project"
        (src / "pkg").mkdir(parents=True)
        (src / "tests").mkdir()
        (src / "docs").mkdir()
        (src / ".git").mkdir()               # noise: never descended into
        (src / "node_modules" / "dep").mkdir(parents=True)  # noise
        (src / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (src / "pkg" / "core.py").write_text("x = 1\n", encoding="utf-8")
        (src / "tests" / "test_core.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
        (src / "README.md").write_text("# proj\n", encoding="utf-8")
        (src / "docs" / "guide.md").write_text("# guide\n", encoding="utf-8")
        (src / "CHANGELOG.md").write_text("## 0.1.0\n", encoding="utf-8")
        (src / ".git" / "config").write_text("junk\n", encoding="utf-8")
        (src / "node_modules" / "dep" / "index.js").write_text("junk\n", encoding="utf-8")

        out_dir = work / "survey-out"

        # Put this interpreter's dir first on PATH so run.sh's `python3` resolves to
        # the venv/interpreter running the tests (there may be no other python3).
        env = dict(os.environ)
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
        proc = subprocess.run(
            ["sh", str(run_sh), str(src), str(out_dir)],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 0, proc.stderr

        # Both outputs exist under out_dir.
        json_path = out_dir / "tree-summary.json"
        md_path = out_dir / "tree-summary.md"
        assert json_path.is_file(), out_dir
        assert md_path.is_file(), out_dir

        # The input tree was not modified (read-only).
        assert not (src / "tree-summary.json").exists()

        data = json.loads(json_path.read_text(encoding="utf-8"))
        # Right test count (>= 1) — the tests/ dir and test_core.py both counted.
        assert data["tests"]["count"] >= 1, data["tests"]
        assert "tests/test_core.py" in data["tests"]["files"], data["tests"]
        # Doc map lists both README.md and docs/guide.md.
        assert "README.md" in data["docs"], data["docs"]
        assert "docs/guide.md" in data["docs"], data["docs"]
        # Noise dirs were skipped: no .js / node_modules content leaked into counts.
        assert not any(f.startswith("node_modules") for f in data["docs"]), data["docs"]

        md = md_path.read_text(encoding="utf-8")
        # The ordered outline is present and catalogs the docs and tests.
        assert "## Ordered outline" in md, md
        assert "docs/guide.md" in md and "README.md" in md, md
        assert "tests/test_core.py" in md, md


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
    print(f"\nall {len(ALL_TESTS)} builtin-skill tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
