from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from rekit_factory.evidence import EvidenceStore, Provenance
from rekit_factory.store import FactoryLedger


RESULT = b"tool=scan\nexit_code=0\nstdout:\nclean\nstderr:\n"


def _ledger(tmp_path: Path) -> tuple[FactoryLedger, str, str]:
    ledger = FactoryLedger(tmp_path / "run.db")
    run_id = "run-publication"
    ledger.create_run(
        run_id=run_id, project_id="project-fixture", target_path=tmp_path / "target",
        target_root=tmp_path, storage_root=tmp_path, run_dir=tmp_path,
        config_json="{}",
    )
    work_id = ledger.enqueue(
        run_id=run_id, key="tool-work", target=str(tmp_path / "target"),
        operation="rekit-tool", category="analysis", title="Scan fixture",
    )
    return ledger, run_id, work_id


def _capture(tmp_path: Path, run_id: str, call_id: str, work_id: str):
    store = EvidenceStore(tmp_path / "evidence")
    outcome = store.capture_tool_output(RESULT, Provenance(
        run_id=run_id, source="rekit:scan", capture_reason="tool execution proof",
        captured_at="2026-07-13T18:00:00Z", environment_id="worker:fixture",
        target_sha256="a" * 64, tool_id="scan", worker_id="worker-fixture",
        invocation_id=call_id, work_item_id=work_id, lease_id="lease-fixture",
    ))
    assert outcome.record is not None
    return store, outcome


def _stage(ledger: FactoryLedger, run_id: str, work_id: str):
    call_id = ledger.start_tool_call(
        run_id, work_id, "scan", 0, manifest_digest="b" * 64,
        declared_actions=("read_local_target",), credential_use=False,
    )
    authority = {
        "approvalId": None, "declaredActions": ["read_local_target"],
        "invocationId": call_id, "leaseId": "lease-fixture",
        "manifestDigest": "b" * 64, "scope": {"digest": "c" * 64},
        "targetSha256": "a" * 64, "toolId": "scan",
        "workerId": "worker-fixture", "remoteArtifacts": [],
    }
    key = ledger.stage_tool_evidence_publication(
        call_id, result_bytes=RESULT, exit_code=0, authority=authority,
    )
    return call_id, key, authority


@pytest.mark.parametrize(
    "boundary", ["artifacts", "events", "tool-completion", "work-resolution",
                 "terminal-event"],
)
def test_factory_publication_rolls_back_each_visibility_boundary_and_retries(
        tmp_path, boundary):
    ledger, run_id, work_id = _ledger(tmp_path)
    call_id, key, _ = _stage(ledger, run_id, work_id)
    store, outcome = _capture(tmp_path, run_id, call_id, work_id)
    record = outcome.record
    store.reconcile_capture_audit(record.artifact_id)

    def fail(name):
        if name == boundary:
            raise RuntimeError(f"crash after {name}")

    with pytest.raises(RuntimeError, match="crash after"):
        ledger.complete_tool_evidence_publication(
            key, evidence_artifact_id=record.artifact_id,
            evidence_original_sha256=record.original_sha256,
            evidence_path=store.root / record.display_path,
            evidence_size=record.display_size,
            evidence_metadata={"evidenceArtifactId": record.artifact_id},
            failure_injector=fail,
        )
    assert ledger.count_artifacts(run_id) == 0
    assert ledger.tool_calls(run_id)[0]["status"] == "running"
    assert ledger.get_work_item(work_id)["status"] == "queued"
    assert ledger.events(run_id) == []

    first = ledger.complete_tool_evidence_publication(
        key, evidence_artifact_id=record.artifact_id,
        evidence_original_sha256=record.original_sha256,
        evidence_path=store.root / record.display_path,
        evidence_size=record.display_size,
        evidence_metadata={"evidenceArtifactId": record.artifact_id},
    )
    second = ledger.complete_tool_evidence_publication(
        key, evidence_artifact_id=record.artifact_id,
        evidence_original_sha256=record.original_sha256,
        evidence_path=store.root / record.display_path,
        evidence_size=record.display_size,
        evidence_metadata={"evidenceArtifactId": record.artifact_id},
    )
    assert second == first
    assert ledger.count_artifacts(run_id) == 1
    assert ledger.tool_calls(run_id)[0]["status"] == "done"
    assert ledger.get_work_item(work_id)["status"] == "done"
    assert [event["kind"] for event in ledger.events(run_id)] == [
        "evidence.captured", "tool.completed",
    ]


def test_evidence_row_without_audit_is_verified_and_repaired(tmp_path):
    ledger, run_id, work_id = _ledger(tmp_path)
    call_id, _, _ = _stage(ledger, run_id, work_id)
    store = EvidenceStore(tmp_path / "evidence")
    provenance = Provenance(
        run_id=run_id, source="rekit:scan", capture_reason="tool execution proof",
        captured_at="2026-07-13T18:00:00Z", environment_id="worker:fixture",
        target_sha256="a" * 64, tool_id="scan", worker_id="worker-fixture",
        invocation_id=call_id, work_item_id=work_id, lease_id="lease-fixture",
    )
    with patch.object(store, "_audit", side_effect=RuntimeError("audit boundary crash")):
        with pytest.raises(RuntimeError, match="audit boundary"):
            store.capture_tool_output(RESULT, provenance)
    record = store.tool_record(run_id=run_id, invocation_id=call_id, work_item_id=work_id)
    assert record is not None
    repaired = store.reconcile_capture_audit(record.artifact_id)
    assert repaired.action.value == "captured"
    assert store.reconcile_capture_audit(record.artifact_id).sequence == repaired.sequence


def test_orphan_content_addressed_blobs_converge_and_corruption_fails_closed(tmp_path):
    ledger, run_id, work_id = _ledger(tmp_path)
    call_id, _, _ = _stage(ledger, run_id, work_id)
    store = EvidenceStore(tmp_path / "evidence")
    digest = hashlib.sha256(RESULT).hexdigest()
    store._write_blob(store.root / store._blob_path("raw", digest), RESULT, digest)
    store._write_blob(store.root / store._blob_path("display", digest), RESULT, digest)
    _, outcome = _capture(tmp_path, run_id, call_id, work_id)
    record = outcome.record
    assert record.original_sha256 == digest
    (store.root / record.raw_path).write_bytes(b"conflict")
    with pytest.raises(ValueError, match="missing or conflicting"):
        store.reconcile_capture_audit(record.artifact_id)


def test_publication_key_rejects_authority_result_and_evidence_conflicts(tmp_path):
    ledger, run_id, work_id = _ledger(tmp_path)
    call_id, key, authority = _stage(ledger, run_id, work_id)
    assert ledger.stage_tool_evidence_publication(
        call_id, result_bytes=RESULT, exit_code=0, authority=authority,
    ) == key
    with pytest.raises(ValueError, match="conflicting result or authority"):
        ledger.stage_tool_evidence_publication(
            call_id, result_bytes=RESULT + b"changed", exit_code=0, authority=authority,
        )
    store, outcome = _capture(tmp_path, run_id, call_id, work_id)
    with pytest.raises(ValueError, match="bytes conflict"):
        ledger.complete_tool_evidence_publication(
            key, evidence_artifact_id=outcome.record.artifact_id,
            evidence_original_sha256="f" * 64,
            evidence_path=store.root / outcome.record.display_path,
            evidence_size=outcome.record.display_size, evidence_metadata={},
        )
