from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import struct
import zlib

import pytest

from rekit_factory.evidence import (
    AuditAction,
    CapturePolicy,
    EvidenceState,
    EvidenceStore,
    Provenance,
    RetentionClass,
    default_expiry,
    redact,
)


NOW = "2026-07-13T12:00:00Z"
TARGET_HASH = hashlib.sha256(b"fixture-target").hexdigest()


def provenance(*, source="terminal", reason="fixture proof"):
    return Provenance(
        run_id="run-fixture", source=source, capture_reason=reason,
        captured_at=NOW, environment_id="local:test", target_sha256=TARGET_HASH,
        tool_id="fixture-scan", worker_id="worker-1", invocation_id="invoke-1",
        work_item_id="work-1",
    )


def png_fixture(*, descending=False, note=b""):
    width, height = 9, 8
    rows = []
    for _y in range(height):
        values = [255 - x * 20 if descending else x * 20 for x in range(width)]
        rows.append(b"\x00" + bytes(channel for value in values for channel in (value, value, value)))

    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + (chunk(b"tEXt", note) if note else b"")
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def test_raw_and_display_are_independently_hashed_and_secrets_never_project(tmp_path):
    store = EvidenceStore(tmp_path / "evidence")
    secret = "super-secret-value"
    private = "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----"
    raw = f"api_key={secret}\nAuthorization: Bearer abcdefghijklmnop\n{private}\n".encode()

    outcome = store.capture_tool_output(raw, provenance())
    record = outcome.record

    assert record is not None and record.redacted
    assert record.original_sha256 == hashlib.sha256(raw).hexdigest()
    assert record.raw_sha256 == record.original_sha256
    assert record.display_sha256 != record.raw_sha256
    assert store.verify(record.artifact_id)
    assert (store.root / record.raw_path).read_bytes() == raw
    assert ((store.root / record.raw_path).stat().st_mode & 0o077) == 0
    display = store.display_text(record.artifact_id)
    assert display is not None
    assert secret not in display and "abc123" not in display and "abcdefghijklmnop" not in display
    assert "[REDACTED:CREDENTIAL]" in display
    assert "[REDACTED:PRIVATE_KEY]" in display
    assert store.knowledge_candidate_text(record.artifact_id) == display
    assert {event.action for event in outcome.events} == {AuditAction.CAPTURED, AuditAction.REDACTED}


def test_redaction_is_deterministic_for_tokens_and_keys():
    raw = (
        b"password=hunter-hunter\nAKIAABCDEFGHIJKLMNOP\n"
        b"ghp_abcdefghijklmnopqrstuvwxyz123456\n"
        b"eyJabcdefghij.abcdefghij.abcdefghij\n"
    )
    first = redact(raw)
    second = redact(raw)

    assert first == second
    assert b"hunter-hunter" not in first.data
    assert b"AKIAABCDEFGHIJKLMNOP" not in first.data
    assert b"ghp_abcdefghijklmnopqrstuvwxyz123456" not in first.data
    assert b"eyJabcdefghij" not in first.data


def test_proof_policy_withholds_incidental_terminal_and_cli_has_no_screenshots(tmp_path):
    store = EvidenceStore(tmp_path / "evidence")

    incidental = store.capture_terminal(b"shell chatter", provenance(), proof_required=False)
    proof = store.capture_terminal(b"assertion: ok", provenance(reason="test assertion"), proof_required=True)

    assert incidental.record is None
    assert incidental.events[0].action is AuditAction.WITHHELD
    assert proof.record is not None and proof.record.kind == "terminal-output"
    assert not store.policy.allow_screenshots
    assert not list(store.root.rglob("*.png"))


