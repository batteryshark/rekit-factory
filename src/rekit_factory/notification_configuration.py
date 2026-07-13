"""External, revision-bound notification configuration for Mission Control.

The browser may select only server-declared preference presets and opaque channel references.
Webhook endpoints and credential references remain in the environment-owned channel catalog and
are never persisted in this store or projected through the API.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Mapping, Sequence

from rekit_factory.notification_delivery import DesktopChannel, WebhookChannel
from rekit_factory.notification_preferences import NotificationPreferences


CONFIGURATION_SCHEMA_VERSION = 1
MAX_CHANNELS = 16
MAX_PRESETS = 16
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class NotificationConfigurationConflict(ValueError):
    """The requested configuration was based on a stale revision."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _revision(preset_id: str, preference_id: str, channel_refs: Sequence[str]) -> str:
    body = _canonical({
        "preferencePresetId": preset_id, "preferenceId": preference_id,
        "channelRefs": sorted(channel_refs),
    })
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def _safe_id(value: Any, field: str) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field} must be a safe stable identifier")
    return value


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def default_preferences() -> dict[str, NotificationPreferences]:
    base = {
        "schemaVersion": 1,
        "severity": {}, "projects": {}, "campaigns": {},
        "quietHours": {"timezone": "UTC", "windows": []},
    }
    return {
        "immediate": NotificationPreferences.from_dict({
            **base, "revision": "mission-control-immediate-v1",
            "default": {"mode": "immediate"},
        }),
        "batched-15m": NotificationPreferences.from_dict({
            **base, "revision": "mission-control-batched-15m-v1",
            "default": {"mode": "batched", "intervalMinutes": 15},
        }),
        "daily-digest": NotificationPreferences.from_dict({
            **base, "revision": "mission-control-daily-digest-v1",
            "default": {"mode": "digest", "atUtcMinute": 1020},
        }),
        "escalation-30m": NotificationPreferences.from_dict({
            **base, "revision": "mission-control-escalation-30m-v1",
            "default": {"mode": "escalation", "afterMinutes": 30},
        }),
        "muted": NotificationPreferences.from_dict({
            **base, "revision": "mission-control-muted-v1",
            "default": {"mode": "muted"},
        }),
    }


