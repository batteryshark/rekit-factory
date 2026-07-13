from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from rekit_factory.proof_bundles import (
    AttemptSummary,
    CitedStatement,
    ContentHash,
    EnvironmentFact,
    IncludedArtifact,
    OpaqueCitation,
    OperatorDecision,
    Prerequisites,
    ProofAction,
    ProofManifest,
    ScopeBinding,
    SealedProofBundle,
    ToolVersion,
    WorkerIdentity,
    canonical_bundle_json,
    canonical_manifest_json,
    included_artifact_paths,
    render_html,
    render_markdown,
    seal_manifest,
    verify_bundle,
)


def _digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def _included(citation_id: str, data: bytes, *, purpose="evidence",
              export_policy="internal-only", content_state="safe") -> IncludedArtifact:
    digest = _digest(data)
    return IncludedArtifact(
        citation_id=citation_id, purpose=purpose,
        path=f"artifacts/sha256/{digest}", sha256=digest, size=len(data),
        media_type="application/octet-stream",
        material_class={
            "required-input": "operator-provided-fixture",
            "evidence": "derived-evidence",
            "reproduction-log": "redacted-log",
            "optional-visual-aid": "visual-aid",
        }[purpose], export_policy=export_policy,
        content_state=content_state,
    )


def _materialize(root: Path, artifacts: tuple[IncludedArtifact | OpaqueCitation, ...],
                 content: dict[str, bytes]) -> None:
    for artifact in artifacts:
        if isinstance(artifact, IncludedArtifact):
            path = root / artifact.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content[artifact.citation_id])


def _manifest(*, status="reproduced", verdict="validated", scope=None,
              artifacts=None, actions=None, observations=None) -> ProofManifest:
    input_data = b"fixture-input"
    observed_data = b"bounded observable\n"
    artifacts = artifacts or (
        _included("input-fixture", input_data, purpose="required-input"),
        _included("evidence-observed", observed_data),
        OpaqueCitation(
            citation_id="private-trace", purpose="evidence",
            factory_uri="factory://runs/run-proof/artifacts/artifact-private",
            export_policy="private", retention_state="private",
            reason="target-private evidence remains local",
        ),
    )
    actions = actions or (
        ProofAction(
            order=1, action="stage-input", description="Stage the required fixture",
            citation_ids=("input-fixture",),
        ),
        ProofAction(
            order=2, action="invoke", description="Run the bounded parser fixture",
            tool_id="fixture-runner", argv=("fixture-runner", "--input", "input-fixture"),
            environment=(EnvironmentFact(name="LC_ALL", value="C"),),
        ),
        ProofAction(
            order=3, action="observe", description="Record the allocation observation",
            citation_ids=("evidence-observed",),
        ),
    )
    observations = observations or (
        CitedStatement(
            text="The staged record caused the expected bounded observable.",
            citation_ids=("evidence-observed",),
        ),
    )
    return ProofManifest(
        manifest_id="proof-f-length-v1", created_at="2026-07-13T06:00:00Z",
        target_inputs=(
            ContentHash(id="target-primary", role="target", sha256="a" * 64),
            ContentHash(id="input-fixture", role="required-input", sha256=_digest(input_data)),
        ),
        scope=scope or ScopeBinding(
            scope_id="scope-proof", revision=3, digest="b" * 64,
            data_handling="local_only", export_mode="internal",
        ),
        run_id="run-proof", work_item_ids=("work-origin", "work-validator"),
        hypothesis_id="h-parser", finding_id="f-length", finding_state_sha256="c" * 64,
        finding_status=status, validation_verdict=verdict,
        workers=(WorkerIdentity(
            worker_id="worker-validator", session_id="session-validator",
            environment_id="clean:fixture", clean=True, model_profile="fixture-v1",
            facts=(EnvironmentFact(name="kernel", value="fixture-kernel"),),
        ),),
        prerequisites=Prerequisites(
            platform="linux", architecture="x86_64", isolation="clean-container",
            facts=(EnvironmentFact(name="filesystem", value="case-sensitive"),),
        ),
        tool_versions=(ToolVersion(
            tool_id="fixture-runner", version="1.4.2",
            capability_id="cap-parser-fixture", capability_version="2",
        ),),
        network_policy="none", artifacts=artifacts, actions=actions,
        expected_outcome=CitedStatement(
            text="The runner emits the bounded allocation observation.",
            citation_ids=("input-fixture",),
        ),
        observed_outcomes=(CitedStatement(
            text="The runner emitted the expected allocation observation.",
            citation_ids=("evidence-observed",),
        ),),
        observations=observations,
        interpretations=(CitedStatement(
            text="The recorded behavior is consistent with the finding claim.",
            citation_ids=("evidence-observed",),
        ),),
        limitations=(CitedStatement(
            text="The private trace is available only to authorized local reviewers.",
            citation_ids=("private-trace",),
        ),),
        contradictions=(CitedStatement(
            text="An earlier environment produced a negative result.",
            citation_ids=("private-trace",),
        ),),
        attempts=(AttemptSummary(
            attempt_id="repro-f-length-1", outcome="success",
            statement=CitedStatement(
                text="Clean validation matched the expected observation.",
                citation_ids=("evidence-observed",),
            ),
            environment_differences=("fresh filesystem state",),
        ),),
        operator_decisions=(OperatorDecision(
            decision_id="decision-accept", decision="accepted",
            rationale="The independent proof was reviewed.",
            citation_ids=("evidence-observed",),
        ),),
    )


