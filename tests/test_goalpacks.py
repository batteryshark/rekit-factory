"""E6 — goalpacks: goals that run on the rekit loop, reporting **optional**.

``REKIT_HOME`` is the only env var. Discovery scans builtin ``rekit/goalpacks`` +
``$REKIT_HOME/goalpacks``; a goalpack elsewhere on disk is loaded by direct path via
:func:`load_goalpack_from_path`. Proves the goalpack model end-to-end on the loop
**against fixture goalpacks** — no domain goalpack ships with rekit, so this
framework test builds its own goalpacks in temp dirs:

- ``discover_goalpacks()`` finds a fixture goalpack dropped into
  ``$REKIT_HOME/goalpacks/``;
- ``load_goalpack_from_path(dir)`` loads a goalpack from any folder holding a
  ``GOALPACK.md`` (the "point rekit at this goalpack" entry point) and resolves its
  renderer callable;
- **report-as-artifact:** a scripted :class:`MockAdapter` emits ``FINDING: [does] ...``
  / ``[brittle] ...`` lines then ``DONE``; ``run_goalpack(...)`` drives the loop,
  folds the findings into the generic ledger, and — because the fixture declares a
  renderer — returns a :class:`GoalpackResult` whose ``.report`` carries the fixture's
  shape AND records the report as ``report/json`` + ``report/markdown`` ledger
  artifacts (content-addressed: re-running is a no-op);
- **bundled skills:** a goalpack's own ``skills/`` folder is registered during
  ``run_goalpack`` (passed to skill discovery as an extra root);
- **ad-hoc run_goal:** the primary interface — a target + a tools dir + a goal
  string, no goalpack — surfaces the tools dir's skill to the loop;
- **reporting is optional:** a no-renderer fixture goalpack (the *act*-goal case)
  runs the loop and returns ``report=None`` / ``report_artifacts=[]`` while still
  surfacing findings.

Plain-python style (runnable via ``python tests/test_goalpacks.py``) and
pytest-compatible. Temp ``$REKIT_HOME`` keeps everything hermetic — no network, no
dependency on any shipped goalpack.
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
    load_goalpack_from_path,
    run_goal,
    run_goalpack,
)
from rekit.harness import MockAdapter, MockTurn  # noqa: E402
from rekit.ledger import open_project  # noqa: E402


@contextlib.contextmanager
def temp_env():
    """Temp ``REKIT_HOME`` (restored afterwards), a temp goalpack collection dir (for
    load-by-path), and a temp workspace. Yields ``(home, collection, work)`` as
    ``Path``s. Keeps discovery hermetic: nothing outside these temp roots is seen."""
    saved_home = os.environ.get("REKIT_HOME")
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as collection, tempfile.TemporaryDirectory() as work:
        os.environ["REKIT_HOME"] = home
        try:
            yield Path(home), Path(collection), Path(work)
        finally:
            if saved_home is None:
                os.environ.pop("REKIT_HOME", None)
            else:
                os.environ["REKIT_HOME"] = saved_home


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


def render_html(report):
    name = report.get("summary", {}).get("goalpack", "fixture")
    parts = ["<!doctype html><html><head><meta charset=\\"utf-8\\">",
             "<title>" + name + "</title></head><body>", "<h1>" + name + "</h1>"]
    for lens in LENSES:
        parts.append("<h2>" + lens + "</h2><ul>")
        for entry in report.get(lens, []):
            parts.append("<li>" + entry["text"] + "</li>")
        parts.append("</ul>")
    parts.append("</body></html>")
    return "".join(parts)
'''


def _drop_fixture_goalpack(root: Path, name: str = "fixture") -> Path:
    """Author a fixture goalpack (own renderer + markdown) under ``root/<name>``.

    ``root`` is any goalpack root — a collection dir (for load-by-path) or
    ``$REKIT_HOME/goalpacks``.
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