class NotificationConfigurationStore:
    """Persist safe selections while keeping channel capabilities outside durable run state."""

    def __init__(self, path: Path, *,
                 preferences: Mapping[str, NotificationPreferences] | None = None,
                 channels: Mapping[str, DesktopChannel | WebhookChannel] | None = None,
                 stale_operator_decision_after_seconds: int | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.preferences = dict(preferences or default_preferences())
        self.channels = dict(channels or {"desktop-primary": DesktopChannel("desktop-primary")})
        if stale_operator_decision_after_seconds is not None and (
                type(stale_operator_decision_after_seconds) is not int
                or not 1 <= stale_operator_decision_after_seconds <= 31_536_000):
            raise ValueError("stale-decision threshold must be 1..31536000 seconds")
        # None is an explicit disabled state: no elapsed-time transition is guessed.
        self.stale_operator_decision_after_seconds = stale_operator_decision_after_seconds
        if not 1 <= len(self.preferences) <= MAX_PRESETS:
            raise ValueError("notification preference catalog must be bounded and non-empty")
        if not 1 <= len(self.channels) <= MAX_CHANNELS:
            raise ValueError("notification channel catalog must be bounded and non-empty")
        for preset_id, preference in self.preferences.items():
            _safe_id(preset_id, "preference preset id")
            if not isinstance(preference, NotificationPreferences):
                raise TypeError("preference catalog values must be NotificationPreferences")
            # Reject forged dataclass construction at the catalog boundary.
            self.preferences[preset_id] = NotificationPreferences.from_json(
                preference.canonical_json
            )
        for channel_ref, channel in self.channels.items():
            _safe_id(channel_ref, "channel ref")
            if not isinstance(channel, (DesktopChannel, WebhookChannel)) \
                    or channel.channel_id != channel_ref:
                raise ValueError("channel catalog keys must match opaque channel identities")
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.executescript("""
                create table if not exists notification_configuration (
                    singleton integer primary key check(singleton=1),
                    preference_preset_id text not null,
                    channel_refs_json text not null,
                    revision text not null,
                    updated_at text not null
                );
            """)
            if connection.execute(
                    "select 1 from notification_configuration where singleton=1").fetchone() is None:
                preset_id = next(iter(self.preferences))
                refs = [next(iter(self.channels))]
                connection.execute(
                    "insert into notification_configuration values (1,?,?,?,?)",
                    (preset_id, _canonical(refs), self._revision(preset_id, refs), _timestamp()),
                )

    def _revision(self, preset_id: str, channel_refs: Sequence[str]) -> str:
        preference = self.preferences.get(preset_id)
        if preference is None:
            raise ValueError("unknown notification preference preset")
        return _revision(preset_id, preference.identity, channel_refs)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _selection(self, connection: sqlite3.Connection) -> tuple[str, list[str], str, str]:
        row = connection.execute(
            "select * from notification_configuration where singleton=1"
        ).fetchone()
        if row is None:
            raise ValueError("notification configuration is missing")
        try:
            refs = json.loads(row["channel_refs_json"])
        except json.JSONDecodeError as exc:
            raise ValueError("notification configuration is corrupt") from exc
        preset_id = row["preference_preset_id"]
        if preset_id not in self.preferences or type(refs) is not list \
                or not 1 <= len(refs) <= MAX_CHANNELS or len(set(refs)) != len(refs) \
                or any(ref not in self.channels for ref in refs) \
                or row["revision"] != self._revision(preset_id, refs):
            raise ValueError("notification configuration is corrupt")
        return preset_id, sorted(refs), row["revision"], row["updated_at"]

    def public_snapshot(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            preset_id, refs, revision, updated_at = self._selection(connection)
        presets = []
        for identity, preference in sorted(self.preferences.items()):
            rule = preference.document["default"]
            presets.append({
                "id": identity, "mode": rule["mode"],
                "parameter": next((value for key, value in rule.items() if key != "mode"), None),
                "preferenceRevision": preference.document["revision"],
            })
        channels = [{
            "ref": identity,
            "kind": "desktop" if isinstance(channel, DesktopChannel) else "webhook",
        } for identity, channel in sorted(self.channels.items())]
        return {
            "schemaVersion": CONFIGURATION_SCHEMA_VERSION,
            "revision": revision, "updatedAt": updated_at,
            "preferencePresetId": preset_id, "channelRefs": refs,
            "preferencePresets": presets, "channels": channels,
        }

    def update(self, *, expected_revision: str, preference_preset_id: str,
               channel_refs: Sequence[str]) -> dict[str, Any]:
        _safe_id(expected_revision, "expectedRevision")
        preset_id = _safe_id(preference_preset_id, "preferencePresetId")
        if preset_id not in self.preferences:
            raise ValueError("unknown notification preference preset")
        if type(channel_refs) is not list or not 1 <= len(channel_refs) <= MAX_CHANNELS:
            raise ValueError("channelRefs must be a bounded non-empty array")
        refs = sorted(_safe_id(item, "channel ref") for item in channel_refs)
        if len(set(refs)) != len(refs) or any(item not in self.channels for item in refs):
            raise ValueError("channelRefs contain duplicates or unknown references")
        requested_revision = self._revision(preset_id, refs)
        with self._lock, self._connect() as connection:
            connection.execute("begin immediate")
            current_preset, current_refs, current_revision, _ = self._selection(connection)
            if current_preset == preset_id and current_refs == refs:
                connection.rollback()
                return self.public_snapshot()
            if current_revision != expected_revision:
                connection.rollback()
                raise NotificationConfigurationConflict("notification configuration revision is stale")
            connection.execute(
                "update notification_configuration set preference_preset_id=?,"
                "channel_refs_json=?,revision=?,updated_at=? where singleton=1",
                (preset_id, _canonical(refs), requested_revision, _timestamp()),
            )
            connection.commit()
        return self.public_snapshot()

    def selected_preference(self) -> NotificationPreferences:
        with self._lock, self._connect() as connection:
            preset_id, _, _, _ = self._selection(connection)
        return self.preferences[preset_id]

    def selected_delivery(self) -> tuple[NotificationPreferences, tuple[str, ...]]:
        """Return one lock-consistent, fully revalidated scheduling selection."""
        with self._lock, self._connect() as connection:
            preset_id, refs, _, _ = self._selection(connection)
            preference = NotificationPreferences.from_json(
                self.preferences[preset_id].canonical_json
            )
        return preference, tuple(refs)

    def channel(self, channel_ref: str) -> DesktopChannel | WebhookChannel:
        _safe_id(channel_ref, "channel ref")
        channel = self.channels.get(channel_ref)
        if channel is None:
            raise KeyError(channel_ref)
        return channel
