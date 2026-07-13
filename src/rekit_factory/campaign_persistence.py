"""Durable, exactly-once persistence for bounded campaign contracts.

The append-only event stream is canonical.  The tables beside it are transactional
projections which can be rebuilt and compared without rewriting history.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Literal

from muster import utcnow

from .campaign_contracts import (
    CampaignChangeRequest,
    CampaignCheckpoint,
    CampaignContract,
    CampaignStatus,
    CampaignAuthority,
    EpochContract,
    ResourceUsage,
    TerminalOutcome,
    validate_campaign_transition,
)


FailureInjector = Callable[[str], None]
LeaseStatus = Literal["active", "released", "recovery-required"]
ZERO_DIGEST = "0" * 64
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
WRITE_BOUNDARIES = (
    "event-appended", "campaign-projected", "epoch-projected",
    "lease-projected", "checkpoint-projected", "decision-projected",
    "terminal-projected",
)


class CampaignPersistenceError(ValueError):
    """Canonical history or requested authority is invalid."""


class CampaignWriteInterrupted(BaseException):
    """Test-only process interruption used at transactional write boundaries."""


@dataclass(frozen=True)
class CampaignProjection:
    campaign_id: str
    status: CampaignStatus
    revision: int
    current_epoch_id: str | None
    latest_checkpoint_id: str | None
    cumulative_usage: ResourceUsage
    terminal: TerminalOutcome | None
    degraded: bool = False
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class CampaignRebuild:
    projection: CampaignProjection | None
    matches_live: bool
    degraded: bool
    problems: tuple[str, ...]


SCHEMA = """
create table if not exists factory_campaign_events (
    campaign_id text not null,
    campaign_seq integer not null,
    event_id text not null unique,
    operation_id text not null,
    kind text not null,
    payload_json text not null,
    payload_digest text not null,
    previous_digest text not null,
    event_digest text not null,
    created_at text not null,
    primary key (campaign_id, campaign_seq),
    unique (campaign_id, operation_id)
);
create table if not exists factory_campaigns (
    campaign_id text primary key,
    contract_json text not null,
    status text not null,
    revision integer not null,
    current_epoch_id text,
    latest_checkpoint_id text,
    cumulative_usage_json text not null,
    terminal_json text,
    updated_at text not null
);
create table if not exists factory_campaign_epochs (
    epoch_id text primary key,
    campaign_id text not null,
    ordinal integer not null,
    contract_json text not null,
    status text not null,
    checkpoint_id text,
    unique (campaign_id, ordinal)
);
create table if not exists factory_campaign_leases (
    lease_id text primary key,
    campaign_id text not null,
    epoch_id text not null,
    owner_id text not null,
    status text not null,
    created_at text not null,
    updated_at text not null
);
create unique index if not exists idx_factory_campaign_active_lease
    on factory_campaign_leases(epoch_id) where status = 'active';
