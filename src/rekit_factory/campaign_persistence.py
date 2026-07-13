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
MAX_HEALTH_HISTORY = 32
MAX_HEALTH_ARTIFACTS = 256
MAX_HEALTH_PROBLEMS = 16
MAX_POLICY_INPUT_BYTES = 262_144
MAX_CHANGE_COMPONENTS = 64
MAX_CHANGE_REQUIRED_ARTIFACTS = 256
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
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

    @property
    def problem_codes(self) -> tuple[str, ...]:
        """Bounded non-sensitive diagnostics suitable for public projections."""
        codes: list[str] = []
        for problem in self.problems:
            if "health" in problem:
                code = "campaign-health-invalid"
            elif "hash chain" in problem or "history gap" in problem:
                code = "campaign-history-integrity"
            elif "live projection" in problem:
                code = "campaign-projection-mismatch"
            else:
                code = "campaign-history-invalid"
            if code not in codes:
                codes.append(code)
            if len(codes) == MAX_HEALTH_PROBLEMS:
                break
        return tuple(codes)


@dataclass(frozen=True)
class CampaignHealthRollup:
    """Exact canonical health facts applied with one policy recommendation."""

    campaign_id: str
    epoch_id: str
    checkpoint_id: str
    policy_input_digest: str
    recommendation_id: str
    sequence: int
    phase: Literal["recon", "hypothesis", "validation"]
    coverage_basis_points: int
    resolved_hypotheses: int
    reproduced_findings: int
    artifact_ids: tuple[str, ...]
    epoch_novel_progress: int
    cumulative_novel_progress: int
    retry_count: int
    no_progress_count: int
    elapsed_wall_seconds: int
    next_checkpoint_expected_wall_seconds: int | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.campaign_id, "campaign_id"), (self.epoch_id, "epoch_id"),
            (self.checkpoint_id, "checkpoint_id"),
            (self.recommendation_id, "recommendation_id"),
        ):
            _identifier(value, label)
        if _DIGEST.fullmatch(self.policy_input_digest) is None:
            raise CampaignPersistenceError("policy_input_digest must be a SHA-256 digest")
        if self.phase not in {"recon", "hypothesis", "validation"}:
            raise CampaignPersistenceError("health phase is invalid")
        bounded = (
            (self.sequence, "sequence", 1, 2**63 - 1),
            (self.coverage_basis_points, "coverage", 0, 10_000),
            (self.resolved_hypotheses, "resolved hypotheses", 0, 2**63 - 1),
            (self.reproduced_findings, "reproduced findings", 0, 2**63 - 1),
            (self.epoch_novel_progress, "epoch novel progress", 0, 2**63 - 1),
            (self.cumulative_novel_progress, "cumulative novel progress", 0, 2**63 - 1),
            (self.retry_count, "retry count", 0, 2**63 - 1),
            (self.no_progress_count, "no-progress count", 0, 2**63 - 1),
            (self.elapsed_wall_seconds, "elapsed observation", 0, 2**63 - 1),
        )
        for value, label, minimum, maximum in bounded:
            if type(value) is not int or not minimum <= value <= maximum:
                raise CampaignPersistenceError(f"{label} is outside its bounded range")
        artifacts = tuple(sorted(_identifier(item, "artifact_id") for item in self.artifact_ids))
        if len(artifacts) > MAX_HEALTH_ARTIFACTS or len(set(artifacts)) != len(artifacts):
            raise CampaignPersistenceError("health artifact identities are duplicate or unbounded")
        object.__setattr__(self, "artifact_ids", artifacts)
        expected = self.next_checkpoint_expected_wall_seconds
        if expected is not None and (type(expected) is not int
                                     or not self.elapsed_wall_seconds <= expected <= 2**63 - 1):
            raise CampaignPersistenceError("next checkpoint expectation precedes elapsed work")

    def to_dict(self) -> dict[str, object]:
        return {
            "artifactIds": list(self.artifact_ids), "campaignId": self.campaign_id,
            "checkpointId": self.checkpoint_id,
            "coverageBasisPoints": self.coverage_basis_points,
            "cumulativeNovelProgress": self.cumulative_novel_progress,
            "elapsedWallSeconds": self.elapsed_wall_seconds, "epochId": self.epoch_id,
            "epochNovelProgress": self.epoch_novel_progress,
            "nextCheckpointExpectedWallSeconds": self.next_checkpoint_expected_wall_seconds,
            "noProgressCount": self.no_progress_count, "phase": self.phase,
            "policyInputDigest": self.policy_input_digest,
            "recommendationId": self.recommendation_id,
            "reproducedFindings": self.reproduced_findings,
            "resolvedHypotheses": self.resolved_hypotheses,
            "retryCount": self.retry_count, "schemaVersion": 1,
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "CampaignHealthRollup":
        if not isinstance(raw, dict) or set(raw) != {
            "artifactIds", "campaignId", "checkpointId", "coverageBasisPoints",
            "cumulativeNovelProgress", "elapsedWallSeconds", "epochId",
            "epochNovelProgress", "nextCheckpointExpectedWallSeconds", "noProgressCount",
            "phase", "policyInputDigest", "recommendationId", "reproducedFindings",
            "resolvedHypotheses", "retryCount", "schemaVersion", "sequence",
        } or raw.get("schemaVersion") != 1:
            raise CampaignPersistenceError("health rollup has an invalid schema")
        return cls(
            raw["campaignId"], raw["epochId"], raw["checkpointId"],
            raw["policyInputDigest"], raw["recommendationId"], raw["sequence"],
            raw["phase"], raw["coverageBasisPoints"], raw["resolvedHypotheses"],
            raw["reproducedFindings"], tuple(raw["artifactIds"]),
            raw["epochNovelProgress"], raw["cumulativeNovelProgress"],
            raw["retryCount"], raw["noProgressCount"], raw["elapsedWallSeconds"],
            raw["nextCheckpointExpectedWallSeconds"],
        )


@dataclass(frozen=True)
class CampaignHealthProjection:
    current: CampaignHealthRollup | None
    previous: CampaignHealthRollup | None
    total_observations: int
    history: tuple[CampaignHealthRollup, ...] = ()
    degraded: bool = False
    problem_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CampaignChangeProjection:
    request: CampaignChangeRequest
    status: Literal["pending", "approved", "rejected"]
    revision: int
    base_campaign_revision: int
    application_status: Literal["not-applicable", "pending", "applied"]


def _validate_health_content(rollup: CampaignHealthRollup,
                             policy_input: dict[str, object],
                             recommendation: dict[str, object],
                             prior: CampaignHealthRollup | None) -> None:
    """Derive every rollup-only fact from the exact staged envelopes."""
    body = dict(recommendation)
    recommendation_id = body.pop("recommendationId", None)
    totals = policy_input.get("totals")
    attempts = policy_input.get("attempts")
    novel = recommendation.get("novelProgress")
    epoch_result = policy_input.get("epochResult")
    expected = policy_input.get("nextCheckpointExpectedWallSeconds")
    if not isinstance(totals, dict) or not isinstance(attempts, list) \
            or not isinstance(novel, list) or recommendation_id != rollup.recommendation_id \
            or recommendation_id != "policy-" + _digest(_json(body)) \
            or _digest(_json(policy_input)) != rollup.policy_input_digest \
            or policy_input.get("campaignId") != rollup.campaign_id \
            or policy_input.get("epochId") != rollup.epoch_id \
            or policy_input.get("checkpointId") != rollup.checkpoint_id \
            or policy_input.get("phase") != rollup.phase:
        raise CampaignPersistenceError("health content authority is invalid")
    if expected is not None and (
        recommendation.get("action") not in {"continue", "reprioritize"}
        or not isinstance(epoch_result, dict) or not epoch_result.get("nextActionIds")
    ):
        raise CampaignPersistenceError("next checkpoint expectation lacks scheduled work")
    try:
        equivalence = [item["equivalenceDigest"] for item in attempts]
        outcomes = [item["outcome"] for item in attempts]
    except (KeyError, TypeError) as exc:
        raise CampaignPersistenceError("health attempt facts are invalid") from exc
    epoch_retries = len(equivalence) - len(set(equivalence))
    epoch_no_progress = sum(item != "productive" for item in outcomes) if not novel else 0
    if (rollup.coverage_basis_points != totals.get("coverageBasisPoints")
            or rollup.resolved_hypotheses != totals.get("resolvedHypotheses")
            or rollup.reproduced_findings != totals.get("reproducedFindings")
            or list(rollup.artifact_ids) != totals.get("artifactIds")
            or rollup.epoch_novel_progress != len(novel)
            or rollup.cumulative_novel_progress
            != (0 if prior is None else prior.cumulative_novel_progress) + len(novel)
            or rollup.retry_count != epoch_retries
            or rollup.no_progress_count != epoch_no_progress
            or rollup.next_checkpoint_expected_wall_seconds != expected):
        raise CampaignPersistenceError("health rollup contradicts canonical policy facts")
    if prior is not None and (
        rollup.coverage_basis_points < prior.coverage_basis_points
        or rollup.resolved_hypotheses < prior.resolved_hypotheses
        or rollup.reproduced_findings < prior.reproduced_findings
        or not set(prior.artifact_ids).issubset(rollup.artifact_ids)
        or rollup.cumulative_novel_progress < prior.cumulative_novel_progress
        or rollup.elapsed_wall_seconds < prior.elapsed_wall_seconds
    ):
        raise CampaignPersistenceError("canonical campaign health regressed")


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
create table if not exists factory_campaign_health (
    campaign_id text not null,
    sequence integer not null,
    epoch_id text not null,
    checkpoint_id text not null,
    recommendation_id text not null,
    rollup_json text not null,
    primary key (campaign_id, sequence),
    unique (campaign_id, epoch_id),
    unique (campaign_id, checkpoint_id),
    unique (campaign_id, recommendation_id)
);
create table if not exists factory_campaign_change_requests (
    campaign_id text not null,
    request_id text primary key,
    request_json text not null,
    status text not null,
    revision integer not null,
    base_campaign_revision integer not null,
    application_status text not null,
    decision_operation_id text,
    decision_payload_json text,
    published_at text not null,
    decided_at text
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
            # The loopback API owns a threaded HTTP server. Writes remain serialized by
            # SQLite transactions (and the controller's shared lock), but the durable authority
            # must be usable from the request thread rather than being tied to construction.
            self.conn = sqlite3.connect(str(db), check_same_thread=False)
            self._owns_connection = True
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        columns = {row[1] for row in self.conn.execute(
            "pragma table_info(factory_campaign_change_requests)"
        )}
        if "base_campaign_revision" not in columns:
            self.conn.execute(
                "alter table factory_campaign_change_requests add column "
                "base_campaign_revision integer not null default 1"
            )
            self.conn.commit()

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
                            expected_revision: int | None = None,
                            failure_injector: FailureInjector | None = None) -> CampaignProjection:
        operation_id = _operation(operation_id)
        payload_value = {"authority": authority, "status": target,
                         "terminal": None if terminal is None else terminal.to_dict()}
        # Preserve the byte identity of pre-W-0055 operations so their exact retries
        # remain valid after upgrading. Optimistic concurrency is content-bound only
        # for the new operator-facing calls which explicitly supply a revision.
        if expected_revision is not None:
            payload_value["expectedRevision"] = expected_revision
        payload = _json(payload_value)
        with self.conn:
            if self._existing_operation(campaign_id, operation_id,
                                        "campaign.transitioned", payload):
                return self.campaign(campaign_id)
            current = self._campaign_row(campaign_id)
            if expected_revision is not None:
                if type(expected_revision) is not int or expected_revision < 1:
                    raise CampaignPersistenceError("expected_revision must be a positive integer")
                if current["revision"] != expected_revision:
                    raise CampaignPersistenceError("campaign revision is stale")
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

    def record_health_rollup(self, rollup: CampaignHealthRollup, *, policy_input_json: str,
                             recommendation_json: str,
                             operation_id: str,
                             failure_injector: FailureInjector | None = None
                             ) -> CampaignHealthProjection:
        """Commit one staged health observation to history and its bounded read model."""
        operation_id = _operation(operation_id)
        if type(policy_input_json) is not str \
                or len(policy_input_json.encode("utf-8")) > MAX_POLICY_INPUT_BYTES:
            raise CampaignPersistenceError("policy input envelope is absent or unbounded")
        try:
            envelope = json.loads(policy_input_json)
        except json.JSONDecodeError as exc:
            raise CampaignPersistenceError("policy input envelope is not canonical JSON") from exc
        if _json(envelope) != policy_input_json \
                or _digest(policy_input_json) != rollup.policy_input_digest:
            raise CampaignPersistenceError("policy input envelope contradicts its digest")
        if not isinstance(envelope, dict) \
                or envelope.get("campaignId") != rollup.campaign_id \
                or envelope.get("epochId") != rollup.epoch_id \
                or envelope.get("checkpointId") != rollup.checkpoint_id \
                or envelope.get("phase") != rollup.phase:
            raise CampaignPersistenceError("policy input envelope crosses health authority")
        try:
            recommendation = json.loads(recommendation_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise CampaignPersistenceError("recommendation envelope is invalid") from exc
        if not isinstance(recommendation, dict) or _json(recommendation) != recommendation_json:
            raise CampaignPersistenceError("recommendation envelope is not canonical JSON")
        recommendation_body = dict(recommendation)
        recommendation_id = recommendation_body.pop("recommendationId", None)
        if recommendation_id != rollup.recommendation_id \
                or recommendation_id != "policy-" + _digest(_json(recommendation_body)):
            raise CampaignPersistenceError("health recommendation identity is invalid")
        totals = envelope.get("totals")
        attempts = envelope.get("attempts")
        novel = recommendation.get("novelProgress")
        if not isinstance(totals, dict) or not isinstance(attempts, list) \
                or not isinstance(novel, list):
            raise CampaignPersistenceError("health source envelopes omit canonical facts")
        epoch_result = envelope.get("epochResult")
        expected = envelope.get("nextCheckpointExpectedWallSeconds")
        if expected is not None and (
            recommendation.get("action") not in {"continue", "reprioritize"}
            or not isinstance(epoch_result, dict) or not epoch_result.get("nextActionIds")
        ):
            raise CampaignPersistenceError(
                "next checkpoint expectation lacks scheduled recommendation work"
            )
        if (rollup.coverage_basis_points != totals.get("coverageBasisPoints")
                or rollup.resolved_hypotheses != totals.get("resolvedHypotheses")
                or rollup.reproduced_findings != totals.get("reproducedFindings")
                or list(rollup.artifact_ids) != totals.get("artifactIds")
                or rollup.epoch_novel_progress != len(novel)
                or rollup.next_checkpoint_expected_wall_seconds
                != envelope.get("nextCheckpointExpectedWallSeconds")):
            raise CampaignPersistenceError("health rollup contradicts canonical policy facts")
        try:
            equivalence = [item["equivalenceDigest"] for item in attempts]
            outcomes = [item["outcome"] for item in attempts]
        except (KeyError, TypeError) as exc:
            raise CampaignPersistenceError("health attempt facts are invalid") from exc
        epoch_retries = len(equivalence) - len(set(equivalence))
        epoch_no_progress = sum(item != "productive" for item in outcomes) if not novel else 0
        payload = _json({"policyInput": envelope, "recommendation": recommendation,
                         "rollup": rollup.to_dict()})
        with self.conn:
            if self._existing_operation(rollup.campaign_id, operation_id,
                                        "campaign.health-recorded", payload):
                return self.health(rollup.campaign_id)
            campaign_row = self._campaign_row(rollup.campaign_id)
            campaign = CampaignContract.from_dict(json.loads(campaign_row["contract_json"]))
            if envelope.get("campaignDigest") != campaign.digest:
                raise CampaignPersistenceError("health input does not bind campaign content")
            epoch = self._epoch_row(rollup.campaign_id, rollup.epoch_id)
            checkpoint = self.conn.execute(
                "select checkpoint_json from factory_campaign_checkpoints "
                "where campaign_id=? and epoch_id=? and checkpoint_id=?",
                (rollup.campaign_id, rollup.epoch_id, rollup.checkpoint_id),
            ).fetchone()
            if epoch is None or checkpoint is None:
                raise CampaignPersistenceError("health rollup references a dangling epoch checkpoint")
            canonical_checkpoint = CampaignCheckpoint.from_dict(json.loads(checkpoint[0]))
            if rollup.sequence != canonical_checkpoint.sequence \
                    or rollup.elapsed_wall_seconds != canonical_checkpoint.cumulative_usage.wall_seconds:
                raise CampaignPersistenceError("health rollup contradicts its checkpoint")
            if rollup.next_checkpoint_expected_wall_seconds is not None \
                    and rollup.next_checkpoint_expected_wall_seconds \
                    > campaign.cumulative_budget.wall_seconds.value:
                raise CampaignPersistenceError("next checkpoint expectation exceeds wall authority")
            prior_row = self.conn.execute(
                "select rollup_json from factory_campaign_health where campaign_id=? "
                "order by sequence desc limit 1", (rollup.campaign_id,),
            ).fetchone()
            prior = None if prior_row is None else CampaignHealthRollup.from_dict(
                json.loads(prior_row[0])
            )
            _validate_health_content(rollup, envelope, recommendation, prior)
            if prior is not None and rollup.sequence != prior.sequence + 1:
                raise CampaignPersistenceError("health sequence is stale or discontinuous")
            if prior is not None and (
                rollup.coverage_basis_points < prior.coverage_basis_points
                or rollup.resolved_hypotheses < prior.resolved_hypotheses
                or rollup.reproduced_findings < prior.reproduced_findings
                or not set(prior.artifact_ids).issubset(rollup.artifact_ids)
                or rollup.cumulative_novel_progress < prior.cumulative_novel_progress
                or rollup.elapsed_wall_seconds < prior.elapsed_wall_seconds
            ):
                raise CampaignPersistenceError("canonical campaign health regressed")
            if rollup.cumulative_novel_progress \
                    != (0 if prior is None else prior.cumulative_novel_progress) + len(novel) \
                    or rollup.retry_count != epoch_retries \
                    or rollup.no_progress_count != epoch_no_progress:
                raise CampaignPersistenceError("health cumulative counters are not canonical")
            self._append_event(rollup.campaign_id, operation_id,
                               "campaign.health-recorded", payload)
            self._fault(failure_injector, "event-appended")
            self.conn.execute(
                "insert into factory_campaign_health "
                "(campaign_id,sequence,epoch_id,checkpoint_id,recommendation_id,rollup_json) "
                "values (?,?,?,?,?,?)",
                (rollup.campaign_id, rollup.sequence, rollup.epoch_id, rollup.checkpoint_id,
                 rollup.recommendation_id, _json(rollup.to_dict())),
            )
            self.conn.execute(
                "delete from factory_campaign_health where campaign_id=? and sequence <= ?",
                (rollup.campaign_id, rollup.sequence - MAX_HEALTH_HISTORY),
            )
            self._fault(failure_injector, "health-projected")
        return self.health(rollup.campaign_id)

    def health(self, campaign_id: str, *, history_limit: int = 2
               ) -> CampaignHealthProjection:
        """Return a bounded projection; canonical policy envelopes remain private."""
        _identifier(campaign_id, "campaign_id")
        if type(history_limit) is not int or not 1 <= history_limit <= MAX_HEALTH_HISTORY:
            raise CampaignPersistenceError("health history limit must be between 1 and 32")
        rows = self.conn.execute(
            "select rollup_json from factory_campaign_health where campaign_id=? "
            "order by sequence desc limit ?", (campaign_id, history_limit),
        ).fetchall()
        try:
            values = tuple(CampaignHealthRollup.from_dict(json.loads(row[0])) for row in rows)
        except (ValueError, TypeError, json.JSONDecodeError):
            return CampaignHealthProjection(
                None, None, 0, (), True, ("health-projection-invalid",),
            )
        current = values[0] if values else None
        previous = values[1] if len(values) > 1 else None
        total = self.conn.execute(
            "select count(*) from factory_campaign_events where campaign_id=? "
            "and kind='campaign.health-recorded'", (campaign_id,),
        ).fetchone()[0]
        return CampaignHealthProjection(current, previous, total, values)

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

    def publish_change_request(self, request: CampaignChangeRequest, *, operation_id: str
                               ) -> CampaignChangeProjection:
        operation_id = _operation(operation_id)
        payload = _json(request.to_dict())
        with self.conn:
            if self._existing_operation(request.current_campaign_id, operation_id,
                                        "campaign.change-request-published", payload):
                return self.change_request(request.current_campaign_id, request.request_id)
            campaign_row = self._campaign_row(request.current_campaign_id)
            if campaign_row["status"] not in {"running", "waiting", "suspended"}:
                raise CampaignPersistenceError("change requests require active campaign authority")
            current = CampaignContract.from_dict(json.loads(campaign_row["contract_json"]))
            for label, candidate in (("current", current), ("proposed", request.proposed)):
                if len(candidate.components) > MAX_CHANGE_COMPONENTS:
                    raise CampaignPersistenceError(
                        f"{label} change authority exceeds 64 component versions"
                    )
                if len(candidate.completion.required_artifact_ids) \
                        > MAX_CHANGE_REQUIRED_ARTIFACTS:
                    raise CampaignPersistenceError(
                        f"{label} change authority exceeds 256 required artifacts"
                    )
            from .campaign_contracts import requires_operator_decision
            if not requires_operator_decision(current, request.proposed):
                raise CampaignPersistenceError("campaign change requires changed authority")
            if self.conn.execute(
                "select 1 from factory_campaign_change_requests where request_id=?",
                (request.request_id,),
            ).fetchone() is not None:
                raise CampaignPersistenceError("change request already exists")
            if self.conn.execute(
                "select 1 from factory_campaign_change_requests where campaign_id=? "
                "and status='pending'", (request.current_campaign_id,),
            ).fetchone() is not None:
                raise CampaignPersistenceError("campaign already has a pending change request")
            self._append_event(request.current_campaign_id, operation_id,
                               "campaign.change-request-published", payload)
            self.conn.execute(
                "insert into factory_campaign_change_requests "
                "(campaign_id,request_id,request_json,status,revision,base_campaign_revision,"
                "application_status,published_at) values (?,?,?,'pending',1,?,'not-applicable',?)",
                (request.current_campaign_id, request.request_id, payload,
                 self._campaign_row(request.current_campaign_id)["revision"], utcnow()),
            )
        return self.change_request(request.current_campaign_id, request.request_id)

    def change_request(self, campaign_id: str, request_id: str) -> CampaignChangeProjection:
        row = self.conn.execute(
            "select * from factory_campaign_change_requests where campaign_id=? and request_id=?",
            (campaign_id, request_id),
        ).fetchone()
        if row is None:
            raise CampaignPersistenceError("campaign change request does not exist")
        return CampaignChangeProjection(
            CampaignChangeRequest.from_dict(json.loads(row["request_json"])), row["status"],
            row["revision"], row["base_campaign_revision"], row["application_status"],
        )

    def change_requests(self, campaign_id: str, *, limit: int = 32
                        ) -> tuple[CampaignChangeProjection, ...]:
        if type(limit) is not int or not 1 <= limit <= 32:
            raise CampaignPersistenceError("change request limit must be between 1 and 32")
        rows = self.conn.execute(
            "select request_id from factory_campaign_change_requests where campaign_id=? "
            "order by published_at desc,request_id limit ?", (campaign_id, limit),
        ).fetchall()
        return tuple(self.change_request(campaign_id, row[0]) for row in rows)

    def decide_change_request(self, campaign_id: str, request_id: str, *, approved: bool,
                              expected_revision: int, operation_id: str,
                              decided_by: str = "operator") -> CampaignChangeProjection:
        operation_id = _operation(operation_id)
        if type(approved) is not bool or type(expected_revision) is not int:
            raise CampaignPersistenceError("decision fields have invalid exact types")
        projection = self.change_request(campaign_id, request_id)
        payload = _json({"approved": approved, "decidedBy": decided_by,
                         "request": projection.request.to_dict()})
        row = self.conn.execute(
            "select * from factory_campaign_change_requests where request_id=?", (request_id,),
        ).fetchone()
        if row["decision_operation_id"] is not None:
            if row["decision_operation_id"] != operation_id \
                    or row["decision_payload_json"] != payload:
                raise CampaignPersistenceError("conflicting reuse of change decision")
            return self.change_request(campaign_id, request_id)
        if row["revision"] != expected_revision or row["status"] != "pending":
            raise CampaignPersistenceError("campaign change request revision is stale")
        if self._campaign_row(campaign_id)["revision"] != row["base_campaign_revision"]:
            raise CampaignPersistenceError("base campaign revision changed after publication")
        with self.conn:
            self._append_event(campaign_id, operation_id, "operator.decided", payload)
            self.conn.execute(
                "update factory_campaign_change_requests set status=?,revision=revision+1,"
                "application_status=?,decision_operation_id=?,decision_payload_json=?,decided_at=? "
                "where request_id=?",
                ("approved" if approved else "rejected",
                 "pending" if approved else "not-applicable", operation_id, payload, utcnow(),
                 request_id),
            )
        return self.change_request(campaign_id, request_id)

    def mark_change_applied(self, campaign_id: str, request_id: str) -> CampaignChangeProjection:
        projection = self.change_request(campaign_id, request_id)
        if projection.status != "approved":
            raise CampaignPersistenceError("only approved change authority can be applied")
        if projection.application_status == "applied":
            return projection
        payload = _json({"approvedCampaignId": projection.request.proposed.campaign_id,
                         "requestId": request_id})
        with self.conn:
            self._append_event(campaign_id, f"apply-complete:{request_id}",
                               "campaign.change-applied", payload)
            self.conn.execute(
                "update factory_campaign_change_requests set application_status='applied',"
                "revision=revision+1 where request_id=? and application_status='pending'",
                (request_id,),
            )
        return self.change_request(campaign_id, request_id)

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

    def reconcile_epoch_lease(self, campaign_id: str, epoch_id: str, owner_id: str, *,
                              operation_id: str,
                              failure_injector: FailureInjector | None = None) -> str:
        """Re-activate one orphaned lease after its exact runner result was verified.

        Recovery itself never retries work or implies success.  The controller may call this
        operation only after it has a durable, authority-bound execution result to checkpoint.
        """
        operation_id = _operation(operation_id)
        owner_id = _identifier(owner_id, "lease owner_id")
        payload = _json({"epochId": epoch_id, "ownerId": owner_id})
        with self.conn:
            if self._existing_operation(campaign_id, operation_id,
                                        "epoch.lease-reconciled", payload):
                row = self.conn.execute(
                    "select lease_id from factory_campaign_leases where campaign_id=? "
                    "and epoch_id=? and owner_id=? and status='active'",
                    (campaign_id, epoch_id, owner_id),
                ).fetchone()
                if row is None:
                    raise CampaignPersistenceError(
                        "reconciled lease event has a dangling projection"
                    )
                return row["lease_id"]
            campaign = self._campaign_row(campaign_id)
            if campaign["status"] != "waiting":
                raise CampaignPersistenceError(
                    "orphaned lease reconciliation requires a waiting campaign"
                )
            row = self.conn.execute(
                "select lease_id from factory_campaign_leases where campaign_id=? "
                "and epoch_id=? and owner_id=? and status='recovery-required'",
                (campaign_id, epoch_id, owner_id),
            ).fetchone()
            if row is None:
                raise CampaignPersistenceError("matching recovery-required lease does not exist")
            self._append_event(campaign_id, operation_id, "epoch.lease-reconciled", payload)
            self._fault(failure_injector, "event-appended")
            self.conn.execute(
                "update factory_campaign_leases set status='active',updated_at=? "
                "where lease_id=?", (utcnow(), row["lease_id"]),
            )
            self.conn.execute(
                "update factory_campaigns set status='running',revision=revision+1,updated_at=? "
                "where campaign_id=?", (utcnow(), campaign_id),
            )
            self._fault(failure_injector, "lease-projected")
        return row["lease_id"]

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
        leased_epochs: dict[str, tuple[str, str]] = {}
        recovery_epochs: set[str] = set()
        checkpoint_ids: set[str] = set()
        checkpoint_values: dict[str, CampaignCheckpoint] = {}
        health_rollups: list[CampaignHealthRollup] = []
        campaign_contract: CampaignContract | None = None
        operations: set[str] = set()
        change_request_ids: set[str] = set()
        published_changes: dict[str, dict[str, object]] = {}
        decided_changes: set[str] = set()
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
                    campaign_contract = CampaignContract.from_dict(payload["contract"])
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
                    checkpoint_values[checkpoint.checkpoint_id] = checkpoint
                    latest_checkpoint, usage = checkpoint.checkpoint_id, checkpoint.cumulative_usage
                    revision += 1
                    leased_epochs.pop(checkpoint.epoch_id, None)
                    recovery_epochs.discard(checkpoint.epoch_id)
                elif kind == "epoch.leased":
                    if payload["epochId"] not in epochs:
                        raise CampaignPersistenceError("leased epoch is dangling")
                    if payload["epochId"] in leased_epochs:
                        raise CampaignPersistenceError("epoch has duplicate lease authority")
                    leased_epochs[payload["epochId"]] = (
                        payload["ownerId"], row["operation_id"],
                    )
                elif kind == "campaign.recovered" and payload["leases"]:
                    recovered = {item["epoch_id"] for item in payload["leases"]}
                    if not recovered.issubset(leased_epochs):
                        raise CampaignPersistenceError("recovered lease is dangling")
                    recovery_epochs.update(recovered)
                    status, revision = "waiting", revision + 1
                elif kind == "epoch.lease-reconciled":
                    epoch_id = payload["epochId"]
                    authority = leased_epochs.get(epoch_id)
                    if status != "waiting" or epoch_id not in recovery_epochs \
                            or authority is None or authority[0] != payload["ownerId"]:
                        raise CampaignPersistenceError(
                            "reconciled lease does not match recovery authority"
                        )
                    recovery_epochs.remove(epoch_id)
                    status, revision = "running", revision + 1
                elif kind == "campaign.health-recorded":
                    if set(payload) != {"policyInput", "recommendation", "rollup"}:
                        raise CampaignPersistenceError("health event schema is invalid")
                    health = CampaignHealthRollup.from_dict(payload["rollup"])
                    policy_input = payload["policyInput"]
                    recommendation = payload["recommendation"]
                    if not isinstance(policy_input, dict) or not isinstance(recommendation, dict):
                        raise CampaignPersistenceError("health envelopes are invalid")
                    body = dict(recommendation)
                    recommendation_id = body.pop("recommendationId", None)
                    checkpoint = checkpoint_values.get(health.checkpoint_id)
                    totals = policy_input.get("totals")
                    attempts = policy_input.get("attempts")
                    novel = recommendation.get("novelProgress")
                    epoch_result = policy_input.get("epochResult")
                    expected_checkpoint = policy_input.get(
                        "nextCheckpointExpectedWallSeconds"
                    )
                    if campaign_contract is None or checkpoint is None \
                            or policy_input.get("campaignId") != campaign_id \
                            or policy_input.get("campaignDigest") != campaign_contract.digest \
                            or policy_input.get("epochId") != health.epoch_id \
                            or policy_input.get("checkpointId") != health.checkpoint_id \
                            or policy_input.get("phase") != health.phase \
                            or _digest(_json(policy_input)) != health.policy_input_digest \
                            or recommendation_id != health.recommendation_id \
                            or recommendation_id != "policy-" + _digest(_json(body)) \
                            or not isinstance(totals, dict) \
                            or not isinstance(attempts, list) or not isinstance(novel, list):
                        raise CampaignPersistenceError("health content authority is invalid")
                    if expected_checkpoint is not None and (
                        recommendation.get("action") not in {"continue", "reprioritize"}
                        or not isinstance(epoch_result, dict)
                        or not epoch_result.get("nextActionIds")
                    ):
                        raise CampaignPersistenceError(
                            "health checkpoint expectation has no scheduled work"
                        )
                    equivalence = [item["equivalenceDigest"] for item in attempts]
                    outcomes = [item["outcome"] for item in attempts]
                    prior_health = health_rollups[-1] if health_rollups else None
                    epoch_retries = len(equivalence) - len(set(equivalence))
                    epoch_no_progress = sum(item != "productive" for item in outcomes) \
                        if not novel else 0
                    _validate_health_content(
                        health, policy_input, recommendation, prior_health,
                    )
                    if (prior_health is not None
                            and health.sequence != prior_health.sequence + 1) \
                            or health.sequence != checkpoint.sequence \
                            or health.elapsed_wall_seconds \
                            != checkpoint.cumulative_usage.wall_seconds \
                            or health.coverage_basis_points \
                            != totals.get("coverageBasisPoints") \
                            or health.resolved_hypotheses != totals.get("resolvedHypotheses") \
                            or health.reproduced_findings != totals.get("reproducedFindings") \
                            or list(health.artifact_ids) != totals.get("artifactIds") \
                            or health.epoch_novel_progress != len(novel) \
                            or health.cumulative_novel_progress \
                            != (0 if prior_health is None else
                                prior_health.cumulative_novel_progress) + len(novel) \
                            or health.retry_count != epoch_retries \
                            or health.no_progress_count != epoch_no_progress \
                            or health.next_checkpoint_expected_wall_seconds \
                            != policy_input.get("nextCheckpointExpectedWallSeconds"):
                        raise CampaignPersistenceError("health facts are not canonical")
                    if prior_health is not None and (
                        health.coverage_basis_points < prior_health.coverage_basis_points
                        or health.resolved_hypotheses < prior_health.resolved_hypotheses
                        or health.reproduced_findings < prior_health.reproduced_findings
                        or not set(prior_health.artifact_ids).issubset(health.artifact_ids)
                        or health.elapsed_wall_seconds < prior_health.elapsed_wall_seconds
                    ):
                        raise CampaignPersistenceError("health history regressed")
                    health_rollups.append(health)
                elif kind == "campaign.change-request-published":
                    change = CampaignChangeRequest.from_dict(payload)
                    if change.current_campaign_id != campaign_id \
                            or change.request_id in change_request_ids:
                        raise CampaignPersistenceError("change request history is invalid")
                    change_request_ids.add(change.request_id)
                    published_changes[change.request_id] = change.to_dict()
                elif kind == "operator.decided":
                    change = CampaignChangeRequest.from_dict(payload["request"])
                    if type(payload.get("approved")) is not bool:
                        raise CampaignPersistenceError("operator decision is invalid")
                    if change.request_id in published_changes:
                        if payload["request"] != published_changes[change.request_id] \
                                or change.request_id in decided_changes:
                            raise CampaignPersistenceError("published decision content is invalid")
                        decided_changes.add(change.request_id)
                elif kind == "campaign.change-applied":
                    if payload.get("requestId") not in decided_changes:
                        raise CampaignPersistenceError("applied change request is dangling")
                elif kind != "campaign.recovered":
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
            live_health = self.health(campaign_id)
            replay_current = health_rollups[-1] if health_rollups else None
            replay_previous = health_rollups[-2] if len(health_rollups) > 1 else None
            if live_health.degraded or live_health.current != replay_current \
                    or live_health.previous != replay_previous \
                    or live_health.total_observations != len(health_rollups):
                problems.append("health projection differs from canonical replay")
                matches = False
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
