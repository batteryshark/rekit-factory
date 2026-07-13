import json
import os

import pytest

from rekit_factory.campaign_lifecycle import (
    ARCHIVE_AUTHORITY,
    CAMPAIGN_AUTHORITY,
    COVERAGE_AUTHORITY,
    ArchiveRecord,
    CampaignLifecycleState,
    CampaignLifecycleStore,
    CampaignRecord,
    CoverageRecord,
)


def campaign_state():
    state = CampaignLifecycleState().create_campaign(
        "campaign-b", authority=CAMPAIGN_AUTHORITY,
    )
    return state.create_campaign("campaign-a", authority=CAMPAIGN_AUTHORITY)


def test_coverage_is_orthogonal_to_campaign_completion_and_archive_state():
    state = campaign_state()
    state = state.record_coverage(
        "campaign-a", completed_units=4, total_units=4,
        expected_revision=1, authority=COVERAGE_AUTHORITY,
    )
    covered = state.campaigns[0]
    assert covered.state == "planned"
    assert covered.coverage.state == "covered"
    assert state.archives == ()

    state = state.transition_campaign(
        "campaign-a", "active", expected_revision=2, authority=CAMPAIGN_AUTHORITY,
    ).transition_campaign(
        "campaign-a", "completed", expected_revision=3, authority=CAMPAIGN_AUTHORITY,
    )
    state = state.create_archive(
        "archive-a", "campaign-a", authority=ARCHIVE_AUTHORITY,
    )
    assert state.campaigns[0].state == "completed"
    assert state.archives[0].state == "unarchived"
    state = state.transition_archive(
        "archive-a", "archived", expected_revision=1, authority=ARCHIVE_AUTHORITY,
    )
    assert state.archives[0].state == "archived"
    assert state.campaigns[0].coverage.state == "covered"


def test_authority_and_transition_matrix_fail_closed():
    state = CampaignLifecycleState().create_campaign(
        "campaign-a", authority=CAMPAIGN_AUTHORITY,
    )
    with pytest.raises(ValueError, match="authority must be muster"):
        state.record_coverage(
            "campaign-a", completed_units=1, total_units=1,
            expected_revision=1, authority=CAMPAIGN_AUTHORITY,
        )
    with pytest.raises(ValueError, match="invalid campaign transition"):
        state.transition_campaign(
            "campaign-a", "completed", expected_revision=1, authority=CAMPAIGN_AUTHORITY,
        )
    with pytest.raises(ValueError, match="stale lifecycle revision"):
        state.transition_campaign(
            "campaign-a", "active", expected_revision=2, authority=CAMPAIGN_AUTHORITY,
        )
    with pytest.raises(ValueError, match="parent campaign"):
        CampaignLifecycleState(
            archives=(ArchiveRecord("archive-a", "missing-campaign"),),
        )


def test_coverage_counts_are_strict_monotonic_and_self_verifying():
    assert CoverageRecord(0, 0).state == "uncovered"
    assert CoverageRecord(0, 3).state == "uncovered"
    assert CoverageRecord(1, 3).state == "partial"
    assert CoverageRecord(3, 3).state == "covered"
    with pytest.raises(ValueError, match="must not exceed"):
        CoverageRecord(4, 3)
    value = CoverageRecord(1, 3).to_dict()
    value["state"] = "covered"
    with pytest.raises(ValueError, match="does not match"):
        CoverageRecord.from_dict(value)

    state = CampaignLifecycleState((CampaignRecord("campaign-a"),))
    state = state.record_coverage(
        "campaign-a", completed_units=2, total_units=4,
        expected_revision=1, authority=COVERAGE_AUTHORITY,
    )
    with pytest.raises(ValueError, match="must not decrease"):
        state.record_coverage(
            "campaign-a", completed_units=1, total_units=4,
            expected_revision=2, authority=COVERAGE_AUTHORITY,
        )


def test_serialization_is_sorted_canonical_and_strict_round_trip():
    state = campaign_state()
    state = state.create_archive("archive-z", "campaign-b", authority=ARCHIVE_AUTHORITY)
    state = state.create_archive("archive-a", "campaign-a", authority=ARCHIVE_AUTHORITY)
    encoded = state.canonical_bytes()
    assert encoded.endswith(b"\n")
    assert CampaignLifecycleState.from_dict(json.loads(encoded)) == state
    assert [item["campaignId"] for item in json.loads(encoded)["campaigns"]] == [
        "campaign-a", "campaign-b",
    ]
    assert CampaignLifecycleState.from_dict(state.to_dict()).canonical_bytes() == encoded

    malformed = state.to_dict()
    malformed["unexpected"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        CampaignLifecycleState.from_dict(malformed)


def test_atomic_store_round_trip_and_failed_replace_preserves_checkpoint(tmp_path, monkeypatch):
    store = CampaignLifecycleStore(tmp_path / "lifecycle")
    assert store.load() == CampaignLifecycleState()
    state = campaign_state()
    store.save(state)
    before = store.path.read_bytes()
    assert store.load() == state

    advanced = state.transition_campaign(
        "campaign-a", "active", expected_revision=1, authority=CAMPAIGN_AUTHORITY,
    )
    real_replace = os.replace

    def fail_replace(*args, **kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        store.save(advanced)
    monkeypatch.setattr(os, "replace", real_replace)
    assert store.path.read_bytes() == before
    assert store.load() == state


def test_store_rejects_duplicate_keys_symlinks_and_oversized_state(tmp_path):
    store = CampaignLifecycleStore(tmp_path / "safe")
    store.path.write_text('{"schemaVersion":1,"schemaVersion":1}')
    with pytest.raises(ValueError, match="duplicate JSON field"):
        store.load()

    store.path.unlink()
    outside = tmp_path / "outside"
    outside.write_text("{}")
    store.path.symlink_to(outside)
    with pytest.raises(ValueError, match="must not be a symlink"):
        store.load()

    bounded = CampaignLifecycleStore(tmp_path / "bounded", max_bytes=8)
    bounded.path.write_bytes(b"123456789")
    with pytest.raises(ValueError, match="size limit"):
        bounded.load()

