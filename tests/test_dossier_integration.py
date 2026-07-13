from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from urllib.error import HTTPError
from urllib.request import urlopen
import zipfile
import pytest

from muster import resolve_run_dir, stable_key, utcnow

from rekit_factory.control import InvestigationController, RunRequest, _project_memory_log
from rekit_factory.api import FactoryServer
from rekit_factory.dossiers import (
    DossierNotReady, DossierPublisher, dossier_list, verify_published_dossier,
)
from rekit_factory.evidence import EvidenceStore, Provenance
from rekit_factory.findings import (
    FindingMemory, FindingProposal, FindingTransition, ObservationEvidence, ReproductionAttempt,
    ReproductionRecipe, ReproductionStep,
)
from rekit_factory.hypotheses import (
    DiscriminatingTestProposal, HypothesisMemory, HypothesisProposal,
)
from rekit_factory.memory import EvidenceRef
from rekit_factory.models import ModelProfile, WorkerReport
from rekit_factory.rekit_client import ToolManifest
from rekit_factory.scope import (
    ActionAuthority, AuthorizedScope, ScopeApproval, ScopeEnvelope, TargetGrant,
)
from rekit_factory.store import FactoryLedger


class FixtureRekit:
    def __init__(self):
        self.value = ToolManifest(
            id="fixture-reader", name="Fixture reader", description="Read a staged fixture",
            safety_tier=0, executes_input="no", network="none", source="fixture",
            actions=(ActionAuthority.READ_LOCAL_TARGET,),
        )

    def manifest(self, tool_id):
        if tool_id != self.value.id:
            raise KeyError(tool_id)
        return self.value

    def list_tools(self):
        return [self.value]


class QuietBackend:
    profile = ModelProfile(
        name="fixture", provider="test", model="fixture", base_url="https://invalid.test",
        api_key="secret",
    )

    async def analyze(self, **_kwargs):
        return WorkerReport(summary="done", status_update="done"), {}


