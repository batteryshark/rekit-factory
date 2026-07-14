from pathlib import Path

from rekit_factory.api import UI_ASSETS


UI = Path(__file__).parents[1] / "src" / "rekit_factory" / "ui"


def test_research_workspace_is_bound_to_canonical_snapshot_and_deep_links():
    page = (UI / "index.html").read_text()
    script = (UI / "mission-control.js").read_text()
    style = (UI / "mission-control.css").read_text()
    assert 'id="researchWorkspace"' in page
    assert 'src="/ui/mission-research.js"' in page
    assert "MissionResearch.render(snapshot)" in script
    assert "data-research-ref-kind" in script
    assert "openResearchReference" in script
    for marker in (".research-lanes", ".research-card", ".research-proof", ".research-decision"):
        assert marker in style


def test_research_module_is_inside_the_static_asset_boundary():
    assert UI_ASSETS["mission-research.js"] == "text/javascript; charset=utf-8"