def _drop_fixture_skill(root: Path, name: str, *, capability: str = "code-reading",
                        tier: str = "read-only") -> Path:
    """Write a minimal valid ``<root>/<name>/SKILL.md`` (+ runnable scripts/) fixture.

    ``accepts: ['*']`` so it is in scope for the seeded ``tree`` root; a runnable
    ``scripts/run.sh`` so it resolves as *available* (in-scope, not shadowed as
    unavailable) — the discriminator these registration tests rely on."""
    skill_dir = root / name
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\n"
        f"name: {name}\n"
        f"capability: {capability}\n"
        f"tier: {tier}\n"
        f"accepts: ['*']\n"
        f"emits: [analysis/observations]\n"
        f"run: scripts/run.sh\n"
        f"description: A fixture skill named {name}, keyword {capability}.\n"
        f"---\n# {name}\n",
        encoding="utf-8",
    )
    run_sh = skill_dir / "scripts" / "run.sh"
    run_sh.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    run_sh.chmod(0o755)
    return skill_dir


def test_discover_finds_user_goalpack():
    """Discovery surfaces a fixture in ``$REKIT_HOME/goalpacks`` — and rekit ships no
    domain goalpacks of its own."""
    with temp_env() as (home, _collection, _work):
        _drop_fixture_goalpack(home / "goalpacks", "on-home")
        names = {gp.name for gp in discover_goalpacks()}
        assert "on-home" in names, f"user goalpack missing: {names}"
        # rekit ships zero domain goalpacks; only the fixture we dropped is seen.
        assert names == {"on-home"}, names


def test_load_goalpack_from_path_loads_by_dir():
    """``load_goalpack_from_path(dir)`` loads a goalpack from a direct folder path —
    the "point rekit at this goalpack" entry point, no env var, no install."""
    with temp_env() as (_home, collection, _work):
        gp_dir = _drop_fixture_goalpack(collection, "external")
        gp = load_goalpack_from_path(gp_dir)
        assert gp.name == "external"
        assert gp.dir == gp_dir
        assert callable(gp.renderer), "renderer should resolve to a callable"


def test_load_goalpack_from_path_missing_manifest_raises():
    """A folder with no ``GOALPACK.md`` raises ``FileNotFoundError``."""
    with temp_env() as (_home, collection, _work):
        empty = collection / "empty"
        empty.mkdir()
        try:
            load_goalpack_from_path(empty)
        except FileNotFoundError:
            pass
        else:  # pragma: no cover - failure path
            raise AssertionError("expected FileNotFoundError for a folder with no GOALPACK.md")


def test_load_fixture_resolves_renderer_callable():
    """``load_goalpack(...)`` (name lookup) resolves the renderer to a real callable
    and carries the declared goal + requested capabilities."""
    with temp_env() as (home, _collection, _work):
        _drop_fixture_goalpack(home / "goalpacks", "fixture")
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
    with temp_env() as (_home, collection, work):
        gp_dir = _drop_fixture_goalpack(collection, "fixture")
        target = _make_target(work)
        project = open_project(str(target))
        gp = load_goalpack_from_path(gp_dir)
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

        # The report is a first-class ledger artifact: report/json + markdown + html.
        kinds = {a.kind for a in result.report_artifacts}
        assert kinds == {"report/json", "report/markdown", "report/html"}, kinds
        for art in result.report_artifacts:
            assert art.meta.get("goalpack") == "fixture"
            assert art.meta.get("findingCount") == 4
        ledger = project.reload()
        for art in result.report_artifacts:
            assert ledger.has_artifact(art.content_hash), "report should be in the ledger"

        # report.json / report.md / report.html exist on disk under the project.
        json_path = project.dir / "reports" / "fixture" / "report.json"
        md_path = project.dir / "reports" / "fixture" / "report.md"
        html_path = project.dir / "reports" / "fixture" / "report.html"
        assert json_path.is_file(), "report.json should be written under the project"
        assert md_path.is_file(), "report.md should be written under the project"
        assert html_path.is_file(), "report.html should be written under the project"
        on_disk = json.loads(json_path.read_text(encoding="utf-8"))
        assert on_disk["summary"]["total"] == 4
        md_text = md_path.read_text(encoding="utf-8")
        assert "greeting" in md_text
        html_text = html_path.read_text(encoding="utf-8")
        assert html_text.startswith("<!doctype html>") and "greeting" in html_text

        # The findings are durable in the ledger — the generic substrate rendered from.
        assert len(ledger.findings()) == 4

        # The loop received the goalpack's own system prompt, not the goal string.
        assert adapter.calls[0].system_prompt == gp.system_prompt

        # Re-rendering the same ledger state yields a byte-identical report, so its
        # artifact hashes are stable and re-recording it is a content-addressed no-op.
        from rekit.goalpacks import _persist_report

        report_again = gp.renderer(project, gp, result.summary)
        md_again = gp.render_markdown(report_again)
        html_again = gp.render_html(report_again) if gp.render_html else None
        arts_again = _persist_report(
            project, gp, report_again, md_again, html_again, ledger.findings())
        assert {a.content_hash for a in arts_again} == {
            a.content_hash for a in result.report_artifacts
        }, "same report → same content hash (no-op)"
        assert project.add_artifact(arts_again[0]) is False, "report already in ledger"