create table if not exists factory_campaign_checkpoints (
    checkpoint_id text primary key,
    campaign_id text not null,
    epoch_id text not null unique,
    sequence integer not null,
    checkpoint_json text not null,
    unique (campaign_id, sequence)
);
create table if not exists factory_campaign_decisions (
    campaign_id text not null,
    request_id text not null,
    operation_id text not null,
    request_json text not null,
    approved integer not null,
    decided_by text not null,
    created_at text not null,
    primary key (campaign_id, request_id),
    unique (campaign_id, operation_id)
);
"""


def _json(value: object) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False,
                      separators=(",", ":"), sort_keys=True)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _operation(value: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise CampaignPersistenceError("operation_id must be a bounded stable identifier")
    return value


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise CampaignPersistenceError(f"{label} must be a bounded stable identifier")
    return value


class CampaignPersistence:
    """Campaign authority stored transactionally beside the Factory/Muster ledger."""

    def __init__(self, db: str | Path | sqlite3.Connection) -> None:
        if isinstance(db, sqlite3.Connection):
            self.conn = db
            self._owns_connection = False
        else:
            self.conn = sqlite3.connect(str(db))
            self._owns_connection = True
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        if self._owns_connection:
            self.conn.close()

    @staticmethod
    def _fault(injector: FailureInjector | None, boundary: str) -> None:
        if injector is not None:
            injector(boundary)

    def _existing_operation(self, campaign_id: str, operation_id: str,
                            kind: str, payload_json: str) -> bool:
        row = self.conn.execute(
            "select kind,payload_json from factory_campaign_events "
            "where campaign_id=? and operation_id=?", (campaign_id, operation_id),
        ).fetchone()
        if row is None:
            return False
        if row["kind"] != kind or row["payload_json"] != payload_json:
            raise CampaignPersistenceError("conflicting reuse of campaign operation_id")
        return True

    def _append_event(self, campaign_id: str, operation_id: str, kind: str,
                      payload_json: str) -> None:
        row = self.conn.execute(
            "select campaign_seq,event_digest from factory_campaign_events "
            "where campaign_id=? order by campaign_seq desc limit 1", (campaign_id,),
        ).fetchone()
        sequence = 1 if row is None else row["campaign_seq"] + 1
        previous = ZERO_DIGEST if row is None else row["event_digest"]
        payload_digest = _digest(payload_json)
        binding = _json({
            "campaignId": campaign_id, "campaignSequence": sequence, "kind": kind,
            "operationId": operation_id, "payloadDigest": payload_digest,
            "previousDigest": previous,
        })
        event_digest = _digest(binding)
        self.conn.execute(
            "insert into factory_campaign_events "
            "(campaign_id,campaign_seq,event_id,operation_id,kind,payload_json,"
            "payload_digest,previous_digest,event_digest,created_at) "
            "values (?,?,?,?,?,?,?,?,?,?)",
            (campaign_id, sequence, "campaign-event-" + event_digest, operation_id,
             kind, payload_json, payload_digest, previous, event_digest, utcnow()),
        )

    def create_campaign(self, contract: CampaignContract, *, operation_id: str,
                        failure_injector: FailureInjector | None = None) -> CampaignProjection:
        operation_id = _operation(operation_id)
        payload = _json({"contract": contract.to_dict(), "status": "requested"})
        with self.conn:
            if self._existing_operation(contract.campaign_id, operation_id,
                                        "campaign.created", payload):
                return self.campaign(contract.campaign_id)
            if self.conn.execute(
                "select 1 from factory_campaigns where campaign_id=?", (contract.campaign_id,),
            ).fetchone():
                raise CampaignPersistenceError("campaign already exists under another operation")
            self._append_event(contract.campaign_id, operation_id, "campaign.created", payload)
            self._fault(failure_injector, "event-appended")
            self.conn.execute(
                "insert into factory_campaigns "
                "(campaign_id,contract_json,status,revision,current_epoch_id,"
                "latest_checkpoint_id,cumulative_usage_json,terminal_json,updated_at) "
                "values (?,?, 'requested',1,null,null,?,null,?)",
                (contract.campaign_id, _json(contract.to_dict()),
                 _json(ResourceUsage().to_dict()), utcnow()),
            )
            self._fault(failure_injector, "campaign-projected")
        return self.campaign(contract.campaign_id)

    def transition_campaign(self, campaign_id: str, target: CampaignStatus, *,
                            authority: CampaignAuthority, operation_id: str,
                            terminal: TerminalOutcome | None = None,
                            failure_injector: FailureInjector | None = None) -> CampaignProjection:
        operation_id = _operation(operation_id)
        payload = _json({"authority": authority, "status": target,
                         "terminal": None if terminal is None else terminal.to_dict()})
        with self.conn:
            if self._existing_operation(campaign_id, operation_id,
                                        "campaign.transitioned", payload):
                return self.campaign(campaign_id)
            current = self._campaign_row(campaign_id)
            validate_campaign_transition(current["status"], target, authority=authority)
            if (terminal is None) != (target in {"requested", "running", "waiting", "suspended"}):
                raise CampaignPersistenceError("terminal transition must carry exactly one outcome")
            if terminal is not None:
                if terminal.campaign_id != campaign_id or terminal.status != target:
                    raise CampaignPersistenceError("terminal outcome does not match transition")
                if terminal.final_checkpoint_id is not None:
                    checkpoint = self.conn.execute(
                        "select 1 from factory_campaign_checkpoints where campaign_id=? "
                        "and checkpoint_id=?", (campaign_id, terminal.final_checkpoint_id),
                    ).fetchone()
                    if checkpoint is None:
                        raise CampaignPersistenceError(
                            "terminal outcome references a dangling checkpoint"
                        )
            self._append_event(campaign_id, operation_id, "campaign.transitioned", payload)
            self._fault(failure_injector, "event-appended")
            self.conn.execute(
                "update factory_campaigns set status=?,revision=revision+1,terminal_json=?,"
                "updated_at=? where campaign_id=?",
                (target, None if terminal is None else _json(terminal.to_dict()),
                 utcnow(), campaign_id),
            )
            self._fault(failure_injector,
                        "terminal-projected" if terminal is not None else "campaign-projected")
        return self.campaign(campaign_id)

    def publish_epoch(self, epoch: EpochContract, *, operation_id: str,
                      failure_injector: FailureInjector | None = None) -> CampaignProjection:
        operation_id = _operation(operation_id)
        payload = _json(epoch.to_dict())
        with self.conn:
            if self._existing_operation(epoch.campaign_id, operation_id,
                                        "epoch.published", payload):
                return self.campaign(epoch.campaign_id)
            campaign = CampaignContract.from_dict(json.loads(
                (campaign_row := self._campaign_row(epoch.campaign_id))["contract_json"]
            ))
            if campaign_row["status"] != "running":
                raise CampaignPersistenceError("epochs may only be published for a running campaign")
            epoch.validate_for(campaign)
            prior = self.conn.execute(
                "select epoch_id,checkpoint_id from factory_campaign_epochs "
                "where campaign_id=? order by ordinal desc limit 1", (epoch.campaign_id,),
            ).fetchone()
            expected = 1 if prior is None else self.conn.execute(
                "select max(ordinal)+1 from factory_campaign_epochs where campaign_id=?",
                (epoch.campaign_id,),
            ).fetchone()[0]
            if epoch.ordinal != expected:
                raise CampaignPersistenceError("epoch ordinal must be contiguous")
            if prior is not None and (prior["checkpoint_id"] is None or
                                      epoch.previous_checkpoint_id != prior["checkpoint_id"]):
                raise CampaignPersistenceError("epoch does not bind the prior committed checkpoint")
            self._append_event(epoch.campaign_id, operation_id, "epoch.published", payload)
            self._fault(failure_injector, "event-appended")
            self.conn.execute(
                "insert into factory_campaign_epochs "
                "(epoch_id,campaign_id,ordinal,contract_json,status,checkpoint_id) "
                "values (?,?,?,?, 'published',null)",
                (epoch.epoch_id, epoch.campaign_id, epoch.ordinal, payload),
            )
            self.conn.execute(
                "update factory_campaigns set current_epoch_id=?,revision=revision+1,updated_at=? "
                "where campaign_id=?", (epoch.epoch_id, utcnow(), epoch.campaign_id),
            )
            self._fault(failure_injector, "epoch-projected")
        return self.campaign(epoch.campaign_id)

    def acquire_epoch_lease(self, campaign_id: str, epoch_id: str, owner_id: str, *,
                            operation_id: str,
                            failure_injector: FailureInjector | None = None) -> str:
        operation_id = _operation(operation_id)
        owner_id = _identifier(owner_id, "lease owner_id")
        payload = _json({"epochId": epoch_id, "ownerId": owner_id})
        lease_id = "campaign-lease-" + _digest(payload + campaign_id)
        with self.conn:
            if self._existing_operation(campaign_id, operation_id, "epoch.leased", payload):
                row = self.conn.execute(
                    "select lease_id from factory_campaign_leases where campaign_id=? "
                    "and epoch_id=? and owner_id=?", (campaign_id, epoch_id, owner_id),
                ).fetchone()
                if row is None:
                    raise CampaignPersistenceError("lease event has a dangling projection")
                return row["lease_id"]
            campaign = self._campaign_row(campaign_id)
            if campaign["status"] != "running":
                raise CampaignPersistenceError("epochs may only be leased for a running campaign")
            epoch = self._epoch_row(campaign_id, epoch_id)
            if epoch["status"] != "published":
                raise CampaignPersistenceError("epoch is not available for lease")
            self._append_event(campaign_id, operation_id, "epoch.leased", payload)
            self._fault(failure_injector, "event-appended")
            now = utcnow()
            self.conn.execute(
                "insert into factory_campaign_leases "
                "(lease_id,campaign_id,epoch_id,owner_id,status,created_at,updated_at) "
                "values (?,?,?,?, 'active',?,?)",
                (lease_id, campaign_id, epoch_id, owner_id, now, now),
            )
            self.conn.execute(
                "update factory_campaign_epochs set status='leased' where epoch_id=?",
                (epoch_id,),
            )
            self._fault(failure_injector, "lease-projected")
        return lease_id

    def record_checkpoint(self, checkpoint: CampaignCheckpoint, *, operation_id: str,
                          failure_injector: FailureInjector | None = None) -> CampaignProjection:
        operation_id = _operation(operation_id)
        payload = _json(checkpoint.to_dict())
        with self.conn:
            if self._existing_operation(checkpoint.campaign_id, operation_id,
                                        "checkpoint.recorded", payload):
                return self.campaign(checkpoint.campaign_id)
            epoch = self._epoch_row(checkpoint.campaign_id, checkpoint.epoch_id)
            if epoch["status"] != "leased":
                raise CampaignPersistenceError("checkpoint requires a durably leased epoch")
            prior = self.conn.execute(
                "select checkpoint_json from factory_campaign_checkpoints "
                "where campaign_id=? order by sequence desc limit 1", (checkpoint.campaign_id,),
            ).fetchone()
            expected_sequence = 1 if prior is None else CampaignCheckpoint.from_dict(
                json.loads(prior["checkpoint_json"])
            ).sequence + 1
            if checkpoint.sequence != expected_sequence:
                raise CampaignPersistenceError("checkpoint sequence must be contiguous")
            prior_usage = ResourceUsage() if prior is None else CampaignCheckpoint.from_dict(
                json.loads(prior["checkpoint_json"])
            ).cumulative_usage
            for field in ResourceUsage._ATTRIBUTES:
                if getattr(checkpoint.cumulative_usage, field) < getattr(prior_usage, field):
                    raise CampaignPersistenceError("cumulative resource usage must not decrease")
            contract = CampaignContract.from_dict(json.loads(
                self._campaign_row(checkpoint.campaign_id)["contract_json"]
            ))
            epoch_contract = EpochContract.from_dict(json.loads(epoch["contract_json"]))
            for field in ResourceUsage._ATTRIBUTES:
                delta = getattr(checkpoint.cumulative_usage, field) - getattr(prior_usage, field)
                if delta > getattr(epoch_contract.budget, field).value:
                    raise CampaignPersistenceError("checkpoint delta exceeds epoch budget")
                if getattr(checkpoint.cumulative_usage, field) > \
                        getattr(contract.cumulative_budget, field).value:
                    raise CampaignPersistenceError("checkpoint exceeds cumulative campaign budget")
            self._append_event(checkpoint.campaign_id, operation_id,
                               "checkpoint.recorded", payload)
            self._fault(failure_injector, "event-appended")
            self.conn.execute(
                "insert into factory_campaign_checkpoints "
                "(checkpoint_id,campaign_id,epoch_id,sequence,checkpoint_json) values (?,?,?,?,?)",
                (checkpoint.checkpoint_id, checkpoint.campaign_id, checkpoint.epoch_id,
                 checkpoint.sequence, payload),
            )
            self.conn.execute(
                "update factory_campaign_epochs set status='checkpointed',checkpoint_id=? "
                "where epoch_id=?", (checkpoint.checkpoint_id, checkpoint.epoch_id),
            )
            self.conn.execute(
                "update factory_campaign_leases set status='released',updated_at=? "
                "where campaign_id=? and epoch_id=? and status='active'",
                (utcnow(), checkpoint.campaign_id, checkpoint.epoch_id),
            )
            self.conn.execute(
                "update factory_campaigns set latest_checkpoint_id=?,"
                "cumulative_usage_json=?,revision=revision+1,updated_at=? where campaign_id=?",
                (checkpoint.checkpoint_id, _json(checkpoint.cumulative_usage.to_dict()),
                 utcnow(), checkpoint.campaign_id),
            )
            self._fault(failure_injector, "checkpoint-projected")
        return self.campaign(checkpoint.campaign_id)

    def record_operator_decision(self, campaign_id: str,
                                 request: CampaignChangeRequest, *, approved: bool,
                                 decided_by: str, operation_id: str,
                                 failure_injector: FailureInjector | None = None) -> str:
        operation_id = _operation(operation_id)
        if type(approved) is not bool:
            raise CampaignPersistenceError("approved must be exactly boolean")
        decided_by = _identifier(decided_by, "decision authority")
        if request.current_campaign_id != campaign_id:
            raise CampaignPersistenceError("operator decision campaign does not match request")
        payload = _json({"approved": approved, "decidedBy": decided_by,
                         "request": request.to_dict()})
        with self.conn:
            if self._existing_operation(campaign_id, operation_id,
                                        "operator.decided", payload):
                return request.request_id
            self._campaign_row(campaign_id)
            self._append_event(campaign_id, operation_id, "operator.decided", payload)
            self._fault(failure_injector, "event-appended")
            self.conn.execute(
                "insert into factory_campaign_decisions "
                "(campaign_id,request_id,operation_id,request_json,approved,decided_by,created_at) "
                "values (?,?,?,?,?,?,?)",
                (campaign_id, request.request_id, operation_id,
                 _json(request.to_dict()), int(approved), decided_by, utcnow()),
            )
            self._fault(failure_injector, "decision-projected")
        return request.request_id

    def recover(self, campaign_id: str, *, operation_id: str,
                failure_injector: FailureInjector | None = None) -> CampaignProjection:
        """Conservatively block orphaned leases; absence is never completion."""
        operation_id = _operation(operation_id)
        prior = self.conn.execute(
            "select kind from factory_campaign_events where campaign_id=? and operation_id=?",
            (campaign_id, operation_id),
        ).fetchone()
        if prior is not None:
            if prior["kind"] != "campaign.recovered":
                raise CampaignPersistenceError("conflicting reuse of campaign operation_id")
            return self.campaign(campaign_id)
        campaign = self._campaign_row(campaign_id)
        if campaign["status"] not in {"running", "waiting"}:
            raise CampaignPersistenceError("only a running or waiting campaign can recover")
        active = self.conn.execute(
            "select lease_id,epoch_id,owner_id from factory_campaign_leases "
            "where campaign_id=? and status='active' order by created_at", (campaign_id,),
        ).fetchall()
        payload = _json({"leases": [dict(row) for row in active]})
        with self.conn:
            if self._existing_operation(campaign_id, operation_id,
                                        "campaign.recovered", payload):
                return self.campaign(campaign_id)
            self._append_event(campaign_id, operation_id, "campaign.recovered", payload)
            self._fault(failure_injector, "event-appended")
            if active:
                now = utcnow()
                self.conn.execute(
                    "update factory_campaign_leases set status='recovery-required',updated_at=? "
                    "where campaign_id=? and status='active'", (now, campaign_id),
                )
                self.conn.execute(
                    "update factory_campaigns set status='waiting',revision=revision+1,updated_at=? "
                    "where campaign_id=?", (now, campaign_id),
                )
            self._fault(failure_injector, "lease-projected")
        return self.campaign(campaign_id)

    def _campaign_row(self, campaign_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "select * from factory_campaigns where campaign_id=?", (campaign_id,),
        ).fetchone()
        if row is None:
            raise CampaignPersistenceError("campaign does not exist")
        return row

    def _epoch_row(self, campaign_id: str, epoch_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "select * from factory_campaign_epochs where campaign_id=? and epoch_id=?",
            (campaign_id, epoch_id),
        ).fetchone()
        if row is None:
            raise CampaignPersistenceError("epoch does not belong to campaign")
        return row

    def campaign(self, campaign_id: str) -> CampaignProjection:
        row = self._campaign_row(campaign_id)
        return CampaignProjection(
            campaign_id, row["status"], row["revision"], row["current_epoch_id"],
            row["latest_checkpoint_id"],
            ResourceUsage.from_dict(json.loads(row["cumulative_usage_json"])),
            None if row["terminal_json"] is None else TerminalOutcome.from_dict(
                json.loads(row["terminal_json"])
            ),
        )

    def rebuild_projection(self, campaign_id: str) -> CampaignRebuild:
        """Validate canonical history and compare its replay with the live projection."""
        rows = self.conn.execute(
            "select * from factory_campaign_events where campaign_id=? order by campaign_seq",
            (campaign_id,),
        ).fetchall()
        problems: list[str] = []
        if not rows:
            return CampaignRebuild(None, False, True, ("campaign history is absent",))
        status: CampaignStatus = "requested"
        revision = 0
        current_epoch = latest_checkpoint = None
        usage = ResourceUsage()
        terminal = None
        previous = ZERO_DIGEST
        epochs: dict[str, dict[str, Any]] = {}
        checkpoint_ids: set[str] = set()
        operations: set[str] = set()
        for expected, row in enumerate(rows, 1):
            if row["campaign_seq"] != expected:
                problems.append(f"history gap before sequence {row['campaign_seq']}")
            payload_digest = _digest(row["payload_json"])
            binding = _json({
                "campaignId": campaign_id, "campaignSequence": row["campaign_seq"],
                "kind": row["kind"], "operationId": row["operation_id"],
                "payloadDigest": payload_digest, "previousDigest": previous,
            })
            if row["operation_id"] in operations:
                problems.append(f"duplicate operation {row['operation_id']}")
            operations.add(row["operation_id"])
            if row["payload_digest"] != payload_digest or row["previous_digest"] != previous \
                    or row["event_digest"] != _digest(binding):
                problems.append(f"corrupt hash chain at sequence {row['campaign_seq']}")
            previous = row["event_digest"]
            try:
                payload = json.loads(row["payload_json"])
                kind = row["kind"]
                if kind == "campaign.created":
                    if revision:
                        raise CampaignPersistenceError("duplicate campaign creation")
                    CampaignContract.from_dict(payload["contract"])
                    status, revision = "requested", 1
                elif kind == "campaign.transitioned":
                    validate_campaign_transition(status, payload["status"],
                                                 authority=payload["authority"])
                    status, revision = payload["status"], revision + 1
                    if payload["terminal"] is not None:
                        terminal = TerminalOutcome.from_dict(payload["terminal"])
                        if terminal.final_checkpoint_id is not None \
                                and terminal.final_checkpoint_id not in checkpoint_ids:
                            raise CampaignPersistenceError("terminal checkpoint is dangling")
                elif kind == "epoch.published":
                    epoch = EpochContract.from_dict(payload)
                    if epoch.ordinal != len(epochs) + 1:
                        raise CampaignPersistenceError("non-contiguous epoch ordinal")
                    if epoch.ordinal > 1 and epoch.previous_checkpoint_id not in checkpoint_ids:
                        raise CampaignPersistenceError("epoch checkpoint reference is dangling")
                    epochs[epoch.epoch_id] = payload
                    current_epoch, revision = epoch.epoch_id, revision + 1
                elif kind == "checkpoint.recorded":
                    checkpoint = CampaignCheckpoint.from_dict(payload)
                    if checkpoint.epoch_id not in epochs:
                        raise CampaignPersistenceError("checkpoint epoch is dangling")
                    if checkpoint.sequence != len(checkpoint_ids) + 1:
                        raise CampaignPersistenceError("non-contiguous checkpoint sequence")
                    checkpoint_ids.add(checkpoint.checkpoint_id)
                    latest_checkpoint, usage = checkpoint.checkpoint_id, checkpoint.cumulative_usage
                    revision += 1
                elif kind == "campaign.recovered" and payload["leases"]:
                    status, revision = "waiting", revision + 1
                elif kind not in {"epoch.leased", "operator.decided", "campaign.recovered"}:
                    raise CampaignPersistenceError(f"unknown event kind {kind}")
            except (KeyError, TypeError, ValueError) as exc:
                problems.append(f"impossible event {row['campaign_seq']}: {exc}")
        replay = CampaignProjection(campaign_id, status, revision, current_epoch,
                                    latest_checkpoint, usage, terminal,
                                    bool(problems), tuple(problems))
        try:
            live = self.campaign(campaign_id)
            comparable = (live.campaign_id, live.status, live.revision,
                          live.current_epoch_id, live.latest_checkpoint_id,
                          live.cumulative_usage, live.terminal)
            replayable = (replay.campaign_id, replay.status, replay.revision,
                           replay.current_epoch_id, replay.latest_checkpoint_id,
                           replay.cumulative_usage, replay.terminal)
            matches = comparable == replayable and not problems
            if comparable != replayable:
                problems.append("live projection differs from canonical replay")
        except (ValueError, json.JSONDecodeError) as exc:
            matches = False
            problems.append(f"live projection is invalid: {exc}")
        return CampaignRebuild(
            CampaignProjection(replay.campaign_id, replay.status, replay.revision,
                               replay.current_epoch_id, replay.latest_checkpoint_id,
                               replay.cumulative_usage, replay.terminal,
                               bool(problems), tuple(problems)),
            matches, bool(problems), tuple(problems),
        )
