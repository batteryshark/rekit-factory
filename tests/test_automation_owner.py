from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from muster import resolve_run_dir, stable_key, utcnow

from rekit_factory.automation import (
    AutomationGateway, AutomationPrincipal, AutomationTemplate, signature,
)
from rekit_factory.automation_owner import (
    ApprovedInvestigation, ApprovedInvestigationCatalog, InvestigationAutomationOwner,
    scope_reference,
)
from rekit_factory.control import InvestigationController, _project_memory_log
from rekit_factory.dossiers import DossierPublisher
from rekit_factory.evidence import EvidenceStore, Provenance
from rekit_factory.findings import (
    FindingMemory, FindingProposal, ObservationEvidence, ReproductionAttempt,
    ReproductionRecipe, ReproductionStep,
)
from rekit_factory.hypotheses import (
    DiscriminatingTestProposal, HypothesisMemory, HypothesisProposal,
)
from rekit_factory.memory import EvidenceRef
from rekit_factory.scope import (
    ActionAuthority, AuthorizedScope, ScopeApproval, ScopeEnvelope, TargetGrant,
)
from rekit_factory.store import FactoryLedger

from test_dossier_integration import FixtureRekit, QuietBackend


NOW = datetime(2026, 7, 14, 5, 0, tzinfo=timezone.utc)
SECRET = b"o" * 32


def setup_owner(tmp_path, *, submit=True, creation_fault=None):
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "fixture.bin"
    target.write_bytes(b"owned offline fixture")
    grant = TargetGrant.from_path(target)
    envelope = ScopeEnvelope(
        scope_id="automation-owned", revision=7,
        valid_from="2026-07-01T00:00:00Z", valid_until="2026-08-01T00:00:00Z",
        targets=(grant,), actions=(ActionAuthority.READ_LOCAL_TARGET,),
    )
    scope = AuthorizedScope(envelope, ScopeApproval(
        scope_id=envelope.scope_id, revision=envelope.revision,
        content_digest=envelope.content_digest, approved_by="operator:fixture",
        approved_at="2026-07-01T00:00:00Z", expires_at="2026-08-01T00:00:00Z",
        rationale="Exact owned offline automation fixture",
    ))
    template = AutomationTemplate(
        "owned-fixture", 1, grant.path_fingerprint, scope_reference(scope),
    )
    approved = ApprovedInvestigation(
        template, root, "fixture.bin", scope, "Inspect the approved offline fixture",
        worker_roles=("recon",), concurrency=1,
    )
    rekit = FixtureRekit()
    backend = QuietBackend()
    controller = InvestigationController(
        storage_root=tmp_path / "runs", rekit=rekit, workers=backend,
        creation_fault_injector=creation_fault,
    )
    submitter = (lambda path: asyncio.run(controller.drive(path))) if submit else None
    owner = InvestigationAutomationOwner(
        controller, ApprovedInvestigationCatalog({template.template_id: approved}),
        submit=submitter,
    )
    gateway = AutomationGateway(
        tmp_path / "automation.db", owner,
        templates={template.template_id: template},
        principals={"scheduler": AutomationPrincipal("scheduler", SECRET)},
        clock=lambda: NOW,
    )
    return gateway, owner, controller, backend, template, scope, target


def auth(method, path, payload, nonce, key=""):
    timestamp = int(NOW.timestamp())
    return {"X-Factory-Client": "scheduler", "X-Factory-Timestamp": str(timestamp),
            "X-Factory-Nonce": nonce, "Idempotency-Key": key,
            "X-Factory-Signature": signature(
                SECRET, timestamp=timestamp, nonce=nonce, method=method, path=path,
                idempotency_key=key, payload=payload,
            )}


def launch(gateway, nonce="launch-1"):
    path = "/api/automation/v1/launch"
    body = {"templateId": "owned-fixture", "schedule": {
        "scheduleId": "offline-proof", "scheduledFor": "2026-07-14T05:00:00Z",
    }}
    return gateway.handle("POST", path, auth("POST", path, body, nonce, "launch-key"), body)


