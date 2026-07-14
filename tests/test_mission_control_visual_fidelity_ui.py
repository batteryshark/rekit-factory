from pathlib import Path


UI = Path(__file__).parents[1] / "src" / "rekit_factory" / "ui"


def test_v3_chrome_uses_semantic_svg_icons() -> None:
    page = (UI / "index.html").read_text(encoding="utf-8")

    for icon in ("grid", "message", "plus", "wrench", "settings", "theme", "refresh"):
        assert f'data-icon="{icon}"' in page
    assert page.count('class="nav-icon" aria-hidden="true"><svg') == 5
    rail = page.split('<aside class="rail"', 1)[1].split("</aside>", 1)[0]
    for placeholder in ("◈", "◇", "☷", "∷"):
        assert placeholder not in rail


def test_v3_grid_and_theme_contracts_are_visible() -> None:
    style = (UI / "mission-control.css").read_text(encoding="utf-8")
    script = (UI / "mission-control.js").read_text(encoding="utf-8")

    assert "--grid:" in style
    assert "linear-gradient(var(--grid) 1px,transparent 1px)" in style
    assert "linear-gradient(90deg,var(--grid) 1px,transparent 1px)" in style
    assert "background-size:44px 44px" in style
    assert '[data-theme="light"]' in style
    assert '.theme-sun' in style and '.theme-moon' in style
    assert 'const THEME_KEY = "rekit-factory-theme"' in script
    assert "function initializeTheme()" in script
    assert "function toggleTheme()" in script
    assert "localStorage.setItem(THEME_KEY, resolved)" in script


def test_v3_topbar_restores_real_jump_health_and_attention_controls() -> None:
    page = (UI / "index.html").read_text(encoding="utf-8")
    style = (UI / "mission-control.css").read_text(encoding="utf-8")
    script = (UI / "mission-control.js").read_text(encoding="utf-8")

    for marker in (
        'class="topbar-wordmark"', 'id="topbarSearch"', 'id="topbarHealth"',
        'id="topHealthRun"', 'id="topHealthNeed"', 'id="topHealthDone"',
        'id="topInboxBadge"', 'class="operator-mark"',
    ):
        assert marker in page
    for behavior in (
        '$("topHealthRun").textContent = running',
        '$("topHealthNeed").textContent = needs',
        '$("topHealthDone").textContent = completed',
        '$("topInboxBadge").textContent = needs',
        'event.key.toLowerCase() === "k"',
        '$("topbarSearch").addEventListener("input"',
    ):
        assert behavior in script
    assert ".topbar-search" in style
    assert ".topbar-health" in style
    assert "conic-gradient(var(--green)" in style


def test_target_kind_icons_use_trusted_svg_vocabulary() -> None:
    script = (UI / "mission-control.js").read_text(encoding="utf-8")

    assert "const TARGET_ICONS = {" in script
    for kind in ("TREE", "PE", "APK", "JAR", "ELF", "BIN"):
        assert f"  {kind}: `<svg" in script
    assert "TARGET_ICONS[label] || TARGET_ICONS.BIN" in script
    assert 'icon: suffix ? "◇" : "⌘"' not in script
