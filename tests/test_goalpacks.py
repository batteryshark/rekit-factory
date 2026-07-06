"""E6 — goalpacks: goals that run on the rekit loop, reporting **optional**.

Proves the goalpack model end-to-end on the loop **against fixture goalpacks** — no
domain goalpack ships with rekit, so this framework test builds its own goalpacks in
temp dirs and puts them on the discovery search path:

- ``discover_goalpacks()`` finds a fixture goalpack dropped on ``$REKIT_GOALPATH`` and
  a second one dropped into ``$REKIT_HOME/goalpacks/``;
- a ``$REKIT_GOALPATH`` dir is scanned (the search-path root sits between builtin and
  user);
- ``load_goalpack(...)`` resolves the fixture's renderer callable;
- **report-as-artifact:** a scripted :class:`MockAdapter` emits ``FINDING: [does] ...``
  / ``[brittle] ...`` lines then ``DONE``; ``run_goalpack(...)`` drives the loop,
  folds the findings into the generic ledger, and — because the fixture declares a
  renderer — returns a :class:`GoalpackResult` whose ``.report`` carries the fixture's
  shape AND records the report as ``report/json`` + ``report/markdown`` ledger
  artifacts (content-addressed: re-running is a no-op);
- **reporting is optional:** a no-renderer fixture goalpack (the *act*-goal case)
  runs the loop and returns ``report=None`` / ``report_artifacts=[]`` while still
  surfacing findings.

Plain-python style (runnable via ``python tests/test_goalpacks.py``) and
pytest-compatible. Temp ``$REKIT_HOME`` + ``$REKIT_GOALPATH`` keep everything
hermetic — no network, no dependency on any shipped goalpack.
"""

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.goalpacks import (  # noqa: E402
    GoalpackResult,
    discover_goalpacks,
    load_goalpack,
    run_goalpack,
)
from rekit.harness import MockAdapter, MockTurn  # noqa: E402
from rekit.ledger import open_project  # noqa: E402


@contextlib.contextmanager
def temp_env():
    """Temp ``REKIT_HOME`` + ``REKIT_GOALPATH`` (both restored afterwards), a temp
    goalpath dir, and a temp workspace. Yields ``(home, goalpath, work)`` as
    ``Path``s. Keeps discovery hermetic: nothing outside these temp roots is seen."""
    saved_home = os.environ.get("REKIT_HOME")
    saved_goalpath = os.environ.get("REKIT_GOALPATH")
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as goalpath, tempfile.TemporaryDirectory() as work:
        os.environ["REKIT_HOME"] = home
        os.environ["REKIT_GOALPATH"] = goalpath
        try:
            yield Path(home), Path(goalpath), Path(work)
        finally:
            for key, saved in (("REKIT_HOME", saved_home), ("REKIT_GOALPATH", saved_goalpath)):
                if saved is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = saved