def publish_owned_dossier(controller, run_dir, target):
    paths = resolve_run_dir(run_dir)
    target_hash = TargetGrant.from_path(target).content_sha256
    evidence = EvidenceStore(run_dir / "evidence")

    def capture(data, kind):
        outcome = evidence.capture(data, Provenance(
            run_id=paths.run_id, source="automation-fixture",
            capture_reason="owned automation proof", captured_at=utcnow(),
            environment_id="clean:automation", target_sha256=target_hash,
            worker_id="validator", work_item_id="work-validation",
        ), kind=kind, media_type="text/plain; charset=utf-8")
        return EvidenceRef("artifact", outcome.record.artifact_id)

    staged = capture(b"hello\n", "operator-fixture")
    observed = capture(b"OBSERVED:hello\n", "reproduction-output")
    log = _project_memory_log(paths)
    HypothesisMemory(log).propose(HypothesisProposal(
        id="h-automation", claim="The owned reader emits the staged value", scope="target",
        expected_observation="Output includes the staged value",
        falsifier="Output omits the staged value", confidence=.8, references=[observed],
        proposed_test=DiscriminatingTestProposal(
            id="test-automation", objective="Read the fixture", method="clean replay",
            expected_observation="OBSERVED:hello", falsifying_observation="missing value",
            information_gain=90, risk=0, cost_units=1,
        ),
    ))
    proposal = FindingProposal(
        id="f-automation", hypothesis_id="h-automation", scope="target",
        observations=[ObservationEvidence(
            observation="The clean reader emitted OBSERVED:hello", references=[observed],
        )],
        affected_component="owned fixture reader", impact_claim="The input is reproducible",
        assumptions=["UTF-8 input"], known_uncertainty="Only the fixture is covered",
        finding_type="informational", consequence="low", confidence=.9,
        references=[staged, observed],
        recipe=ReproductionRecipe(
            id="recipe-automation-v1", staged_inputs=[staged],
            steps=[ReproductionStep(
                action="invoke", description="Read the staged input",
                tool_id="fixture-reader", argv=["fixture-reader", "--input", "{staged:0}"],
                references=[staged],
            )], expected_observation="OBSERVED:hello",
            clean_environment_requirements=["empty temporary directory"],
        ),
    )
    findings = FindingMemory(log)
    findings.propose(
        proposal, origin_worker_id="origin-worker", origin_session_id="session:origin",
        origin_model_profile="fixture",
    )
    findings.mark_validation_pending("f-automation")
    findings.record_attempt(ReproductionAttempt(
        id="attempt-automation", finding_id="f-automation",
        recipe_id="recipe-automation-v1", outcome="success",
        worker_id="validator-worker", session_id="session:validator",
        environment_id="clean:automation", clean_environment=True,
        model_profile="fixture", platform="test-os", architecture="test-arch",
        isolation="fresh-process", observations=["OBSERVED:hello"], references=[observed],
    ))
    with FactoryLedger(paths.db_path) as ledger:
        ledger.enqueue(
            run_id=paths.run_id, key=stable_key("validation", "f-automation"),
            target=str(target), operation="model-worker", category="finding-validation",
            title="Validate automation fixture", priority=200,
            payload={"findingId": "f-automation", "workerId": "validator-worker"},
        )
        manifest = controller.rekit.manifest("fixture-reader")
        tool_work = ledger.enqueue(
            run_id=paths.run_id, key=stable_key("tool-validation", "f-automation"),
            target=str(target), operation="model-rekit-tool", category="tool",
            title="Execute fixture reader", priority=210,
            payload={"findingId": "f-automation", "workerItemId": "work-validation",
                     "toolId": "fixture-reader",
                     "manifestDigest": manifest.effective_manifest_digest,
                     "authorityVersion": manifest.authority_version},
        )
        call_id = ledger.start_tool_call(
            paths.run_id, tool_work, "fixture-reader", 0,
            manifest_digest=manifest.effective_manifest_digest,
            declared_actions=(ActionAuthority.READ_LOCAL_TARGET.value,),
            credential_use=False,
        )
        ledger.finish_tool_call(call_id, status="done", output_path=str(target), exit_code=0)
        ledger.add_artifact(
            run_id=paths.run_id, kind="tool-output", path=target,
            logical_path="tool-output/fixture-reader.txt", origin="rekit:fixture-reader",
            metadata={"toolId": "fixture-reader",
                      "effectiveManifestDigest": manifest.effective_manifest_digest,
                      "verifiedManifestDigest": manifest.effective_manifest_digest,
                      "provenance": {"work_item_id": tool_work,
                                     "invocation_id": call_id}},
        )
        ledger.set_work_status(tool_work, "done", result={"observation": "OBSERVED:hello"})
        return DossierPublisher(paths, ledger, controller.rekit).publish("f-automation")


