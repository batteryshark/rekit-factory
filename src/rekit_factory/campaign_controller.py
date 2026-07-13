"""Restart-safe composition of bounded campaign contracts, policy, and Factory runs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Callable, Protocol

from .campaign_contracts import (
    CampaignCheckpoint, CampaignContract, CampaignRiskAssessment, CampaignRiskMeasurement,
    CheckpointSource,
    EpochContract, EpochResult, ProgressSignal, ResourceUsage, ScopeBinding, TerminalOutcome,
)
from .campaign_lifecycle import CAMPAIGN_AUTHORITY, CampaignLifecycleStore
from .notification_configuration import NotificationConfigurationStore
from .notification_supervisor import NotificationDeliverySupervisor
from .campaign_persistence import (
    CampaignHealthRollup, CampaignPersistence, CampaignPersistenceError,
)
from .campaign_policy import (
    AttemptFact, BudgetAccount, BudgetReservation, CampaignPhase, CampaignPolicyConfig,
    CampaignPolicyInput, CampaignPolicyRecommendation, CanonicalOutcomeTotals,
    evaluate_campaign_policy, validate_campaign_change_approval,
)
from .control import InvestigationController, RunRequest


CONTROLLER_BOUNDARIES = (
    "launched", "execution-staged", "checkpointed", "recommendation-staged",
    "recommendation-effect", "health-recorded", "recommendation-applied",
)
DEFAULT_NOTIFICATION_BUDGET_THRESHOLDS = {"costUnits": [8000, 10000]}


class CampaignControllerError(ValueError):
    """The requested operation conflicts with durable campaign authority."""


class CampaignControllerInterrupted(BaseException):
    """Test-only process interruption at a controller reconciliation boundary."""


def _serialized(method):
    """Serialize one controller and its shared SQLite connection across API/scheduler threads."""
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


@dataclass(frozen=True)
class CampaignRunRequest:
    campaign: CampaignContract
    epoch: EpochContract
    lease_id: str
    reservation: ResourceUsage
    owner_id: str
    committed_usage: ResourceUsage = ResourceUsage()


@dataclass(frozen=True)
class EpochExecution:
    campaign_id: str
    epoch_id: str
    lease_id: str
    factory_run_id: str
    project_id: str
    scope: ScopeBinding
    checkpoint_sources: tuple[CheckpointSource, ...]
    cumulative_usage: ResourceUsage
    result: EpochResult
    evidence_ids: tuple[str, ...]
    risk_assessment: CampaignRiskAssessment | None = None

    def __post_init__(self) -> None:
        if self.result.epoch_id != self.epoch_id:
            raise CampaignControllerError("epoch result belongs to another execution")
        if not self.checkpoint_sources:
            raise CampaignControllerError("execution requires canonical checkpoint sources")
        if not self.evidence_ids:
            raise CampaignControllerError("execution requires bounded evidence references")
        if len(self.evidence_ids) > 64:
            raise CampaignControllerError("execution evidence references exceed the finite limit")
        if self.risk_assessment is not None \
                and type(self.risk_assessment) is not CampaignRiskAssessment:
            raise CampaignControllerError("execution risk must be an explicit typed assessment")


class EpochRunner(Protocol):
    def run(self, request: CampaignRunRequest) -> EpochExecution: ...


@dataclass(frozen=True)
class EpochLaunch:
    campaign_id: str
    epoch_id: str
    lease_id: str
    reservation_id: str


@dataclass(frozen=True)
class CampaignSnapshot:
    campaign_id: str
    status: str
    current_epoch_id: str | None
    latest_checkpoint_id: str | None
    cumulative_usage: ResourceUsage
    recommendation_id: str | None
    terminal: TerminalOutcome | None


@dataclass(frozen=True)
class CampaignHandoff:
    campaign_id: str
    status: str
    reason_code: str
    checkpoint_id: str | None
    evidence_ids: tuple[str, ...]
    factory_run_ids: tuple[str, ...]
    evidence_count: int
    factory_run_count: int
    truncated: bool


SCHEMA = """
create table if not exists factory_campaign_controller_epochs (
    campaign_id text not null,
    epoch_id text primary key,
    owner_id text not null,
    lease_id text not null unique,
    reservation_id text not null unique,
    reservation_json text not null,
    execution_json text,
    recommendation_id text,
    recommendation_json text,
    policy_input_digest text,
    policy_input_json text,
    health_rollup_json text,
    factory_run_id text,
    recommendation_applied integer not null default 0,
    recommendation_disposition text not null default 'pending'
);
drop index if exists idx_campaign_factory_run;
create unique index if not exists idx_campaign_factory_run
    on factory_campaign_controller_epochs(campaign_id,factory_run_id)
    where factory_run_id is not null;