def test_dedupe_and_budgets_are_durable_across_restart_and_concurrency(tmp_path):
    root = tmp_path / "evidence"
    policy = CapturePolicy(max_run_bytes=12, max_run_artifacts=2, max_artifact_bytes=8)
    store = EvidenceStore(root, policy=policy)
    first = store.capture_tool_output(b"abcdefghijk", provenance())
    duplicate = store.capture_tool_output(b"abcdefghijk", provenance(source="second-worker"))

    assert first.record is not None and first.record.truncated
    assert first.record.raw_size == 8 and first.record.original_size == 11
    assert duplicate.record == first.record
    assert duplicate.events[0].action is AuditAction.DEDUPED
    assert any(event.action is AuditAction.TRUNCATED for event in first.events)

    restarted = EvidenceStore(root, policy=policy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(
            lambda value: restarted.capture_tool_output(value, provenance(source=value.decode())),
            (b"WXYZ", b"1234"),
        ))
    retained = [outcome.record for outcome in outcomes if outcome.record]
    withheld = [outcome for outcome in outcomes if outcome.record is None]
    assert len(retained) == 1
    assert retained[0].raw_size == 4
    assert len(withheld) == 1
    assert withheld[0].events[0].action is AuditAction.WITHHELD


def test_redaction_happens_before_truncation_so_partial_secrets_do_not_leak(tmp_path):
    policy = CapturePolicy(max_run_bytes=14, max_run_artifacts=1, max_artifact_bytes=14)
    store = EvidenceStore(tmp_path / "evidence", policy=policy)
    outcome = store.capture_tool_output(b"api_key=super-secret-value and more", provenance())

    assert outcome.record is not None and outcome.record.truncated
    projection = store.display_text(outcome.record.artifact_id)
    assert projection is not None
    assert "super" not in projection


def test_classifier_quarantine_blocks_display_and_knowledge(tmp_path):
    classifier = lambda data: ("personal_data",) if b"patient" in data else ()
    store = EvidenceStore(tmp_path / "evidence", classifiers=(classifier,))
    outcome = store.capture_tool_output(b"patient=fixture", provenance())

    assert outcome.record is not None
    assert outcome.record.state is EvidenceState.QUARANTINED
    assert store.display_text(outcome.record.artifact_id) is None
    assert store.knowledge_candidate_text(outcome.record.artifact_id) is None
    assert AuditAction.QUARANTINED in {event.action for event in outcome.events}


def test_citation_and_hold_make_expiry_or_delete_an_operator_visible_conflict(tmp_path):
    store = EvidenceStore(tmp_path / "evidence")
    expires = "2026-07-13T12:01:00Z"
    first = store.capture_tool_output(b"cited proof", provenance(), expires_at=expires).record
    second = store.capture_tool_output(b"held proof", provenance(), expires_at=expires).record
    assert first and second
    store.pin(first.artifact_id, "finding-1", now=NOW)
    store.hold(second.artifact_id, True, now=NOW)

    events = store.expire_due(now="2026-07-13T12:02:00Z")
    assert {event.action for event in events} == {AuditAction.RETENTION_CONFLICT}
    assert store.get(first.artifact_id).state is EvidenceState.RETENTION_CONFLICT
    assert store.get(second.artifact_id).state is EvidenceState.RETENTION_CONFLICT
    assert store.display_text(first.artifact_id) == "cited proof"

    deletion = store.request_delete(first.artifact_id, now="2026-07-13T12:03:00Z")
    assert deletion.action is AuditAction.RETENTION_CONFLICT
    assert "citation_pin" in deletion.payload["reasons"]
    store.unpin(first.artifact_id, "finding-1", now="2026-07-13T12:04:00Z")
    store.hold(second.artifact_id, False, now="2026-07-13T12:04:00Z")
    assert store.get(first.artifact_id).state is EvidenceState.RETAINED
    assert store.get(second.artifact_id).state is EvidenceState.RETAINED


