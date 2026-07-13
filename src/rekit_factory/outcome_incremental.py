"""Pure in-memory incremental fold for canonical Factory outcome sources.

This is a parity reference, not a production cache. Source changes update a canonical source
snapshot and refold only directly affected intrinsic entities. Shared outcome primitives then
materialize global ordering, dangling-parent diagnostics, consistency semantics, and identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable, Literal, Mapping, Self

from rekit_factory.outcomes import (
    SCHEMA_VERSION,
    _ACCEPTANCE,
    _DOSSIER_VALIDATION,
    _FINDING_VALIDATION,
    _HYPOTHESIS_VALIDATION,
    _RUN_EXECUTION,
    _VALIDATION_ATTEMPT,
    _WORK_EXECUTION,
    _completion,
    _disposition,
    _entity,
    _fold_report,
    _finalize_outcome_projection,
    _json_snapshot,
    _set,
    _source_diagnostics,
    _validate_operator_decision_identity_uniqueness,
)


SOURCE_CHANGE_VERSION = "factory-outcome-source-change/v1"
SOURCE_SNAPSHOT_VERSION = "factory-outcome-source-state/v1"
_SOURCE_KINDS = frozenset({
    "run", "worker", "work-item", "project-memory", "dossier", "pending-decision",
})
_OPERATIONS = frozenset({"upsert", "remove"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SNAPSHOT_FIELDS = frozenset({
    "schemaVersion", "sourceVersion", "run", "workers", "workItems", "projectMemory",
    "dossiers", "pendingDecisions", "sourceWatermarks", "sourceHeads", "changeReceipts",
})
EntityKey = tuple[str, str]
StreamKey = tuple[str, str]


class OutcomeSourceChangeError(ValueError):
    """A source change or canonical source snapshot is invalid."""


class OutcomeSourceChangeConflict(OutcomeSourceChangeError):
    """A change ID or source revision was reused for different canonical content."""


def _identifier(value: Any, name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise OutcomeSourceChangeError(f"{name} must be a stable identifier")
    return value


def _record(value: Any, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise OutcomeSourceChangeError(f"{name} must be a JSON object")
    try:
        return _json_snapshot(value, path=name)
    except (TypeError, ValueError) as exc:
        raise OutcomeSourceChangeError(str(exc)) from exc


def _record_id(value: Mapping[str, Any], name: str) -> str:
    return _identifier(value.get("id"), f"{name}.id")


def _validate_memory(value: dict[str, Any]) -> dict[str, Any]:
    for field in (
        "hypotheses", "findings", "finding_attempts", "finding_operator_decisions",
    ):
        collection = value.get(field, {})
        if type(collection) is not dict:
            raise OutcomeSourceChangeError(f"project-memory.{field} must be a JSON object")
        for key, item in collection.items():
            identifier = _identifier(key, f"project-memory.{field} key")
            record = _record(item, f"project-memory.{field}.{identifier}")
            if "id" in record and _record_id(record, f"project-memory.{field}.{identifier}") \
                    != identifier:
                raise OutcomeSourceChangeError(
                    f"project-memory.{field} key must match record id"
                )
            if field == "finding_operator_decisions":
                if "id" not in record:
                    raise OutcomeSourceChangeError("finding operator decisions require id")
                if type(record.get("_eventSeq", 0)) is not int:
                    raise OutcomeSourceChangeError("finding decision _eventSeq must be an integer")
            if field in {"finding_attempts", "finding_operator_decisions"}:
                _identifier(record.get("findingId"), f"project-memory.{field}.findingId")
    diagnostics = value.get("diagnostics", [])
    if type(diagnostics) is not list:
        raise OutcomeSourceChangeError("project-memory.diagnostics must be a JSON array")
    if "degraded" in value and type(value["degraded"]) is not bool:
        raise OutcomeSourceChangeError("project-memory.degraded must be a boolean")
    return value


@dataclass(frozen=True)
class OutcomeSourceChangeV1:
    """Strict, revisioned upsert/removal of one canonical outcome source."""

    schema_version: int
    source_version: str
    change_id: str
    source_kind: Literal[
        "run", "worker", "work-item", "project-memory", "dossier", "pending-decision"
    ]
    source_id: str
    source_revision: int
    operation: Literal["upsert", "remove"]
    value: dict[str, Any] | None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise OutcomeSourceChangeError("schemaVersion must be 1")
        if self.source_version != SOURCE_CHANGE_VERSION:
            raise OutcomeSourceChangeError(f"sourceVersion must be {SOURCE_CHANGE_VERSION}")
        _identifier(self.change_id, "changeId")
        if type(self.source_kind) is not str or self.source_kind not in _SOURCE_KINDS:
            raise OutcomeSourceChangeError("sourceKind is unsupported")
        _identifier(self.source_id, "sourceId")
        if self.source_kind == "run" and self.source_id != "run":
            raise OutcomeSourceChangeError("run changes must use sourceId 'run'")
        if self.source_kind == "project-memory" and self.source_id != "project-memory":
            raise OutcomeSourceChangeError(
                "project-memory changes must use sourceId 'project-memory'"
            )
        if type(self.source_revision) is not int or self.source_revision < 1:
            raise OutcomeSourceChangeError("sourceRevision must be a positive integer")
        if type(self.operation) is not str or self.operation not in _OPERATIONS:
            raise OutcomeSourceChangeError("operation must be upsert or remove")
        if self.operation == "remove":
            if self.value is not None:
                raise OutcomeSourceChangeError("remove changes must have null value")
            return
        record = _record(self.value, "value")
        if self.source_kind == "project-memory":
            record = _validate_memory(record)
        else:
            identifier = _record_id(record, "value")
            if self.source_kind != "run" and identifier != self.source_id:
                raise OutcomeSourceChangeError("sourceId must match value.id")
            if self.source_kind == "dossier":
                _identifier(record.get("findingId"), "value.findingId")
        object.__setattr__(self, "value", record)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "sourceVersion": self.source_version,
            "changeId": self.change_id,
            "sourceKind": self.source_kind,
            "sourceId": self.source_id,
            "sourceRevision": self.source_revision,
            "operation": self.operation,
            "value": _json_snapshot(self.value),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        if type(value) is not dict:
            raise OutcomeSourceChangeError("source change must be a JSON object")
        fields = {
            "schemaVersion", "sourceVersion", "changeId", "sourceKind", "sourceId",
            "sourceRevision", "operation", "value",
        }
        if set(value) != fields:
            raise OutcomeSourceChangeError("source change fields must match the v1 envelope")
        return cls(
            schema_version=value["schemaVersion"], source_version=value["sourceVersion"],
            change_id=value["changeId"], source_kind=value["sourceKind"],
            source_id=value["sourceId"], source_revision=value["sourceRevision"],
            operation=value["operation"], value=value["value"],
        )

    @property
    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(), allow_nan=False, ensure_ascii=False, separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


@dataclass
class _SourceState:
    run: dict[str, Any] | None
    workers: dict[str, dict[str, Any]]
    work_items: dict[str, dict[str, Any]]
    memory: dict[str, Any]
    dossiers: dict[str, dict[str, Any]]
    pending_decisions: dict[str, dict[str, Any]]
    source_watermarks: dict[str, Any]

    @classmethod
    def empty(cls, source_watermarks: Mapping[str, Any] | None = None) -> Self:
        return cls(None, {}, {}, {}, {}, {}, _record(dict(source_watermarks or {}), "watermarks"))

    def copy(self) -> Self:
        return type(self)(
            self.run, dict(self.workers), dict(self.work_items), self.memory,
            dict(self.dossiers), dict(self.pending_decisions), self.source_watermarks,
        )


def _run_id(state: _SourceState) -> str:
    return str((state.run or {}).get("id", "missing-run"))


def _fold_run(record: Mapping[str, Any]) -> dict[str, Any]:
    item = _entity("run", record["id"])
    diagnostics: list[dict[str, Any]] = []
    raw = record.get("status")
    terminal = {"completed", "partial", "failed", "blocked", "cancelled", "canceled"}
    _set(item, "execution", raw, _RUN_EXECUTION, terminal_raw=terminal,
         owner="factory-scheduler", diagnostics=diagnostics)
    _completion(raw, active={"queued", "running", "needs_input"}, complete=terminal,
                terminal_raw=terminal, owner="factory-scheduler", entity=item,
                diagnostics=diagnostics)
    _disposition(raw, {
        "queued": "deferred", "running": "deferred", "needs_input": "needs-review",
        "completed": "successful", "partial": "mixed", "failed": "failed",
        "blocked": "blocked", "cancelled": "cancelled", "canceled": "cancelled",
    }, terminal_raw=terminal, owner="factory-scheduler", entity=item,
        diagnostics=diagnostics)
    return item


def _fold_worker(record: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    item = _entity("worker", record["id"], parent={"entityType": "run", "entityId": run_id})
    diagnostics: list[dict[str, Any]] = []
    raw = record.get("status")
    terminal = {"done", "failed", "cancelled", "canceled"}
    _set(item, "execution", raw, _WORK_EXECUTION, terminal_raw=terminal,
         owner="factory-scheduler", diagnostics=diagnostics)
    _completion(raw, active={"queued", "running", "blocked"}, complete=terminal,
                terminal_raw=terminal, owner="factory-scheduler", entity=item,
                diagnostics=diagnostics)
    _disposition(raw, {
        "queued": "deferred", "running": "deferred", "blocked": "blocked",
        "done": "successful", "failed": "failed", "cancelled": "cancelled",
        "canceled": "cancelled",
    }, terminal_raw=terminal, owner="factory-scheduler", entity=item,
        diagnostics=diagnostics)
    return item


def _fold_work_item(record: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    item = _entity(
        "work-item", record["id"], parent={"entityType": "run", "entityId": run_id},
    )
    diagnostics: list[dict[str, Any]] = []
    raw = record.get("status")
    terminal = {"done", "failed", "cancelled", "canceled"}
    _set(item, "execution", raw, _WORK_EXECUTION, terminal_raw=terminal,
         owner="muster", diagnostics=diagnostics)
    _completion(raw, active={"queued", "running", "blocked"}, complete=terminal,
                terminal_raw=terminal, owner="muster", entity=item, diagnostics=diagnostics)
    _disposition(raw, {
        "queued": "deferred", "running": "deferred", "blocked": "blocked",
        "done": "successful", "failed": "failed", "cancelled": "cancelled",
        "canceled": "cancelled",
    }, terminal_raw=terminal, owner="muster", entity=item, diagnostics=diagnostics)
    return item


def _fold_hypothesis(identifier: str, record: Mapping[str, Any],
                     run_id: str) -> dict[str, Any]:
    item = _entity(
        "hypothesis", identifier, parent={"entityType": "run", "entityId": run_id},
    )
    diagnostics: list[dict[str, Any]] = []
    raw = record.get("status")
    _set(item, "validation", raw, _HYPOTHESIS_VALIDATION, terminal_raw={"retired"},
         owner="validator-policy", diagnostics=diagnostics)
    _disposition(raw, {
        "proposed": "deferred", "queued": "deferred", "testing": "deferred",
        "supported": "successful", "reproduced": "successful", "contradicted": "mixed",
        "disproved": "failed", "blocked": "blocked", "retired": "cancelled",
    }, terminal_raw={"retired"}, owner="validator-policy", entity=item,
        diagnostics=diagnostics)
    return item


def _fold_finding(identifier: str, record: Mapping[str, Any], run_id: str,
                  decisions: Iterable[Mapping[str, Any]],
                  dossiers: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    item = _entity(
        "finding", identifier, parent={"entityType": "run", "entityId": run_id},
    )
    diagnostics: list[dict[str, Any]] = []
    raw = record.get("status")
    terminal = {"rejected", "withdrawn"}
    _set(item, "validation", raw, _FINDING_VALIDATION, terminal_raw=terminal,
         owner="validator-policy", diagnostics=diagnostics)
    _completion(raw, active={
        "lead", "candidate", "demonstrated", "reproduction-pending", "inconclusive",
    }, complete={"reproduced", "rejected", "withdrawn"}, terminal_raw=terminal,
        owner="validator-policy", entity=item, diagnostics=diagnostics)
    _disposition(raw, {
        "lead": "needs-review", "candidate": "needs-review",
        "demonstrated": "needs-review", "reproduction-pending": "deferred",
        "reproduced": "successful", "inconclusive": "needs-review",
        "rejected": "failed", "withdrawn": "cancelled",
    }, terminal_raw=terminal, owner="validator-policy", entity=item,
        diagnostics=diagnostics)
    ordered_decisions = sorted(
        decisions, key=lambda value: (value.get("_eventSeq", 0), str(value.get("id", ""))),
    )
    if ordered_decisions:
        _set(item, "acceptance", ordered_decisions[-1].get("decision"), _ACCEPTANCE,
             terminal_raw=set(_ACCEPTANCE), owner="operator", diagnostics=diagnostics)
    else:
        item["facets"]["acceptance"] = {
            "rawState": None, "state": "undecided", "known": True, "terminal": False,
            "owner": "operator",
        }
    publication = sorted(dossiers, key=lambda value: str(value.get("id", "")))
    item["facets"]["publication"] = {
        "rawState": [value.get("id") for value in publication],
        "state": "published" if publication else "unpublished", "known": True,
        "terminal": bool(publication), "owner": "factory-dossier-publisher",
    }
    return item


def _fold_attempt(identifier: str, record: Mapping[str, Any]) -> dict[str, Any]:
    parent = {"entityType": "finding", "entityId": str(record.get("findingId", ""))}
    item = _entity("validation", identifier, parent=parent)
    diagnostics: list[dict[str, Any]] = []
    _set(item, "validation", record.get("outcome"), _VALIDATION_ATTEMPT,
         terminal_raw=set(_VALIDATION_ATTEMPT), owner="validator-policy",
         diagnostics=diagnostics)
    return item


def _fold_dossier(record: Mapping[str, Any]) -> dict[str, Any]:
    parent = {"entityType": "finding", "entityId": str(record.get("findingId", ""))}
    item = _entity("proof-bundle", record["id"], parent=parent)
    diagnostics: list[dict[str, Any]] = []
    raw = record.get("verificationStatus")
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
        "rawState": "published", "state": "published", "known": True, "terminal": True,
        "owner": "factory-dossier-publisher",
    }
    return item


def _fold_pending(record: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    item = _entity(
        "operator-decision", record["id"],
        parent={"entityType": "run", "entityId": run_id},
    )
    item["facets"]["disposition"] = {
        "rawState": "pending", "state": "needs-review", "known": True,
        "terminal": False, "owner": "operator",
    }
    item["facets"]["acceptance"] = {
        "rawState": None, "state": "undecided", "known": True, "terminal": False,
        "owner": "operator",
    }
    return item


def _fold_decision(identifier: str, record: Mapping[str, Any]) -> dict[str, Any]:
    parent = {"entityType": "finding", "entityId": str(record.get("findingId", ""))}
    item = _entity("operator-decision", identifier, parent=parent)
    diagnostics: list[dict[str, Any]] = []
    raw = record.get("decision")
    _set(item, "acceptance", raw, _ACCEPTANCE, terminal_raw=set(_ACCEPTANCE),
         owner="operator", diagnostics=diagnostics)
    key = str(raw)
    item["facets"]["disposition"] = {
        "rawState": raw,
        "state": {"accepted": "successful", "rejected": "failed",
                  "waived": "needs-review"}.get(key, "unknown"),
        "known": key in _ACCEPTANCE, "terminal": key in _ACCEPTANCE, "owner": "operator",
    }
    return item


def _memory_map(state: _SourceState, field: str) -> dict[str, dict[str, Any]]:
    return state.memory.get(field) or {}


def _decisions_for(state: _SourceState, finding_id: str) -> list[Mapping[str, Any]]:
    return [
        value for value in _memory_map(state, "finding_operator_decisions").values()
        if str(value.get("findingId")) == finding_id
    ]


def _dossiers_for(state: _SourceState, finding_id: str) -> list[Mapping[str, Any]]:
    return [
        value for value in state.dossiers.values()
        if str(value.get("findingId")) == finding_id
    ]


def _all_entity_keys(state: _SourceState) -> set[EntityKey]:
    keys: set[EntityKey] = set()
    if state.run is not None:
        keys.add(("run", str(state.run["id"])))
    keys.update(("worker", value) for value in state.workers)
    keys.update(("work-item", value) for value in state.work_items)
    keys.update(
        ("report", identifier) for identifier, value in state.work_items.items()
        if _fold_report(value) is not None
    )
    keys.update(("hypothesis", value) for value in _memory_map(state, "hypotheses"))
    keys.update(("finding", value) for value in _memory_map(state, "findings"))
    keys.update(("validation", value) for value in _memory_map(state, "finding_attempts"))
    keys.update(("proof-bundle", value) for value in state.dossiers)
    keys.update(("operator-decision", value) for value in state.pending_decisions)
    keys.update(
        ("operator-decision", value)
        for value in _memory_map(state, "finding_operator_decisions")
    )
    return keys


def _validate_cross_source_identities(state: _SourceState) -> None:
    try:
        _validate_operator_decision_identity_uniqueness(
            state.memory, state.pending_decisions.values(),
        )
    except ValueError as exc:
        raise OutcomeSourceChangeError(str(exc)) from exc


def _refold_key(state: _SourceState, key: EntityKey) -> dict[str, Any] | None:
    kind, identifier = key
    run_id = _run_id(state)
    if kind == "run":
        if state.run is None or str(state.run["id"]) != identifier:
            return None
        return _fold_run(state.run)
    if kind == "worker":
        record = state.workers.get(identifier)
        return _fold_worker(record, run_id) if record else None
    if kind == "work-item":
        record = state.work_items.get(identifier)
        return _fold_work_item(record, run_id) if record else None
    if kind == "report":
        record = state.work_items.get(identifier)
        return _fold_report(record) if record else None
    if kind == "hypothesis":
        record = _memory_map(state, "hypotheses").get(identifier)
        return _fold_hypothesis(identifier, record, run_id) if record else None
    if kind == "finding":
        record = _memory_map(state, "findings").get(identifier)
        return _fold_finding(
            identifier, record, run_id, _decisions_for(state, identifier),
            _dossiers_for(state, identifier),
        ) if record else None
    if kind == "validation":
        record = _memory_map(state, "finding_attempts").get(identifier)
        return _fold_attempt(identifier, record) if record else None
    if kind == "proof-bundle":
        record = state.dossiers.get(identifier)
        return _fold_dossier(record) if record else None
    if kind == "operator-decision":
        pending = state.pending_decisions.get(identifier)
        if pending:
            return _fold_pending(pending, run_id)
        decision = _memory_map(state, "finding_operator_decisions").get(identifier)
        return _fold_decision(identifier, decision) if decision else None
    raise AssertionError(f"unsupported entity key {key!r}")


def _fold_all(state: _SourceState) -> dict[EntityKey, dict[str, Any]]:
    _validate_cross_source_identities(state)
    return {
        key: entity
        for key in sorted(_all_entity_keys(state))
        if (entity := _refold_key(state, key)) is not None
    }


def _changed_map_keys(old: Mapping[str, Any], new: Mapping[str, Any]) -> set[str]:
    return {
        key for key in set(old) | set(new)
        if old.get(key) != new.get(key)
    }


def _affected_keys(old: _SourceState, new: _SourceState,
                   change: OutcomeSourceChangeV1) -> set[EntityKey]:
    if old.run != new.run:
        if _run_id(old) != _run_id(new):
            return _all_entity_keys(old) | _all_entity_keys(new)
        return {
            key for key in _all_entity_keys(old) | _all_entity_keys(new)
            if key[0] == "run"
        }
    if change.source_kind == "worker":
        return {("worker", change.source_id)} if old.workers != new.workers else set()
    if change.source_kind == "work-item":
        return {
            (kind, change.source_id) for kind in ("work-item", "report")
        } if old.work_items != new.work_items else set()
    if change.source_kind == "pending-decision":
        return {("operator-decision", change.source_id)} \
            if old.pending_decisions != new.pending_decisions else set()
    if change.source_kind == "dossier":
        if old.dossiers == new.dossiers:
            return set()
        affected: set[EntityKey] = {("proof-bundle", change.source_id)}
        old_record = old.dossiers.get(change.source_id)
        new_record = new.dossiers.get(change.source_id)
        old_parent = str(old_record.get("findingId")) if old_record else None
        new_parent = str(new_record.get("findingId")) if new_record else None
        if old_record is None or new_record is None or old_parent != new_parent:
            affected.update(
                ("finding", parent) for parent in {old_parent, new_parent} if parent is not None
            )
        return affected
    if change.source_kind == "project-memory":
        if old.memory == new.memory:
            return set()
        affected = set()
        for field, entity_type in (
            ("hypotheses", "hypothesis"), ("findings", "finding"),
            ("finding_attempts", "validation"),
        ):
            affected.update(
                (entity_type, identifier) for identifier in _changed_map_keys(
                    _memory_map(old, field), _memory_map(new, field),
                )
            )
        old_decisions = _memory_map(old, "finding_operator_decisions")
        new_decisions = _memory_map(new, "finding_operator_decisions")
        for identifier in _changed_map_keys(old_decisions, new_decisions):
            affected.add(("operator-decision", identifier))
            for record in (old_decisions.get(identifier), new_decisions.get(identifier)):
                if record is not None:
                    affected.add(("finding", str(record.get("findingId", ""))))
        return affected
    return set()


def _stream_key(change: OutcomeSourceChangeV1) -> StreamKey:
    return (change.source_kind, change.source_id)


def _apply_to_source(state: _SourceState, change: OutcomeSourceChangeV1) -> _SourceState:
    candidate = state.copy()
    value = _json_snapshot(change.value) if change.operation == "upsert" else None
    if change.source_kind == "run":
        candidate.run = value
    elif change.source_kind == "project-memory":
        candidate.memory = value or {}
    else:
        collection = {
            "worker": candidate.workers,
            "work-item": candidate.work_items,
            "dossier": candidate.dossiers,
            "pending-decision": candidate.pending_decisions,
        }[change.source_kind]
        if value is None:
            collection.pop(change.source_id, None)
        else:
            collection[change.source_id] = value
    _validate_cross_source_identities(candidate)
    return candidate


def _list_to_map(value: Any, name: str) -> dict[str, dict[str, Any]]:
    if type(value) is not list:
        raise OutcomeSourceChangeError(f"{name} must be a JSON array")
    result: dict[str, dict[str, Any]] = {}
    for item in value:
        record = _record(item, name)
        identifier = _record_id(record, name)
        if identifier in result:
            raise OutcomeSourceChangeError(f"{name} contains duplicate id {identifier!r}")
        result[identifier] = record
    return result


def _state_from_snapshot(snapshot: Mapping[str, Any]) -> _SourceState:
    if type(snapshot) is not dict or set(snapshot) != _SNAPSHOT_FIELDS:
        raise OutcomeSourceChangeError("source snapshot fields must match the v1 envelope")
    if type(snapshot["schemaVersion"]) is not int or snapshot["schemaVersion"] != SCHEMA_VERSION:
        raise OutcomeSourceChangeError("source snapshot schemaVersion must be 1")
    if snapshot["sourceVersion"] != SOURCE_SNAPSHOT_VERSION:
        raise OutcomeSourceChangeError(
            f"source snapshot sourceVersion must be {SOURCE_SNAPSHOT_VERSION}"
        )
    run = snapshot["run"]
    if run is not None:
        run = _record(run, "run")
        _record_id(run, "run")
    state = _SourceState(
        run=run,
        workers=_list_to_map(snapshot["workers"], "workers"),
        work_items=_list_to_map(snapshot["workItems"], "workItems"),
        memory=_validate_memory(_record(snapshot["projectMemory"], "projectMemory")),
        dossiers=_list_to_map(snapshot["dossiers"], "dossiers"),
        pending_decisions=_list_to_map(snapshot["pendingDecisions"], "pendingDecisions"),
        source_watermarks=_record(snapshot["sourceWatermarks"], "sourceWatermarks"),
    )
    for record in state.dossiers.values():
        _identifier(record.get("findingId"), "dossier.findingId")
    _validate_cross_source_identities(state)
    return state


def _heads_from_snapshot(snapshot: Mapping[str, Any]) -> dict[StreamKey, int]:
    values = snapshot["sourceHeads"]
    if type(values) is not list:
        raise OutcomeSourceChangeError("sourceHeads must be a JSON array")
    heads: dict[StreamKey, int] = {}
    for value in values:
        if type(value) is not dict or set(value) != {
            "sourceKind", "sourceId", "sourceRevision",
        }:
            raise OutcomeSourceChangeError("source head fields must match the v1 envelope")
        kind = value["sourceKind"]
        source_id = value["sourceId"]
        revision = value["sourceRevision"]
        if type(kind) is not str or kind not in _SOURCE_KINDS:
            raise OutcomeSourceChangeError("source head kind is unsupported")
        _identifier(source_id, "source head sourceId")
        if kind == "run" and source_id != "run":
            raise OutcomeSourceChangeError("run source heads must use sourceId 'run'")
        if kind == "project-memory" and source_id != "project-memory":
            raise OutcomeSourceChangeError(
                "project-memory source heads must use sourceId 'project-memory'"
            )
        if type(revision) is not int or revision < 1:
            raise OutcomeSourceChangeError("source head revision must be a positive integer")
        stream = (kind, source_id)
        if stream in heads:
            raise OutcomeSourceChangeError("sourceHeads contains duplicate streams")
        heads[stream] = revision
    return heads


def _materialized_stream_value(state: _SourceState, stream: StreamKey) -> dict[str, Any] | None:
    kind, source_id = stream
    if kind == "run":
        return state.run
    if kind == "project-memory":
        return state.memory
    return {
        "worker": state.workers,
        "work-item": state.work_items,
        "dossier": state.dossiers,
        "pending-decision": state.pending_decisions,
    }[kind].get(source_id)


def _receipts_from_snapshot(
    snapshot: Mapping[str, Any], state: _SourceState, heads: Mapping[StreamKey, int],
) -> tuple[dict[str, bytes], dict[tuple[StreamKey, int], bytes]]:
    values = snapshot["changeReceipts"]
    if type(values) is not list:
        raise OutcomeSourceChangeError("changeReceipts must be a JSON array")
    change_receipts: dict[str, bytes] = {}
    revision_receipts: dict[tuple[StreamKey, int], bytes] = {}
    head_changes: dict[StreamKey, OutcomeSourceChangeV1] = {}
    for item in values:
        change = OutcomeSourceChangeV1.from_dict(item)
        payload = change.canonical_bytes
        if change.change_id in change_receipts:
            raise OutcomeSourceChangeError("changeReceipts contains duplicate changeId")
        stream = _stream_key(change)
        revision_key = (stream, change.source_revision)
        if revision_key in revision_receipts:
            raise OutcomeSourceChangeError("changeReceipts contains duplicate source revision")
        head = heads.get(stream)
        if head is None:
            raise OutcomeSourceChangeError("change receipt has no matching source head")
        if change.source_revision > head:
            raise OutcomeSourceChangeError("change receipt revision exceeds its source head")
        change_receipts[change.change_id] = payload
        revision_receipts[revision_key] = payload
        if change.source_revision == head:
            head_changes[stream] = change

    if set(head_changes) != set(heads):
        raise OutcomeSourceChangeError("every source head requires its exact change receipt")

    present_streams: set[StreamKey] = set()
    if state.run is not None:
        present_streams.add(("run", "run"))
    present_streams.update(("worker", source_id) for source_id in state.workers)
    present_streams.update(("work-item", source_id) for source_id in state.work_items)
    present_streams.update(("dossier", source_id) for source_id in state.dossiers)
    present_streams.update(
        ("pending-decision", source_id) for source_id in state.pending_decisions
    )
    if state.memory:
        present_streams.add(("project-memory", "project-memory"))
    if not present_streams <= set(heads):
        raise OutcomeSourceChangeError("materialized source record has no source head")

    for stream, change in head_changes.items():
        materialized = _materialized_stream_value(state, stream)
        expected = change.value if change.operation == "upsert" else (
            {} if stream == ("project-memory", "project-memory") else None
        )
        if materialized != expected:
            raise OutcomeSourceChangeError(
                "current source head does not match materialized source state"
            )
    return change_receipts, revision_receipts


class IncrementalOutcomeFold:
    """Genuine in-memory source accumulator with selective entity refolding."""

    def __init__(self, *, source_watermarks: Mapping[str, Any] | None = None) -> None:
        self._state = _SourceState.empty(source_watermarks)
        self._entities = _fold_all(self._state)
        self._change_receipts: dict[str, bytes] = {}
        self._revision_receipts: dict[tuple[StreamKey, int], bytes] = {}
        self._heads: dict[StreamKey, int] = {}
        self._last_refolded: tuple[EntityKey, ...] = ()

    @classmethod
    def from_source_snapshot(cls, snapshot: Mapping[str, Any]) -> Self:
        value = cls()
        value._state = _state_from_snapshot(snapshot)
        value._entities = _fold_all(value._state)
        value._heads = _heads_from_snapshot(snapshot)
        value._change_receipts, value._revision_receipts = _receipts_from_snapshot(
            snapshot, value._state, value._heads,
        )
        return value

    @property
    def last_refolded_entities(self) -> tuple[EntityKey, ...]:
        return self._last_refolded

    def apply(self, change: OutcomeSourceChangeV1 | Mapping[str, Any]) -> bool:
        if type(change) is dict:
            change = OutcomeSourceChangeV1.from_dict(change)
        if type(change) is not OutcomeSourceChangeV1:
            raise OutcomeSourceChangeError("change must be an exact OutcomeSourceChangeV1")
        payload = change.canonical_bytes
        previous = self._change_receipts.get(change.change_id)
        if previous is not None:
            if previous != payload:
                raise OutcomeSourceChangeConflict(
                    f"changeId {change.change_id!r} was reused for different content"
                )
            self._last_refolded = ()
            return False
        stream = _stream_key(change)
        revision_key = (stream, change.source_revision)
        revision_payload = self._revision_receipts.get(revision_key)
        if revision_payload is not None and revision_payload != payload:
            raise OutcomeSourceChangeConflict(
                f"source revision {change.source_revision} was reused for different content"
            )
        head = self._heads.get(stream, 0)
        if change.source_revision < head:
            self._change_receipts[change.change_id] = payload
            self._revision_receipts[revision_key] = payload
            self._last_refolded = ()
            return True
        if change.source_revision == head:
            raise OutcomeSourceChangeConflict(
                f"source revision {change.source_revision} conflicts with the current head"
            )

        candidate = _apply_to_source(self._state, change)
        affected = _affected_keys(self._state, candidate, change)
        replacements = {key: _refold_key(candidate, key) for key in affected}
        entities = dict(self._entities)
        for key, entity in replacements.items():
            if entity is None:
                entities.pop(key, None)
            else:
                entities[key] = entity
        if set(entities) != _all_entity_keys(candidate):
            raise AssertionError("selective refold did not cover the canonical entity key set")

        self._state = candidate
        self._entities = entities
        self._change_receipts[change.change_id] = payload
        self._revision_receipts[revision_key] = payload
        self._heads[stream] = change.source_revision
        self._last_refolded = tuple(sorted(affected))
        return True

    def apply_batch(self, changes: Iterable[OutcomeSourceChangeV1 | Mapping[str, Any]]) -> int:
        parsed = [
            OutcomeSourceChangeV1.from_dict(change) if type(change) is dict else change
            for change in changes
        ]
        if any(type(change) is not OutcomeSourceChangeV1 for change in parsed):
            raise OutcomeSourceChangeError("batch changes must be exact v1 envelopes")
        clone = self._clone()
        accepted = 0
        for change in sorted(parsed, key=lambda item: (
            _stream_key(item), item.source_revision, item.change_id,
        )):
            accepted += int(clone.apply(change))
        self._adopt(clone)
        return accepted

    def projection(self) -> dict[str, Any]:
        return _finalize_outcome_projection(
            entities=self._entities.values(),
            source_diagnostics=_source_diagnostics(self._state.run, self._state.memory),
            source_watermarks=self._state.source_watermarks,
        )

    def source_snapshot(self) -> dict[str, Any]:
        receipts = [json.loads(payload) for payload in self._change_receipts.values()]
        receipts.sort(key=lambda item: (
            item["sourceKind"], item["sourceId"], item["sourceRevision"], item["changeId"],
        ))
        return _json_snapshot({
            "schemaVersion": SCHEMA_VERSION,
            "sourceVersion": SOURCE_SNAPSHOT_VERSION,
            "run": self._state.run,
            "workers": [self._state.workers[key] for key in sorted(self._state.workers)],
            "workItems": [
                self._state.work_items[key] for key in sorted(self._state.work_items)
            ],
            "projectMemory": self._state.memory,
            "dossiers": [self._state.dossiers[key] for key in sorted(self._state.dossiers)],
            "pendingDecisions": [
                self._state.pending_decisions[key]
                for key in sorted(self._state.pending_decisions)
            ],
            "sourceWatermarks": self._state.source_watermarks,
            "sourceHeads": [
                {
                    "sourceKind": stream[0], "sourceId": stream[1],
                    "sourceRevision": self._heads[stream],
                }
                for stream in sorted(self._heads)
            ],
            "changeReceipts": receipts,
        })

    def _clone(self) -> Self:
        value = type(self)()
        value._state = self._state.copy()
        value._entities = dict(self._entities)
        value._change_receipts = dict(self._change_receipts)
        value._revision_receipts = dict(self._revision_receipts)
        value._heads = dict(self._heads)
        value._last_refolded = self._last_refolded
        return value

    def _adopt(self, other: Self) -> None:
        self._state = other._state
        self._entities = other._entities
        self._change_receipts = other._change_receipts
        self._revision_receipts = other._revision_receipts
        self._heads = other._heads
        self._last_refolded = other._last_refolded