def _published_fixture(tmp_path: Path, *, decoy_target_first: bool = False):
    target = tmp_path / "target.bin"
    target.write_bytes(b"immutable target bytes")
    scope_authorization = None
    if decoy_target_first:
        decoy = tmp_path / "decoy.bin"
        decoy.write_bytes(b"different authorized target")
        envelope = ScopeEnvelope(
            scope_id="dossier-multi-target", revision=1,
            valid_from="2026-07-01T00:00:00Z",
            valid_until="2026-08-01T00:00:00Z",
            targets=(TargetGrant.from_path(decoy), TargetGrant.from_path(target)),
            actions=(ActionAuthority.READ_LOCAL_TARGET,),
        )
        scope_authorization = AuthorizedScope(envelope, ScopeApproval(
            scope_id=envelope.scope_id, revision=envelope.revision,
            content_digest=envelope.content_digest, approved_by="operator:test",
            approved_at="2026-07-01T00:00:00Z",
            expires_at="2026-08-01T00:00:00Z", rationale="owned dossier fixtures",
        ))
    rekit = FixtureRekit()
    controller = InvestigationController(
        storage_root=tmp_path / "runs", rekit=rekit, workers=QuietBackend(),
    )
    run_dir = controller.create(RunRequest(
        target, "Prove fixture behavior", worker_roles=("recon",), concurrency=1,
        scope=scope_authorization,
    ))
    paths = resolve_run_dir(run_dir)
    target_hash = TargetGrant.from_path(target).content_sha256
    store = EvidenceStore(run_dir / "evidence")

    def capture(data: bytes, kind: str):
        outcome = store.capture(data, Provenance(
            run_id=paths.run_id, source="fixture", capture_reason="proof dossier test",
            captured_at=utcnow(), environment_id="clean:test", target_sha256=target_hash,
            worker_id="validator", work_item_id="work-validation",
        ), kind=kind, media_type="text/plain; charset=utf-8")
        assert outcome.record is not None
        return EvidenceRef("artifact", outcome.record.artifact_id)

    staged = capture(b"hello\n", "operator-fixture")
    observed = capture(b"OBSERVED:hello\n", "reproduction-output")
    log = _project_memory_log(paths)
    HypothesisMemory(log).propose(HypothesisProposal(
        id="h-fixture", claim="The reader emits the staged value", scope="target",
        expected_observation="Output includes the staged value",
        falsifier="Output omits the staged value", confidence=.8, references=[observed],
        proposed_test=DiscriminatingTestProposal(
            id="test-fixture", objective="Read the fixture", method="clean replay",
            expected_observation="OBSERVED:hello", falsifying_observation="missing value",
            information_gain=90, risk=0, cost_units=1,
        ),
    ))
    proposal = FindingProposal(
        id="f-fixture", hypothesis_id="h-fixture", scope="target",
        observations=[ObservationEvidence(
            observation="The clean reader emitted OBSERVED:hello", references=[observed],
        )],
        affected_component="fixture reader", impact_claim="The staged input is reproducible",
        assumptions=["UTF-8 input"], known_uncertainty="Only the retained fixture is covered",
        finding_type="informational", consequence="low", confidence=.9,
        references=[staged, observed],
        recipe=ReproductionRecipe(
            id="recipe-fixture-v1", staged_inputs=[staged],
            steps=[ReproductionStep(
                action="invoke", description="Read the first staged input",
                tool_id="fixture-reader",
                argv=["fixture-reader", "--input", "{staged:0}"],
                references=[staged],
            )],
            expected_observation="OBSERVED:hello",
            clean_environment_requirements=["empty temporary directory"],
        ),
    )
    findings = FindingMemory(log)
    findings.propose(
        proposal, origin_worker_id="origin-worker", origin_session_id="session:origin",
        origin_model_profile="fixture",
    )
    findings.mark_validation_pending("f-fixture")
    findings.record_attempt(ReproductionAttempt(
        id="attempt-fixture", finding_id="f-fixture", recipe_id="recipe-fixture-v1",
        outcome="success", worker_id="validator-worker", session_id="session:validator",
        environment_id="clean:test", clean_environment=True, model_profile="fixture",
        platform="test-os", architecture="test-arch", isolation="fresh-process",
        observations=["OBSERVED:hello"], references=[observed],
    ))
    with FactoryLedger(paths.db_path) as ledger:
        ledger.enqueue(
            run_id=paths.run_id, key=stable_key("validation", "f-fixture"),
            target=str(target), operation="model-worker", category="finding-validation",
            title="Validate fixture", priority=200,
            payload={
                "findingId": "f-fixture", "workerId": "validator-worker",
            },
        )
        manifest = rekit.manifest("fixture-reader")
        tool_work = ledger.enqueue(
            run_id=paths.run_id, key=stable_key("tool-validation", "f-fixture"),
            target=str(target), operation="model-rekit-tool", category="tool",
            title="Execute fixture reader", priority=210,
            payload={
                "findingId": "f-fixture", "workerItemId": "work-validation",
                "toolId": "fixture-reader",
                "manifestDigest": manifest.effective_manifest_digest,
                "authorityVersion": manifest.authority_version,
            },
        )
        call_id = ledger.start_tool_call(
            paths.run_id, tool_work, "fixture-reader", 0,
            manifest_digest=manifest.effective_manifest_digest,
            declared_actions=(ActionAuthority.READ_LOCAL_TARGET.value,),
            credential_use=False,
        )
        ledger.finish_tool_call(
            call_id, status="done", output_path=str(target), exit_code=0,
        )
        ledger.add_artifact(
            run_id=paths.run_id, kind="tool-output", path=target,
            logical_path="tool-output/fixture-reader.txt", origin="rekit:fixture-reader",
            metadata={
                "toolId": "fixture-reader",
                "effectiveManifestDigest": manifest.effective_manifest_digest,
                "verifiedManifestDigest": manifest.effective_manifest_digest,
                "provenance": {
                    "work_item_id": tool_work, "invocation_id": call_id,
                },
            },
        )
        ledger.set_work_status(tool_work, "done", result={"observation": "OBSERVED:hello"})
        unrelated = ledger.enqueue(
            run_id=paths.run_id, key=stable_key("unrelated"), target=str(target),
            operation="model-worker", category="worker", title="Unrelated", priority=1,
            payload={"workerId": "origin-worker"},
        )
        dossier = DossierPublisher(paths, ledger, rekit).publish("f-fixture")
        snapshot = controller._snapshot_open(ledger, paths)
    return controller, paths, dossier, snapshot, unrelated, rekit