def test_concrete_owner_launches_terminal_fixture_and_restart_reuses_exact_run(tmp_path):
    gateway, owner, controller, _backend, template, _scope, _target = setup_owner(tmp_path)
    status, first = launch(gateway)
    assert status == 202 and first["status"] == "completed" and first["terminal"] is True
    assert first["projectId"] and "campaignId" not in first
    run_id = first["runId"]
    run_dirs = list(controller.storage_root.glob("projects/*/runs/*/run.json"))
    assert len(run_dirs) == 1
    paths = resolve_run_dir(run_dirs[0].parent)
    with FactoryLedger(paths.db_path) as ledger:
        assert ledger.conn.execute("select count(*) from runs").fetchone()[0] == 1
        assert ledger.conn.execute(
            "select count(*) from factory_events where kind='automation.launched'"
        ).fetchone()[0] == 1
        model_calls = ledger.conn.execute(
            "select count(*) from factory_model_calls"
        ).fetchone()[0]
    gateway.close()

    restarted = AutomationGateway(
        tmp_path / "automation.db", owner, templates={template.template_id: template},
        principals={"scheduler": AutomationPrincipal("scheduler", SECRET)},
        clock=lambda: NOW,
    )
    retry = launch(restarted, "launch-after-restart")[1]
    assert retry["runId"] == run_id and retry == first
    assert len(list(controller.storage_root.glob("projects/*/runs/*/run.json"))) == 1
    with FactoryLedger(paths.db_path) as ledger:
        assert ledger.conn.execute(
            "select count(*) from factory_model_calls"
        ).fetchone()[0] == model_calls


def test_cancellation_and_handoff_ack_are_canonical_and_never_answer_gate(tmp_path):
    gateway, owner, controller, backend, _template, _scope, _target = setup_owner(
        tmp_path, submit=False,
    )
    run_id = launch(gateway)[1]["runId"]
    run_dir = owner._run_dir(run_id)
    paths = resolve_run_dir(run_dir)
    with FactoryLedger(paths.db_path) as ledger:
        ledger.ask_question(
            run_id, qid="human-only", node="Operator", kind="direction",
            prompt="Human direction required", options=[],
        )
    cancel_path = f"/api/automation/v1/runs/{run_id}/cancel"
    cancel_body = {"reasonCode": "schedule-superseded"}
    assert gateway.handle("POST", cancel_path, auth(
        "POST", cancel_path, cancel_body, "cancel-1", "cancel-key",
    ), cancel_body)[0] == 202
    snapshot = asyncio.run(controller.drive(run_dir))
    assert snapshot["run"]["status"] == "canceled"
    assert backend.profile.name == "fixture"  # cancellation happened before worker dispatch
    with FactoryLedger(paths.db_path) as ledger:
        assert ledger.conn.execute("select count(*) from answers").fetchone()[0] == 0
        assert ledger.conn.execute(
            "select status from factory_run_cancellations"
        ).fetchone()[0] == "applied"

    state = owner.status(run_id)
    ack_path = f"/api/automation/v1/runs/{run_id}/acknowledge-handoff"
    ack_body = {"handoffId": state["handoffId"]}
    first = gateway.handle("POST", ack_path, auth(
        "POST", ack_path, ack_body, "ack-1", "ack-key",
    ), ack_body)
    retry = gateway.handle("POST", ack_path, auth(
        "POST", ack_path, ack_body, "ack-2", "ack-key",
    ), ack_body)
    assert first == retry and first[1]["acknowledged"] is True
    with FactoryLedger(paths.db_path) as ledger:
        assert ledger.conn.execute("select count(*) from answers").fetchone()[0] == 0
        assert ledger.conn.execute(
            "select count(*) from factory_run_handoff_acknowledgements"
        ).fetchone()[0] == 1


