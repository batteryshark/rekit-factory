from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).parents[1]
UI = ROOT / "src" / "rekit_factory" / "ui"


def test_campaign_ui_adversarial_helper():
    node = shutil.which("node")
    if node is None:
        return
    result = subprocess.run(
        [node, str(ROOT / "tests" / "mission_campaigns_adversarial.test.js")],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "mission campaigns adversarial: ok" in result.stdout
    links = subprocess.run(
        [node, str(ROOT / "tests" / "mission_campaign_links_adversarial.test.js")],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert links.returncode == 0, links.stdout + links.stderr
    assert "mission campaign links adversarial: ok" in links.stdout


def test_campaign_surface_preserves_keyboard_mobile_motion_and_bounded_errors():
    page = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "mission-control.js").read_text(encoding="utf-8")
    style = (UI / "mission-control.css").read_text(encoding="utf-8")
    api = (UI.parent / "api.py").read_text(encoding="utf-8")

    for marker in (
        'id="campaignFleet" aria-live="polite"', 'id="campaignBoardHealth" role="status"',
        'id="campaignDialog" aria-labelledby="campaignDialogTitle"',
        'data-campaign-close aria-label="Close campaign detail"',
    ):
        assert marker in page
    assert page.index("/ui/mission-campaigns.js") < page.index("/ui/mission-control.js")
    assert '"mission-campaigns.js": "text/javascript; charset=utf-8"' in api

    for behavior in (
        'event.target.matches("[data-campaign]")', 'event.key === "Escape"',
        "MissionCampaigns.canonicalActions(campaign).includes(action)",
        "if (state.campaignAction) return", "expectedRevision: campaign.revision",
        "encodeURIComponent(campaignId)", "Campaign projection identity mismatch",
        "campaignListRequest", "campaignDetailRequest", "campaignReturnFocus",
        'value: `mission-control:${campaignId}:${action}:${revision}`',
        'evidenceIds: []', 'setAttribute("aria-busy", "true")',
        "}, 1800)", "link.dataset.campaignRun", "link.dataset.campaignKind",
        "CSS.escape(entityId)", "campaign-linked-target",
        "The canonical record is no longer present in this run.",
        "decideCampaignChange(changeDecision)",
        "MissionCampaigns.changeDecisionPayload(campaign, requestId, approved)",
        "JSON.stringify(decision)",
        'querySelectorAll("[data-campaign-change-decision], [data-campaign-action]")',
        "Campaign change response identity mismatch",
        "/change-decisions",
        "await refreshOpenCampaign(current.campaignId)",
        "state.campaignSelected !== campaignId", "stale links were removed",
        "MissionCampaigns.renderDetail(bounded)",
    ):
        assert behavior in script
    # Campaign failures are deliberately summarized; backend exception text can carry paths.
    degraded = script[script.index("async function refreshCampaigns"):
                      script.index("async function openCampaign")]
    assert "error.message" not in degraded
    transition = script[script.index("async function transitionCampaign"):
                        script.index("function openCampaignLink")]
    assert "error.message" not in transition

    assert "@media(max-width:560px)" in style
    assert "@media(prefers-reduced-motion:reduce)" in style
    assert "campaign-arrive" in style and "campaign-orbit" in style
    assert ".campaign-change-row" in style and ".campaign-change-actions" in style
    decision = script[script.index("async function decideCampaignChange"):
                      script.index("async function openCampaignLink")]
    assert "proposedContract" not in decision and "replacementContract" not in decision
    assert "request.diff" not in decision and "request.proposed" not in decision
