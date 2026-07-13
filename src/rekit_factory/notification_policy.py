"""Pure, fail-closed notification candidates from canonical outcome transitions.

This module deliberately does not persist, deliver, retry, acknowledge, or batch anything.
Those are durable outbox concerns owned by W-0030.  It only turns a verified old/new
current supported ``factory-outcomes`` pair into deterministic, redacted candidate records.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from rekit_factory.outcomes import (
    SCHEMA_VERSION,
    VOCABULARY_VERSION,
    decode_outcome_semantic_canonical_base64,
    verify_outcome_semantic_sha256,
)


POLICY_VERSION = "factory-notification-policy/v1"
CANDIDATE_SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class InvalidOutcomeProjection(ValueError):
    """Raised when input is not an intact canonical v1 outcome projection."""


def _validate_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    if type(value) is not dict:
        raise InvalidOutcomeProjection("outcome projection must be a JSON object")
    if value.get("schemaVersion") != SCHEMA_VERSION:
        raise InvalidOutcomeProjection("unsupported outcome projection schema")
    if value.get("vocabularyVersion") != VOCABULARY_VERSION:
        raise InvalidOutcomeProjection("unsupported outcome projection vocabulary")
    try:
        verified = verify_outcome_semantic_sha256(value)
        decode_outcome_semantic_canonical_base64(value)
    except (TypeError, ValueError) as exc:
        raise InvalidOutcomeProjection("invalid outcome projection identity") from exc
    if not verified:
        raise InvalidOutcomeProjection("invalid outcome projection identity")
    entities = value.get("entities")
    if type(entities) is not list or any(type(item) is not dict for item in entities):
        raise InvalidOutcomeProjection("outcome projection entities must be a list of objects")
    return value


def _safe_id(value: Any) -> str | None:
    return value if type(value) is str and _SAFE_ID.fullmatch(value) else None


def _entity_map(projection: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]] | None:
    entities: dict[tuple[str, str], dict[str, Any]] = {}
    for item in projection["entities"]:
        entity_type = _safe_id(item.get("entityType"))
        entity_id = _safe_id(item.get("entityId"))
        if entity_type is None or entity_id is None:
            return None
        identity = (entity_type, entity_id)
        if identity in entities:
            return None
        entities[identity] = item
    return entities


def _facet(entity: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    facets = entity.get("facets")
    if type(facets) is not dict:
        return None
    value = facets.get(name)
    return value if type(value) is dict and value.get("known") is True else None


def _waiting_decision(entity: Mapping[str, Any] | None) -> bool:
    if entity is None:
        return False
    disposition = _facet(entity, "disposition")
    acceptance = _facet(entity, "acceptance")
    return bool(
        disposition and acceptance
        and disposition.get("state") == "needs-review"
        and disposition.get("terminal") is False
        and acceptance.get("state") == "undecided"
        and acceptance.get("terminal") is False
    )


def _finding_at(entity: Mapping[str, Any] | None, facet_name: str, state: str) -> bool:
    if entity is None:
        return False
    facet = _facet(entity, facet_name)
    return bool(facet and facet.get("state") == state)


def _candidate(*, kind: str, severity: str, run_id: str, entity_id: str,
               message: str, entity_type: str | None = None) -> dict[str, Any]:
    linked_type = entity_type or (
        "operator-decision" if kind == "operator-decision.waiting" else "finding"
    )
    identity = {
        "policyVersion": POLICY_VERSION,
        "runId": run_id,
        "entityType": linked_type,
        "entityId": entity_id,
        "transition": kind,
    }
    canonical = json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return {
        "schemaVersion": CANDIDATE_SCHEMA_VERSION,
        "policyVersion": POLICY_VERSION,
        "dedupeKey": f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
        "kind": kind,
        "severity": severity,
        "runId": run_id,
        "entity": {"entityType": linked_type, "entityId": entity_id},
        "message": message,
    }


def _exact_proof_link(
    entities: Mapping[tuple[str, str], Mapping[str, Any]], finding_id: str,
) -> str | None:
    """Return one exact published proof child; ambiguity deliberately yields no guess."""
    matches: list[str] = []
    for (entity_type, entity_id), entity in entities.items():
        if entity_type != "proof-bundle":
            continue
        parent = entity.get("parent")
        publication = _facet(entity, "publication")
        if (type(parent) is dict
                and parent == {"entityType": "finding", "entityId": finding_id}
                and publication and publication.get("state") == "published"):
            matches.append(entity_id)
    return matches[0] if len(matches) == 1 else None


def notification_candidates(
    old: Mapping[str, Any] | None,
    new: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return stable redacted candidates for consequential canonical transitions.

    ``old=None`` is initial hydration and intentionally emits nothing.  A durable outbox may
    later use each candidate's dedupe key for exactly-once admission; this pure function makes
    no delivery or persistence claim.
    """
    current = _validate_projection(new)
    if old is None:
        return []
    previous = _validate_projection(old)

    # Diagnostics mean some canonical state is unknown.  Suppress rather than guess whether a
    # consequential threshold was actually crossed.
    if previous.get("degraded") is not False or current.get("degraded") is not False:
        return []
    if previous.get("semanticSha256") == current.get("semanticSha256"):
        return []

    old_entities = _entity_map(previous)
    new_entities = _entity_map(current)
    if old_entities is None or new_entities is None:
        return []
    run_ids = sorted(
        entity_id for (entity_type, entity_id) in new_entities if entity_type == "run"
    )
    if len(run_ids) != 1:
        return []
    run_id = run_ids[0]

    candidates: list[dict[str, Any]] = []
    for (entity_type, entity_id), entity in sorted(new_entities.items()):
        before = old_entities.get((entity_type, entity_id))
        if entity_type == "operator-decision":
            if _waiting_decision(entity) and not _waiting_decision(before):
                candidates.append(_candidate(
                    kind="operator-decision.waiting", severity="action-required",
                    run_id=run_id, entity_id=entity_id,
                    message="Operator decision is waiting in Mission Control.",
                ))
        elif entity_type == "finding":
            proof_id = _exact_proof_link(new_entities, entity_id)
            linked_type = "proof-bundle" if proof_id is not None else "finding"
            linked_id = proof_id or entity_id
            if (_finding_at(entity, "validation", "reproduced")
                    and not _finding_at(before, "validation", "reproduced")):
                candidates.append(_candidate(
                    kind="finding.reproduced", severity="consequential",
                    run_id=run_id, entity_id=linked_id, entity_type=linked_type,
                    message="A finding reached the reproduced threshold.",
                ))
            if (_finding_at(entity, "acceptance", "accepted")
                    and _finding_at(entity, "validation", "reproduced")
                    and not _finding_at(before, "acceptance", "accepted")):
                candidates.append(_candidate(
                    kind="finding.accepted", severity="consequential",
                    run_id=run_id, entity_id=linked_id, entity_type=linked_type,
                    message="A finding was accepted by the operator.",
                ))

    return sorted(candidates, key=lambda item: (item["kind"], item["entity"]["entityId"]))