def test_canonical_manifest_and_projections_are_byte_identical_for_unchanged_state(tmp_path):
    manifest = _manifest()
    bundle = seal_manifest(manifest)
    assert canonical_manifest_json(manifest) == canonical_manifest_json(
        ProofManifest.model_validate(manifest.model_dump(mode="json"))
    )
    assert canonical_bundle_json(bundle) == canonical_bundle_json(seal_manifest(manifest))
    assert render_markdown(manifest) == render_markdown(manifest)
    assert render_html(manifest) == render_html(manifest)
    assert "VALIDATED" in render_markdown(manifest)

    source = tmp_path / "proof.json"
    source.write_text(canonical_bundle_json(bundle), encoding="utf-8")
    content = {"input-fixture": b"fixture-input", "evidence-observed": b"bounded observable\n"}
    _materialize(tmp_path, manifest.artifacts, content)
    report = verify_bundle(
        source, expected_scope_digest="b" * 64, expected_scope_revision=3,
        expected_finding_state_sha256="c" * 64,
    )
    assert report.valid
    assert report.errors == ()


def test_artifact_mutation_manifest_tampering_and_stale_state_fail_closed(tmp_path):
    manifest = _manifest()
    bundle = seal_manifest(manifest)
    source = tmp_path / "proof.json"
    source.write_text(canonical_bundle_json(bundle), encoding="utf-8")
    content = {"input-fixture": b"fixture-input", "evidence-observed": b"bounded observable\n"}
    _materialize(tmp_path, manifest.artifacts, content)
    evidence = next(item for item in manifest.artifacts
                    if isinstance(item, IncludedArtifact) and item.citation_id == "evidence-observed")
    (tmp_path / evidence.path).write_bytes(b"tampered observable\n")
    report = verify_bundle(source)
    assert not report.valid
    assert any("size mismatch" in error or "hash mismatch" in error for error in report.errors)

    value = bundle.model_dump(mode="json")
    value["manifest"]["created_at"] = "2026-07-13T07:00:00Z"
    source.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    report = verify_bundle(source)
    assert not report.valid
    assert "manifest hash mismatch" in report.errors

    pristine = seal_manifest(manifest)
    report = verify_bundle(
        pristine, root=tmp_path, expected_finding_state_sha256="d" * 64,
    )
    assert not report.valid
    assert "finding state is stale" in report.errors


