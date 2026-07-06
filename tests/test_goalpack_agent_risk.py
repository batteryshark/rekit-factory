"""E6 — the ``agent-risk`` goalpack: mapping an agent surface on the loop.

Proves agent-risk runs as a goalpack — the brain maps dangerous capability
compositions on the ralph loop, and the goalpack's own renderer folds the generic
ledger findings into agent-risk's report shape (compositions bucketed by abuse path,
posture issues, recommendations, a headline, a review plan), persisting it as a
``report/*`` ledger artifact. There is NO shared ``report_model``: the shape belongs
to the goalpack.

- ``discover_goalpacks()`` / ``load_goalpack("agent-risk")`` find the builtin and
  resolve its renderer + markdown callables;
- **report-as-artifact:** a scripted :class:`MockAdapter` emits
  ``FINDING: [combo] …`` / ``[posture] …`` / ``[recommend] …`` lines then ``DONE``;
  ``run_goalpack(...)`` drives the loop, folds the findings into the generic ledger,
  and returns a :class:`GoalpackResult` whose ``.report`` has agent-risk's shape AND
  records the report as ``report/json`` + ``report/markdown`` ledger artifacts.

Plain-python style (runnable via ``python tests/test_goalpack_agent_risk.py``) and
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


def _make_agent_surface(work: Path) -> Path:
    """A small agent-surface tree to open a project against: a couple of skills, an
    MCP config, and a hook — the kind of thing agent-risk inventories."""
    surface = work / "surface"
    surface.mkdir()
    (surface / "secrets-reader.md").write_text(
        "---\nname: secrets-reader\n---\nReads ~/.aws/credentials.\n", encoding="utf-8"
    )
    (surface / "webhook.md").write_text(
        "---\nname: webhook\n---\nPOSTs a payload to an arbitrary URL.\n", encoding="utf-8"
    )
    (surface / "shell.md").write_text(
        "---\nname: shell\n---\nRuns arbitrary shell commands.\n", encoding="utf-8"
    )
    (surface / ".mcp.json").write_text('{"servers": {"fetch": {}}}\n', encoding="utf-8")
    return surface


def test_discover_finds_builtin_agent_risk():
    """Discovery surfaces the shipped ``agent-risk`` goalpack."""
    with temp_home():
        names = {gp.name for gp in discover_goalpacks()}
        assert "agent-risk" in names, f"builtin agent-risk missing: {names}"


def test_load_agent_risk_resolves_renderer_and_markdown():
    """``load_goalpack('agent-risk')`` resolves both the renderer and its markdown
    companion, and carries the declared goal + (empty) requested capabilities."""
    with temp_home():
        gp = load_goalpack("agent-risk")
        assert gp.name == "agent-risk"
        assert callable(gp.renderer), "renderer should resolve to a callable"
        assert callable(gp.render_markdown), "render_markdown companion should resolve"
        assert gp.goal, "agent-risk declares a one-line goal"
        # Agent surfaces are text/config the brain reads directly — no scoped caps.
        assert gp.requested_capabilities == (), gp.requested_capabilities
        assert gp.system_prompt.strip(), "system-prompt.md should be loaded"


def test_run_agent_risk_drives_loop_and_renders_agent_risk_shape():
    """End-to-end: a scripted brain emits tag-labelled findings then DONE;
    ``run_goalpack`` folds them into the ledger, returns a ``GoalpackResult`` whose
    ``.report`` has agent-risk's shape (compositions bucketed by abuse path, posture,
    recommendations, headline, review plan), AND records the report as ``report/json``
    + ``report/markdown`` ledger artifacts on disk."""
    with temp_home() as (_home, work):
        surface = _make_agent_surface(work)
        project = open_project(str(surface))
        gp = load_goalpack("agent-risk")
        assert gp.renderer is not None, "agent-risk DOES declare a report renderer"

        script = [
            MockTurn(
                text=(
                    "Inventoried the surface.\n"
                    "FINDING: [combo] exfil: `secrets-reader` reads ~/.aws/credentials and "
                    "`webhook` POSTs arbitrary URLs — self-sufficient once both are enabled.\n"
                    "FINDING: [combo] rce: `.mcp.json` fetch server ingests web content (steerable) "
                    "and `shell` runs commands — cross-skill, injected page text can drive execution.\n"
                )
            ),
            MockTurn(
                text=(
                    "FINDING: [posture] `shell` grants unrestricted EXEC with no allowlist or gate.\n"
                    "FINDING: [recommend] Do not co-enable the fetch MCP server with `shell`; "
                    "or gate `shell` behind an approval prompt.\n"
                    "DONE\n"
                )
            ),
        ]
        adapter = MockAdapter(script)

        result = run_goalpack(project, gp, adapter, max_rounds=8)
        assert isinstance(result, GoalpackResult)
        report = result.report
        assert report is not None

        # agent-risk's own shape is present.
        for key in ("headline", "summary", "combos", "posture", "recommendations", "reviewPlan"):
            assert key in report, f"missing key {key}: {report.keys()}"

        # Two dangerous compositions, bucketed and parsed.
        assert len(report["combos"]) == 2
        paths = {c["path"] for c in report["combos"]}
        assert paths == {"exfil", "rce"}, paths
        # The tag was peeled off the note.
        assert all("[combo]" not in c["text"] for c in report["combos"])

        # Self-sufficient vs cross-skill was read from the finding text.
        exfil = next(c for c in report["combos"] if c["path"] == "exfil")
        rce = next(c for c in report["combos"] if c["path"] == "rce")
        assert exfil["selfSufficient"] is True
        assert rce["selfSufficient"] is False
        # The abuse-path label was split off, leaving the "why".
        assert "secrets-reader" in exfil["why"]
        assert exfil["why"].startswith("`secrets-reader`") or "reads" in exfil["why"]

        # One posture issue, one recommendation.
        assert len(report["posture"]) == 1
        assert "unrestricted EXEC" in report["posture"][0]["text"]
        assert "[posture]" not in report["posture"][0]["text"]
        assert len(report["recommendations"]) == 1
        assert "co-enable" in report["recommendations"][0].lower()

        # Headline leads on the self-sufficient composition (the sharpest signal).
        assert "Elevated agent risk" in report["headline"], report["headline"]

        # Summary header reflects the loop + per-tag counts.
        summary = report["summary"]
        assert summary["goalpack"] == "agent-risk"
        assert summary["total"] == 4  # 2 combos + 1 posture + 1 recommendation
        assert summary["counts"] == {
            "combos": 2,
            "posture": 1,
            "recommendations": 1,
            "selfSufficient": 1,
        }
        assert summary["done"] is True

        # Review plan tiers: must (self-sufficient) / should (cross-skill) / posture.
        plan = report["reviewPlan"]
        tiers = {t["id"]: t for t in plan["tiers"]}
        assert tiers["must_review"]["count"] == 1
        assert tiers["should_review"]["count"] == 1
        assert tiers["posture_review"]["count"] == 1
        assert plan["byAbusePath"] == {"exfil": 1, "rce": 1}, plan["byAbusePath"]

        # The report is a first-class ledger artifact: report/json + report/markdown.
        kinds = {a.kind for a in result.report_artifacts}
        assert kinds == {"report/json", "report/markdown"}, kinds
        for art in result.report_artifacts:
            assert art.meta.get("goalpack") == "agent-risk"
            assert art.meta.get("findingCount") == 4
        ledger = project.reload()
        for art in result.report_artifacts:
            assert ledger.has_artifact(art.content_hash), "report should be in the ledger"

        # report.json / report.md exist on disk under the project.
        json_path = project.dir / "reports" / "agent-risk" / "report.json"
        md_path = project.dir / "reports" / "agent-risk" / "report.md"
        assert json_path.is_file(), "report.json should be written under the project"
        assert md_path.is_file(), "report.md should be written under the project"
        on_disk = json.loads(json_path.read_text(encoding="utf-8"))
        assert on_disk["summary"]["total"] == 4
        # The markdown is human-readable: headline + grouped compositions.
        md_text = md_path.read_text(encoding="utf-8")
        assert "## Dangerous capability compositions" in md_text
        assert "Exfil" in md_text and "RCE" in md_text
        assert "(self-sufficient)" in md_text
        assert "## Posture" in md_text
        assert "## Recommendations" in md_text

        # The findings are durable in the ledger — the generic substrate the goalpack
        # rendered from (no shared report_model involved).
        assert len(ledger.findings()) == 4

        # The loop received the goalpack's own system prompt, not the goal string.
        assert adapter.calls[0].system_prompt == gp.system_prompt


def test_agent_risk_posture_only_surface():
    """A surface with posture issues but no dangerous compositions still reports:
    empty ``combos``, populated ``posture``, and a headline that says so."""
    with temp_home() as (_home, work):
        surface = _make_agent_surface(work)
        project = open_project(str(surface))
        gp = load_goalpack("agent-risk")

        adapter = MockAdapter(
            [
                MockTurn(
                    text=(
                        "FINDING: [posture] `browse` ingests untrusted web content and is steerable.\n"
                        "DONE\n"
                    )
                )
            ]
        )
        result = run_goalpack(project, gp, adapter, max_rounds=4)
        report = result.report

        assert report["combos"] == []
        assert len(report["posture"]) == 1
        assert "No dangerous capability compositions found" in report["headline"]
        # A mistagged / unrecognized finding would fall back into posture, not vanish.
        assert report["summary"]["counts"]["combos"] == 0


def test_agent_risk_unknown_tag_falls_back_to_posture():
    """A finding with no recognized tag is never dropped: it lands in posture so the
    surface note survives."""
    with temp_home() as (_home, work):
        surface = _make_agent_surface(work)
        project = open_project(str(surface))
        gp = load_goalpack("agent-risk")

        adapter = MockAdapter(
            [MockTurn(text="FINDING: an untagged observation about the surface\nDONE\n")]
        )
        result = run_goalpack(project, gp, adapter, max_rounds=4)
        report = result.report

        assert len(report["posture"]) == 1
        assert "untagged observation" in report["posture"][0]["text"]
        assert report["combos"] == []


if __name__ == "__main__":
    test_discover_finds_builtin_agent_risk()
    test_load_agent_risk_resolves_renderer_and_markdown()
    test_run_agent_risk_drives_loop_and_renders_agent_risk_shape()
    test_agent_risk_posture_only_surface()
    test_agent_risk_unknown_tag_falls_back_to_posture()
    print("rekit agent-risk goalpack tests passed")