def notification_supersession_ids(
    old: Mapping[str, Any] | None,
    new: Mapping[str, Any],
) -> list[str]:
    """Return previously admitted notification ids that no longer require delivery.

    This is deliberately the inverse of the candidate predicates, not a generic entity-removal
    heuristic.  A degraded observation cannot prove resolution and therefore cancels nothing.
    Re-entering the same state later retains the original content-addressed id, so a
    wait/resolve/wait flap cannot create fresh delivery work.
    """
    current = _validate_projection(new)
    if old is None:
        return []
    previous = _validate_projection(old)
    if previous.get("degraded") is not False or current.get("degraded") is not False:
        return []
    if previous.get("semanticSha256") == current.get("semanticSha256"):
        return []
    old_entities = _entity_map(previous)
    new_entities = _entity_map(current)
    if old_entities is None or new_entities is None:
        return []
    old_runs = sorted(
        entity_id for (entity_type, entity_id) in old_entities if entity_type == "run"
    )
    new_runs = sorted(
        entity_id for (entity_type, entity_id) in new_entities if entity_type == "run"
    )
    if len(old_runs) != 1 or old_runs != new_runs:
        return []
    run_id = old_runs[0]
    superseded: list[str] = []
    for (entity_type, entity_id), before in sorted(old_entities.items()):
        after = new_entities.get((entity_type, entity_id))
        candidates: list[dict[str, Any]] = []
        if entity_type == "operator-decision":
            if _waiting_decision(before) and not _waiting_decision(after):
                candidates.append(_candidate(
                    kind="operator-decision.waiting", severity="action-required",
                    run_id=run_id, entity_id=entity_id,
                    message="Operator decision is waiting in Mission Control.",
                ))
        elif entity_type == "finding":
            proof_id = _exact_proof_link(old_entities, entity_id)
            linked_type = "proof-bundle" if proof_id is not None else "finding"
            linked_id = proof_id or entity_id
            if (_finding_at(before, "validation", "reproduced")
                    and not _finding_at(after, "validation", "reproduced")):
                candidates.append(_candidate(
                    kind="finding.reproduced", severity="consequential",
                    run_id=run_id, entity_id=linked_id, entity_type=linked_type,
                    message="A finding reached the reproduced threshold.",
                ))
            if (_finding_at(before, "acceptance", "accepted")
                    and _finding_at(before, "validation", "reproduced")
                    and not (_finding_at(after, "acceptance", "accepted")
                             and _finding_at(after, "validation", "reproduced"))):
                candidates.append(_candidate(
                    kind="finding.accepted", severity="consequential",
                    run_id=run_id, entity_id=linked_id, entity_type=linked_type,
                    message="A finding was accepted by the operator.",
                ))
        superseded.extend(
            "notification-" + candidate["dedupeKey"].removeprefix("sha256:")
            for candidate in candidates
        )
    return sorted(set(superseded))