def test_noncanonical_json_and_symlink_escape_are_rejected(tmp_path):
    manifest = _manifest()
    bundle = seal_manifest(manifest)
    source = tmp_path / "proof.json"
    source.write_text(json.dumps(bundle.model_dump(mode="json"), indent=2) + "\n")
    content = {"input-fixture": b"fixture-input", "evidence-observed": b"bounded observable\n"}
    _materialize(tmp_path, manifest.artifacts, content)
    assert "bundle JSON is not in canonical byte representation" in verify_bundle(source).errors

    evidence = next(item for item in manifest.artifacts
                    if isinstance(item, IncludedArtifact) and item.citation_id == "evidence-observed")
    artifact_path = tmp_path / evidence.path
    artifact_path.unlink()
    outside = tmp_path.parent / "outside-proof-evidence"
    outside.write_bytes(b"bounded observable\n")
    artifact_path.symlink_to(outside)
    source.write_text(canonical_bundle_json(bundle))
    report = verify_bundle(source)
    assert not report.valid
    assert f"artifact path escapes or is missing: {evidence.path}" in report.errors
    outside.unlink()


@pytest.mark.parametrize("path", ["/tmp/artifact", "../artifact", "artifacts/../artifact"])
def test_absolute_and_traversing_artifact_paths_are_rejected_by_schema_and_verifier(
    tmp_path, path,
):
    digest = _digest(b"fixture")
    with pytest.raises(ValidationError, match="safe bundle-relative|content-addressed"):
        IncludedArtifact(
            citation_id="bad-path", purpose="evidence", path=path, sha256=digest,
            size=7, media_type="application/octet-stream",
            material_class="derived-evidence",
            export_policy="internal-only", content_state="safe",
        )
    value = seal_manifest(_manifest()).model_dump(mode="json")
    value["manifest"]["artifacts"][0]["path"] = path
    source = tmp_path / "invalid.json"
    source.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    report = verify_bundle(source)
    assert not report.valid
    assert report.errors[0].startswith("schema/read failure")


def test_export_scope_and_private_policy_are_fail_closed_and_opaque():
    with pytest.raises(ValidationError, match="approved_export"):
        ScopeBinding(
            scope_id="scope-local", revision=1, digest="a" * 64,
            data_handling="local_only", export_mode="export",
        )
    approved = ScopeBinding(
        scope_id="scope-export", revision=1, digest="e" * 64,
        data_handling="approved_export", export_mode="export",
    )
    with pytest.raises(ValidationError, match="explicitly exportable"):
        _manifest(scope=approved)

    exported_artifacts = tuple(
        item.model_copy(update={"export_policy": "exportable"})
        if isinstance(item, IncludedArtifact) else item
        for item in _manifest().artifacts
    )
    manifest = _manifest(scope=approved, artifacts=exported_artifacts)
    private = next(item for item in manifest.artifacts if isinstance(item, OpaqueCitation))
    assert private.factory_uri.startswith("factory://")
    assert not hasattr(private, "path")
    assert private.factory_uri not in included_artifact_paths(manifest)
    with pytest.raises(ValidationError):
        OpaqueCitation.model_validate({
            **private.model_dump(mode="json"), "path": "/private/target.bin",
        })