def _make_target(work: Path) -> Path:
    """A small target tree to open a project against."""
    target = work / "app"
    target.mkdir()
    (target / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (target / "config.json").write_text('{"k": 1}\n', encoding="utf-8")
    return target


# -- fixture goalpacks (built on disk, not shipped with rekit) ----------------

#: A renderer.py for the four-lens fixture goalpack — buckets ``[lens]``-tagged
#: findings into a small four-section report + a markdown companion. Self-contained
#: so the framework test never depends on a domain goalpack.
_FIXTURE_RENDERER = '''\
import re

LENSES = ("does", "decides", "brittle", "surprising")
_LENS_RE = re.compile(r"^\\s*\\[\\s*([a-zA-Z]+)\\s*\\]\\s*(.*)$", re.DOTALL)


def _split(note):
    m = _LENS_RE.match(note)
    if m and m.group(1).lower() in LENSES:
        return m.group(1).lower(), m.group(2).strip()
    return "does", note.strip()


def render_report(project, goalpack, summary):
    sections = {lens: [] for lens in LENSES}
    for f in project.ledger.findings():
        note = f.get("note") or f.get("text") or ""
        lens, text = _split(str(note))
        sections[lens].append({"text": text})
    report = {lens: sections[lens] for lens in LENSES}
    report["summary"] = {
        "goalpack": getattr(goalpack, "name", "fixture"),
        "total": sum(len(v) for v in sections.values()),
        "counts": {lens: len(sections[lens]) for lens in LENSES},
        "done": getattr(summary, "done", None),
    }
    return report


def render_markdown(report):
    lines = ["# " + report.get("summary", {}).get("goalpack", "fixture"), ""]
    for lens in LENSES:
        lines.append("## " + lens)
        for entry in report.get(lens, []):
            lines.append("- " + entry["text"])
        lines.append("")
    return "\\n".join(lines).rstrip() + "\\n"
'''


def _drop_fixture_goalpack(root: Path, name: str = "fixture") -> Path:
    """Author a fixture goalpack (own renderer + markdown) under ``root/<name>``.

    ``root`` is any goalpack root — a ``$REKIT_GOALPATH`` dir or ``$REKIT_HOME/goalpacks``.
    """
    gp = root / name
    gp.mkdir(parents=True)
    (gp / "GOALPACK.md").write_text(
        "---\n"
        f"name: {name}\n"
        "title: Fixture goalpack\n"
        "goal: Read the target and answer four questions about it.\n"
        "requestedCapabilities: [code-reading]\n"
        "renderer: renderer:render_report\n"
        "---\n\nA self-contained fixture goalpack for the framework test.\n",
        encoding="utf-8",
    )
    (gp / "system-prompt.md").write_text(
        "Emit [lens]-tagged findings then DONE.\n", encoding="utf-8"
    )
    (gp / "renderer.py").write_text(_FIXTURE_RENDERER, encoding="utf-8")
    return gp


def _drop_no_renderer_goalpack(root: Path, name: str = "act") -> Path:
    """Author an *act*-style fixture goalpack with NO renderer — reporting is optional.

    No ``renderer:`` frontmatter and no ``renderer.py`` on disk → ``renderer`` is
    ``None``; running it produces findings/artifacts but no report."""
    gp = root / name
    gp.mkdir(parents=True)
    (gp / "GOALPACK.md").write_text(
        "---\n"
        f"name: {name}\n"
        "title: Act goalpack (no report)\n"
        "goal: Do the thing; produce a patch, not a report.\n"
        "requestedCapabilities: [code-reading]\n"
        "---\n\nAn act-goal: findings/artifacts, no report renderer.\n",
        encoding="utf-8",
    )
    (gp / "system-prompt.md").write_text("Emit findings then DONE.\n", encoding="utf-8")
    return gp


def test_discover_finds_goalpath_and_user_goalpacks():
    """Discovery surfaces a fixture on ``$REKIT_GOALPATH`` and one in
    ``$REKIT_HOME/goalpacks`` — and rekit ships no domain goalpacks of its own."""
    with temp_env() as (home, goalpath, _work):
        _drop_fixture_goalpack(goalpath, "on-goalpath")
        _drop_fixture_goalpack(home / "goalpacks", "on-home")
        names = {gp.name for gp in discover_goalpacks()}
        assert "on-goalpath" in names, f"REKIT_GOALPATH goalpack missing: {names}"
        assert "on-home" in names, f"user goalpack missing: {names}"
        # rekit ships zero domain goalpacks; only the fixtures we dropped are seen.
        assert names == {"on-goalpath", "on-home"}, names


def test_goalpath_dir_is_discovered():
    """A dir on ``$REKIT_GOALPATH`` is scanned for ``*/GOALPACK.md`` — the search-path
    root (between builtin and user) is what makes an external collection discoverable."""
    with temp_env() as (_home, goalpath, _work):
        _drop_fixture_goalpack(goalpath, "external")
        gp = load_goalpack("external")
        assert gp.name == "external"
        assert gp.dir.parent == goalpath, "should resolve from the REKIT_GOALPATH dir"


def test_goalpath_multiple_dirs_are_all_scanned():
    """``$REKIT_GOALPATH`` is ``os.pathsep``-separated (like ``PATH``): every dir on
    it is scanned."""
    with temp_env() as (_home, goalpath, work):
        second = work / "extra-goalpacks"
        second.mkdir()
        _drop_fixture_goalpack(goalpath, "first")
        _drop_fixture_goalpack(second, "second")
        os.environ["REKIT_GOALPATH"] = os.pathsep.join([str(goalpath), str(second)])
        names = {gp.name for gp in discover_goalpacks()}
        assert {"first", "second"} <= names, names


def test_load_fixture_resolves_renderer_callable():
    """``load_goalpack(...)`` resolves the renderer to a real callable and carries the
    declared goal + requested capabilities."""
    with temp_env() as (_home, goalpath, _work):
        _drop_fixture_goalpack(goalpath, "fixture")
        gp = load_goalpack("fixture")
        assert gp.name == "fixture"
        assert callable(gp.renderer), "renderer should resolve to a callable"
        assert gp.goal, "the fixture declares a one-line goal"
        assert "code-reading" in gp.requested_capabilities
        assert gp.system_prompt.strip(), "system-prompt.md should be loaded"


def test_load_unknown_goalpack_raises():
    with temp_env():
        try:
            load_goalpack("does-not-exist")
        except KeyError:
            pass
        else:  # pragma: no cover - failure path
            raise AssertionError("expected KeyError for an unknown goalpack")


def test_run_goalpack_drives_loop_and_records_report_artifact():
    """End-to-end against a fixture: a scripted brain emits lens-tagged findings then
    DONE; ``run_goalpack`` folds them into the ledger, returns a ``GoalpackResult``
    whose ``.report`` carries the fixture's four-section shape, AND records the report
    as ``report/json`` + ``report/markdown`` ledger artifacts on disk."""
    with temp_env() as (_home, goalpath, work):
        _drop_fixture_goalpack(goalpath, "fixture")
        target = _make_target(work)
        project = open_project(str(target))
        gp = load_goalpack("fixture")
        assert gp.renderer is not None, "the fixture DOES declare a report renderer"

        script = [
            MockTurn(
                text=(
                    "Read the tree.\n"
                    "FINDING: [does] main.py prints a greeting to stdout\n"
                    "FINDING: [decides] config.json keys behaviour on a single field\n"
                )
            ),
            MockTurn(
                text=(
                    "FINDING: [brittle] assumes config.json is present; no guard\n"
                    "FINDING: [surprising] a debug flag ships enabled\n"
                    "DONE\n"
                )
            ),
        ]
        adapter = MockAdapter(script)

        result = run_goalpack(project, gp, adapter, max_rounds=8)
        assert isinstance(result, GoalpackResult)
        report = result.report
        assert report is not None

        # The four fixture sections are present.
        for lens in ("does", "decides", "brittle", "surprising"):
            assert lens in report, f"missing section {lens}: {report.keys()}"

        # Findings landed under the right lens (prefix peeled).
        assert len(report["does"]) == 1
        assert "greeting" in report["does"][0]["text"]
        assert "[does]" not in report["does"][0]["text"], "the lens tag should be peeled"
        assert len(report["decides"]) == 1
        assert len(report["brittle"]) == 1
        assert "config.json" in report["brittle"][0]["text"]
        assert len(report["surprising"]) == 1

        # The summary header reflects the loop + per-lens counts.
        summary = report["summary"]
        assert summary["goalpack"] == "fixture"
        assert summary["total"] == 4
        assert summary["counts"] == {"does": 1, "decides": 1, "brittle": 1, "surprising": 1}
        assert summary["done"] is True

        # The report is a first-class ledger artifact: report/json + report/markdown.
        kinds = {a.kind for a in result.report_artifacts}
        assert kinds == {"report/json", "report/markdown"}, kinds
        for art in result.report_artifacts:
            assert art.meta.get("goalpack") == "fixture"
            assert art.meta.get("findingCount") == 4
        ledger = project.reload()
        for art in result.report_artifacts:
            assert ledger.has_artifact(art.content_hash), "report should be in the ledger"

        # report.json / report.md exist on disk under the project.
        json_path = project.dir / "reports" / "fixture" / "report.json"
        md_path = project.dir / "reports" / "fixture" / "report.md"
        assert json_path.is_file(), "report.json should be written under the project"
        assert md_path.is_file(), "report.md should be written under the project"
        on_disk = json.loads(json_path.read_text(encoding="utf-8"))
        assert on_disk["summary"]["total"] == 4
        md_text = md_path.read_text(encoding="utf-8")
        assert "greeting" in md_text

        # The findings are durable in the ledger — the generic substrate rendered from.
        assert len(ledger.findings()) == 4

        # The loop received the goalpack's own system prompt, not the goal string.
        assert adapter.calls[0].system_prompt == gp.system_prompt

        # Re-rendering the same ledger state yields a byte-identical report, so its
        # artifact hashes are stable and re-recording it is a content-addressed no-op.
        from rekit.goalpacks import _persist_report

        report_again = gp.renderer(project, gp, result.summary)
        md_again = gp.render_markdown(report_again)
        arts_again = _persist_report(project, gp, report_again, md_again, ledger.findings())
        assert {a.content_hash for a in arts_again} == {
            a.content_hash for a in result.report_artifacts
        }, "same report → same content hash (no-op)"
        assert project.add_artifact(arts_again[0]) is False, "report already in ledger"


def test_no_renderer_goalpack_produces_no_report():
    """Reporting is optional: an *act*-goal goalpack with no renderer runs the loop
    and returns ``report=None`` / ``report_artifacts=[]`` while still surfacing the
    ledger findings — proving a report is not required."""
    with temp_env() as (_home, goalpath, work):
        _drop_no_renderer_goalpack(goalpath, "act")
        target = _make_target(work)
        project = open_project(str(target))
        gp = load_goalpack("act")
        assert gp.renderer is None, "a goalpack with no renderer.py declares no report"
        assert gp.render_markdown is None

        adapter = MockAdapter(
            [MockTurn(text="FINDING: [does] patched the config loader\nDONE\n")]
        )
        result = run_goalpack(project, gp, adapter, max_rounds=4)

        assert isinstance(result, GoalpackResult)
        assert result.report is None, "no renderer → no report"
        assert result.report_artifacts == [], "no report artifacts recorded"
        # But the ledger substrate (findings) is still surfaced.
        assert any("patched the config loader" in f.get("note", "") for f in result.findings)
        assert len(result.findings) == 1

        # No report/* artifact leaked into the ledger.
        report_kinds = [k for k in project.reload().kinds if str(k).startswith("report/")]
        assert report_kinds == [], report_kinds
        # And no reports/ directory was written for this goalpack.
        assert not (project.dir / "reports" / "act").exists()


if __name__ == "__main__":
    test_discover_finds_goalpath_and_user_goalpacks()
    test_goalpath_dir_is_discovered()
    test_goalpath_multiple_dirs_are_all_scanned()
    test_load_fixture_resolves_renderer_callable()
    test_load_unknown_goalpack_raises()
    test_run_goalpack_drives_loop_and_records_report_artifact()
    test_no_renderer_goalpack_produces_no_report()
    print("rekit goalpacks tests passed")
