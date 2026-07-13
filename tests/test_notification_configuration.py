from __future__ import annotations

import json

import pytest

from rekit_factory.notification_configuration import (
    NotificationConfigurationConflict, NotificationConfigurationStore, default_preferences,
)
from rekit_factory.notification_delivery import DesktopChannel, WebhookChannel


def test_configuration_is_external_redacted_revision_bound_and_restart_safe(tmp_path):
    path = tmp_path / "operator" / "notifications.sqlite3"
    channels = {
        "desktop-main": DesktopChannel("desktop-main"),
        "webhook-private": WebhookChannel(
            "webhook-private", "https://hooks.example.test/private/path?key=never-project",
            "credential:host-keychain/private-token",
        ),
    }
    store = NotificationConfigurationStore(
        path, preferences=default_preferences(), channels=channels,
    )
    initial = store.public_snapshot()
    assert initial["channelRefs"] == ["desktop-main"]
    encoded = json.dumps(initial)
    assert "hooks.example" not in encoded
    assert "host-keychain" not in encoded
    assert "never-project" not in encoded

    updated = store.update(
        expected_revision=initial["revision"], preference_preset_id="daily-digest",
        channel_refs=["webhook-private", "desktop-main"],
        finding_notification_stage_id="accepted",
    )
    replay = store.update(
        expected_revision=initial["revision"], preference_preset_id="daily-digest",
        channel_refs=["desktop-main", "webhook-private"],
        finding_notification_stage_id="accepted",
    )
    assert replay == updated
    restarted = NotificationConfigurationStore(
        path, preferences=default_preferences(), channels=channels,
    )
    assert restarted.public_snapshot() == updated
    database = path.read_bytes()
    assert b"hooks.example" not in database
    assert b"host-keychain" not in database

    with pytest.raises(NotificationConfigurationConflict, match="revision is stale"):
        restarted.update(
            expected_revision=initial["revision"], preference_preset_id="muted",
            channel_refs=["desktop-main"], finding_notification_stage_id="reproduced",
        )


@pytest.mark.parametrize("channel_refs", [[], ["missing"], ["desktop-main", "desktop-main"]])
def test_configuration_rejects_unbounded_or_unknown_channel_selection(tmp_path, channel_refs):
    store = NotificationConfigurationStore(
        tmp_path / "notifications.sqlite3",
        channels={"desktop-main": DesktopChannel("desktop-main")},
    )
    with pytest.raises(ValueError):
        store.update(
            expected_revision=store.public_snapshot()["revision"],
            preference_preset_id="immediate", channel_refs=channel_refs,
            finding_notification_stage_id="reproduced",
        )


def test_finding_stage_catalog_is_server_declared_and_invalid_stage_fails_closed(tmp_path):
    store = NotificationConfigurationStore(tmp_path / "notifications.sqlite3")
    snapshot = store.public_snapshot()
    assert snapshot["findingNotificationStageId"] == "reproduced"
    assert [item["id"] for item in snapshot["findingNotificationStages"]] == [
        "reproduced", "accepted",
    ]
    assert all(item["policyRevision"].startswith("sha256:")
               for item in snapshot["findingNotificationStages"])
    with pytest.raises(ValueError, match="reproduced or accepted"):
        store.update(
            expected_revision=snapshot["revision"], preference_preset_id="immediate",
            channel_refs=["desktop-primary"], finding_notification_stage_id="custom-prose",
        )