def test_required_input_hash_must_match_included_bytes_and_cannot_be_opaque():
    value = _manifest().model_dump(mode="json")
    value["target_inputs"][1]["sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="does not match"):
        ProofManifest.model_validate(value)

    value = _manifest().model_dump(mode="json")
    value["artifacts"][0] = OpaqueCitation(
        citation_id="input-fixture", purpose="required-input",
        factory_uri="factory://runs/run-proof/artifacts/private-required-input",
        export_policy="private", retention_state="private",
        reason="private required input",
    ).model_dump(mode="json")
    with pytest.raises(ValidationError, match="included content-addressed"):
        ProofManifest.model_validate(value)


@pytest.mark.parametrize(
    ("status", "verdict"),
    [
        ("candidate", "validated"), ("reproduction-pending", "validated"),
        ("inconclusive", "validated"), ("withdrawn", "validated"),
        ("denied", "validated"),
    ],
)
def test_unreproduced_and_terminal_negative_statuses_cannot_claim_validated(status, verdict):
    with pytest.raises(ValidationError):
        _manifest(status=status, verdict=verdict)


def test_validated_and_operator_accepted_statuses_require_current_proof_state():
    value = _manifest().model_dump(mode="json")
    value["attempts"] = []
    with pytest.raises(ValidationError, match="successful reproduction"):
        ProofManifest.model_validate(value)
    value = _manifest(status="operator-accepted").model_dump(mode="json")
    value["operator_decisions"] = []
    with pytest.raises(ValidationError, match="accepted operator decision"):
        ProofManifest.model_validate(value)


def test_denied_inconclusive_and_withdrawn_are_visible_without_a_valid_badge():
    denied = _manifest(status="denied", verdict="rejected")
    inconclusive = _manifest(status="inconclusive", verdict="inconclusive")
    withdrawn = _manifest(status="withdrawn", verdict="withdrawn")
    for manifest, label in (
        (denied, "REJECTED"), (inconclusive, "INCONCLUSIVE"), (withdrawn, "WITHDRAWN"),
    ):
        markdown = render_markdown(manifest)
        assert f"**Proof status:** {label}" in markdown
        assert "**Proof status:** VALIDATED" not in markdown


def test_schema_excludes_credentials_reasoning_noise_and_opaque_shell_transcripts():
    with pytest.raises(ValidationError, match="secret-bearing"):
        EnvironmentFact(name="API_KEY", value="opaque")
    log_data = b"redacted log"
    with pytest.raises(ValidationError, match="redacted projection"):
        _included("run-log", log_data, purpose="reproduction-log", content_state="safe")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ProofManifest.model_validate({
            **_manifest().model_dump(mode="json"),
            "provider_chain_of_thought": "hidden reasoning",
        })
    unrelated = _included(
        "unrelated-screenshot", b"decorative pixels", purpose="optional-visual-aid",
    )
    with pytest.raises(ValidationError, match="unrelated artifacts"):
        _manifest(artifacts=_manifest().artifacts + (unrelated,))
    with pytest.raises(ValidationError, match="structured argv"):
        ProofAction(
            order=1, action="invoke", description="opaque shell command",
            tool_id="fixture-runner", argv=(),
        )
    with pytest.raises(ValidationError, match="opaque shell transcript"):
        ProofAction(
            order=1, action="invoke", description="shell transcript",
            tool_id="shell", argv=("bash", "-c", "run fixture"),
        )
    with pytest.raises(ValidationError, match="credential-like"):
        ProofAction(
            order=1, action="invoke", description="unsafe argument",
            tool_id="fixture-runner", argv=("fixture-runner", "--api-key=secret-value"),
        )


def test_actions_must_be_ordered_and_tool_capabilities_versioned():
    actions = (
        ProofAction(order=2, action="stage-input", description="out of order",
                    citation_ids=("input-fixture",)),
        ProofAction(order=1, action="observe", description="out of order",
                    citation_ids=("evidence-observed",)),
    )
    with pytest.raises(ValidationError, match="ordered contiguously"):
        _manifest(actions=actions)
    with pytest.raises(ValidationError, match="supplied together"):
        ToolVersion(tool_id="fixture", version="1", capability_id="cap-only")


def test_html_projection_escapes_observation_content():
    manifest = _manifest(observations=(CitedStatement(
        text="Observed <script>alert('x')</script> as inert fixture text.",
        citation_ids=("evidence-observed",),
    ),))
    output = render_html(manifest)
    assert "<script>" not in output
    assert "&lt;script&gt;" in output
