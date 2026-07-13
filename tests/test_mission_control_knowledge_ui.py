from pathlib import Path


UI = Path(__file__).parents[1] / "src" / "rekit_factory" / "ui"


def test_knowledge_console_projects_bounded_calls_and_durable_references():
    page = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "mission-control.js").read_text(encoding="utf-8")

    for marker in (
        'data-tab="knowledge"', 'id="knowledgeFlow"', 'id="knowledgeReferences"',
        'id="knowledgeRootCatalog"', 'id="knowledgeRunRoots"',
        'aria-controls="tab-knowledge"',
        "Root paths and concept bodies are never retained here",
    ):
        assert marker in page
    for field in (
        'item.operation === "model-knowledge"', "snapshot.knowledgeReferences",
        "knowledgeOperation", "meta?.knowledgeRoots", "queryRationale", "provenance", "contentHash",
        "citations", "sourceConceptId", "linkTarget", "data-knowledge-toggle",
        "data-knowledge-copy", "path withheld", "reference metadata only",
    ):
        assert field in script
    assert "root.path" not in script
    assert "concept.body" not in script


def test_knowledge_interactions_are_keyboard_accessible_and_motion_safe():
    script = (UI / "mission-control.js").read_text(encoding="utf-8")
    style = (UI / "mission-control.css").read_text(encoding="utf-8")

    for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
        assert key in script
    assert 'aria-expanded="false"' in script
    assert "knowledge-enter" in style
    assert "knowledge-orbit" in style
    assert "prefers-reduced-motion" in style
