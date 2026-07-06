"""E6 — goalpacks: goals that run on the rekit loop, reporting **optional**.

Proves the goalpack model end-to-end on the loop:

- ``discover_goalpacks()`` finds the builtin ``understand`` and a user goalpack
  dropped into a temp ``$REKIT_HOME/goalpacks/``;
- ``load_goalpack("understand")`` resolves its renderer callable;
- **report-as-artifact:** a scripted :class:`MockAdapter` emits ``FINDING: [does] ...``
  / ``[brittle] ...`` lines then ``DONE``; ``run_goalpack(...)`` drives the loop,
  folds the findings into the generic ledger, and — because understand declares a
  renderer — returns a :class:`GoalpackResult` whose ``.report`` has understand's
  four-section shape AND records the report as ``report/json`` + ``report/markdown``
  ledger artifacts (content-addressed: re-running is a no-op);
- **reporting is optional:** a no-renderer fixture goalpack (the *act*-goal case)
  runs the loop and returns ``report=None`` / ``report_artifacts=[]`` while still
  surfacing findings.

Plain-python style (runnable via ``python tests/test_goalpacks.py``) and
pytest-compatible. A temp ``$REKIT_HOME`` keeps everything hermetic — no network.
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
def temp_home():
    """A temp ``REKIT_HOME`` (restored afterwards) + a temp workspace. Yields
    ``(home_path, work_path)``."""
    saved = os.environ.get("REKIT_HOME")
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as work:
        os.environ["REKIT_HOME"] = home
        try:
            yield Path(home), Path(work)
        finally:
            if saved is None:
                os.environ.pop("REKIT_HOME", None)
            else:
                os.environ["REKIT_HOME"] = saved


def _make_target(work: Path) -> Path:
    """A small target tree to open a project against."""
    target = work / "app"
    target.mkdir()
    (target / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (target / "config.json").write_text('{"k": 1}\n', encoding="utf-8")
    return target


def _drop_user_goalpack(home: Path, name: str = "echo") -> Path:
    """Author a minimal user goalpack under ``$REKIT_HOME/goalpacks/<name>``."""
    gp = home / "goalpacks" / name
    gp.mkdir(parents=True)
    (gp / "GOALPACK.md").write_text(
        "---\n"
        f"name: {name}\n"
        "title: Echo goalpack\n"
        "goal: Echo the findings back.\n"
        "requestedCapabilities: [code-reading]\n"
        "renderer: renderer:render\n"
        "---\n\nA tiny user goalpack for tests.\n",
        encoding="utf-8",
    )
    (gp / "system-prompt.md").write_text("Emit findings then DONE.\n", encoding="utf-8")
    (gp / "renderer.py").write_text(
        "def render(project, goalpack, summary):\n"
        "    notes = [f.get('note', '') for f in project.ledger.findings()]\n"
        "    return {'name': goalpack.name, 'findings': notes}\n",
        encoding="utf-8",
    )
    return gp


def _drop_no_renderer_goalpack(home: Path, name: str = "act") -> Path:
    """Author an *act*-style user goalpack with NO renderer — reporting is optional.

    No ``renderer:`` frontmatter and no ``renderer.py`` on disk → ``renderer`` is
    ``None``; running it produces findings/artifacts but no report."""
    gp = home / "goalpacks" / name
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


def test_discover_finds_builtin_understand_and_user_goalpack():
    """Discovery surfaces the shipped ``understand`` and a user goalpack dropped
    into ``$REKIT_HOME/goalpacks``."""
    with temp_home() as (home, _work):
        _drop_user_goalpack(home)
        names = {gp.name for gp in discover_goalpacks()}
        assert "understand" in names, f"builtin understand missing: {names}"
        assert "echo" in names, f"user goalpack missing: {names}"


def test_load_understand_resolves_renderer_callable():
    """``load_goalpack('understand')`` resolves the renderer to a real callable and
    carries the declared goal + requested capabilities."""
    with temp_home():
        gp = load_goalpack("understand")
        assert gp.name == "understand"
        assert callable(gp.renderer), "renderer should resolve to a callable"
        assert gp.goal, "understand declares a one-line goal"
        assert "code-reading" in gp.requested_capabilities
        assert gp.system_prompt.strip(), "system-prompt.md should be loaded"


def test_load_unknown_goalpack_raises():
    with temp_home():
        try:
            load_goalpack("does-not-exist")
        except KeyError:
            pass
        else:  # pragma: no cover - failure path
            raise AssertionError("expected KeyError for an unknown goalpack")


def test_run_goalpack_drives_loop_and_renders_understand_shape():
    """End-to-end: a scripted brain emits lens-tagged findings then DONE;
    ``run_goalpack`` folds them into the ledger, returns a ``GoalpackResult`` whose
    ``.report`` has understand's four-section shape, AND records the report as
    ``report/json`` + ``report/markdown`` ledger artifacts on disk."""
    with temp_home() as (_home, work):
        target = _make_target(work)
        project = open_project(str(target))
        gp = load_goalpack("understand")
        assert gp.renderer is not None, "understand DOES declare a report renderer"

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

        # The four understand sections are present.
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
        assert summary["goalpack"] == "understand"
        assert summary["total"] == 4
        assert summary["counts"] == {"does": 1, "decides": 1, "brittle": 1, "surprising": 1}
        assert summary["done"] is True

        # The report is a first-class ledger artifact: report/json + report/markdown.
        kinds = {a.kind for a in result.report_artifacts}
        assert kinds == {"report/json", "report/markdown"}, kinds
        for art in result.report_artifacts:
            assert art.meta.get("goalpack") == "understand"
            assert art.meta.get("findingCount") == 4
        ledger = project.reload()
        for art in result.report_artifacts:
            assert ledger.has_artifact(art.content_hash), "report should be in the ledger"

        # report.json exists on disk under the project and holds the structured data.
        json_path = project.dir / "reports" / "understand" / "report.json"
        md_path = project.dir / "reports" / "understand" / "report.md"
        assert json_path.is_file(), "report.json should be written under the project"
        assert md_path.is_file(), "report.md should be written under the project"
        on_disk = json.loads(json_path.read_text(encoding="utf-8"))
        assert on_disk["summary"]["total"] == 4
        # The markdown is human-readable: headed sections + the finding text.
        md_text = md_path.read_text(encoding="utf-8")
        assert "## What it does" in md_text
        assert "greeting" in md_text

        # The findings are durable in the ledger — the generic substrate the
        # goalpack rendered from (no shared report_model involved).
        assert len(ledger.findings()) == 4

        # The loop received the goalpack's own system prompt, not the goal string.
        assert adapter.calls[0].system_prompt == gp.system_prompt

        # Re-rendering the same ledger state yields a byte-identical report, so its
        # artifact hashes are stable and re-recording it is a content-addressed
        # no-op (add_artifact dedupes on the hash → returns False the second time).
        from rekit.goalpacks import _persist_report

        report_again = gp.renderer(project, gp, result.summary)
        md_again = gp.render_markdown(report_again)
        arts_again = _persist_report(project, gp, report_again, md_again, ledger.findings())
        assert {a.content_hash for a in arts_again} == {
            a.content_hash for a in result.report_artifacts
        }, "same report → same content hash (no-op)"
        # No new artifact events entered the ledger on the second persist.
        assert project.add_artifact(arts_again[0]) is False, "report already in ledger"


def test_run_user_goalpack_end_to_end():
    """A user-authored goalpack (own renderer, imported by path) runs on the loop
    and its own shape is reachable via ``GoalpackResult.report``."""
    with temp_home() as (home, work):
        _drop_user_goalpack(home, "echo")
        target = _make_target(work)
        project = open_project(str(target))
        gp = load_goalpack("echo")

        adapter = MockAdapter([MockTurn(text="FINDING: hello world\nDONE\n")])
        result = run_goalpack(project, gp, adapter, max_rounds=4)

        report = result.report
        assert report["name"] == "echo"
        assert any("hello world" in note for note in report["findings"])
        # echo declares a renderer but no markdown → JSON-only report artifact.
        kinds = {a.kind for a in result.report_artifacts}
        assert kinds == {"report/json"}, kinds


def test_no_renderer_goalpack_produces_no_report():
    """Reporting is optional: an *act*-goal goalpack with no renderer runs the loop
    and returns ``report=None`` / ``report_artifacts=[]`` while still surfacing the
    ledger findings — proving a report is not required."""
    with temp_home() as (home, work):
        _drop_no_renderer_goalpack(home, "act")
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
    test_discover_finds_builtin_understand_and_user_goalpack()
    test_load_understand_resolves_renderer_callable()
    test_load_unknown_goalpack_raises()
    test_run_goalpack_drives_loop_and_renders_understand_shape()
    test_run_user_goalpack_end_to_end()
    test_no_renderer_goalpack_produces_no_report()
    print("rekit goalpacks tests passed")