def test_uncited_expiry_removes_blobs_and_audits(tmp_path):
    store = EvidenceStore(tmp_path / "evidence")
    record = store.capture_tool_output(
        b"temporary proof", provenance(), expires_at="2026-07-13T12:01:00Z",
        retention_class=RetentionClass.EPHEMERAL,
    ).record
    assert record

    events = store.expire_due(now="2026-07-13T12:02:00Z")

    assert events[0].action is AuditAction.EXPIRED
    assert store.get(record.artifact_id).state is EvidenceState.EXPIRED
    assert store.display_bytes(record.artifact_id) is None
    assert not (store.root / record.raw_path).exists()
    assert not (store.root / record.display_path).exists()
    assert AuditAction.EXPIRED in {event.action for event in store.audit_events("run-fixture")}


def test_retention_default_is_deterministic():
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    assert default_expiry(RetentionClass.EPHEMERAL, now=now) == "2026-07-14T00:00:00Z"
    assert default_expiry(RetentionClass.ARCHIVE, now=now) is None


def test_visual_capture_exact_and_perceptual_dedupe_without_desktop_capture(tmp_path):
    store = EvidenceStore(tmp_path / "evidence", policy=CapturePolicy(allow_screenshots=True))
    first = store.capture_visual(png_fixture(), provenance(source="gui"), meaningful=True)
    exact = store.capture_visual(png_fixture(), provenance(source="gui-exact"), meaningful=True)
    perceptual = store.capture_visual(
        png_fixture(note=b"fixture-note"), provenance(source="gui-similar"), meaningful=True,
    )

    assert first.record is not None and first.record.perceptual_hash == "0000000000000000"
    assert exact.record == first.record and exact.events[0].action is AuditAction.DEDUPED
    assert perceptual.record == first.record and perceptual.events[0].action is AuditAction.DEDUPED
    assert "perceptually equivalent" in perceptual.events[0].reason


def test_visual_policy_requires_meaningful_frame_and_enforces_desktop_and_budget(tmp_path):
    rejected = EvidenceStore(tmp_path / "rejected", policy=CapturePolicy(allow_screenshots=True))
    assert rejected.capture_visual(
        png_fixture(), provenance(source="gui"), meaningful=False
    ).record is None
    assert rejected.capture_visual(
        png_fixture(), provenance(source="gui"), meaningful=True, full_desktop=True
    ).record is None

    policy = CapturePolicy(
        allow_screenshots=True, max_screenshots=1, max_screenshot_bytes=500_000,
        full_desktop_disposition="quarantine",
    )
    store = EvidenceStore(tmp_path / "quarantined", policy=policy)
    desktop = store.capture_visual(
        png_fixture(), provenance(source="gui"), meaningful=True, full_desktop=True
    )
    exhausted = store.capture_visual(
        png_fixture(descending=True), provenance(source="gui-second"), meaningful=True
    )
    assert desktop.record is not None and desktop.record.state is EvidenceState.QUARANTINED
    assert desktop.record.quarantine_labels == ("full_desktop",)
    assert store.display_bytes(desktop.record.artifact_id) is None
    assert exhausted.record is None and "budget" in exhausted.events[0].reason


def test_remote_seam_cot_exclusion_and_public_metadata(tmp_path):
    store = EvidenceStore(tmp_path / "evidence")
    data = b"remote proof"
    outcome = store.ingest_remote_artifact(
        data, provenance(source="remote-provider"), expected_sha256=hashlib.sha256(data).hexdigest(),
        media_type="application/octet-stream",
    )
    assert outcome.record is not None and outcome.record.kind == "remote-artifact"
    with pytest.raises(ValueError, match="mismatch"):
        store.ingest_remote_artifact(
            data, provenance(), expected_sha256="0" * 64, media_type="application/octet-stream"
        )
    reasoning = store.capture(
        b"private reasoning", provenance(), kind="provider-chain-of-thought", media_type="text/plain"
    )
    assert reasoning.record is None and reasoning.events[0].action is AuditAction.WITHHELD

    public = store.public_records("run-fixture")[0]
    assert public["provenance"]["capture_reason"] == "fixture proof"
    assert "raw_path" not in public and "display_path" not in public
    assert "rawPath" not in public and "displayPath" not in public
