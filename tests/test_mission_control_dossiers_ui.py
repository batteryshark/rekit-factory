from pathlib import Path


ROOT = Path(__file__).parents[1] / "src" / "rekit_factory" / "ui"


def test_dossier_surface_keeps_e7_motion_and_contained_actions():
    html = (ROOT / "index.html").read_text()
    script = (ROOT / "mission-control.js").read_text()
    style = (ROOT / "mission-control.css").read_text()
    for value in (
        'data-tab="dossiers"', 'id="dossiers"', 'id="dossierCount"',
        'aria-controls="tab-dossiers"', "VERIFIED HANDOFF", "Open dossier", "Export ZIP",
    ):
        assert value in html + script
    assert "/dossiers/${encodeURIComponent(dossier.id)}" in script
    assert "dossier-arrive" in style
    assert "dossier-spin" in style
    assert ".dossier-card.stale" in style
    assert "max-width:720px" in style
    assert "Republish required" in script
    assert "dossier.verified === true" in script
    assert "prefers-reduced-motion:reduce" in style


def test_dossier_html_response_is_inert_by_default():
    api = (ROOT.parent / "api.py").read_text()
    assert "default-src 'none'" in api
    assert "frame-ancestors 'none'" in api
    assert '"X-Content-Type-Options", "nosniff"' in api
    assert '"X-Frame-Options", "DENY"' in api
