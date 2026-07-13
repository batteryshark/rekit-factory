from pathlib import Path


ROOT = Path(__file__).parents[1]
UI = ROOT / "src" / "rekit_factory" / "ui"


def test_notification_configuration_surface_uses_only_opaque_revision_bound_commands():
    page = (UI / "index.html").read_text()
    script = (UI / "mission-control.js").read_text()
    style = (UI / "mission-control.css").read_text()

    for marker in (
        'id="notificationConfiguration"', 'id="notificationPreview"',
        "data-notification-config-save", "data-notification-channel-test",
        "data-notification-preview", "loadNotificationConfiguration",
        "saveNotificationConfiguration", "testNotificationChannel", "previewNotification",
    ):
        assert marker in page + script
    assert "expectedRevision: configuration.revision, preferencePresetId:" in script
    assert "findingNotificationStageId: $(\"notificationFindingStage\").value" in script
    assert "expectedRevision: state.notificationConfiguration.revision, testId" in script
    configuration_command = script[
        script.index("async function saveNotificationConfiguration"):
        script.index("async function testNotificationChannel")
    ]
    test_command = script[
        script.index("async function testNotificationChannel"):
        script.index("async function previewNotification")
    ]
    for forbidden in ("endpoint:", "credential", "message:", "payload:"):
        assert forbidden not in configuration_command
        assert forbidden not in test_command
    assert "endpoint and credentials withheld" in script
    assert 'data-channel-ref="${esc(channel.ref)}">Test</button>' in script
    assert "@media(max-width:560px){.notification-configuration{grid-template-columns:1fr}" in style