def test_run_goalpack_registers_bundled_skills():
    """A goalpack's bundled ``skills/`` folder is registered during ``run_goalpack``:
    the loop surfaces the bundled skill in its scoped tool set (via ``RUN_SKILL``
    feedback), proving it was passed to skill discovery as an extra root."""
    with temp_env() as (_home, collection, work):
        gp_dir = _drop_fixture_goalpack(collection, "bundled")
        # Bundle a skill inside the goalpack. Its capability matches the goalpack's
        # requestedCapabilities (code-reading) so scoping surfaces it.
        _drop_fixture_skill(gp_dir / "skills", "bundled-tool", capability="code-reading")
        target = _make_target(work)
        project = open_project(str(target))
        gp = load_goalpack_from_path(gp_dir)

        # The brain asks the loop to run the bundled skill by name. If the skill were
        # NOT registered, the loop reports "not in the scoped skill set". If it IS
        # registered and in scope, the loop attempts to run it (its host tool won't
        # resolve, but that is a different, non-"skipped-out-of-scope" outcome).
        adapter = MockAdapter(
            [MockTurn(text="RUN_SKILL: bundled-tool\nFINDING: [does] tried the bundled tool\nDONE\n")]
        )
        result = run_goalpack(project, gp, adapter, max_rounds=2)

        runs = result.summary.skill_runs
        assert runs, "the bundled skill run should have been dispatched"
        run = runs[0]
        assert run.skill == "bundled-tool"
        # The key assertion: it was NOT rejected as out-of-scope (which is what an
        # unregistered skill would produce). It was in the scoped set.
        assert not (run.status == "skipped" and "not in the scoped" in (run.detail or "")), (
            f"bundled skill was not registered/in-scope: {run.status} / {run.detail}"
        )


def test_run_goal_adhoc_registers_tools_dir_skill():
    """The ad-hoc primary interface: ``run_goal(project, goal, adapter, tools=[dir])``
    — a target + a tools dir + a goal string, no goalpack — surfaces the tools dir's
    skill to the loop's scoped set."""
    with temp_env() as (_home, _collection, work):
        tools_dir = work / "tools"
        tools_dir.mkdir()
        _drop_fixture_skill(tools_dir, "adhoc-tool", capability="code-reading")
        target = _make_target(work)
        project = open_project(str(target))

        adapter = MockAdapter(
            [MockTurn(text="RUN_SKILL: adhoc-tool\nFINDING: tried the ad-hoc tool\nDONE\n")]
        )
        summary = run_goal(
            project,
            "Read the code using code-reading tools.",
            adapter,
            tools=[tools_dir],
            requested_capabilities=["code-reading"],
            max_rounds=2,
        )

        runs = summary.skill_runs
        assert runs, "the ad-hoc tools-dir skill run should have been dispatched"
        run = runs[0]
        assert run.skill == "adhoc-tool"
        assert not (run.status == "skipped" and "not in the scoped" in (run.detail or "")), (
            f"ad-hoc tools skill was not registered/in-scope: {run.status} / {run.detail}"
        )


def test_no_renderer_goalpack_produces_no_report():
    """Reporting is optional: an *act*-goal goalpack with no renderer runs the loop
    and returns ``report=None`` / ``report_artifacts=[]`` while still surfacing the
    ledger findings — proving a report is not required."""
    with temp_env() as (_home, collection, work):
        gp_dir = _drop_no_renderer_goalpack(collection, "act")
        target = _make_target(work)
        project = open_project(str(target))
        gp = load_goalpack_from_path(gp_dir)
        assert gp.renderer is None, "a goalpack with no renderer.py declares no report"
        assert gp.render_markdown is None
        assert gp.render_html is None

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
    print(f"\nall {len(ALL_TESTS)} goalpack tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
