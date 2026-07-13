from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).parents[1]
UI = ROOT / "src" / "rekit_factory" / "ui"


def test_outcomes_surface_is_packaged_first_class_and_projection_only():
    page = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "mission-control.js").read_text(encoding="utf-8")
    helper = (UI / "mission-outcomes.js").read_text(encoding="utf-8")
    style = (UI / "mission-control.css").read_text(encoding="utf-8")
    api = (UI.parent / "api.py").read_text(encoding="utf-8")

    assert page.index("/ui/mission-outcomes.js") < page.index("/ui/mission-control.js")
    assert '"mission-outcomes.js": "text/javascript; charset=utf-8"' in api
    for marker in (
        'data-tab="outcomes"', 'id="tab-outcomes"', 'id="outcomeCount"',
        'id="outcomeSearch"', 'id="outcomeType"', 'id="outcomeState"',
        'id="outcomeOwner"', 'id="outcomeTerminal"', 'id="outcomeResults"',
        'id="outcomeAnnouncement"',
        "ORTHOGONAL OUTCOME READ MODEL",
    ):
        assert marker in page
    for facet in (
        "execution", "completion", "disposition", "validation", "acceptance", "publication",
    ):
        assert facet in helper
    for entity_type in (
        "run", "worker", "work-item", "finding", "validation", "proof-bundle",
        "operator-decision",
    ):
        assert entity_type in script + helper
    assert "snapshot?.outcomeProjection" in script
    outcome_slice = script[script.index("const OUTCOME_TYPES"):script.index("function renderDetail")]
    for forbidden in (
        "logical_path", "target", "prompt", "secret", "notification", "evidence.records",
        "workerReports",
    ):
        assert forbidden not in outcome_slice
    assert "sourceWatermarks" not in outcome_slice
    assert "MissionOutcomes.canonicalLink(entity)" in outcome_slice
    assert "renderOutcomeProjection" in script
    assert "state.outcomes.tracker.accept" in script
    assert "semanticCanonicalBase64" in helper
    assert "decodeSemanticEnvelope" in helper
    assert "actual = await digest(prepared.bytes)" in helper
    assert 'aria-label="Search outcomes"' in page
    assert '$("view-detail").classList.contains("active")' in script
    assert "state.runRequests.isCurrent(requestGeneration)" in script
    assert "state.snapshotRefreshes.isCurrent(refreshGeneration)" in script
    assert "isCurrentEventStream(stream, state.stream, runId, state.selected)" in script
    assert script.index("isCurrentEventStream(stream, state.stream, runId, state.selected)") < script.index("const refreshGeneration = state.snapshotRefreshes.begin()")
    assert 'id="outcomeResults" aria-busy="false"' in page
    assert 'id="outcomeResults" aria-live=' not in page
    assert 'id="outcomeAnnouncement" role="status" aria-live="polite"' in page
    assert "Exact canonical outcome bytes are unavailable" in script
    assert "state.outcomes.renders.isCurrent(renderGeneration)" in script
    assert 'setAttribute("aria-busy", "false")' in script
    assert "outcome-rise" in style
    assert "outcome-orbit" in style
    assert "prefers-reduced-motion:reduce" in style
    assert "max-width:560px" in style
    assert "MissionOutcomes.latestEventId(snapshot.events)" in script
    assert "MissionOutcomes.eventStreamUrl(runId, state.streamCursors.get(runId))" in script
    assert 'stream.addEventListener("reset", refresh)' in script
    assert "if (event.lastEventId) state.streamCursors.set(runId, event.lastEventId)" in script


def test_outcomes_behavior_is_dom_independent_and_deterministic():
    node = shutil.which("node")
    if node is None:
        import pytest
        pytest.skip("Node.js is unavailable")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "mission_outcomes.test.js")],
        cwd=ROOT, check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "mission outcomes behavior: ok" in result.stdout


def test_outcomes_source_comparison_keeps_mockup_as_visual_authority():
    mockup = (ROOT / "docs" / "mockups" / "e7-mission-control-v3.html").read_text(
        encoding="utf-8"
    )
    style = (UI / "mission-control.css").read_text(encoding="utf-8")
    audit = (ROOT / "docs" / "mission-control-outcomes-audit.md").read_text(encoding="utf-8")
    for visual_contract in (
        ".tabs", ".tab", ".card", "button:focus-visible", "prefers-reduced-motion",
    ):
        assert visual_contract in mockup
    for live_extension in (
        ".outcome-stage", ".outcome-card", ".outcome-facets", ".outcome-controls",
        "@media(prefers-reduced-motion:reduce)",
    ):
        assert live_extension in style
    assert "e7-mission-control-v3.html" in audit
    assert "Rendered audit completed" in audit
    assert "normal desktop" in audit
    assert "560×900" in audit
    assert "reduced-motion" in audit
