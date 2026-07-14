from pathlib import Path

from rekit_factory.api import UI_ASSETS


UI = Path(__file__).parents[1] / "src" / "rekit_factory" / "ui"


def test_research_workspace_is_bound_to_canonical_snapshot_and_deep_links():
    page = (UI / "index.html").read_text()
    script = (UI / "mission-control.js").read_text()
    style = (UI / "mission-control.css").read_text()
    assert 'id="researchWorkspace"' in page
    assert 'src="/ui/mission-research.js"' in page
    assert "MissionResearch.render({...snapshot, evidenceRecords: state.evidence})" in script
    assert "data-research-ref-kind" in script
    assert "openResearchReference" in script
    for marker in ("applyProjectMemoryOperation", 'data-memory-operation="workstream-stop"',
                   "state.snapshot?.memoryAuthority?.operations?.some",
                   "/memory-operations", "expectedEntitySha256"):
        assert marker in script
    for marker in (".research-lanes", ".research-card", ".research-proof", ".research-decision"):
        assert marker in style


def test_campaign_watch_contains_one_screen_canonical_synthesis():
    page = (UI / "index.html").read_text()
    script = (UI / "mission-control.js").read_text()
    campaigns = (UI / "mission-campaigns.js").read_text()
    assert 'id="campaignSynthesis"' in page
    assert "MissionCampaigns.renderSynthesis(campaigns)" in script
    for marker in ("strongestReproducedResult", "strongestResult", "renderSynthesis",
                   "data-campaign-link", "canonical health unavailable"):
        assert marker in campaigns


def test_full_page_routes_are_server_declared_and_canonically_revalidated():
    script = (UI / "mission-control.js").read_text()
    routes = (UI / "mission-notifications.js").read_text()
    for marker in ("restoreExactRoute", "navigateExactRoute", "persistExactRoute",
                   "canonicalTarget", "parseUrlRoute", "navigationRoute"):
        assert marker in script + routes
    assert "await restoreExactRoute()" in script
    assert "window.history.replaceState" in script
    assert "snapshot?.outcomeProjection?.degraded !== false" in routes
    assert "pendingQuestions" in routes and "dossiers" in routes
    assert "window.location.search" not in routes  # the pure parser receives bounded text only


def test_research_module_is_inside_the_static_asset_boundary():
    assert UI_ASSETS["mission-research.js"] == "text/javascript; charset=utf-8"


def test_research_refreshes_reject_stale_and_replaced_stream_responses():
    script = (UI / "mission-control.js").read_text()
    assert "snapshotRefreshes: MissionOutcomes.createGenerationGate()" in script
    assert "state.snapshotRefreshes.invalidate();" in script
    assert "const refreshGeneration = state.snapshotRefreshes.begin();" in script
    assert "!state.snapshotRefreshes.isCurrent(refreshGeneration)" in script
    assert "!MissionOutcomes.isCurrentEventStream(stream, state.stream, runId, state.selected)" in script


def test_research_static_accessibility_responsive_and_motion_contracts():
    research = (UI / "mission-research.js").read_text()
    style = (UI / "mission-control.css").read_text()
    assert 'aria-labelledby="researchWorkspaceHeading"' in research
    assert 'aria-label="${safe(name.replaceAll("-", " "))}"' in research
    assert '<details class="research-proof-detail"><summary>' in research
    assert ".research-proof-detail>summary:focus-visible" in style
    assert "@media(max-width:560px){.research-head" in style
    assert ".research-lanes{grid-auto-columns:minmax(min(86vw,320px),1fr)}" in style
    assert "@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}" in style
