"""Exact campaign-owned proof qualification for existing run notifications.

The resolver never emits a candidate.  It can only replace a finding-surface link while the
ordinary run transition is being admitted, preserving the run policy's one-candidate boundary.
Campaign handoff ownership, exact project/scope identity, and intact canonical outcomes are all
required; incomplete or ambiguous joins yield no qualification.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from rekit_factory.outcomes import (
    decode_outcome_semantic_canonical_base64,
    verify_outcome_semantic_sha256,
)


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _safe(value: object) -> bool:
    return type(value) is str and _SAFE_ID.fullmatch(value) is not None


def _run_dir(storage_root: Path, run_id: str) -> Path | None:
    matches: list[Path] = []
    for meta_path in storage_root.glob(f"projects/*/runs/{run_id}/run.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if (type(meta) is dict and meta.get("runId") == run_id
                and meta.get("creationComplete") is not False):
            matches.append(meta_path.parent)
    return matches[0] if len(matches) == 1 else None


def _entities(projection: object) -> dict[tuple[str, str], dict[str, Any]] | None:
    if type(projection) is not dict or projection.get("degraded") is not False:
        return None
    try:
        if not verify_outcome_semantic_sha256(projection):
            return None
        decode_outcome_semantic_canonical_base64(projection)
    except (TypeError, ValueError):
        return None
    raw = projection.get("entities")
    if type(raw) is not list:
        return None
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for item in raw:
        if type(item) is not dict:
            return None
        entity_type, entity_id = item.get("entityType"), item.get("entityId")
        if not _safe(entity_type) or not _safe(entity_id):
            return None
        key = (entity_type, entity_id)
        if key in result:
            return None
        result[key] = item
    return result


def _known_facet(entity: dict[str, Any], name: str) -> dict[str, Any] | None:
    facets = entity.get("facets")
    value = facets.get(name) if type(facets) is dict else None
    return value if type(value) is dict and value.get("known") is True else None


class CampaignOwnedProofResolver:
    """Resolve one exact cross-run dossier through canonical campaign ownership."""

    def __init__(self, campaign_controller: object, factory_controller: object) -> None:
        self.campaign_controller = campaign_controller
        self.factory_controller = factory_controller

    def __call__(self, source_run_id: str, finding_id: str) -> tuple[str, str] | None:
        if not _safe(source_run_id) or not _safe(finding_id):
            return None
        try:
            contexts = self.campaign_controller.notification_proof_contexts(source_run_id)
            storage_root = Path(self.factory_controller.storage_root)
        except Exception:
            # This is downstream link qualification.  An unavailable campaign or Factory
            # authority must leave the ordinary finding link intact, not block run progress.
            return None
        # A run shared by multiple campaigns does not authorize choosing either campaign's proof.
        if type(contexts) is not tuple or len(contexts) != 1:
            return None
        context = contexts[0]
        if type(context) is not dict:
            return None
        project_id, scope, run_ids = (
            context.get("projectId"), context.get("scope"), context.get("factoryRunIds"),
        )
        if not _safe(project_id) or type(scope) is not dict or type(run_ids) is not list \
                or source_run_id not in run_ids or len(run_ids) > 32:
            return None
        scope_identity = (scope.get("scopeId"), scope.get("revision"), scope.get("digest"))

        def owned_entities(run_id: str) -> dict[tuple[str, str], dict[str, Any]] | None:
            if not _safe(run_id):
                return None
            run_dir = _run_dir(storage_root, run_id)
            if run_dir is None:
                return None
            try:
                snapshot = self.factory_controller.snapshot(
                    run_dir, admit_notifications=False,
                )
            except Exception:
                return None
            if type(snapshot) is not dict:
                return None
            run, meta = snapshot.get("run"), snapshot.get("meta")
            run_scope = meta.get("scope") if type(meta) is dict else None
            if (type(run) is not dict or type(meta) is not dict
                    or run.get("id") != run_id or meta.get("runId") != run_id
                    or meta.get("projectId") != project_id or type(run_scope) is not dict
                    or (run_scope.get("scopeId"), run_scope.get("revision"),
                        run_scope.get("digest")) != scope_identity):
                return None
            entities = _entities(snapshot.get("outcomeProjection"))
            projection_runs = ([] if entities is None else sorted(
                entity_id for (entity_type, entity_id) in entities
                if entity_type == "run"
            ))
            if projection_runs != [run_id]:
                return None
            return entities

        # Handoff membership alone is insufficient: mirror typed-link ownership by proving the
        # source run's exact project/scope/projection before consulting any sibling run.
        source_entities = owned_entities(source_run_id)
        source_finding = (None if source_entities is None
                          else source_entities.get(("finding", finding_id)))
        source_validation = (None if source_finding is None
                             else _known_facet(source_finding, "validation"))
        if source_validation is None or source_validation.get("state") != "reproduced":
            return None

        matches: set[tuple[str, str]] = set()
        for run_id in run_ids:
            if run_id == source_run_id:
                continue
            entities = owned_entities(run_id)
            finding = None if entities is None else entities.get(("finding", finding_id))
            if finding is None:
                continue
            validation = _known_facet(finding, "validation")
            if validation is None or validation.get("state") != "reproduced":
                continue
            for (entity_type, proof_id), proof in entities.items():
                publication = _known_facet(proof, "publication")
                if (entity_type == "proof-bundle"
                        and proof.get("parent") == {
                            "entityType": "finding", "entityId": finding_id,
                        }
                        and publication is not None
                        and publication.get("state") == "published"):
                    matches.add((run_id, proof_id))
        return next(iter(matches)) if len(matches) == 1 else None