def test_clean_worker_replays_only_published_dossier_and_unrelated_work_is_excluded(tmp_path):
    _controller, paths, dossier, snapshot, unrelated, _rekit = _published_fixture(tmp_path)
    assert snapshot["dossiers"][0]["id"] == dossier["id"]
    assert snapshot["dossiers"][0]["verificationStatus"] == "published"
    root = paths.run_dir / "dossiers" / dossier["manifestSha256"]
    bundle = json.loads((root / "proof.json").read_text())
    manifest = bundle["manifest"]
    assert unrelated not in manifest["work_item_ids"]

    clean = tmp_path / "clean-worker"
    clean.mkdir()
    staged_paths = []
    included = {item["citation_id"]: item for item in manifest["artifacts"]
                if item["kind"] == "included"}
    for action in manifest["actions"]:
        if action["action"] == "stage-input":
            source = root / included[action["citation_ids"][0]]["path"]
            destination = clean / source.name
            destination.write_bytes(source.read_bytes())
            staged_paths.append(destination)
        elif action["action"] == "invoke":
            argv = [str(staged_paths[0]) if value == "{staged:0}" else value
                    for value in action["argv"]]
            # The clean worker resolves the dossier's tool identity locally; no chat is consulted.
            result = subprocess.run(
                [sys.executable, "-c",
                 "import pathlib,sys; print('OBSERVED:' + pathlib.Path(sys.argv[1]).read_text().strip())",
                 argv[-1]], cwd=clean, text=True, capture_output=True, check=True,
            )
            observed = result.stdout.strip()
        elif action["action"] == "compare":
            assert action["description"].endswith(observed)

    assert observed == "OBSERVED:hello"
    assert verify_published_dossier(paths.run_dir, {**dossier, "runId": paths.run_id}).valid


def test_publication_uses_run_bound_target_and_tool_identities_despite_drift(tmp_path):
    _controller, paths, dossier, _snapshot, _unrelated, rekit = _published_fixture(tmp_path)
    root = paths.run_dir / "dossiers" / dossier["manifestSha256"]
    before = (root / "proof.json").read_bytes()
    target = Path(json.loads(paths.run_json.read_text())["target"])
    target.write_bytes(b"drifted after run")
    rekit.value = ToolManifest(
        id="fixture-reader", name="Drifted", description="different registry entry",
        safety_tier=3, executes_input="full", network="optional", source="drifted",
        actions=(ActionAuthority.READ_LOCAL_TARGET, ActionAuthority.EXECUTE_UNTRUSTED,
                 ActionAuthority.NETWORK_ACCESS),
    )
    with FactoryLedger(paths.db_path) as ledger:
        repeated = DossierPublisher(paths, ledger, rekit).publish("f-fixture")
    assert repeated["manifestSha256"] == dossier["manifestSha256"]
    assert (root / "proof.json").read_bytes() == before


def test_dossier_binds_the_run_target_not_the_first_authorized_target(tmp_path):
    _controller, paths, dossier, _snapshot, _unrelated, _rekit = _published_fixture(
        tmp_path, decoy_target_first=True,
    )
    bundle = json.loads((
        paths.run_dir / "dossiers" / dossier["manifestSha256"] / "proof.json"
    ).read_text())
    target_hash = TargetGrant.from_path(tmp_path / "target.bin").content_sha256
    decoy_hash = TargetGrant.from_path(tmp_path / "decoy.bin").content_sha256
    assert bundle["manifest"]["target_inputs"][0]["sha256"] == target_hash
    assert target_hash != decoy_hash


def test_dossier_rejects_incomplete_or_unattested_finding_tool_calls(tmp_path):
    _controller, paths, _dossier, _snapshot, _unrelated, rekit = _published_fixture(tmp_path)
    with FactoryLedger(paths.db_path) as ledger:
        publisher = DossierPublisher(paths, ledger, rekit)
        memory = _project_memory_log(paths).replay()
        ledger.conn.execute(
            "update factory_tool_calls set status='failed' where tool_id='fixture-reader'"
        )
        ledger.conn.commit()
        with pytest.raises(DossierNotReady, match="attestation"):
            publisher._build_manifest(memory, "f-fixture")

        ledger.conn.execute(
            "update factory_tool_calls set status='done' where tool_id='fixture-reader'"
        )
        row = ledger.conn.execute(
            "select id,metadata_json from artifacts where kind='tool-output'"
        ).fetchone()
        metadata = json.loads(row["metadata_json"])
        metadata["verifiedManifestDigest"] = "f" * 64
        ledger.conn.execute(
            "update artifacts set metadata_json=? where id=?",
            (json.dumps(metadata, sort_keys=True), row["id"]),
        )
        ledger.conn.commit()
        with pytest.raises(DossierNotReady, match="attestation"):
            publisher._build_manifest(memory, "f-fixture")