def test_terminal_cursor_projects_verified_owned_dossier_and_fetch_reverifies(tmp_path):
    gateway, owner, controller, _backend, _template, _scope, target = setup_owner(tmp_path)
    first = launch(gateway)[1]
    run_id = first["runId"]
    dossier = publish_owned_dossier(controller, owner._run_dir(run_id), target)

    feed_path = "/api/automation/v1/events?after=0&limit=100"
    status, feed = gateway.handle("GET", feed_path, auth(
        "GET", feed_path, {}, "proof-feed",
    ), {})
    assert status == 200
    kinds = [item["kind"] for item in feed["events"]]
    assert "run.terminal" in kinds and "proof.available" in kinds
    proof = next(item for item in feed["events"] if item["kind"] == "proof.available")
    assert proof["payload"]["verified"] is True
    assert "target" not in str(feed).lower() and str(target) not in str(feed)

    dossier_path = f"/api/automation/v1/runs/{run_id}/dossiers/{dossier['id']}"
    dossier_status, fetched = gateway.handle("GET", dossier_path, auth(
        "GET", dossier_path, {}, "proof-fetch",
    ), {})
    assert dossier_status == 200 and fetched["verified"] is True
    assert fetched["manifestSha256"] == dossier["manifestSha256"]
    cursor = feed["nextCursor"]
    resume_path = f"/api/automation/v1/events?after={cursor}&limit=100"
    assert gateway.handle("GET", resume_path, auth(
        "GET", resume_path, {}, "proof-resume",
    ), {})[1]["events"] == []


def test_catalog_rechecks_target_bytes_and_stale_entry_fails_closed(tmp_path):
    gateway, _owner, _controller, _backend, _template, _scope, target = setup_owner(tmp_path)
    target.write_bytes(b"changed after operator approval")
    status, response = launch(gateway)
    assert status == 503 and response["error"] == "canonical-owner-unavailable"
    assert gateway.conn.execute(
        "select count(*) from factory_automation_runs"
    ).fetchone()[0] == 0
    assert "owner-unavailable" in {row[0] for row in gateway.conn.execute(
        "select outcome from factory_automation_audit"
    )}


@pytest.mark.parametrize("boundary", ("run-authority", "creation-complete"))
def test_deterministic_owner_creation_recovers_interruption_without_duplicate_run(
        tmp_path, boundary):
    class Once:
        fired = False

        def __call__(self, value):
            if value == boundary and not self.fired:
                self.fired = True
                raise RuntimeError(value)

    gateway, owner, controller, backend, template, _scope, _target = setup_owner(
        tmp_path, creation_fault=Once(),
    )
    assert launch(gateway)[0] == 503
    assert len(list(controller.storage_root.glob("projects/*/runs/*/run.json"))) == 1
    gateway.close()

    restarted_controller = InvestigationController(
        storage_root=controller.storage_root, rekit=controller.rekit, workers=backend,
    )
    restarted_owner = InvestigationAutomationOwner(
        restarted_controller, owner.catalog,
        submit=lambda path: asyncio.run(restarted_controller.drive(path)),
    )
    restarted = AutomationGateway(
        tmp_path / "automation.db", restarted_owner,
        templates={template.template_id: template},
        principals={"scheduler": AutomationPrincipal("scheduler", SECRET)},
        clock=lambda: NOW,
    )
    status, result = launch(restarted, "restart-recovery")
    assert status == 202 and result["status"] == "completed"
    assert len(list(controller.storage_root.glob("projects/*/runs/*/run.json"))) == 1
