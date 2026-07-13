"""Canonical, offline-verifiable proof manifests and deterministic dossiers.

This module intentionally does not execute a proof or maintain a report index. A bundle is
one sealed manifest plus content-addressed files. Markdown and HTML are pure projections.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from html import escape
import json
from pathlib import Path, PurePosixPath
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MEDIA_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_FACTORY_URI = re.compile(r"^factory://runs/[A-Za-z0-9._:-]+/(?:artifacts|events)/[A-Za-z0-9._:-]+$")
_SENSITIVE_NAME = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|password|passwd|secret|token|credential|cookie|authorization)(?:$|[_-])"
)
_SENSITIVE_VALUE = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]\s*['\"]?[^\s'\"]{8,}"
    ),
)
_FORBIDDEN_MATERIAL = re.compile(
    r"(?i)(?:chain[-_ ]of[-_ ]thought|provider[-_ ]reasoning|model[-_ ]thinking|"
    r"credentials?|secrets?|unredacted|raw[-_ ]log|noisy[-_ ]log)"
)


def _safe_text(value: str, name: str, *, max_length: int = 8_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")
    if any(pattern.search(value) for pattern in _SENSITIVE_VALUE):
        raise ValueError(f"{name} contains credential-like material")
    return value


class ProofModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContentHash(ProofModel):
    id: str
    role: Literal["target", "required-input"]
    sha256: str

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("content hash id must be stable")
        return value

    @field_validator("sha256")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return value


class ScopeBinding(ProofModel):
    scope_id: str
    revision: int = Field(ge=1)
    digest: str
    data_handling: Literal["local_only", "approved_export"]
    export_mode: Literal["internal", "export"] = "internal"

    @field_validator("scope_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("scope_id must be stable")
        return value

    @field_validator("digest")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("scope digest must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def export_is_authorized(self):
        if self.export_mode == "export" and self.data_handling != "approved_export":
            raise ValueError("export bundle requires an approved_export scope")
        return self


class EnvironmentFact(ProofModel):
    name: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=1_000)
    source: Literal["observed", "declared"] = "observed"

    @model_validator(mode="after")
    def contains_no_secret_material(self):
        if _SENSITIVE_NAME.search(self.name):
            raise ValueError("secret-bearing environment facts are prohibited")
        _safe_text(self.value, "environment fact", max_length=1_000)
        return self


class WorkerIdentity(ProofModel):
    worker_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    environment_id: str = Field(min_length=1, max_length=256)
    clean: bool
    model_profile: str = Field(min_length=1, max_length=128)
    facts: tuple[EnvironmentFact, ...] = ()


class ToolVersion(ProofModel):
    tool_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    capability_id: str | None = Field(default=None, max_length=128)
    capability_version: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def capability_is_versioned(self):
        if (self.capability_id is None) != (self.capability_version is None):
            raise ValueError("capability id and version must be supplied together")
        return self


class Prerequisites(ProofModel):
    platform: str = Field(min_length=1, max_length=128)
    architecture: str = Field(min_length=1, max_length=128)
    isolation: str = Field(min_length=1, max_length=128)
    facts: tuple[EnvironmentFact, ...] = ()


ArtifactPurpose = Literal[
    "required-input", "evidence", "reproduction-log", "optional-visual-aid",
]


class IncludedArtifact(ProofModel):
    kind: Literal["included"] = "included"
    citation_id: str
    purpose: ArtifactPurpose
    path: str
    sha256: str
    size: int = Field(ge=0)
    media_type: str
    material_class: Literal[
        "operator-provided-fixture", "derived-evidence", "redacted-log", "visual-aid",
    ]
    export_policy: Literal["internal-only", "exportable"]
    content_state: Literal["safe", "redacted"]
    retention_class: Literal["run", "project", "pinned"] = "run"
    retention_state: Literal["retained", "held"] = "retained"

    @field_validator("citation_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("citation_id must be stable")
        return value

    @field_validator("sha256")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("artifact sha256 must be a lowercase SHA-256 digest")
        return value

    @field_validator("media_type")
    @classmethod
    def valid_media_type(cls, value: str) -> str:
        if not _MEDIA_TYPE.fullmatch(value):
            raise ValueError("artifact media_type must be a concrete MIME type")
        return value

    @model_validator(mode="after")
    def safe_content_addressed_path(self):
        path = PurePosixPath(self.path)
        if path.is_absolute() or not path.parts or ".." in path.parts or "." in path.parts:
            raise ValueError("artifact path must be a safe bundle-relative path")
        expected = PurePosixPath("artifacts", "sha256", self.sha256)
        if self.path != expected.as_posix():
            raise ValueError("artifact path must be content-addressed as artifacts/sha256/<digest>")
        if _FORBIDDEN_MATERIAL.search(self.path):
            raise ValueError("prohibited material cannot be included in a proof bundle")
        if self.purpose == "reproduction-log" and self.content_state != "redacted":
            raise ValueError("reproduction logs must use a redacted projection")
        material_for_purpose = {
            "required-input": "operator-provided-fixture",
            "evidence": "derived-evidence",
            "reproduction-log": "redacted-log",
            "optional-visual-aid": "visual-aid",
        }
        if self.material_class != material_for_purpose[self.purpose]:
            raise ValueError("artifact purpose and safe material class do not match")
        return self


class OpaqueCitation(ProofModel):
    kind: Literal["opaque"] = "opaque"
    citation_id: str
    purpose: ArtifactPurpose
    factory_uri: str
    export_policy: Literal["private", "non-exportable"]
    retention_class: Literal["run", "project", "pinned", "unknown"] = "unknown"
    retention_state: Literal[
        "private", "non-exportable", "withheld", "quarantined", "expired", "deleted",
    ]
    reason: str = Field(min_length=1, max_length=1_000)

    @field_validator("citation_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("citation_id must be stable")
        return value

    @field_validator("factory_uri")
    @classmethod
    def opaque_portable_uri(cls, value: str) -> str:
        if not _FACTORY_URI.fullmatch(value):
            raise ValueError("opaque citations require a portable factory:// run URI")
        return value

    @field_validator("reason")
    @classmethod
    def safe_reason(cls, value: str) -> str:
        return _safe_text(value, "opaque citation reason", max_length=1_000)


ArtifactReference = Annotated[
    IncludedArtifact | OpaqueCitation,
    Field(discriminator="kind"),
]


class ProofAction(ProofModel):
    order: int = Field(ge=1)
    action: Literal["stage-input", "invoke", "observe", "compare"]
    description: str = Field(min_length=1, max_length=2_000)
    tool_id: str | None = Field(default=None, max_length=128)
    argv: tuple[str, ...] = ()
    environment: tuple[EnvironmentFact, ...] = ()
    citation_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def invocation_is_structured(self):
        _safe_text(self.description, "action description", max_length=2_000)
        if self.action == "invoke":
            if not self.tool_id or not self.argv:
                raise ValueError("invoke actions require tool_id and structured argv")
        elif self.tool_id is not None or self.argv:
            raise ValueError("only invoke actions may contain tool_id or argv")
        if any(not isinstance(argument, str) or not argument for argument in self.argv):
            raise ValueError("argv must contain non-empty strings")
        for argument in self.argv:
            if "\x00" in argument or "\n" in argument or "\r" in argument:
                raise ValueError("argv entries cannot contain control-separated shell text")
            _safe_text(argument, "argv entry", max_length=4_000)
        if self.argv and PurePosixPath(self.argv[0]).name.lower() in {
            "sh", "bash", "zsh", "fish", "cmd", "cmd.exe", "powershell", "pwsh",
        }:
            raise ValueError("proof actions cannot encode an opaque shell transcript")
        return self


class CitedStatement(ProofModel):
    text: str = Field(min_length=1, max_length=8_000)
    citation_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def safe_statement(cls, value: str) -> str:
        return _safe_text(value, "statement")


class AttemptSummary(ProofModel):
    attempt_id: str = Field(min_length=1, max_length=128)
    outcome: Literal["success", "negative", "flaky", "contradictory", "inconclusive"]
    statement: CitedStatement
    environment_differences: tuple[str, ...] = ()

    @field_validator("environment_differences")
    @classmethod
    def safe_differences(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_safe_text(value, "environment difference", max_length=1_000)
                     for value in values)


class OperatorDecision(ProofModel):
    decision_id: str = Field(min_length=1, max_length=128)
    decision: Literal["accepted", "rejected", "waived"]
    rationale: str = Field(min_length=1, max_length=4_000)
    unmet_criteria: tuple[str, ...] = ()
    citation_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def safe_decision(self):
        _safe_text(self.rationale, "operator rationale", max_length=4_000)
        if self.decision == "waived" and not self.unmet_criteria:
            raise ValueError("waiver must preserve unmet criteria")
        for item in self.unmet_criteria:
            _safe_text(item, "unmet criterion", max_length=1_000)
        return self


FindingStatus = Literal[
    "lead", "candidate", "demonstrated", "reproduction-pending", "reproduced",
    "operator-accepted", "rejected", "denied", "withdrawn", "inconclusive",
]
ValidationVerdict = Literal["validated", "unvalidated", "rejected", "withdrawn", "inconclusive"]


class ProofManifest(ProofModel):
    schema_version: Literal[1] = 1
    manifest_id: str
    created_at: str = Field(min_length=1, max_length=64)
    target_inputs: tuple[ContentHash, ...] = Field(min_length=1)
    scope: ScopeBinding
    run_id: str
    work_item_ids: tuple[str, ...] = Field(min_length=1)
    hypothesis_id: str
    finding_id: str
    finding_state_sha256: str
    finding_status: FindingStatus
    validation_verdict: ValidationVerdict
    workers: tuple[WorkerIdentity, ...] = Field(min_length=1)
    prerequisites: Prerequisites
    tool_versions: tuple[ToolVersion, ...]
    network_policy: Literal["none", "sinkhole", "restricted", "unrestricted"]
    endpoint_refs: tuple[str, ...] = ()
    artifacts: tuple[ArtifactReference, ...] = ()
    actions: tuple[ProofAction, ...] = Field(min_length=1)
    expected_outcome: CitedStatement
    observed_outcomes: tuple[CitedStatement, ...] = Field(min_length=1)
    observations: tuple[CitedStatement, ...] = ()
    interpretations: tuple[CitedStatement, ...] = ()
    limitations: tuple[CitedStatement, ...] = ()
    contradictions: tuple[CitedStatement, ...] = ()
    attempts: tuple[AttemptSummary, ...] = ()
    operator_decisions: tuple[OperatorDecision, ...] = ()

    @field_validator(
        "manifest_id", "run_id", "hypothesis_id", "finding_id",
    )
    @classmethod
    def stable_ids(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("manifest identifiers must be stable")
        return value

    @field_validator("created_at")
    @classmethod
    def timestamp_is_timezone_bound(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("created_at must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        return value

    @field_validator("work_item_ids")
    @classmethod
    def stable_work_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _ID.fullmatch(value) for value in values) or len(set(values)) != len(values):
            raise ValueError("work_item_ids must be unique stable identifiers")
        return values

    @field_validator("finding_state_sha256")
    @classmethod
    def state_digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("finding_state_sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def coherent_proof(self):
        if not any(item.role == "target" for item in self.target_inputs):
            raise ValueError("manifest must bind at least one target hash")
        if len({item.id for item in self.target_inputs}) != len(self.target_inputs):
            raise ValueError("target/input hash ids must be unique")
        if [action.order for action in self.actions] != list(range(1, len(self.actions) + 1)):
            raise ValueError("proof actions must be ordered contiguously from 1")
        if len({worker.worker_id for worker in self.workers}) != len(self.workers):
            raise ValueError("worker identities must be unique")
        tool_ids = {tool.tool_id for tool in self.tool_versions}
        if len(tool_ids) != len(self.tool_versions):
            raise ValueError("tool versions must be unique")
        if any(action.tool_id not in tool_ids for action in self.actions if action.tool_id):
            raise ValueError("every invoked tool must have a pinned tool version")
        if self.network_policy == "none" and self.endpoint_refs:
            raise ValueError("network-none proof cannot cite endpoints")
        if self.network_policy != "none" and any(
            not value.startswith("endpoint:") for value in self.endpoint_refs
        ):
            raise ValueError("network endpoints must remain opaque endpoint: references")
        citations = [artifact.citation_id for artifact in self.artifacts]
        if len(set(citations)) != len(citations):
            raise ValueError("artifact citation ids must be unique")
        cited = set(citations)
        referenced = set(self.expected_outcome.citation_ids)
        for group in (
            self.observed_outcomes, self.observations, self.interpretations,
            self.limitations, self.contradictions,
        ):
            for statement in group:
                referenced.update(statement.citation_ids)
        for action in self.actions:
            referenced.update(action.citation_ids)
        for attempt in self.attempts:
            referenced.update(attempt.statement.citation_ids)
        for decision in self.operator_decisions:
            referenced.update(decision.citation_ids)
        missing = referenced - cited
        if missing:
            raise ValueError(f"manifest references unknown citations: {sorted(missing)!r}")
        unused = cited - referenced
        if unused:
            raise ValueError(
                f"unrelated artifacts cannot enter a proof bundle: {sorted(unused)!r}"
            )
        required_hashes = {item.id for item in self.target_inputs if item.role == "required-input"}
        required_artifacts = {
            artifact.citation_id: artifact for artifact in self.artifacts
            if isinstance(artifact, IncludedArtifact)
            and artifact.purpose == "required-input"
        }
        if required_hashes != set(required_artifacts):
            raise ValueError("required inputs must be included content-addressed artifacts")
        hash_by_id = {item.id: item.sha256 for item in self.target_inputs}
        if any(hash_by_id[identifier] != artifact.sha256
               for identifier, artifact in required_artifacts.items()):
            raise ValueError("required input manifest hash does not match its artifact hash")
        if self.scope.export_mode == "export" and any(
            isinstance(artifact, IncludedArtifact)
            and artifact.export_policy != "exportable"
            for artifact in self.artifacts
        ):
            raise ValueError("export bundles may include only explicitly exportable bytes")
        validated_statuses = {"reproduced", "operator-accepted"}
        if self.validation_verdict == "validated" and self.finding_status not in validated_statuses:
            raise ValueError("only reproduced findings may carry a validated verdict")
        if self.validation_verdict == "validated" and not any(
            attempt.outcome == "success" for attempt in self.attempts
        ):
            raise ValueError("validated proof requires a recorded successful reproduction")
        if self.validation_verdict == "validated" and not any(
            worker.clean for worker in self.workers
        ):
            raise ValueError("validated proof requires a clean validation environment")
        if self.finding_status == "operator-accepted" and not any(
            decision.decision == "accepted" for decision in self.operator_decisions
        ):
            raise ValueError("operator-accepted status requires an accepted operator decision")
        if self.finding_status in {"rejected", "denied"} \
                and self.validation_verdict != "rejected":
            raise ValueError("rejected or denied findings require a rejected verdict")
        if self.finding_status == "withdrawn" and self.validation_verdict != "withdrawn":
            raise ValueError("withdrawn findings require a withdrawn verdict")
        if self.finding_status == "inconclusive" \
                and self.validation_verdict != "inconclusive":
            raise ValueError("inconclusive findings require an inconclusive verdict")
        if self.finding_status not in validated_statuses | {
            "rejected", "denied", "withdrawn", "inconclusive",
        } and self.validation_verdict != "unvalidated":
            raise ValueError("unreproduced findings require an unvalidated verdict")
        return self


class SealedProofBundle(ProofModel):
    schema_version: Literal[1] = 1
    manifest_sha256: str
    manifest: ProofManifest

    @field_validator("manifest_sha256")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("manifest_sha256 must be a lowercase SHA-256 digest")
        return value


class VerificationReport(ProofModel):
    valid: bool
    manifest_sha256: str | None = None
    errors: tuple[str, ...] = ()


def canonical_manifest_json(manifest: ProofManifest) -> str:
    return json.dumps(
        manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ) + "\n"


def seal_manifest(manifest: ProofManifest) -> SealedProofBundle:
    digest = sha256(canonical_manifest_json(manifest).encode("utf-8")).hexdigest()
    return SealedProofBundle(manifest_sha256=digest, manifest=manifest)


def canonical_bundle_json(bundle: SealedProofBundle) -> str:
    return json.dumps(
        bundle.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ) + "\n"


def included_artifact_paths(manifest: ProofManifest) -> tuple[str, ...]:
    """The only bytes eligible for copying; opaque citations never become paths."""
    return tuple(
        artifact.path for artifact in manifest.artifacts
        if isinstance(artifact, IncludedArtifact)
    )


def verify_bundle(bundle_or_path: SealedProofBundle | str | Path, *, root: str | Path | None = None,
                  expected_scope_digest: str | None = None,
                  expected_scope_revision: int | None = None,
                  expected_finding_state_sha256: str | None = None) -> VerificationReport:
    errors: list[str] = []
    raw: str | None = None
    try:
        if isinstance(bundle_or_path, SealedProofBundle):
            bundle = bundle_or_path
        else:
            source = Path(bundle_or_path)
            raw = source.read_text(encoding="utf-8")
            bundle = SealedProofBundle.model_validate_json(raw)
            if root is None:
                root = source.parent
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        return VerificationReport(valid=False, errors=(f"schema/read failure: {exc}",))
    canonical = canonical_bundle_json(bundle)
    if raw is not None and raw != canonical:
        errors.append("bundle JSON is not in canonical byte representation")
    actual_manifest_sha = sha256(
        canonical_manifest_json(bundle.manifest).encode("utf-8")
    ).hexdigest()
    if actual_manifest_sha != bundle.manifest_sha256:
        errors.append("manifest hash mismatch")
    if expected_scope_digest is not None and bundle.manifest.scope.digest != expected_scope_digest:
        errors.append("scope digest is stale or does not match the authorized scope")
    if expected_scope_revision is not None \
            and bundle.manifest.scope.revision != expected_scope_revision:
        errors.append("scope revision is stale or does not match the authorized scope")
    if expected_finding_state_sha256 is not None \
            and bundle.manifest.finding_state_sha256 != expected_finding_state_sha256:
        errors.append("finding state is stale")
    root_path = Path(root).resolve() if root is not None else None
    included = [artifact for artifact in bundle.manifest.artifacts
                if isinstance(artifact, IncludedArtifact)]
    if included and root_path is None:
        errors.append("artifact root is required to verify included evidence")
    for artifact in included:
        if root_path is None:
            break
        candidate = root_path.joinpath(*PurePosixPath(artifact.path).parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root_path)
        except (OSError, ValueError):
            errors.append(f"artifact path escapes or is missing: {artifact.path}")
            continue
        if not resolved.is_file():
            errors.append(f"artifact is not a regular file: {artifact.path}")
            continue
        data = resolved.read_bytes()
        if len(data) != artifact.size:
            errors.append(f"artifact size mismatch: {artifact.path}")
        if sha256(data).hexdigest() != artifact.sha256:
            errors.append(f"artifact hash mismatch: {artifact.path}")
    return VerificationReport(
        valid=not errors, manifest_sha256=bundle.manifest_sha256,
        errors=tuple(errors),
    )


def render_markdown(manifest: ProofManifest) -> str:
    status = (
        "VALIDATED" if manifest.validation_verdict == "validated"
        else manifest.validation_verdict.upper()
    )
    lines = [
        f"# Proof dossier: {manifest.finding_id}", "",
        f"**Proof status:** {status}",
        f"**Finding lifecycle:** {manifest.finding_status}",
        f"**Manifest:** `{manifest.manifest_id}` (schema v{manifest.schema_version})", "",
        "## Scope and provenance", "",
        f"- Run: `{manifest.run_id}`",
        f"- Scope: `{manifest.scope.scope_id}` revision {manifest.scope.revision}",
        f"- Data handling: `{manifest.scope.data_handling}` / `{manifest.scope.export_mode}`",
        f"- Hypothesis: `{manifest.hypothesis_id}`",
        f"- Finding state: `{manifest.finding_state_sha256}`", "",
        "## Prerequisites", "",
        f"- Platform: {manifest.prerequisites.platform}",
        f"- Architecture: {manifest.prerequisites.architecture}",
        f"- Isolation: {manifest.prerequisites.isolation}", "",
        "## Artifacts", "",
    ]
    for artifact in manifest.artifacts:
        if isinstance(artifact, IncludedArtifact):
            lines.append(
                f"- `{artifact.citation_id}` — {artifact.purpose}; `{artifact.path}`; "
                f"{artifact.size} bytes; `{artifact.media_type}`; {artifact.export_policy}; "
                f"retention={artifact.retention_class}/{artifact.retention_state}"
            )
        else:
            lines.append(
                f"- `{artifact.citation_id}` — {artifact.purpose}; opaque "
                f"`{artifact.factory_uri}`; {artifact.export_policy}; "
                f"retention={artifact.retention_class}/{artifact.retention_state}: {artifact.reason}"
            )
    if not manifest.artifacts:
        lines.append("- _None._")
    lines += ["", "## Ordered reproduction actions", ""]
    for action in manifest.actions:
        command = f" argv={json.dumps(action.argv)}" if action.argv else ""
        lines.append(f"{action.order}. **{action.action}** — {action.description}{command}")
    lines += ["", "## Expected outcome", "",
              f"{manifest.expected_outcome.text} {_citation_markdown(manifest.expected_outcome)}",
              "", "## Observed outcomes", ""]
    for statement in manifest.observed_outcomes:
        lines.append(f"- {statement.text} {_citation_markdown(statement)}")
    for title, values in (
        ("Observations", manifest.observations),
        ("Interpretations", manifest.interpretations),
        ("Limitations", manifest.limitations),
        ("Contradictions", manifest.contradictions),
    ):
        lines += ["", f"## {title}", ""]
        lines += ([f"- {item.text} {_citation_markdown(item)}" for item in values]
                  or ["- _None._"])
    lines += ["", "## Reproduction attempts", ""]
    lines += ([
        f"- `{attempt.attempt_id}` — **{attempt.outcome}**: "
        f"{attempt.statement.text} {_citation_markdown(attempt.statement)}"
        for attempt in manifest.attempts
    ] or ["- _None._"])
    lines += ["", "## Operator decisions", ""]
    lines += ([
        f"- `{decision.decision_id}` — **{decision.decision}**: {decision.rationale} "
        f"[{', '.join(decision.citation_ids)}]"
        for decision in manifest.operator_decisions
    ] or ["- _None._"])
    lines += ["", "> Verification note: proof status is manifest state; run the offline "
              "verifier against the sealed manifest and included bytes before relying on it."]
    return "\n".join(lines).rstrip() + "\n"


def render_html(manifest: ProofManifest) -> str:
    """Small self-contained HTML projection with no mutable state or external assets."""
    markdown = render_markdown(manifest)
    body = "\n".join(
        f"<div class=\"line\">{escape(line)}</div>" if line else "<br>"
        for line in markdown.splitlines()
    )
    status = escape(manifest.validation_verdict)
    return (
        "<!doctype html>\n<html><head><meta charset=\"utf-8\">"
        f"<title>Proof dossier {escape(manifest.finding_id)}</title>"
        "<style>body{font:15px/1.5 system-ui;margin:2rem;max-width:72rem}"
        ".status{font-weight:700}.line{white-space:pre-wrap}</style></head>"
        f"<body><div class=\"status\" data-verdict=\"{status}\">"
        f"Verdict: {status}</div>{body}</body></html>\n"
    )


def _citation_markdown(statement: CitedStatement) -> str:
    return "[" + ", ".join(f"`{item}`" for item in statement.citation_ids) + "]"
