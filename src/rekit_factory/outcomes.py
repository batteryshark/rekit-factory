"""Versioned, deterministic outcome projection over canonical Factory state.

This module is deliberately pure.  It does not infer parent success from children and it
does not persist derived lifecycle state.  Callers rebuild the projection from committed
ledger rows, replayed project memory, and dossier publication facts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
VOCABULARY_VERSION = "factory-outcomes/v1"
SEMANTIC_IDENTITY_DOMAIN = "factory-outcomes/semantic-sha256/v1"
SEMANTIC_IDENTITY_FIELD = "semanticSha256"
_NONSEMANTIC_TOP_LEVEL_FIELDS = frozenset({
    SEMANTIC_IDENTITY_FIELD, "sourceWatermarks",
})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
FACETS = (
    "execution", "completion", "disposition", "validation", "acceptance", "publication",
)

AUTHORITIES = {
    "muster": "Muster owns durable work-item execution and completion transitions.",
    "factory-scheduler": "The Factory scheduler owns run and worker execution transitions.",
    "validator-policy": "Validator policy owns hypothesis, finding, and reproduction conclusions.",
    "rekit-tool-result": "Reserved for a future authoritative Rekit result entity.",
    "operator": "The operator owns explicit acceptance, rejection, waiver, and answers.",
    "factory-dossier-publisher": "The Factory dossier publisher owns transactional publication.",
    "offline-proof-verifier": "The offline proof verifier owns current bundle validity.",
}

_NA = {"rawState": None, "state": "not-applicable", "known": True, "terminal": True}

_RUN_EXECUTION = {
    "queued": "queued", "running": "active", "needs_input": "waiting",
    "completed": "terminal", "partial": "terminal", "failed": "terminal",
    "blocked": "terminal", "cancelled": "terminal", "canceled": "terminal",
}
_WORK_EXECUTION = {
    "queued": "queued", "running": "active", "blocked": "waiting", "done": "terminal",
    "failed": "terminal", "cancelled": "terminal", "canceled": "terminal",
}
_HYPOTHESIS_VALIDATION = {
    "proposed": "unvalidated", "queued": "pending", "testing": "pending",
    "supported": "demonstrated", "contradicted": "contradicted",
    "disproved": "invalid", "reproduced": "reproduced", "retired": "unvalidated",
    "blocked": "inconclusive",
}
_FINDING_VALIDATION = {
    "lead": "unvalidated", "candidate": "unvalidated", "demonstrated": "demonstrated",
    "reproduction-pending": "pending", "reproduced": "reproduced",
    "rejected": "invalid", "withdrawn": "unvalidated", "inconclusive": "inconclusive",
}
_VALIDATION_ATTEMPT = {
    "success": "reproduced", "negative": "invalid", "flaky": "inconclusive",
    "contradictory": "contradicted", "inconclusive": "inconclusive",
}
_ACCEPTANCE = {"accepted": "accepted", "rejected": "rejected", "waived": "waived"}
_DOSSIER_VALIDATION = {
    "verified": "verified", "stale-or-invalid": "stale",
}


def _json_snapshot(value: Any, *, path: str = "$") -> Any:
    """Copy an exact JSON value while rejecting Python-only or non-finite values."""
    if value is None or type(value) in {bool, str, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite JSON number")
        return value
    if type(value) is list:
        return [
            _json_snapshot(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError(f"{path} contains a non-string JSON object key")
        return {
            key: _json_snapshot(value[key], path=f"{path}.{key}")
            for key in sorted(value)
        }
    raise TypeError(f"{path} contains non-JSON value {type(value).__name__}")


def canonical_outcome_semantic_bytes(projection: Mapping[str, Any]) -> bytes:
    """Return the v1 canonical semantic byte domain for a public outcome projection.

    The identity field and independently observed source watermarks are non-semantic and
    excluded. Every other present top-level field is included by default so future public
    semantic additions cannot accidentally escape the digest.
    """
    if type(projection) is not dict:
        raise TypeError("outcome projection must be a JSON object")
    if any(type(key) is not str for key in projection):
        raise TypeError("outcome projection contains a non-string JSON object key")
    semantic_projection = {
        key: _json_snapshot(projection[key], path=f"$.{key}")
        for key in sorted(projection)
        if key not in _NONSEMANTIC_TOP_LEVEL_FIELDS
    }
    envelope = {
        "domain": SEMANTIC_IDENTITY_DOMAIN,
        "projection": semantic_projection,
    }
    return json.dumps(
        envelope, allow_nan=False, ensure_ascii=False, separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def outcome_semantic_sha256(projection: Mapping[str, Any]) -> str:
    """Recompute the lowercase semantic SHA-256 from a public projection."""
    return hashlib.sha256(canonical_outcome_semantic_bytes(projection)).hexdigest()


def verify_outcome_semantic_sha256(projection: Mapping[str, Any]) -> bool:
    """Fail closed unless the public projection carries its recomputed semantic identity."""
    if type(projection) is not dict:
        raise TypeError("outcome projection must be a JSON object")
    claimed = projection.get(SEMANTIC_IDENTITY_FIELD)
    if not isinstance(claimed, str) or _SHA256.fullmatch(claimed) is None:
        return False
    return hmac.compare_digest(claimed, outcome_semantic_sha256(projection))


def _na(owner: str) -> dict[str, Any]:
    return {**_NA, "owner": owner}


def _facet(raw: Any, mapping: Mapping[str, str], *, terminal_raw: set[str], owner: str,
           entity_type: str, entity_id: str, facet: str,
           diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    key = str(raw) if raw is not None else None
    known = key in mapping
    state = mapping[key] if known else "unknown"
    if not known:
        diagnostics.append({
            "code": "unknown-state",
            "entityType": entity_type,
            "entityId": entity_id,
            "facet": facet,
            "raw": raw,
            "message": f"Unrecognized {facet} state is preserved without inference.",
        })
    return {
        "rawState": raw, "state": state, "known": known, "terminal": key in terminal_raw,
        "owner": owner,
    }


def _entity(entity_type: str, entity_id: Any, *, parent: dict[str, str] | None = None
            ) -> dict[str, Any]:
    default_owners = {
        "execution": "factory-scheduler", "completion": "factory-scheduler",
        "disposition": "factory-scheduler", "validation": "validator-policy",
        "acceptance": "operator", "publication": "factory-dossier-publisher",
    }
    return {
        "entityType": entity_type, "entityId": str(entity_id), "parent": parent,
        "facets": {name: _na(default_owners[name]) for name in FACETS}, "diagnostics": [],
    }


def _set(entity: dict[str, Any], facet: str, raw: Any, mapping: Mapping[str, str], *,
         terminal_raw: set[str], owner: str, diagnostics: list[dict[str, Any]]) -> None:
    value = _facet(
        raw, mapping, terminal_raw=terminal_raw, owner=owner,
        entity_type=entity["entityType"], entity_id=entity["entityId"], facet=facet,
        diagnostics=entity["diagnostics"],
    )
    entity["facets"][facet] = value
    if not value["known"]:
        diagnostics.append(entity["diagnostics"][-1])


def _completion(raw: Any, *, active: set[str], complete: set[str], terminal_raw: set[str], owner: str,
                entity: dict[str, Any], diagnostics: list[dict[str, Any]]) -> None:
    mapping = {**{state: "incomplete" for state in active},
               **{state: "completed" for state in complete}}
    _set(entity, "completion", raw, mapping, terminal_raw=terminal_raw,
         owner=owner, diagnostics=diagnostics)


def _disposition(raw: Any, mapping: Mapping[str, str], *, terminal_raw: set[str], owner: str,
                 entity: dict[str, Any],
                 diagnostics: list[dict[str, Any]]) -> None:
    _set(entity, "disposition", raw, mapping, terminal_raw=terminal_raw,
         owner=owner, diagnostics=diagnostics)


def project_outcomes(*, run: Mapping[str, Any] | None,
                     workers: Iterable[Mapping[str, Any]],
                     work_items: Iterable[Mapping[str, Any]],
                     memory: Mapping[str, Any],
                     dossiers: Iterable[Mapping[str, Any]],
                     pending_questions: Iterable[Mapping[str, Any]],
                     source_watermarks: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the complete v1 projection from canonical, already-redacted inputs."""
    entities: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    run_id = str((run or {}).get("id", "missing-run"))
    run_parent = {"entityType": "run", "entityId": run_id}
    memory_diagnostics = sorted({str(value) for value in memory.get("diagnostics") or []})
    if memory.get("degraded") and not memory_diagnostics:
        memory_diagnostics = ["Project-memory replay reported degraded state without detail."]
    diagnostics.extend({
        "code": "project-memory-source-degraded",
        "entityType": "project-memory",
        "entityId": run_id,
        "source": "project-memory",
        "message": message,
    } for message in memory_diagnostics)

    if run is None:
        diagnostics.append({
            "code": "missing-run", "entityType": "run", "entityId": run_id,
            "message": "Canonical run row is absent; child state is not promoted.",
        })
    else:
        item = _entity("run", run_id)
        raw = run.get("status")
        run_terminal = {"completed", "partial", "failed", "blocked", "cancelled", "canceled"}
        _set(item, "execution", raw, _RUN_EXECUTION, terminal_raw=run_terminal,
             owner="factory-scheduler", diagnostics=diagnostics)
        _completion(raw, active={"queued", "running", "needs_input"},
                    complete={"completed", "partial", "failed", "blocked", "cancelled", "canceled"},
                    terminal_raw=run_terminal, owner="factory-scheduler", entity=item,
                    diagnostics=diagnostics)
        _disposition(raw, {
            "queued": "deferred", "running": "deferred", "needs_input": "needs-review",
            "completed": "successful", "partial": "mixed", "failed": "failed",
            "blocked": "blocked", "cancelled": "cancelled", "canceled": "cancelled",
        }, terminal_raw=run_terminal, owner="factory-scheduler", entity=item,
                     diagnostics=diagnostics)
        entities.append(item)

    for worker in workers:
        item = _entity("worker", worker.get("id", "missing-worker"), parent=run_parent)
        raw = worker.get("status")
        worker_terminal = {"done", "failed", "cancelled", "canceled"}
        _set(item, "execution", raw, _WORK_EXECUTION, terminal_raw=worker_terminal,
             owner="factory-scheduler", diagnostics=diagnostics)
        _completion(raw, active={"queued", "running", "blocked"},
                    complete={"done", "failed", "cancelled", "canceled"},
                    terminal_raw=worker_terminal, owner="factory-scheduler", entity=item,
                    diagnostics=diagnostics)
        _disposition(raw, {
            "queued": "deferred", "running": "deferred", "blocked": "blocked",
            "done": "successful", "failed": "failed", "cancelled": "cancelled",
            "canceled": "cancelled",
        }, terminal_raw=worker_terminal, owner="factory-scheduler", entity=item,
                     diagnostics=diagnostics)
        entities.append(item)

    for work in work_items:
        item = _entity("work-item", work.get("id", "missing-work"), parent=run_parent)
        raw = work.get("status")
        # All three facets are derived from Muster's durable status. A future separate result
        # entity may assign narrower authority to an explicitly attested Rekit result.
        owner = "muster"
        work_terminal = {"done", "failed", "cancelled", "canceled"}
        _set(item, "execution", raw, _WORK_EXECUTION, terminal_raw=work_terminal,
             owner=owner, diagnostics=diagnostics)
        _completion(raw, active={"queued", "running", "blocked"},
                    complete={"done", "failed", "cancelled", "canceled"},
                    terminal_raw=work_terminal, owner=owner, entity=item,
                    diagnostics=diagnostics)
        _disposition(raw, {
            "queued": "deferred", "running": "deferred", "blocked": "blocked",
            "done": "successful", "failed": "failed", "cancelled": "cancelled",
            "canceled": "cancelled",
        }, terminal_raw=work_terminal, owner=owner, entity=item,
                     diagnostics=diagnostics)
        entities.append(item)

    hypotheses = memory.get("hypotheses") or {}
    for hypothesis_id, hypothesis in hypotheses.items():
        item = _entity("hypothesis", hypothesis_id, parent=run_parent)
        raw = hypothesis.get("status")
        _set(item, "validation", raw, _HYPOTHESIS_VALIDATION,
             terminal_raw={"retired"}, owner="validator-policy",
             diagnostics=diagnostics)
        _disposition(raw, {
            "proposed": "deferred", "queued": "deferred", "testing": "deferred",
            "supported": "successful", "reproduced": "successful",
            "contradicted": "mixed", "disproved": "failed", "blocked": "blocked",
            "retired": "cancelled",
        }, terminal_raw={"retired"}, owner="validator-policy", entity=item,
                     diagnostics=diagnostics)
        entities.append(item)

    decisions_by_finding: dict[str, list[Mapping[str, Any]]] = {}
    for decision in (memory.get("finding_operator_decisions") or {}).values():
        decisions_by_finding.setdefault(str(decision.get("findingId")), []).append(decision)

    dossiers_by_finding: dict[str, list[Mapping[str, Any]]] = {}
    dossier_values = list(dossiers)
    for dossier in dossier_values:
        dossiers_by_finding.setdefault(str(dossier.get("findingId")), []).append(dossier)

    findings = memory.get("findings") or {}
    for finding_id, finding in findings.items():
        item = _entity("finding", finding_id, parent=run_parent)
        raw = finding.get("status")
        _set(item, "validation", raw, _FINDING_VALIDATION,
             terminal_raw={"rejected", "withdrawn"}, owner="validator-policy",
             diagnostics=diagnostics)
        _completion(raw,
                    active={"lead", "candidate", "demonstrated", "reproduction-pending",
                            "inconclusive"},
                    complete={"reproduced", "rejected", "withdrawn"},
                    terminal_raw={"rejected", "withdrawn"}, owner="validator-policy",
                    entity=item, diagnostics=diagnostics)
        _disposition(raw, {
            "lead": "needs-review", "candidate": "needs-review",
            "demonstrated": "needs-review", "reproduction-pending": "deferred",
            "reproduced": "successful", "inconclusive": "needs-review",
            "rejected": "failed", "withdrawn": "cancelled",
        }, terminal_raw={"rejected", "withdrawn"}, owner="validator-policy", entity=item,
                     diagnostics=diagnostics)
        decisions = sorted(decisions_by_finding.get(str(finding_id), []),
                           key=lambda value: (value.get("_eventSeq", 0), str(value.get("id", ""))))
        if decisions:
            _set(item, "acceptance", decisions[-1].get("decision"), _ACCEPTANCE,
                 terminal_raw=set(_ACCEPTANCE), owner="operator", diagnostics=diagnostics)
        else:
            item["facets"]["acceptance"] = {
                "rawState": None, "state": "undecided", "known": True, "terminal": False,
                "owner": "operator",
            }
        publication = dossiers_by_finding.get(str(finding_id), [])
        item["facets"]["publication"] = {
            "rawState": [value.get("id") for value in sorted(
                publication, key=lambda value: str(value.get("id", "")))],
            "state": "published" if publication else "unpublished", "known": True,
            "terminal": bool(publication), "owner": "factory-dossier-publisher",
        }
        entities.append(item)

    for attempt_id, attempt in (memory.get("finding_attempts") or {}).items():
        parent = {"entityType": "finding", "entityId": str(attempt.get("findingId", ""))}
        item = _entity("validation", attempt_id, parent=parent)
        _set(item, "validation", attempt.get("outcome"), _VALIDATION_ATTEMPT,
             terminal_raw=set(_VALIDATION_ATTEMPT), owner="validator-policy",
             diagnostics=diagnostics)
        entities.append(item)

    for dossier in dossier_values:
        parent = {"entityType": "finding", "entityId": str(dossier.get("findingId", ""))}
        item = _entity("proof-bundle", dossier.get("id", "missing-dossier"), parent=parent)
        raw = dossier.get("verificationStatus")
        if raw == "published":
            item["facets"]["validation"] = {
                "rawState": None, "state": "unknown", "known": False, "terminal": False,
                "owner": "offline-proof-verifier",
            }
        else:
            _set(item, "validation", raw, _DOSSIER_VALIDATION,
                 terminal_raw=set(_DOSSIER_VALIDATION), owner="offline-proof-verifier",
                 diagnostics=diagnostics)
        item["facets"]["publication"] = {
            "rawState": "published", "state": "published",
            "known": True, "terminal": True,
            "owner": "factory-dossier-publisher",
        }
        entities.append(item)

    for question in pending_questions:
        item = _entity("operator-decision", question.get("id", "missing-question"), parent=run_parent)
        item["facets"]["disposition"] = {
            "rawState": "pending", "state": "needs-review", "known": True, "terminal": False,
            "owner": "operator",
        }
        item["facets"]["acceptance"] = {
            "rawState": None, "state": "undecided", "known": True, "terminal": False,
            "owner": "operator",
        }
        entities.append(item)

    for decision in (memory.get("finding_operator_decisions") or {}).values():
        parent = {"entityType": "finding", "entityId": str(decision.get("findingId", ""))}
        item = _entity("operator-decision", decision.get("id", "missing-decision"), parent=parent)
        raw = decision.get("decision")
        _set(item, "acceptance", raw, _ACCEPTANCE, terminal_raw=set(_ACCEPTANCE),
             owner="operator", diagnostics=diagnostics)
        item["facets"]["disposition"] = {
            "rawState": raw,
            "state": ({"accepted": "successful", "rejected": "failed", "waived": "needs-review"}
                      .get(str(raw), "unknown")),
            "known": raw in _ACCEPTANCE, "terminal": raw in _ACCEPTANCE, "owner": "operator",
        }
        entities.append(item)

    entities.sort(key=lambda value: (value["entityType"], value["entityId"]))
    diagnostics.sort(key=lambda value: (
        value.get("entityType", ""), value.get("entityId", ""), value.get("facet", ""),
        value.get("code", ""), str(value.get("raw", "")),
    ))
    for item in entities:
        item["diagnostics"].sort(key=lambda value: (
            value.get("facet", ""), value.get("code", ""), str(value.get("raw", "")),
        ))
    known_entities = {(item["entityType"], item["entityId"]) for item in entities}
    for item in entities:
        parent = item.get("parent")
        if parent and (parent["entityType"], parent["entityId"]) not in known_entities:
            diagnostic = {
                "code": "dangling-parent", "entityType": item["entityType"],
                "entityId": item["entityId"], "parent": parent,
                "message": "Parent is absent; child state is preserved without promotion.",
            }
            item["diagnostics"].append(diagnostic)
            diagnostics.append(diagnostic)
        item["diagnostics"].sort(key=lambda value: (
            value.get("facet", ""), value.get("code", ""), str(value.get("raw", "")),
        ))
    diagnostics.sort(key=lambda value: (
        value.get("entityType", ""), value.get("entityId", ""), value.get("facet", ""),
        value.get("code", ""), str(value.get("raw", "")),
    ))
    projection = {
        "schemaVersion": SCHEMA_VERSION,
        "vocabularyVersion": VOCABULARY_VERSION,
        "facets": list(FACETS),
        "authorities": {key: AUTHORITIES[key] for key in sorted(AUTHORITIES)},
        "entities": entities,
        "diagnostics": diagnostics,
        "degraded": bool(diagnostics),
        "sourceWatermarks": dict(source_watermarks or {}),
        "consistency": {
            "mode": "full-fold", "ledgerRead": "single-read-transaction",
            "projectMemoryRead": "external-replay",
            "watermarksAreProjectionIdentity": False,
            "incrementalProjection": "deferred",
        },
    }
    # Snapshot first so mutable canonical inputs cannot change the returned meaning after
    # the identity is computed. The returned object remains ordinary public JSON; a caller
    # mutation is intentionally detectable by the standalone verifier.
    detached = _json_snapshot(projection)
    detached[SEMANTIC_IDENTITY_FIELD] = outcome_semantic_sha256(detached)
    return detached