"""


def _json(value: object) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False,
                      separators=(",", ":"), sort_keys=True)


def _usage_add(left: ResourceUsage, right: ResourceUsage) -> ResourceUsage:
    return ResourceUsage(*(getattr(left, field) + getattr(right, field)
                           for field in ResourceUsage._ATTRIBUTES))


def _usage_fits(usage: ResourceUsage, budget: object) -> bool:
    return all(getattr(usage, field) <= getattr(budget, field).value
               for field in ResourceUsage._ATTRIBUTES)


def _execution_dict(value: EpochExecution) -> dict[str, object]:
    return {
        "campaignId": value.campaign_id, "epochId": value.epoch_id,
        "leaseId": value.lease_id, "factoryRunId": value.factory_run_id,
        "projectId": value.project_id, "scope": value.scope.to_dict(),
        "checkpointSources": [item.to_dict() for item in value.checkpoint_sources],
        "cumulativeUsage": value.cumulative_usage.to_dict(),
        "result": value.result.to_dict(), "evidenceIds": list(value.evidence_ids),
        "riskAssessment": (None if value.risk_assessment is None
                           else value.risk_assessment.to_dict()),
    }


def _execution_from_dict(raw: dict[str, object]) -> EpochExecution:
    return EpochExecution(
        raw["campaignId"], raw["epochId"], raw["leaseId"], raw["factoryRunId"],
        raw["projectId"], ScopeBinding.from_dict(raw["scope"]),
        tuple(CheckpointSource.from_dict(item) for item in raw["checkpointSources"]),
        ResourceUsage.from_dict(raw["cumulativeUsage"]),
        EpochResult.from_dict(raw["result"]), tuple(raw["evidenceIds"]),
        (None if raw.get("riskAssessment") is None else
         CampaignRiskAssessment.from_dict(raw["riskAssessment"])),
    )


class CampaignController:
    """One finite, deterministic campaign state machine over canonical facts."""

    def __init__(self, persistence: CampaignPersistence, runner: EpochRunner, *,
                 owner_id: str, lifecycle: CampaignLifecycleStore | None = None,
                 policy_config: CampaignPolicyConfig = CampaignPolicyConfig(),
                 notification_budget_thresholds: dict[str, list[int]] | None = None,
                 notification_configuration: NotificationConfigurationStore | None = None,
                 fault_injector: Callable[[str], None] | None = None) -> None:
        if not owner_id or len(owner_id) > 256:
            raise CampaignControllerError("owner_id must be a bounded stable identifier")
        self.persistence = persistence
        self._lock = threading.RLock()
        self.runner = runner
        self.owner_id = owner_id
        self.lifecycle = lifecycle
        self.policy_config = policy_config
        configured_thresholds = (DEFAULT_NOTIFICATION_BUDGET_THRESHOLDS
                                 if notification_budget_thresholds is None
                                 else notification_budget_thresholds)
        self.notification_budget_thresholds = {
            name: list(values) for name, values in configured_thresholds.items()
        }
        if notification_configuration is None:
            database_path = self.persistence.conn.execute("pragma database_list").fetchone()[2]
            notification_configuration = (None if not database_path else
                NotificationConfigurationStore(
                    Path(database_path).parent / ".factory" / "notification-configuration.sqlite3"
                ))
        self.notification_configuration = notification_configuration
        self.fault_injector = fault_injector
        self.persistence.conn.executescript(SCHEMA)
        columns = {row[1] for row in self.persistence.conn.execute(
            "pragma table_info(factory_campaign_controller_epochs)"
        )}
        if "recommendation_disposition" not in columns:
            self.persistence.conn.execute(
                "alter table factory_campaign_controller_epochs add column "
                "recommendation_disposition text not null default 'pending'"
            )
        if "policy_input_json" not in columns:
            self.persistence.conn.execute(
                "alter table factory_campaign_controller_epochs add column policy_input_json text"
            )
        if "health_rollup_json" not in columns:
            self.persistence.conn.execute(
                "alter table factory_campaign_controller_epochs add column health_rollup_json text"
            )
        self.persistence.conn.commit()

    def _fault(self, boundary: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(boundary)

    def _require_canonical_projection(self, campaign_id: str) -> None:
        rebuild = self.persistence.rebuild_projection(campaign_id)
        if rebuild.degraded or not rebuild.matches_live:
            raise CampaignControllerError("campaign projection is degraded; controls fail closed")

    def _contract(self, campaign_id: str) -> CampaignContract:
        row = self.persistence.conn.execute(
            "select contract_json from factory_campaigns where campaign_id=?", (campaign_id,),
        ).fetchone()
        if row is None:
            raise CampaignControllerError("campaign does not exist")
        return CampaignContract.from_dict(json.loads(row["contract_json"]))

    def _epoch(self, campaign_id: str, epoch_id: str) -> EpochContract:
        row = self.persistence.conn.execute(
            "select contract_json from factory_campaign_epochs where campaign_id=? and epoch_id=?",
            (campaign_id, epoch_id),
        ).fetchone()
        if row is None:
            raise CampaignControllerError("epoch does not belong to campaign")
        return EpochContract.from_dict(json.loads(row["contract_json"]))

    def _lifecycle_start(self, campaign_id: str) -> None:
        if self.lifecycle is None:
            return
        state = self.lifecycle.load()
        current = next((item for item in state.campaigns if item.campaign_id == campaign_id), None)
        if current is None:
            state = state.create_campaign(campaign_id, authority=CAMPAIGN_AUTHORITY)
            current = next(item for item in state.campaigns if item.campaign_id == campaign_id)
        if current.state == "planned":
            state = state.transition_campaign(campaign_id, "active",
                                               expected_revision=current.revision,
                                               authority=CAMPAIGN_AUTHORITY)
        self.lifecycle.save(state)

    def _lifecycle_terminal(self, campaign_id: str, *, completed: bool) -> None:
        if self.lifecycle is None:
            return
        state = self.lifecycle.load()
        current = next(item for item in state.campaigns if item.campaign_id == campaign_id)
        target = "completed" if completed else "cancelled"
        if current.state == "active":
            self.lifecycle.save(state.transition_campaign(
                campaign_id, target, expected_revision=current.revision,
                authority=CAMPAIGN_AUTHORITY,
            ))

    @_serialized
    def start(self, contract: CampaignContract) -> CampaignSnapshot:
        self.persistence.create_campaign(contract, operation_id="controller-create")
        projection = self.persistence.campaign(contract.campaign_id)
        if projection.status == "requested":
            self.persistence.transition_campaign(
                contract.campaign_id, "running", authority="factory-scheduler",
                operation_id="controller-start",
            )
        self._lifecycle_start(contract.campaign_id)
        return self.snapshot(contract.campaign_id)

    @_serialized
    def record_risk_measurement(
        self, measurement: CampaignRiskMeasurement, *, operation_id: str,
    ) -> CampaignSnapshot:
        """Accept only an explicit source-bound score at the campaign authority boundary."""
        self.persistence.record_risk_measurement(
            measurement, operation_id=operation_id, failure_injector=self.fault_injector,
        )
        return self.snapshot(measurement.campaign_id)

    @_serialized
    def launch(self, epoch: EpochContract, reservation: ResourceUsage) -> EpochLaunch:
        campaign = self._contract(epoch.campaign_id)
        epoch.validate_for(campaign)
        if reservation.work_items < len(epoch.work_ids):
            raise CampaignControllerError("reservation must cover every epoch work item")
        if len(epoch.work_ids) > epoch.budget.concurrency.value:
            raise CampaignControllerError("epoch work exceeds its concurrency ceiling")
        if not _usage_fits(reservation, epoch.budget):
            raise CampaignControllerError("reservation exceeds finite epoch budget")
        reservation_id = "reservation-" + hashlib.sha256(
            (epoch.epoch_id + _json(reservation.to_dict())).encode()
        ).hexdigest()
        row = self.persistence.conn.execute(
            "select * from factory_campaign_controller_epochs where epoch_id=?", (epoch.epoch_id,),
        ).fetchone()
        if row is not None:
            persisted_epoch = self.persistence.conn.execute(
                "select campaign_id,contract_json from factory_campaign_epochs where epoch_id=?",
                (epoch.epoch_id,),
            ).fetchone()
            if row["campaign_id"] != epoch.campaign_id \
                    or row["reservation_id"] != reservation_id \
                    or row["reservation_json"] != _json(reservation.to_dict()) \
                    or row["owner_id"] != self.owner_id \
                    or persisted_epoch is None \
                    or persisted_epoch["campaign_id"] != epoch.campaign_id \
                    or persisted_epoch["contract_json"] != _json(epoch.to_dict()):
                raise CampaignControllerError("epoch launch conflicts with durable reservation")
            return EpochLaunch(epoch.campaign_id, epoch.epoch_id, row["lease_id"], reservation_id)
        projection = self.persistence.campaign(epoch.campaign_id)
        reserved = ResourceUsage()
        for row in self.persistence.conn.execute(
            "select reservation_json from factory_campaign_controller_epochs "
            "where campaign_id=? and execution_json is null", (epoch.campaign_id,),
        ):
            reserved = _usage_add(reserved, ResourceUsage.from_dict(json.loads(row[0])))
        if not _usage_fits(_usage_add(_usage_add(projection.cumulative_usage, reserved), reservation),
                           campaign.cumulative_budget):
            raise CampaignControllerError("reservation exceeds remaining cumulative budget")
        self.persistence.publish_epoch(epoch, operation_id=f"publish:{epoch.epoch_id}")
        lease_id = self.persistence.acquire_epoch_lease(
            epoch.campaign_id, epoch.epoch_id, self.owner_id,
            operation_id=f"lease:{epoch.epoch_id}",
        )
        with self.persistence.conn:
            self.persistence.conn.execute(
                "insert into factory_campaign_controller_epochs "
                "(campaign_id,epoch_id,owner_id,lease_id,reservation_id,reservation_json) "
                "values (?,?,?,?,?,?)", (epoch.campaign_id, epoch.epoch_id, self.owner_id,
                                          lease_id, reservation_id,
                                          _json(reservation.to_dict())),
            )
        self._fault("launched")
        return EpochLaunch(epoch.campaign_id, epoch.epoch_id, lease_id, reservation_id)

    def _stage_execution(self, execution: EpochExecution) -> None:
        row = self.persistence.conn.execute(
            "select * from factory_campaign_controller_epochs where epoch_id=?",
            (execution.epoch_id,),
        ).fetchone()
        if row is None or row["campaign_id"] != execution.campaign_id \
                or row["lease_id"] != execution.lease_id:
            raise CampaignControllerError("execution does not match launched authority")
        expected = _json(_execution_dict(execution))
        if row["execution_json"] is not None and row["execution_json"] != expected:
            raise CampaignControllerError("execution retry conflicts with durable result")
        with self.persistence.conn:
            self.persistence.conn.execute(
                "update factory_campaign_controller_epochs set execution_json=?,factory_run_id=? "
                "where epoch_id=?", (expected, execution.factory_run_id, execution.epoch_id),
            )
        self._fault("execution-staged")

    def _record_execution_risk(self, execution: EpochExecution,
                               checkpoint: CampaignCheckpoint) -> None:
        """Publish a staged producer fact only after its exact checkpoint is durable."""
        assessment = execution.risk_assessment
        if assessment is None:
            return
        source = next((item for item in checkpoint.sources
                       if item.source == assessment.checkpoint_source), None)
        if source is None:
            raise CampaignControllerError(
                "risk assessment does not bind an exact checkpoint source"
            )
        previous = self.persistence.campaign(checkpoint.campaign_id).measured_risk
        if previous is not None and previous.source.revision == source.revision:
            retry = CampaignRiskMeasurement(
                checkpoint.campaign_id, previous.sequence, assessment.score, source,
            )
            if retry != previous:
                raise CampaignControllerError(
                    "risk assessment retry conflicts with its durable checkpoint"
                )
            return
        measurement = CampaignRiskMeasurement(
            checkpoint.campaign_id, 1 if previous is None else previous.sequence + 1,
            assessment.score, source,
        )
        self.record_risk_measurement(
            measurement, operation_id=f"risk:{checkpoint.checkpoint_id}",
        )
        self._fault("risk-recorded")

    @_serialized
    def step(self, campaign_id: str, *, phase: CampaignPhase,
             totals: CanonicalOutcomeTotals,
             previous_totals: CanonicalOutcomeTotals = CanonicalOutcomeTotals(),
             known_progress_digests: tuple[str, ...] = (),
             attempts: tuple[AttemptFact, ...] = (),
             next_checkpoint_expected_wall_seconds: int | None = None) -> CampaignSnapshot:
        initial = self.persistence.campaign(campaign_id)
        if initial.status == "suspended":
            raise CampaignControllerError("campaign is not runnable while suspended")
        if initial.status == "stopped":
            raise CampaignControllerError("campaign is not runnable after operator stop")
        if self._reconcile_pending(campaign_id):
            return self.snapshot(campaign_id)
        projection = self.persistence.campaign(campaign_id)
        if projection.current_epoch_id is None:
            raise CampaignControllerError("campaign is not runnable")
        epoch = self._epoch(campaign_id, projection.current_epoch_id)
        row = self.persistence.conn.execute(
            "select * from factory_campaign_controller_epochs where epoch_id=?", (epoch.epoch_id,),
        ).fetchone()
        if row is None:
            raise CampaignControllerError("current epoch has no controller reservation")
        if row["owner_id"] != self.owner_id:
            raise CampaignControllerError("campaign epoch belongs to another controller owner")
        if row["recommendation_applied"]:
            return self.snapshot(campaign_id)
        if projection.status not in {"running", "waiting"}:
            raise CampaignControllerError("campaign is not runnable")
        if projection.status == "waiting" and row["execution_json"] is None:
            raise CampaignControllerError(
                "orphaned execution requires exact durable runner reconciliation"
            )
        if row["execution_json"] is None:
            request = CampaignRunRequest(
                self._contract(campaign_id), epoch, row["lease_id"],
                ResourceUsage.from_dict(json.loads(row["reservation_json"])), self.owner_id,
                projection.cumulative_usage,
            )
            try:
                execution = self.runner.run(request)
            except CampaignControllerError:
                raise
            except Exception as exc:
                evidence = (f"epoch-failure:{epoch.epoch_id}",)
                outcome = TerminalOutcome(campaign_id, "failed", "infrastructure-failure",
                                          evidence, projection.latest_checkpoint_id)
                self.persistence.transition_campaign(
                    campaign_id, "failed", authority="factory-scheduler",
                    operation_id=f"infrastructure-failure:{epoch.epoch_id}", terminal=outcome,
                )
                self._lifecycle_terminal(campaign_id, completed=False)
                return self.snapshot(campaign_id)
            self._stage_execution(execution)
        else:
            execution = _execution_from_dict(json.loads(row["execution_json"]))
        self._validate_execution(execution, epoch)
        checkpoint = CampaignCheckpoint(
            campaign_id, epoch.epoch_id, epoch.ordinal, execution.checkpoint_sources,
            execution.cumulative_usage,
        )
        if execution.result.checkpoint_id != checkpoint.checkpoint_id:
            raise CampaignControllerError("runner result does not bind the canonical checkpoint")
        projection = self.persistence.campaign(campaign_id)
        if projection.status == "waiting":
            self.persistence.reconcile_epoch_lease(
                campaign_id, epoch.epoch_id, self.owner_id,
                operation_id=f"reconcile:{epoch.epoch_id}",
            )
        if projection.latest_checkpoint_id != checkpoint.checkpoint_id:
            self.persistence.record_checkpoint(
                checkpoint, operation_id=f"checkpoint:{epoch.epoch_id}",
            )
        self._fault("checkpointed")
        self._record_execution_risk(execution, checkpoint)
        previous = None
        if epoch.ordinal > 1:
            prior = self.persistence.conn.execute(
                "select checkpoint_json from factory_campaign_checkpoints where campaign_id=? "
                "and sequence=?", (campaign_id, epoch.ordinal - 1),
            ).fetchone()
            if prior is None:
                raise CampaignControllerError("previous checkpoint is absent")
            previous = CampaignCheckpoint.from_dict(json.loads(prior[0]))
        recommendation = evaluate_campaign_policy(CampaignPolicyInput(
            self._contract(campaign_id), epoch, checkpoint, execution.result,
            BudgetAccount(checkpoint.cumulative_usage), phase, totals,
            previous_checkpoint=previous, previous_totals=previous_totals,
            known_progress_digests=known_progress_digests, attempts=attempts,
        ), self.policy_config)
        if next_checkpoint_expected_wall_seconds is not None \
                and (recommendation.action not in {"continue", "reprioritize"}
                     or not execution.result.next_action_ids):
            raise CampaignControllerError(
                "next checkpoint expectation requires a recommendation that schedules work"
            )
        policy_input = {
            "attempts": [{"attemptId": item.attempt_id,
                           "equivalenceDigest": item.equivalence_digest,
                           "outcome": item.outcome} for item in attempts],
            "campaignId": campaign_id,
            "campaignDigest": self._contract(campaign_id).digest,
            "checkpointId": checkpoint.checkpoint_id,
            "epochId": epoch.epoch_id,
            "epochResult": execution.result.to_dict(),
            "knownProgressDigests": list(sorted(known_progress_digests)),
            "nextCheckpointExpectedWallSeconds": next_checkpoint_expected_wall_seconds,
            "phase": phase,
            "policyConfig": {name: getattr(self.policy_config, name)
                             for name in self.policy_config.__dataclass_fields__},
            "previousCheckpointId": None if previous is None else previous.checkpoint_id,
            "previousTotals": self._totals_dict(previous_totals),
            "totals": self._totals_dict(totals),
        }
        policy_input_json = _json(policy_input)
        policy_input_digest = hashlib.sha256(policy_input_json.encode()).hexdigest()
        previous_health = self.persistence.health(campaign_id).current
        if previous_health is not None and previous_health.sequence >= checkpoint.sequence:
            raise CampaignControllerError("health projection is stale or crossed")
        equivalence = [item.equivalence_digest for item in attempts]
        epoch_retries = len(equivalence) - len(set(equivalence))
        novel_count = len(recommendation.novel_progress)
        epoch_no_progress = sum(item.outcome != "productive" for item in attempts) \
            if not novel_count else 0
        rollup = CampaignHealthRollup(
            campaign_id, epoch.epoch_id, checkpoint.checkpoint_id, policy_input_digest,
            recommendation.recommendation_id, checkpoint.sequence, phase,
            totals.coverage_basis_points, totals.resolved_hypotheses,
            totals.reproduced_findings, totals.artifact_ids, novel_count,
            (0 if previous_health is None else previous_health.cumulative_novel_progress)
            + novel_count,
            epoch_retries, epoch_no_progress,
            checkpoint.cumulative_usage.wall_seconds,
            next_checkpoint_expected_wall_seconds,
        )
        self._stage_recommendation(epoch.epoch_id, recommendation, policy_input_json, rollup)
        self._apply_recommendation(epoch, execution, recommendation)
        return self.snapshot(campaign_id)

    def _reconcile_pending(self, campaign_id: str) -> bool:
        if self.persistence.campaign(campaign_id).status in {"suspended", "stopped"}:
            return False
        pending = self.persistence.conn.execute(
            "select epoch_id,owner_id,execution_json,recommendation_json from "
            "factory_campaign_controller_epochs where campaign_id=? "
            "and recommendation_json is not null and recommendation_applied=0 order by rowid limit 1",
            (campaign_id,),
        ).fetchone()
        if pending is None:
            return False
        if pending["owner_id"] != self.owner_id:
            raise CampaignControllerError("campaign epoch belongs to another controller owner")
        execution = _execution_from_dict(json.loads(pending["execution_json"]))
        recommendation = self._recommendation_from_dict(json.loads(pending["recommendation_json"]))
        self._apply_recommendation(
            self._epoch(campaign_id, pending["epoch_id"]), execution, recommendation,
        )
        return True

    def _validate_execution(self, execution: EpochExecution, epoch: EpochContract) -> None:
        campaign = self._contract(epoch.campaign_id)
        if execution.campaign_id != epoch.campaign_id or execution.epoch_id != epoch.epoch_id:
            raise CampaignControllerError("runner crossed campaign or epoch authority")
        if execution.project_id != campaign.project_id or execution.scope != campaign.scope:
            raise CampaignControllerError("Factory run project or scope contradicts campaign")
        row = self.persistence.conn.execute(
            "select reservation_json from factory_campaign_controller_epochs where epoch_id=?",
            (epoch.epoch_id,),
        ).fetchone()
        reservation = ResourceUsage.from_dict(json.loads(row[0]))
        previous = ResourceUsage()
        if epoch.ordinal > 1:
            prior = self.persistence.conn.execute(
                "select checkpoint_json from factory_campaign_checkpoints where campaign_id=? "
                "and sequence=?", (epoch.campaign_id, epoch.ordinal - 1),
            ).fetchone()
            previous = CampaignCheckpoint.from_dict(json.loads(prior[0])).cumulative_usage
        differences = tuple(getattr(execution.cumulative_usage, field) - getattr(previous, field)
                            for field in ResourceUsage._ATTRIBUTES)
        if any(value < 0 for value in differences):
            raise CampaignControllerError("runner cumulative usage regressed")
        delta = ResourceUsage(*differences)
        if not _usage_fits(execution.cumulative_usage, campaign.cumulative_budget):
            raise CampaignControllerError("cumulative usage exceeds campaign ceiling")
        if not _usage_fits(delta, epoch.budget) or not _usage_fits(delta, type("B", (), {
                field: type("L", (), {"value": getattr(reservation, field)})()
                for field in ResourceUsage._ATTRIBUTES})()):
            raise CampaignControllerError("actual usage exceeds epoch reservation")

    @staticmethod
    def _totals_dict(value: CanonicalOutcomeTotals) -> dict[str, object]:
        return {"artifactIds": list(value.artifact_ids),
                "coverageBasisPoints": value.coverage_basis_points,
                "reproducedFindings": value.reproduced_findings,
                "resolvedHypotheses": value.resolved_hypotheses}

    @staticmethod
    def _recommendation_from_dict(raw: dict[str, object]) -> CampaignPolicyRecommendation:
        recommendation = CampaignPolicyRecommendation(
            raw["action"], raw["reasonCode"], raw["explanation"], raw["nextPhase"],
            tuple(ProgressSignal.from_dict(item) for item in raw["novelProgress"]),
            tuple(raw["limitingResources"]),
        )
        if raw["recommendationId"] != recommendation.recommendation_id:
            raise CampaignControllerError("durable recommendation identity is corrupt")
        return recommendation

    def _stage_recommendation(self, epoch_id: str,
                              recommendation: CampaignPolicyRecommendation,
                              policy_input_json: str,
                              rollup: CampaignHealthRollup) -> None:
        encoded = _json(recommendation.to_dict())
        policy_input_digest = hashlib.sha256(policy_input_json.encode()).hexdigest()
        rollup_json = _json(rollup.to_dict())
        row = self.persistence.conn.execute(
            "select recommendation_id,recommendation_json,policy_input_digest,"
            "policy_input_json,health_rollup_json from "
            "factory_campaign_controller_epochs where epoch_id=?", (epoch_id,),
        ).fetchone()
        if row[0] is not None and (row[0] != recommendation.recommendation_id
                                  or row[1] != encoded or row[2] != policy_input_digest
                                  or row[3] != policy_input_json or row[4] != rollup_json):
            raise CampaignControllerError("policy retry conflicts with durable recommendation")
        with self.persistence.conn:
            self.persistence.conn.execute(
                "update factory_campaign_controller_epochs set recommendation_id=?,"
                "recommendation_json=?,policy_input_digest=?,policy_input_json=?,"
                "health_rollup_json=? where epoch_id=?",
                (recommendation.recommendation_id, encoded, policy_input_digest,
                 policy_input_json, rollup_json, epoch_id),
            )
        self._fault("recommendation-staged")

    def _apply_recommendation(self, epoch: EpochContract, execution: EpochExecution,
                              recommendation: CampaignPolicyRecommendation) -> None:
        campaign_id = epoch.campaign_id
        evidence = tuple(sorted(set((*execution.evidence_ids,
                                     *(item.reference_id for item in execution.result.progress)))))
        terminal_map = {
            "success": ("completed", "factory-scheduler"),
            "exhausted": ("exhausted", "factory-scheduler"),
            "policy-stop": ("policy-stopped", "validator-policy"),
        }
        if recommendation.action in terminal_map:
            status, authority = terminal_map[recommendation.action]
            outcome = TerminalOutcome(campaign_id, status, recommendation.reason_code, evidence,
                                      execution.result.checkpoint_id)
            self.persistence.transition_campaign(
                campaign_id, status, authority=authority,
                operation_id=f"apply:{recommendation.recommendation_id}", terminal=outcome,
            )
            self._lifecycle_terminal(campaign_id, completed=status == "completed")
        elif recommendation.action == "ask-operator" \
                and recommendation.reason_code == "dependency-deadlock":
            outcome = TerminalOutcome(campaign_id, "blocked", recommendation.reason_code,
                                      evidence, execution.result.checkpoint_id)
            self.persistence.transition_campaign(
                campaign_id, "blocked", authority="factory-scheduler",
                operation_id=f"apply:{recommendation.recommendation_id}", terminal=outcome,
            )
            self._lifecycle_terminal(campaign_id, completed=False)
        elif recommendation.action in {"ask-operator", "suspend", "backoff"}:
            target = "suspended" if recommendation.action == "suspend" else "waiting"
            self.persistence.transition_campaign(
                campaign_id, target, authority="factory-scheduler",
                operation_id=f"apply:{recommendation.recommendation_id}",
            )
        else:
            if not execution.result.next_action_ids:
                outcome = TerminalOutcome(campaign_id, "blocked", "no-runnable-next-actions",
                                          evidence, execution.result.checkpoint_id)
                self.persistence.transition_campaign(
                    campaign_id, "blocked", authority="factory-scheduler",
                    operation_id=f"apply:{recommendation.recommendation_id}", terminal=outcome,
                )
                self._lifecycle_terminal(campaign_id, completed=False)
            else:
                next_epoch = EpochContract(
                    campaign_id, epoch.ordinal + 1, execution.result.next_action_ids,
                    self._contract(campaign_id).epoch_budget, execution.result.checkpoint_id,
                )
                campaign = self._contract(campaign_id)
                values = []
                for field in ResourceUsage._ATTRIBUTES:
                    remaining = (getattr(campaign.cumulative_budget, field).value
                                 - getattr(execution.cumulative_usage, field))
                    values.append(min(getattr(next_epoch.budget, field).value, remaining))
                reservation = ResourceUsage(*values)
                if reservation.work_items < len(next_epoch.work_ids):
                    outcome = TerminalOutcome(
                        campaign_id, "exhausted", "insufficient-next-epoch-budget",
                        evidence, execution.result.checkpoint_id,
                    )
                    self.persistence.transition_campaign(
                        campaign_id, "exhausted", authority="factory-scheduler",
                        operation_id=f"apply:{recommendation.recommendation_id}", terminal=outcome,
                    )
                    self._lifecycle_terminal(campaign_id, completed=False)
                else:
                    self.launch(next_epoch, reservation)
        self._fault("recommendation-effect")
        staged = self.persistence.conn.execute(
            "select recommendation_json,policy_input_json,health_rollup_json from "
            "factory_campaign_controller_epochs where campaign_id=? and epoch_id=?",
            (campaign_id, epoch.epoch_id),
        ).fetchone()
        if staged is None or any(staged[index] is None for index in range(3)):
            raise CampaignControllerError("applied recommendation lacks staged health authority")
        rollup = CampaignHealthRollup.from_dict(json.loads(staged[2]))
        if rollup.recommendation_id != recommendation.recommendation_id:
            raise CampaignControllerError("staged health crossed recommendation authority")
        self.persistence.record_health_rollup(
            rollup, policy_input_json=staged[1], recommendation_json=staged[0],
            operation_id=f"health:{recommendation.recommendation_id}",
        )
        self._fault("health-recorded")
        with self.persistence.conn:
            self.persistence.conn.execute(
                "update factory_campaign_controller_epochs set recommendation_applied=1,"
                "recommendation_disposition='applied' "
                "where epoch_id=?", (epoch.epoch_id,),
            )
        self._fault("recommendation-applied")

    @_serialized
    def pause(self, campaign_id: str, *, operation_id: str | None = None,
              expected_revision: int | None = None) -> CampaignSnapshot:
        self._require_canonical_projection(campaign_id)
        revision = self.persistence.campaign(campaign_id).revision
        self.persistence.transition_campaign(campaign_id, "suspended",
            authority="factory-scheduler",
            operation_id=operation_id or f"operator-pause:{revision}",
            expected_revision=expected_revision)
        return self.snapshot(campaign_id)

    @_serialized
    def resume(self, campaign_id: str, *, operation_id: str | None = None,
               expected_revision: int | None = None) -> CampaignSnapshot:
        self._require_canonical_projection(campaign_id)
        revision = self.persistence.campaign(campaign_id).revision
        self.persistence.transition_campaign(campaign_id, "running",
            authority="factory-scheduler",
            operation_id=operation_id or f"operator-resume:{revision}",
            expected_revision=expected_revision)
        return self.snapshot(campaign_id)

    @_serialized
    def stop(self, campaign_id: str, reason_code: str,
             evidence_ids: tuple[str, ...], *, operation_id: str = "operator-stop",
             expected_revision: int | None = None) -> CampaignSnapshot:
        self._require_canonical_projection(campaign_id)
        pending = self.persistence.conn.execute(
            "select epoch_id,execution_json,recommendation_json from "
            "factory_campaign_controller_epochs where campaign_id=? "
            "and recommendation_json is not null and recommendation_applied=0 limit 1",
            (campaign_id,),
        ).fetchone()
        if pending is not None:
            recommendation = self._recommendation_from_dict(json.loads(pending[2]))
            effect = self.persistence.conn.execute(
                "select 1 from factory_campaign_events where campaign_id=? and operation_id=?",
                (campaign_id, f"apply:{recommendation.recommendation_id}"),
            ).fetchone()
            epoch = self._epoch(campaign_id, pending[0])
            next_epoch = self.persistence.conn.execute(
                "select 1 from factory_campaign_epochs where campaign_id=? and ordinal=?",
                (campaign_id, epoch.ordinal + 1),
            ).fetchone()
            if effect is not None or next_epoch is not None:
                if pending[1] is None:
                    raise CampaignControllerError(
                        "started recommendation effect lacks staged execution authority"
                    )
                self._apply_recommendation(
                    epoch, _execution_from_dict(json.loads(pending[1])), recommendation,
                )
        projection = self.persistence.campaign(campaign_id)
        outcome = TerminalOutcome(campaign_id, "stopped", reason_code, evidence_ids,
                                  projection.latest_checkpoint_id)
        self.persistence.transition_campaign(campaign_id, "stopped", authority="operator",
                                             operation_id=operation_id, terminal=outcome,
                                             expected_revision=expected_revision)
        with self.persistence.conn:
            self.persistence.conn.execute(
                "update factory_campaign_controller_epochs set recommendation_applied=1,"
                "recommendation_disposition='superseded-by-operator-stop' "
                "where campaign_id=? and recommendation_json is not null "
                "and recommendation_applied=0", (campaign_id,),
            )
        self._lifecycle_terminal(campaign_id, completed=False)
        return self.snapshot(campaign_id)

    @_serialized
    def publish_change_request(self, request) -> dict[str, object]:
        projection = self.persistence.publish_change_request(
            request, operation_id=f"publish:{request.request_id}",
        )
        return self._public_change_request(projection)

    @_serialized
    def decide_change_request(self, campaign_id: str, request_id: str, *, approved: bool,
                              expected_revision: int, operation_id: str
                              ) -> tuple[dict[str, object], str | None]:
        projection = self.persistence.decide_change_request(
            campaign_id, request_id, approved=approved,
            expected_revision=expected_revision, operation_id=operation_id,
        )
        approved_campaign_id = None
        if projection.status == "approved":
            approved_campaign_id = self._apply_approved_change(projection)
            projection = self.persistence.change_request(campaign_id, request_id)
        return self._public_change_request(projection), approved_campaign_id

    def _apply_approved_change(self, projection) -> str:
        request = projection.request
        proposed = validate_campaign_change_approval(
            self._contract(request.current_campaign_id), request, request.request_id,
        )
        current = self.persistence.campaign(request.current_campaign_id)
        if current.status != "stopped":
            self.stop(request.current_campaign_id, "approved-campaign-change",
                      (f"campaign-change:{request.request_id}",),
                      operation_id=f"apply-stop:{request.request_id}")
        self.start(proposed)
        self.persistence.mark_change_applied(request.current_campaign_id, request.request_id)
        return proposed.campaign_id

    def _public_change_request(self, projection) -> dict[str, object]:
        request = projection.request
        current_id = request.current_campaign_id
        current = self._contract(current_id).to_dict()
        proposed = request.proposed.to_dict()
        # Goal and arbitrary reason text remain private canonical authority.
        return {
            "applicationStatus": projection.application_status,
            "baseCampaignRevision": projection.base_campaign_revision,
            "currentCampaignId": current_id,
            "proposedCampaignId": request.proposed.campaign_id,
            "requestId": request.request_id,
            "revision": projection.revision,
            "status": projection.status,
            "diff": {
                name: {"current": current[source], "proposed": proposed[source]}
                for name, source in (
                    ("scope", "scope"), ("epochBudget", "epochBudget"),
                    ("cumulativeBudget", "cumulativeBudget"),
                    ("completion", "completion"),
                    ("operatorPolicy", "operatorPolicy"),
                    ("componentVersions", "components"),
                )
            },
        }

    @_serialized
    def recover(self, campaign_id: str) -> CampaignSnapshot:
        for change in self.persistence.change_requests(campaign_id):
            if change.status == "approved" and change.application_status == "pending":
                self._apply_approved_change(change)
        initial = self.persistence.campaign(campaign_id)
        if initial.status in {"suspended", "stopped"}:
            return self.snapshot(campaign_id)
        if self._reconcile_pending(campaign_id):
            return self.snapshot(campaign_id)
        projection = self.persistence.campaign(campaign_id)
        if projection.status == "running":
            projection = self.persistence.recover(
                campaign_id, operation_id=f"controller-recover:{projection.revision}",
            )
        if projection.status == "waiting" and projection.current_epoch_id:
            row = self.persistence.conn.execute(
                "select execution_json from factory_campaign_controller_epochs where epoch_id=?",
                (projection.current_epoch_id,),
            ).fetchone()
            if row is None or row[0] is None:
                return self.snapshot(campaign_id)
        return self.snapshot(campaign_id)

    @_serialized
    def snapshot(self, campaign_id: str) -> CampaignSnapshot:
        projection = self.persistence.campaign(campaign_id)
        recommendation = None
        if projection.current_epoch_id:
            row = self.persistence.conn.execute(
                "select recommendation_id from factory_campaign_controller_epochs where epoch_id=?",
                (projection.current_epoch_id,),
            ).fetchone()
            recommendation = None if row is None else row[0]
        return CampaignSnapshot(campaign_id, projection.status, projection.current_epoch_id,
                                projection.latest_checkpoint_id, projection.cumulative_usage,
                                recommendation, projection.terminal)

    @_serialized
    def handoff(self, campaign_id: str) -> CampaignHandoff:
        snapshot = self.snapshot(campaign_id)
        rows = self.persistence.conn.execute(
            "select factory_run_id,execution_json from factory_campaign_controller_epochs "
            "where campaign_id=? order by rowid", (campaign_id,),
        ).fetchall()
        evidence: set[str] = set()
        run_ids: list[str] = []
        for row in rows:
            if row[0]:
                run_ids.append(row[0])
            if row[1]:
                evidence.update(json.loads(row[1])["evidenceIds"])
        recommendation_row = self.persistence.conn.execute(
            "select recommendation_json from factory_campaign_controller_epochs "
            "where campaign_id=? and recommendation_json is not null order by rowid desc limit 1",
            (campaign_id,),
        ).fetchone()
        reason = (snapshot.terminal.reason_code if snapshot.terminal else
                  (json.loads(recommendation_row[0])["reasonCode"]
                   if recommendation_row is not None else snapshot.status))
        evidence_all = tuple(sorted(evidence))
        runs_all = tuple(run_ids)
        evidence_tail, run_tail = evidence_all[-32:], runs_all[-32:]
        return CampaignHandoff(
            campaign_id, snapshot.status, reason, snapshot.latest_checkpoint_id,
            evidence_tail, run_tail, len(evidence_all), len(runs_all),
            len(evidence_all) > len(evidence_tail) or len(runs_all) > len(run_tail),
        )

    @_serialized
    def campaign_ids(self) -> tuple[str, ...]:
        """Return only canonical campaign identities in stable display order."""
        rows = self.persistence.conn.execute(
            "select campaign_id from factory_campaigns order by updated_at desc,campaign_id"
        ).fetchall()
        return tuple(row[0] for row in rows)

    @_serialized
    def notification_proof_contexts(self, source_run_id: str) -> tuple[dict[str, object], ...]:
        """Return exact, bounded handoff ownership for one Factory run.

        A truncated handoff cannot prove that an older omitted run does not own a competing
        proof child, so it is deliberately ineligible for notification link qualification.
        """
        if type(source_run_id) is not str or not source_run_id:
            return ()
        contexts: list[dict[str, object]] = []
        for campaign_id in self.campaign_ids():
            handoff = self.handoff(campaign_id)
            if (handoff.factory_run_count > len(handoff.factory_run_ids)
                    or source_run_id not in handoff.factory_run_ids):
                continue
            contract = self._contract(campaign_id)
            contexts.append({
                "campaignId": campaign_id,
                "projectId": contract.project_id,
                "scope": contract.scope.to_dict(),
                "factoryRunIds": list(handoff.factory_run_ids),
            })
        return tuple(contexts)

    @_serialized
    def public_state(self, campaign_id: str) -> dict[str, object]:
        """Bounded Mission Control projection; never includes logs, paths, or transcripts."""
        snapshot = self.snapshot(campaign_id)
        projection = self.persistence.campaign(campaign_id)
        contract = self._contract(campaign_id)
        epoch = (None if snapshot.current_epoch_id is None else
                 self._epoch(campaign_id, snapshot.current_epoch_id))
        recommendation = disposition = None
        if snapshot.current_epoch_id is not None:
            row = self.persistence.conn.execute(
                "select recommendation_json,recommendation_disposition from "
                "factory_campaign_controller_epochs where campaign_id=? and epoch_id=?",
                (campaign_id, snapshot.current_epoch_id),
            ).fetchone()
            if row is not None:
                recommendation = None if row[0] is None else json.loads(row[0])
                disposition = None if row[0] is None else row[1]
        usage = snapshot.cumulative_usage
        remaining = ResourceUsage(*(
            max(0, getattr(contract.cumulative_budget, field).value - getattr(usage, field))
            for field in ResourceUsage._ATTRIBUTES
        ))
        handoff = self.handoff(campaign_id)
        rebuild = self.persistence.rebuild_projection(campaign_id)
        degraded = rebuild.degraded or not rebuild.matches_live
        health = self.persistence.health(campaign_id)
        health_degraded = degraded or health.degraded
        health_problem_codes = tuple(dict.fromkeys(
            (*rebuild.problem_codes, *health.problem_codes)
        ))[:16]
        def health_item(value: CampaignHealthRollup | None) -> dict[str, object] | None:
            if value is None:
                return None
            return {
                "artifactCount": len(value.artifact_ids),
                "checkpointId": value.checkpoint_id,
                "coverageBasisPoints": value.coverage_basis_points,
                "cumulativeNovelProgress": value.cumulative_novel_progress,
                "elapsedWallSeconds": value.elapsed_wall_seconds,
                "epochId": value.epoch_id,
                "epochNovelProgress": value.epoch_novel_progress,
                "nextCheckpointExpectedWallSeconds":
                    value.next_checkpoint_expected_wall_seconds,
                "noProgressCount": value.no_progress_count,
                "phase": value.phase,
                "recommendationId": value.recommendation_id,
                "reproducedFindings": value.reproduced_findings,
                "resolvedHypotheses": value.resolved_hypotheses,
                "retryCount": value.retry_count,
                "sequence": value.sequence,
            }
        allowed = () if degraded else {
            "requested": ("stop",),
            "running": ("pause", "stop"),
            "waiting": ("pause", "stop"),
            "suspended": ("resume", "stop"),
        }.get(snapshot.status, ())
        state = {
            "schemaVersion": 1,
            "campaignId": campaign_id,
            "projectId": contract.project_id,
            "scope": contract.scope.to_dict(),
            "status": snapshot.status,
            "revision": projection.revision,
            "health": {
                "current": None if health_degraded else health_item(health.current),
                "degraded": health_degraded,
                "previous": None if health_degraded else health_item(health.previous),
                "problemCodes": list(health_problem_codes),
                "problemCount": min(len(rebuild.problems), 16),
                "problemsTruncated": len(rebuild.problems) > 16,
                "totalObservations": health.total_observations,
            },
            "currentEpoch": None if epoch is None else {
                "epochId": epoch.epoch_id,
                "ordinal": epoch.ordinal,
                "workIds": list(epoch.work_ids),
            },
            "latestCheckpointId": snapshot.latest_checkpoint_id,
            "cumulativeUsage": usage.to_dict(),
            "measuredRisk": (None if projection.measured_risk is None
                             else projection.measured_risk.to_dict()),
            "riskPolicy": {
                "continueAfterThresholdRequiresApproval":
                    contract.operator_policy.continue_after_risk_requires_approval,
                "threshold": contract.operator_policy.risk_threshold,
            },
            "budget": {
                "epoch": contract.epoch_budget.to_dict(),
                "cumulative": contract.cumulative_budget.to_dict(),
                "remaining": remaining.to_dict(),
            },
            "recommendation": recommendation,
            "recommendationDisposition": disposition,
            "terminal": None if snapshot.terminal is None else snapshot.terminal.to_dict(),
            "handoff": {
                "status": handoff.status,
                "reasonCode": handoff.reason_code,
                "checkpointId": handoff.checkpoint_id,
                "evidenceIds": list(handoff.evidence_ids),
                "factoryRunIds": list(handoff.factory_run_ids),
                "evidenceCount": handoff.evidence_count,
                "factoryRunCount": handoff.factory_run_count,
                "truncated": handoff.truncated,
            },
            "allowedActions": list(allowed),
            "changeRequests": [self._public_change_request(item)
                               for item in self.persistence.change_requests(campaign_id)],
        }
        try:
            self.persistence.admit_notification_state(
                campaign_id, state,
                budget_thresholds=self.notification_budget_thresholds,
                failure_injector=self.fault_injector,
            )
        except Exception:
            # Notification observation is downstream: canonical campaign reads and
            # progress remain available when admission is unavailable or corrupt.
            pass
        else:
            if self.notification_configuration is not None:
                try:
                    preference, channel_refs = self.notification_configuration.selected_delivery()
                    supervisor = NotificationDeliverySupervisor(self.persistence.conn)
                    supervisor.schedule_unscheduled(
                        preference, project_id=contract.project_id,
                        campaign_id=campaign_id, channel_refs=channel_refs,
                        routing_id=campaign_id,
                    )
                except (sqlite3.Error, ValueError, TypeError, KeyError, json.JSONDecodeError):
                    pass
        return state


class InvestigationEpochRunner:
    """Concrete adapter deriving campaign facts from canonical Factory snapshots."""

    def __init__(self, controller: InvestigationController,
                 request_factory: Callable[[CampaignRunRequest], RunRequest],
                 fact_projector: Callable[[dict[str, object], CampaignRunRequest], EpochExecution]):
        self.controller = controller
        self.request_factory = request_factory
        self.fact_projector = fact_projector

    def run(self, request: CampaignRunRequest) -> EpochExecution:
        run_request = self.request_factory(request)
        run_dir = self.controller.create(run_request)
        # create() is deterministic and drive() is the canonical restart-safe execution path.
        import asyncio
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise CampaignControllerError(
                "InvestigationEpochRunner.run must execute outside an active event loop"
            )
        snapshot = asyncio.run(self.controller.drive(run_dir))
        run = snapshot.get("run") or {}
        meta = snapshot.get("meta") or {}
        scope = meta.get("scope") or {}
        if run.get("id") != meta.get("runId") or meta.get("projectId") != request.campaign.project_id:
            raise CampaignControllerError("Factory snapshot identity contradicts campaign")
        if scope.get("scopeId") != request.campaign.scope.scope_id \
                or scope.get("revision") != request.campaign.scope.revision \
                or scope.get("digest") != request.campaign.scope.digest:
            raise CampaignControllerError("Factory snapshot scope contradicts campaign")
        if run.get("status") not in {"completed", "failed", "blocked"}:
            raise CampaignControllerError("Factory run is not canonically terminal")
        execution = self.fact_projector(snapshot, request)
        if execution.factory_run_id != run.get("id"):
            raise CampaignControllerError("projected execution changed Factory run identity")
        model_calls = snapshot.get("modelCalls") or []
        tool_calls = snapshot.get("toolCalls") or []
        artifacts = snapshot.get("artifacts") or []
        work_items = snapshot.get("workItems") or []
        epoch_usage = ResourceUsage(
            work_items=len(work_items),
            retries=sum(max(0, int(item.get("attempt", 1)) - 1) for item in work_items),
            input_tokens=sum(int(item.get("usage", {}).get("inputTokens", 0))
                             for item in model_calls),
            output_tokens=sum(int(item.get("usage", {}).get("outputTokens", 0))
                              for item in model_calls),
            cost_units=sum(int(item.get("usage", {}).get("costUnits", 0))
                           for item in model_calls),
            tool_calls=len(tool_calls),
            network_calls=sum(
                "network_access" in item.get("declaredActions", []) for item in tool_calls
            ),
            artifact_bytes=sum(int(item.get("size_bytes", 0)) for item in artifacts),
        )
        cumulative = _usage_add(request.committed_usage, epoch_usage)
        canonical_facts = {
            "artifacts": [{key: item.get(key) for key in ("id", "sha256", "size_bytes")}
                          for item in artifacts],
            "coverage": snapshot.get("coverage"),
            "factoryRunId": run.get("id"),
            "projectId": run.get("project_id"),
            "status": run.get("status"),
            "usage": epoch_usage.to_dict(),
            "work": [{key: item.get(key) for key in ("id", "status", "result")}
                     for item in work_items],
        }
        source = CheckpointSource(
            "factory-ledger", int(run.get("iteration", 0)),
            hashlib.sha256(_json(canonical_facts).encode()).hexdigest(),
        )
        checkpoint = CampaignCheckpoint(
            request.campaign.campaign_id, request.epoch.epoch_id, request.epoch.ordinal,
            (source,), cumulative,
        )
        result = EpochResult(
            request.epoch.epoch_id, checkpoint.checkpoint_id,
            execution.result.progress, execution.result.next_action_ids,
        )
        return EpochExecution(
            request.campaign.campaign_id, request.epoch.epoch_id, request.lease_id,
            run["id"], request.campaign.project_id, request.campaign.scope, (source,),
            cumulative, result, execution.evidence_ids, execution.risk_assessment,
        )