def test_dossier_attestation_must_match_the_exact_completed_invocation(tmp_path):
    _controller, paths, _dossier, _snapshot, _unrelated, rekit = _published_fixture(tmp_path)
    with FactoryLedger(paths.db_path) as ledger:
        row = ledger.conn.execute(
            "select id,metadata_json from artifacts where kind='tool-output'"
        ).fetchone()
        metadata = json.loads(row["metadata_json"])
        metadata["provenance"]["invocation_id"] = "tool-an-older-call"
        ledger.conn.execute(
            "update artifacts set metadata_json=? where id=?",
            (json.dumps(metadata, sort_keys=True), row["id"]),
        )
        ledger.conn.commit()
        with pytest.raises(DossierNotReady, match="attestation"):
            DossierPublisher(paths, ledger, rekit)._build_manifest(
                _project_memory_log(paths).replay(), "f-fixture",
            )


def test_generic_snapshot_does_not_rehash_published_dossiers(tmp_path, monkeypatch):
    controller, paths, _dossier, _snapshot, _unrelated, _rekit = _published_fixture(tmp_path)
    monkeypatch.setattr(
        "rekit_factory.dossiers.verify_published_dossier",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected verification")),
    )
    snapshot = controller.snapshot(paths.run_dir)
    assert snapshot["dossiers"][0]["verificationStatus"] == "published"


def test_dossier_export_is_complete_and_deterministic(tmp_path):
    _controller, paths, dossier, _snapshot, _unrelated, rekit = _published_fixture(tmp_path)
    root = paths.run_dir / "dossiers" / dossier["manifestSha256"]
    first = (root / "dossier.zip").read_bytes()
    with zipfile.ZipFile(root / "dossier.zip") as archive:
        assert {"proof.json", "manifest.json", "report.md", "report.html"} <= set(archive.namelist())
    with FactoryLedger(paths.db_path) as ledger:
        DossierPublisher(paths, ledger, rekit).publish("f-fixture")
        assert len([row for row in ledger.conn.execute(
            "select * from artifacts where kind like 'proof-%'"
        )]) == 5
    assert (root / "dossier.zip").read_bytes() == first


@pytest.mark.parametrize("failure_boundary", ["middle-artifact", "publication-event"])
def test_dossier_publication_crash_rolls_back_visibility_and_retries_exactly(
    tmp_path, failure_boundary,
):
    _controller, paths, dossier, _snapshot, _unrelated, rekit = _published_fixture(tmp_path)
    root = paths.run_dir / "dossiers" / dossier["manifestSha256"]
    sealed_bytes = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    with FactoryLedger(paths.db_path) as ledger:
        with ledger.conn:
            ledger.conn.execute(
                "delete from artifacts where run_id=? and origin='proof-dossier'",
                (paths.run_id,),
            )
            ledger.conn.execute(
                "delete from factory_events where run_id=? and kind='dossier.published'",
                (paths.run_id,),
            )
        if failure_boundary == "middle-artifact":
            ledger.conn.execute(
                "create trigger fail_dossier_publish before insert on artifacts "
                "when new.origin='proof-dossier' and new.kind='proof-report' "
                "begin select raise(abort, 'injected dossier artifact failure'); end"
            )
        else:
            ledger.conn.execute(
                "create trigger fail_dossier_publish before insert on factory_events "
                "when new.kind='dossier.published' "
                "begin select raise(abort, 'injected dossier event failure'); end"
            )
        with pytest.raises(sqlite3.IntegrityError, match="injected dossier"):
            DossierPublisher(paths, ledger, rekit).publish("f-fixture")
        assert ledger.conn.execute(
            "select count(*) from artifacts where run_id=? and origin='proof-dossier'",
            (paths.run_id,),
        ).fetchone()[0] == 0
        assert ledger.conn.execute(
            "select count(*) from factory_events where run_id=? and kind='dossier.published'",
            (paths.run_id,),
        ).fetchone()[0] == 0
        ledger.conn.execute("drop trigger fail_dossier_publish")
        recovered = DossierPublisher(paths, ledger, rekit).publish("f-fixture")
        assert recovered["manifestSha256"] == dossier["manifestSha256"]
        assert ledger.conn.execute(
            "select count(*) from artifacts where run_id=? and origin='proof-dossier'",
            (paths.run_id,),
        ).fetchone()[0] == 5
        assert ledger.conn.execute(
            "select count(*) from factory_events where run_id=? and kind='dossier.published'",
            (paths.run_id,),
        ).fetchone()[0] == 1
    assert {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()} == sealed_bytes


