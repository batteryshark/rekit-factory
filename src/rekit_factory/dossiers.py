"""Canonical publication of W-0024 finding state as verified proof dossiers."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import tempfile
import zipfile
from typing import Any

from muster import RunPaths

from rekit_factory.evidence import EvidenceState, EvidenceStore
from rekit_factory.memory import ProjectMemory, ProjectMemoryLog
from rekit_factory.proof_bundles import (
    AttemptSummary, CitedStatement, ContentHash, EnvironmentFact, IncludedArtifact,
    OpaqueCitation, OperatorDecision, OriginIdentity, Prerequisites, ProofAction,
    ProofManifest, ProofPolicyBinding, ScopeBinding, ToolVersion, WorkerIdentity,
    canonical_bundle_json, canonical_manifest_json, render_html, render_markdown,
    seal_manifest, verify_bundle,
)
from rekit_factory.scope import AuthorizedScope, NetworkMode, opaque_ref
from rekit_factory.store import FactoryLedger


class DossierNotReady(ValueError):
    pass


_DOSSIER_ARTIFACT_KINDS = {
    "proof-bundle", "proof-manifest", "proof-report", "proof-report-html", "proof-export",
}


def finding_state_sha256(memory: ProjectMemory, finding_id: str) -> str:
    finding = memory.findings[finding_id]
    value = {
        "finding": finding,
        "attempts": sorted(
            (item for item in memory.finding_attempts.values()
             if item["findingId"] == finding_id), key=lambda item: item["id"],
        ),
        "transitions": sorted(
            (item for item in memory.finding_transitions.values()
             if item["findingId"] == finding_id), key=lambda item: item["id"],
        ),
        "decisions": sorted(
            (item for item in memory.finding_operator_decisions.values()
             if item["findingId"] == finding_id), key=lambda item: item["id"],
        ),
    }
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(raw.encode("utf-8")).hexdigest()


def dossier_list(ledger: FactoryLedger, run_id: str, *, run_dir: Path | None = None
                 ) -> list[dict[str, Any]]:
    rows = ledger.conn.execute(
        "select * from artifacts where run_id=? and kind='proof-bundle' order by created_at,id",
        (run_id,),
    ).fetchall()
    result = []
    for row in rows:
        metadata = json.loads(row["metadata_json"])
        item = {
            "id": metadata["dossierId"], "runId": run_id,
            "findingId": metadata["findingId"],
            "manifestSha256": metadata["manifestSha256"],
            "findingStateSha256": metadata["findingStateSha256"],
            "verdict": metadata["verdict"], "findingStatus": metadata["findingStatus"],
            "artifactIds": metadata["artifactIds"], "createdAt": row["created_at"],
        }
        item["verificationStatus"] = "published"
        item["verified"] = False
        if run_dir is not None:
            try:
                verify_published_dossier(run_dir, item)
            except (DossierNotReady, KeyError, OSError, ValueError):
                item["verificationStatus"] = "stale-or-invalid"
            else:
                item["verificationStatus"] = "verified"
                item["verified"] = True
        result.append(item)
    return result


def verify_published_dossier(run_dir: Path, dossier: dict[str, Any]):
    """Re-anchor a published bundle to current canonical memory and durable run scope."""
    project_memory = ProjectMemoryLog(run_dir.parents[1]).replay()
    finding_id = dossier["findingId"]
    if finding_id not in project_memory.findings:
        raise DossierNotReady("published finding is absent from canonical memory")
    run_id = dossier.get("runId") or run_dir.name
    with FactoryLedger(run_dir / "run.db") as ledger:
        scope = _run_bound_scope(run_dir, ledger, run_id)
        artifacts = _verified_dossier_artifacts(run_dir, ledger, run_id, dossier)
    row = artifacts["proof-bundle"]
    report = verify_bundle(
        Path(row["path"]), expected_scope_digest=scope.content_digest,
        expected_scope_revision=scope.revision,
        expected_finding_state_sha256=finding_state_sha256(project_memory, finding_id),
    )
    if not report.valid or not report.trust_anchor_verified:
        raise DossierNotReady("published dossier no longer matches canonical state and scope")
    if report.manifest_sha256 != dossier["manifestSha256"]:
        raise DossierNotReady("published dossier content identity changed after publication")
    return report


class DossierPublisher:
    def __init__(self, paths: RunPaths, ledger: FactoryLedger, rekit: Any):
        self.paths, self.ledger, self.rekit = paths, ledger, rekit
        self.memory_log = ProjectMemoryLog(paths.run_dir.parents[1])
        self.evidence = EvidenceStore(paths.run_dir / "evidence")

    def publish_ready(self) -> list[dict[str, Any]]:
        memory = self.memory_log.replay()
        for finding_id in sorted(memory.findings):
            try:
                self.publish(finding_id, memory=memory)
            except DossierNotReady as exc:
                self.ledger.event_log(
                    self.paths.run_id, "dossier.deferred",
                    f"Proof dossier deferred for {finding_id}",
                    payload={"findingId": finding_id, "reason": str(exc)},
                )
        return dossier_list(self.ledger, self.paths.run_id, run_dir=self.paths.run_dir)

    def publish(self, finding_id: str, *, memory: ProjectMemory | None = None) -> dict[str, Any]:
        memory = memory or self.memory_log.replay()
        manifest, included_bytes = self._build_manifest(memory, finding_id)
        sealed = seal_manifest(manifest)
        root = self.paths.run_dir / "dossiers"
        root.mkdir(parents=True, exist_ok=True)
        final = root / sealed.manifest_sha256
        if not final.is_dir():
            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=root))
            try:
                for artifact in manifest.artifacts:
                    if isinstance(artifact, IncludedArtifact):
                        destination = staging / artifact.path
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(included_bytes[artifact.citation_id])
                (staging / "manifest.json").write_text(
                    canonical_manifest_json(manifest), encoding="utf-8"
                )
                (staging / "proof.json").write_text(
                    canonical_bundle_json(sealed), encoding="utf-8"
                )
                verification = verify_bundle(
                    staging / "proof.json", expected_scope_digest=manifest.scope.digest,
                    expected_scope_revision=manifest.scope.revision,
                    expected_finding_state_sha256=manifest.finding_state_sha256,
                )
                if not verification.valid or not verification.trust_anchor_verified:
                    raise DossierNotReady("staged dossier did not pass anchored verification")
                (staging / "report.md").write_text(
                    render_markdown(manifest, verification), encoding="utf-8"
                )
                (staging / "report.html").write_text(
                    render_html(manifest, verification), encoding="utf-8"
                )
                _write_deterministic_zip(staging, staging / "dossier.zip")
                os.replace(staging, final)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        verification = verify_bundle(
            final / "proof.json", expected_scope_digest=manifest.scope.digest,
            expected_scope_revision=manifest.scope.revision,
            expected_finding_state_sha256=manifest.finding_state_sha256,
        )
        if not verification.valid or not verification.trust_anchor_verified:
            raise DossierNotReady("materialized dossier is not currently valid")
        records = [
            _record(final / "proof.json", "proof-bundle", "proof.json", "application/json"),
            _record(final / "manifest.json", "proof-manifest", "manifest.json", "application/json"),
            _record(final / "report.md", "proof-report", "report.md", "text/markdown"),
            _record(final / "report.html", "proof-report-html", "report.html", "text/html"),
            _record(final / "dossier.zip", "proof-export", "dossier.zip", "application/zip"),
        ]
        metadata = {
            "dossierId": f"dossier-{sealed.manifest_sha256[:20]}",
            "findingId": finding_id, "manifestSha256": sealed.manifest_sha256,
            "findingStateSha256": manifest.finding_state_sha256,
            "verdict": manifest.validation_verdict, "findingStatus": manifest.finding_status,
        }
        self.ledger.publish_dossier(
            self.paths.run_id, finding_id=finding_id,
            manifest_sha256=sealed.manifest_sha256, records=records, metadata=metadata,
        )
        return next(item for item in dossier_list(
                    self.ledger, self.paths.run_id, run_dir=self.paths.run_dir)
                    if item["manifestSha256"] == sealed.manifest_sha256)

    def _build_manifest(self, memory: ProjectMemory, finding_id: str
                        ) -> tuple[ProofManifest, dict[str, bytes]]:
        try:
            finding = memory.findings[finding_id]
        except KeyError as exc:
            raise DossierNotReady("finding is absent from canonical memory") from exc
        attempts = sorted(
            (item for item in memory.finding_attempts.values()
             if item["findingId"] == finding_id), key=lambda item: item["id"],
        )
        work = self._finding_work(finding)
        if not work:
            raise DossierNotReady("no ledger work is anchored to this finding")
        scope = _run_bound_scope(
            self.paths.run_dir, self.ledger, self.paths.run_id,
        )
        run = self.ledger.get_run(self.paths.run_id)
        if run is None:
            raise DossierNotReady("dossier run is absent from the canonical ledger")
        target_ref = opaque_ref(
            "target-path", str(Path(run["target_path"]).expanduser().resolve())
        )
        target_grants = tuple(
            grant for grant in scope.targets if grant.path_fingerprint == target_ref
        )
        if len(target_grants) != 1:
            raise DossierNotReady(
                "run target does not have one exact binding in the authorized scope"
            )
        target_grant = target_grants[0]
        refs = _all_references(finding, attempts, memory)
        staged = {(item["kind"], item["id"]) for item in finding["recipe"]["staged_inputs"]}
        artifacts, included_bytes, citations = self._artifacts(refs, staged)
        staged_citations = tuple(citations[key] for key in sorted(staged))
        if not staged_citations:
            raise DossierNotReady("finding recipe has no material staged input")
        actions = self._actions(finding, citations)
        attempt_models = tuple(_attempt(item, citations) for item in attempts)
        workers = tuple(_worker(item) for item in attempts)
        environment = attempts[0].get("environment", {}) if attempts else {}
        decisions = tuple(
            _decision(item, citations) for item in sorted(
                (value for value in memory.finding_operator_decisions.values()
                 if value["findingId"] == finding_id), key=lambda value: value["id"],
            )
        )
        status = ("operator-accepted" if finding["status"] == "reproduced"
                  and any(item.decision == "accepted" for item in decisions)
                  else finding["status"])
        verdict = _verdict(status)
        manifest = ProofManifest(
            manifest_id=f"proof-{finding_id}-v1", created_at=_event_time(
                self.memory_log.path, finding.get("_eventSeq", 0)
            ),
            target_inputs=(ContentHash(
                id="target-primary", role="target",
                sha256=target_grant.content_sha256,
            ),) + tuple(ContentHash(
                id=citation, role="required-input",
                sha256=next(item.sha256 for item in artifacts
                            if isinstance(item, IncludedArtifact)
                            and item.citation_id == citation),
            ) for citation in staged_citations),
            scope=ScopeBinding(
                scope_id=scope.scope_id, revision=scope.revision, digest=scope.content_digest,
                data_handling=scope.data_handling.value, export_mode="internal",
            ),
            run_id=self.paths.run_id, work_item_ids=tuple(item["id"] for item in work),
            hypothesis_id=finding["hypothesisId"], finding_id=finding_id,
            finding_state_sha256=finding_state_sha256(memory, finding_id),
            finding_status=status, validation_verdict=verdict,
            origin=OriginIdentity(
                worker_id=finding["originWorkerId"], session_id=finding["originSessionId"],
                model_profile=finding["originModelProfile"],
            ),
            proof_policy=ProofPolicyBinding(**_snake_policy(finding["proofPolicy"])),
            recipe_id=finding["recipe"]["id"], workers=workers or (WorkerIdentity(
                worker_id=finding["originWorkerId"], session_id=finding["originSessionId"],
                environment_id="origin:unknown", clean=False,
                model_profile=finding["originModelProfile"],
            ),),
            prerequisites=Prerequisites(
                platform=environment.get("platform", "unknown"),
                architecture=environment.get("architecture", "unknown"),
                isolation=environment.get("isolation", "unknown"),
                facts=tuple(EnvironmentFact(name=f"requirement-{index + 1}", value=value,
                                            source="declared")
                            for index, value in enumerate(
                                finding["recipe"]["clean_environment_requirements"]
                            )),
            ),
            tool_versions=self._tool_versions(actions, work),
            network_policy=("none" if scope.network_mode is NetworkMode.NONE else "restricted"),
            endpoint_refs=tuple(opaque_ref("endpoint", value) for value in scope.endpoints),
            artifacts=artifacts, actions=actions,
            expected_outcome=CitedStatement(
                text=finding["recipe"]["expected_observation"],
                citation_ids=staged_citations,
            ),
            observed_outcomes=tuple(
                CitedStatement(text="; ".join(item["observations"]),
                               citation_ids=_citation_ids(item["references"], citations))
                for item in attempts
            ) or (CitedStatement(
                text="No independent reproduction outcome has been recorded.",
                citation_ids=_citation_ids(finding["references"], citations),
            ),),
            observations=tuple(
                CitedStatement(text=item["observation"],
                               citation_ids=_citation_ids(item["references"], citations))
                for item in finding["observations"]
            ),
            interpretations=(CitedStatement(
                text=finding["impactClaim"],
                citation_ids=_citation_ids(finding["references"], citations),
            ),),
            limitations=(CitedStatement(
                text=finding["knownUncertainty"],
                citation_ids=_citation_ids(finding["references"], citations),
            ),),
            contradictions=tuple(
                CitedStatement(text="; ".join(item["observations"]),
                               citation_ids=_citation_ids(item["references"], citations))
                for item in attempts if item["outcome"] in {"negative", "flaky", "contradictory"}
            ),
            attempts=attempt_models, operator_decisions=decisions,
        )
        return manifest, included_bytes

    def _finding_work(self, finding: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self.ledger.conn.execute(
            "select id,category,operation,status,payload_json from work_items "
            "where run_id=? order by created_at,id",
            (self.paths.run_id,),
        ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("findingId") == finding["id"]:
                result.append({
                    "id": row["id"], "category": row["category"],
                    "operation": row["operation"], "status": row["status"], "payload": payload,
                })
        return result

    def _artifacts(self, refs: set[tuple[str, str]], staged: set[tuple[str, str]]):
        artifacts, content, citations = [], {}, {}
        for kind, identifier in sorted(refs):
            citation = _citation_id(kind, identifier)
            citations[(kind, identifier)] = citation
            record = self._evidence_record(identifier)
            if record is None or record.state is not EvidenceState.RETAINED:
                if (kind, identifier) in staged:
                    raise DossierNotReady(f"required input {kind}:{identifier} is unavailable")
                artifacts.append(OpaqueCitation(
                    citation_id=citation, purpose="evidence",
                    factory_uri=f"factory://runs/{self.paths.run_id}/artifacts/{citation}",
                    export_policy="private", retention_state="withheld",
                    reason="Durable citation exists but exportable display bytes are unavailable.",
                ))
                continue
            data = self.evidence.display_bytes(record.artifact_id)
            if data is None:
                raise DossierNotReady(f"verified display projection {record.artifact_id} is unavailable")
            purpose = "required-input" if (kind, identifier) in staged else "evidence"
            artifacts.append(IncludedArtifact(
                citation_id=citation, purpose=purpose,
                path=f"artifacts/sha256/{record.display_sha256}",
                sha256=record.display_sha256, size=record.display_size,
                media_type=record.media_type.split(";", 1)[0].strip(),
                material_class=("operator-provided-fixture" if purpose == "required-input"
                                else "derived-evidence"),
                export_policy="internal-only",
                content_state="redacted" if record.redacted else "safe",
                retention_class=("pinned" if record.held else
                                 "run" if record.retention_class.value == "run" else "project"),
                retention_state="held" if record.held else "retained",
                source_run_id=record.run_id, source_artifact_id=record.artifact_id,
                projection_sha256=record.display_sha256,
                capture_policy=record.capture_policy, redaction_policy="display-redaction-v1",
            ))
            content[citation] = data
        return tuple(artifacts), content, citations

    def _evidence_record(self, identifier: str):
        direct = self.evidence.get(identifier)
        if direct is not None:
            return direct
        digest = identifier.removeprefix("sha256:")
        for record in self.evidence.list_records(self.paths.run_id):
            if digest in {record.display_sha256, record.original_sha256, record.raw_sha256}:
                return record
        row = self.ledger.conn.execute(
            "select metadata_json from artifacts where run_id=? and (id=? or sha256=?)",
            (self.paths.run_id, identifier, digest),
        ).fetchone()
        if row is not None:
            evidence_id = json.loads(row["metadata_json"]).get("evidenceArtifactId")
            return self.evidence.get(evidence_id) if evidence_id else None
        return None

    def _actions(self, finding: dict[str, Any], citations: dict[tuple[str, str], str]
                 ) -> tuple[ProofAction, ...]:
        actions = []
        for reference in finding["recipe"]["staged_inputs"]:
            actions.append(ProofAction(
                order=len(actions) + 1, action="stage-input",
                description=f"Stage required input {reference['id']}",
                citation_ids=(citations[(reference["kind"], reference["id"])],),
            ))
        for step in finding["recipe"]["steps"]:
            if isinstance(step, str):
                actions.append(ProofAction(
                    order=len(actions) + 1, action="observe", description=step,
                    citation_ids=(),
                ))
                continue
            actions.append(ProofAction(
                order=len(actions) + 1, action=step["action"],
                description=step["description"], tool_id=step.get("tool_id"),
                argv=tuple(step.get("argv", ())),
                environment=tuple(EnvironmentFact(name=name, value=value, source="declared")
                                  for name, value in sorted(step.get("environment", {}).items())),
                citation_ids=_citation_ids(step.get("references", ()), citations),
            ))
        actions.append(ProofAction(
            order=len(actions) + 1, action="compare",
            description=f"Compare the observation with: {finding['recipe']['expected_observation']}",
            citation_ids=tuple(citations[(item["kind"], item["id"])]
                               for item in finding["recipe"]["staged_inputs"]),
        ))
        return tuple(actions)

    def _tool_versions(self, actions: tuple[ProofAction, ...],
                       work: list[dict[str, Any]]) -> tuple[ToolVersion, ...]:
        recorded: dict[str, set[tuple[str, str]]] = {}
        for item in work:
            payload = item["payload"]
            if (item["operation"] not in {"rekit-tool", "model-rekit-tool"}
                    or item["status"] != "done" or not payload.get("findingId")
                    or not payload.get("toolId") or not payload.get("manifestDigest")):
                continue
            call = self.ledger.conn.execute(
                "select id,manifest_digest,status,exit_code from factory_tool_calls "
                "where run_id=? and work_item_id=? and tool_id=? order by created_at desc",
                (self.paths.run_id, item["id"], payload["toolId"]),
            ).fetchone()
            if (call is None or call["status"] != "done" or call["exit_code"] != 0
                    or call["manifest_digest"] != payload["manifestDigest"]):
                continue
            verified = None
            for row in self.ledger.conn.execute(
                    "select metadata_json from artifacts where run_id=? and kind='tool-output'",
                    (self.paths.run_id,),
            ).fetchall():
                metadata = json.loads(row["metadata_json"])
                provenance = metadata.get("provenance", {})
                if (metadata.get("toolId") == payload["toolId"]
                        and provenance.get("work_item_id") == item["id"]
                        and provenance.get("invocation_id") == call["id"]
                        and metadata.get("effectiveManifestDigest") == call["manifest_digest"]
                        and metadata.get("verifiedManifestDigest") == call["manifest_digest"]):
                    verified = metadata["verifiedManifestDigest"]
                    break
            if verified is not None:
                recorded.setdefault(payload["toolId"], set()).add((
                    verified, f"authority-v{payload.get('authorityVersion', 1)}",
                ))
        result = []
        for tool_id in sorted({item.tool_id for item in actions if item.tool_id}):
            identities = recorded.get(tool_id, set())
            if len(identities) != 1:
                raise DossierNotReady(
                    f"completed finding-linked attestation for {tool_id} is unavailable or ambiguous"
                )
            digest, authority_version = next(iter(identities))
            result.append(ToolVersion(
                tool_id=tool_id, version=f"manifest-sha256:{digest}",
                capability_id=f"rekit:{tool_id}", capability_version=authority_version,
            ))
        return tuple(result)


def _run_bound_scope(run_dir: Path, ledger: FactoryLedger, run_id: str):
    run = ledger.get_run(run_id)
    if run is None:
        raise DossierNotReady("dossier run is absent from the canonical ledger")
    config = json.loads(run["config_json"])
    expected = config.get("scope")
    if not isinstance(expected, dict):
        raise DossierNotReady("run has no pinned scope binding")
    try:
        scope = AuthorizedScope.from_dict(json.loads(
            (run_dir / "scope.json").read_text(encoding="utf-8")
        )).envelope
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise DossierNotReady("run-bound scope record is unavailable or malformed") from exc
    actual = scope.public_dict()
    if any(actual.get(name) != expected.get(name)
           for name in ("scopeId", "revision", "digest")):
        raise DossierNotReady("run-bound scope record changed after run creation")
    return scope


def _verified_dossier_artifacts(run_dir: Path, ledger: FactoryLedger, run_id: str,
                                dossier: dict[str, Any]) -> dict[str, Any]:
    artifact_ids = dossier.get("artifactIds")
    if not isinstance(artifact_ids, dict) or set(artifact_ids) != _DOSSIER_ARTIFACT_KINDS:
        raise DossierNotReady("published dossier artifact set is incomplete or malformed")
    result = {}
    root = run_dir.resolve(strict=True)
    for kind in sorted(_DOSSIER_ARTIFACT_KINDS):
        artifact_id = artifact_ids[kind]
        row = ledger.conn.execute(
            "select id,kind,path,sha256,size_bytes,metadata_json from artifacts "
            "where id=? and run_id=? and kind=?",
            (artifact_id, run_id, kind),
        ).fetchone()
        if row is None:
            raise DossierNotReady(f"published dossier artifact {kind} is absent")
        try:
            metadata = json.loads(row["metadata_json"])
            path = Path(row["path"]).resolve(strict=True)
            path.relative_to(root)
            data = path.read_bytes()
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DossierNotReady(f"published dossier artifact {kind} is unsafe") from exc
        if (metadata.get("manifestSha256") != dossier.get("manifestSha256")
                or metadata.get("artifactIds") != artifact_ids):
            raise DossierNotReady(f"published dossier artifact {kind} lost its ledger binding")
        if len(data) != row["size_bytes"] or sha256(data).hexdigest() != row["sha256"]:
            raise DossierNotReady(f"published dossier artifact {kind} changed after publication")
        result[kind] = row
    return result


def _record(path: Path, kind: str, logical_path: str, media_type: str) -> dict[str, Any]:
    data = path.read_bytes()
    return {"kind": kind, "path": path, "logical_path": f"dossier/{logical_path}",
            "sha256": sha256(data).hexdigest(), "size_bytes": len(data),
            "media_type": media_type}


def _all_references(finding, attempts, memory) -> set[tuple[str, str]]:
    values = list(finding["references"]) + list(finding["recipe"]["staged_inputs"])
    for observation in finding["observations"]:
        values += observation["references"]
    for step in finding["recipe"]["steps"]:
        if isinstance(step, dict):
            values += step.get("references", [])
    for attempt in attempts:
        values += attempt["references"]
    for decision in memory.finding_operator_decisions.values():
        if decision["findingId"] == finding["id"]:
            values += decision["references"]
    return {(item["kind"], item["id"]) for item in values}


def _citation_id(kind: str, identifier: str) -> str:
    digest = sha256(f"{kind}:{identifier}".encode()).hexdigest()[:24]
    return f"citation-{digest}"


def _citation_ids(refs, citations):
    return tuple(citations[(item["kind"], item["id"])] for item in refs)


def _snake_policy(value):
    return {"successful_clean_reproductions": value["successful_clean_reproductions"],
            "require_independent_worker": value["require_independent_worker"],
            "require_independent_session": value["require_independent_session"],
            "require_clean_environment": value.get("require_clean_environment", True),
            "require_distinct_model_profile": value.get("require_distinct_model_profile", False)}


def _attempt(item, citations):
    environment = item.get("environment", {})
    return AttemptSummary(
        attempt_id=item["id"], outcome=item["outcome"],
        statement=CitedStatement(text="; ".join(item["observations"]),
                                 citation_ids=_citation_ids(item["references"], citations)),
        environment_differences=tuple(item["environmentalDifferences"]),
        recipe_id=item["recipeId"], worker_id=item["workerId"],
        session_id=item["sessionId"], environment_id=environment["id"],
        clean_environment=environment["clean"], model_profile=item["modelProfile"],
    )


def _worker(item):
    environment = item.get("environment", {})
    return WorkerIdentity(
        worker_id=item["workerId"], session_id=item["sessionId"],
        environment_id=environment["id"], clean=environment["clean"],
        model_profile=item["modelProfile"],
        facts=tuple(EnvironmentFact(name=name, value=environment[name], source="observed")
                    for name in ("platform", "architecture", "isolation")
                    if environment.get(name)),
    )


def _decision(item, citations):
    return OperatorDecision(
        decision_id=item["id"], decision=item["decision"], rationale=item["rationale"],
        unmet_criteria=tuple(item["unmetCriteria"]),
        citation_ids=_citation_ids(item["references"], citations),
    )


def _verdict(status: str) -> str:
    if status in {"reproduced", "operator-accepted"}:
        return "validated"
    if status in {"rejected", "denied"}:
        return "rejected"
    if status == "withdrawn":
        return "withdrawn"
    if status == "inconclusive":
        return "inconclusive"
    return "unvalidated"


def _event_time(path: Path, sequence: int) -> str:
    for raw in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(raw)
        if value.get("seq") == sequence:
            return value["ts"]
    raise DossierNotReady("finding event timestamp is unavailable")


def _write_deterministic_zip(root: Path, destination: Path) -> None:
    paths = sorted(path for path in root.rglob("*") if path.is_file() and path != destination)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