def test_resealed_dossier_cannot_replace_the_published_content_identity(tmp_path):
    _controller, paths, dossier, _snapshot, _unrelated, _rekit = _published_fixture(tmp_path)
    proof_path = paths.run_dir / "dossiers" / dossier["manifestSha256"] / "proof.json"
    bundle = json.loads(proof_path.read_text())
    bundle["manifest"]["limitations"][0]["text"] = "A different, resealed limitation."
    manifest_json = json.dumps(
        bundle["manifest"], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ) + "\n"
    bundle["manifest_sha256"] = sha256(manifest_json.encode()).hexdigest()
    proof_path.write_text(json.dumps(
        bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ) + "\n")

    with FactoryLedger(paths.db_path) as ledger:
        published = dossier_list(ledger, paths.run_id, run_dir=paths.run_dir)
    assert published[0]["verified"] is False
    assert published[0]["verificationStatus"] == "stale-or-invalid"


@pytest.mark.parametrize("kind", ["proof-report-html", "proof-export"])
def test_modified_published_projection_or_export_invalidates_the_dossier(tmp_path, kind):
    _controller, paths, dossier, _snapshot, _unrelated, _rekit = _published_fixture(tmp_path)
    with FactoryLedger(paths.db_path) as ledger:
        artifact = ledger.conn.execute(
            "select path from artifacts where id=? and run_id=? and kind=?",
            (dossier["artifactIds"][kind], paths.run_id, kind),
        ).fetchone()
        Path(artifact["path"]).write_bytes(Path(artifact["path"]).read_bytes() + b"tampered")
        published = dossier_list(ledger, paths.run_id, run_dir=paths.run_dir)
    assert published[0]["verified"] is False
    assert published[0]["verificationStatus"] == "stale-or-invalid"


def test_replaced_scope_file_cannot_change_the_run_pinned_dossier_scope(tmp_path):
    _controller, paths, _dossier, _snapshot, _unrelated, _rekit = _published_fixture(tmp_path)
    authorized = AuthorizedScope.from_dict(json.loads(
        (paths.run_dir / "scope.json").read_text()
    ))
    replacement_envelope = replace(authorized.envelope, scope_id="scope-replaced")
    replacement = AuthorizedScope(
        replacement_envelope,
        replace(
            authorized.approval, scope_id=replacement_envelope.scope_id,
            content_digest=replacement_envelope.content_digest,
        ),
    )
    (paths.run_dir / "scope.json").write_text(json.dumps(
        replacement.to_dict(), indent=2, sort_keys=True,
    ) + "\n")
    with FactoryLedger(paths.db_path) as ledger:
        published = dossier_list(ledger, paths.run_id, run_dir=paths.run_dir)
    assert published[0]["verified"] is False
    assert published[0]["verificationStatus"] == "stale-or-invalid"


def test_contained_api_lists_opens_and_downloads_only_published_dossier(tmp_path):
    controller, paths, dossier, _snapshot, _unrelated, _rekit = _published_fixture(tmp_path)
    server = FactoryServer(("127.0.0.1", 0), controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/api/runs/{paths.run_id}/dossiers"
    try:
        with urlopen(base) as response:
            assert json.load(response)["dossiers"][0]["id"] == dossier["id"]
        with urlopen(f"{base}/{dossier['id']}") as response:
            assert response.headers["Content-Security-Policy"].startswith("default-src 'none'")
            assert response.headers["X-Frame-Options"] == "DENY"
            assert b"Proof dossier" in response.read()
        with urlopen(f"{base}/{dossier['id']}/download") as response:
            assert response.headers["Content-Disposition"].startswith("attachment;")
            assert response.read(2) == b"PK"
        try:
            urlopen(f"{base}/not-published")
        except HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("unpublished dossier route unexpectedly opened")

        memory = _project_memory_log(paths).replay()
        reference = EvidenceRef(**memory.findings["f-fixture"]["references"][0])
        FindingMemory(_project_memory_log(paths)).transition(FindingTransition(
            finding_id="f-fixture", to_status="demonstrated",
            reason="new canonical review requires reproduction again", references=[reference],
        ))
        with urlopen(base) as response:
            stale = json.load(response)["dossiers"][0]
            assert stale["verified"] is False
            assert stale["verificationStatus"] == "stale-or-invalid"
        try:
            urlopen(f"{base}/{dossier['id']}")
        except HTTPError as exc:
            assert exc.code == 409
        else:
            raise AssertionError("stale dossier unexpectedly opened")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
