"""Concrete automation adapter over canonical Factory investigation owners."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Callable, Mapping

from muster import resolve_run_dir

from .automation import AutomationEvent, AutomationTemplate
from .control import InvestigationController, RunRequest
from .dossiers import dossier_list, verify_published_dossier
from .notification_outbox import NotificationOutbox
from .scope import AuthorizedScope, TargetGrant
from .store import FactoryLedger


def scope_reference(scope: AuthorizedScope) -> str:
    binding = json.dumps({"digest": scope.envelope.content_digest,
                          "revision": scope.envelope.revision,
                          "scopeId": scope.envelope.scope_id}, sort_keys=True)
    return "scope-binding:" + hashlib.sha256(binding.encode()).hexdigest()


@dataclass(frozen=True)
class ApprovedInvestigation:
    """Environment-owned template resolving opaque references to exact local authority."""

    template: AutomationTemplate
    root: Path
    relative_target: str
    scope: AuthorizedScope
    goal: str
    tools: tuple[str, ...] = ()
    model_tools: tuple[str, ...] = ()
    worker_roles: tuple[str, ...] = ("recon", "analyst")
    concurrency: int | None = None
    model_profile: str | None = None
    strategy: str | None = None
    retries_per_worker: int | None = None
    cost_units: int | None = None
    max_workers: int | None = None
    safety_policy_id: str | None = None

    def __post_init__(self) -> None:
        root = self.root.expanduser().resolve()
        relative = Path(self.relative_target)
        if not root.is_dir() or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("approved automation target must be relative to a configured root")
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("approved automation target escapes its configured root") from exc
        if not target.exists():
            raise ValueError("approved automation target is unavailable")
        grant = TargetGrant.from_path(target)
        if grant not in self.scope.envelope.targets:
            raise ValueError("approved automation target is absent from its exact scope")
        scope_ref = scope_reference(self.scope)
        if self.template.target_ref != grant.path_fingerprint \
                or self.template.scope_ref != scope_ref:
            raise ValueError("automation template opaque references contradict local authority")
        if not self.goal.strip() or len(self.goal) > 2048:
            raise ValueError("approved automation goal is invalid")
        object.__setattr__(self, "root", root)

    @property
    def target(self) -> Path:
        return (self.root / self.relative_target).resolve()

    def request(self) -> RunRequest:
        return RunRequest(
            self.target, self.goal, self.tools, self.model_tools, self.worker_roles,
            self.concurrency, self.model_profile, self.strategy, self.retries_per_worker,
            self.cost_units, self.max_workers, self.scope, self.safety_policy_id,
        )


class ApprovedInvestigationCatalog:
    def __init__(self, entries: Mapping[str, ApprovedInvestigation]) -> None:
        self.entries = dict(entries)
        if set(self.entries) != {item.template.template_id for item in self.entries.values()}:
            raise ValueError("approved automation catalog keys contradict template identities")

    def resolve(self, template: AutomationTemplate) -> ApprovedInvestigation:
        approved = self.entries.get(template.template_id)
        if approved is None or approved.template != template:
            raise ValueError("automation template is missing or stale")
        # Reconstruct to recheck target bytes, root containment, and scope binding at launch.
        return ApprovedInvestigation(**approved.__dict__)


class InvestigationAutomationOwner:
    """Delegate automation to InvestigationController and per-run canonical ledgers."""

    def __init__(self, controller: InvestigationController,
                 catalog: ApprovedInvestigationCatalog, *,
                 submit: Callable[[Path], object] | None = None) -> None:
        self.controller = controller
        self.catalog = catalog
        self.submit = submit

    def launch(self, template: AutomationTemplate, *, operation_id: str,
               origin: Mapping[str, object]) -> Mapping[str, object]:
        approved = self.catalog.resolve(template)
        run_dir = self.controller.create_automation_run(
            approved.request(), operation_id=operation_id,
        )
        paths = resolve_run_dir(run_dir)
        with FactoryLedger(paths.db_path) as ledger:
            ledger.event_log_once(
                paths.run_id, f"automation-origin:{operation_id}", "automation.launched",
                "Approved external schedule launched this investigation",
                payload=dict(origin),
            )
        current = self.controller.snapshot(run_dir)["run"]["status"]
        if self.submit is not None and current not in {
                "completed", "failed", "blocked", "canceled", "cancelled"}:
            self.submit(run_dir)
        return self.status(paths.run_id)

    def cancel(self, run_id: str, *, reason_code: str,
               operation_id: str) -> Mapping[str, object]:
        run_dir = self._run_dir(run_id)
        result = self.controller.request_cancel(
            run_dir, operation_id=operation_id, reason_code=reason_code,
        )
        if self.submit is not None:
            self.submit(run_dir)
        return {"schemaVersion": 1, "runId": run_id,
                "status": result["status"],
                "deepLink": self._run_link(run_id)}

    def status(self, run_id: str) -> Mapping[str, object]:
        snapshot = self.controller.snapshot(self._run_dir(run_id))
        run = snapshot["run"]
        meta = snapshot["meta"]
        handoff_id = self._handoff_id(snapshot)
        return {
            "schemaVersion": 1, "runId": run_id, "projectId": meta["projectId"],
            "status": run["status"], "terminal": run["status"] in {
                "completed", "failed", "blocked", "canceled", "cancelled",
            },
            "handoffId": handoff_id, "deepLink": self._run_link(run_id),
            "dossierCount": sum(item.get("verificationStatus") == "published"
                                for item in snapshot.get("dossiers", [])),
        }

    def acknowledge_handoff(self, run_id: str, handoff_id: str, *,
                            operation_id: str) -> Mapping[str, object]:
        run_dir = self._run_dir(run_id)
        snapshot = self.controller.snapshot(run_dir)
        expected = self._handoff_id(snapshot)
        if expected is None or handoff_id != expected:
            raise ValueError("handoff identity is absent or stale")
        paths = resolve_run_dir(run_dir)
        with FactoryLedger(paths.db_path) as ledger:
            row = ledger.acknowledge_run_handoff(
                run_id, handoff_id, operation_id=operation_id,
            )
        return {"schemaVersion": 1, "runId": run_id, "handoffId": handoff_id,
                "acknowledged": True, "acknowledgedAt": row["created_at"]}

    def events(self, run_ids: tuple[str, ...]) -> tuple[AutomationEvent, ...]:
        events: list[AutomationEvent] = []
        for run_id in run_ids:
            try:
                run_dir = self._run_dir(run_id)
                snapshot = self.controller.snapshot(run_dir)
                paths = resolve_run_dir(run_dir)
            except (FileNotFoundError, KeyError, OSError, ValueError):
                continue
            with FactoryLedger(paths.db_path) as ledger:
                for record in NotificationOutbox(ledger.conn).public_records():
                    payload = record["payload"]
                    events.append(AutomationEvent(
                        record["id"], run_id, payload["kind"], {
                            "deepLink": payload["deepLink"],
                            "message": payload["message"],
                            "severity": payload["severity"],
                        },
                    ))
                dossiers = dossier_list(ledger, run_id, run_dir=run_dir)
            for dossier in dossiers:
                if not dossier["verified"]:
                    continue
                events.append(AutomationEvent(
                    "proof-" + dossier["manifestSha256"], run_id, "proof.available", {
                        "deepLink": f"/api/runs/{run_id}/dossiers/{dossier['id']}",
                        "dossierId": dossier["id"], "verified": True,
                    },
                ))
            status = snapshot["run"]["status"]
            if status in {"completed", "failed", "blocked", "canceled", "cancelled"}:
                events.append(AutomationEvent(
                    f"terminal-{run_id}-{status}", run_id, "run.terminal", {
                        "deepLink": self._run_link(run_id), "status": status,
                    },
                ))
        return tuple(events)

    def dossier(self, run_id: str, dossier_id: str) -> Mapping[str, object]:
        run_dir = self._run_dir(run_id)
        paths = resolve_run_dir(run_dir)
        with FactoryLedger(paths.db_path) as ledger:
            dossier = next((item for item in dossier_list(
                ledger, run_id, run_dir=run_dir,
            ) if item["id"] == dossier_id), None)
        if dossier is None or not dossier["verified"]:
            raise FileNotFoundError("verified dossier is unavailable")
        verify_published_dossier(run_dir, dossier)
        return {"schemaVersion": 1, "runId": run_id, "dossierId": dossier_id,
                "findingId": dossier["findingId"],
                "manifestSha256": dossier["manifestSha256"], "verified": True,
                "deepLink": f"/api/runs/{run_id}/dossiers/{dossier_id}",
                "downloadLink": f"/api/runs/{run_id}/dossiers/{dossier_id}/download"}

    def _run_dir(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or len(run_id) > 128:
            raise FileNotFoundError("unknown automation run")
        matches = []
        for path in self.controller.storage_root.glob("projects/*/runs/*/run.json"):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if meta.get("runId") == run_id and meta.get("creationComplete") is not False:
                matches.append(path.parent)
        if len(matches) != 1:
            raise FileNotFoundError("unknown or ambiguous automation run")
        return matches[0]

    @staticmethod
    def _run_link(run_id: str) -> str:
        return f"/mission-control?run={run_id}"

    @staticmethod
    def _handoff_id(snapshot: Mapping[str, object]) -> str | None:
        run = snapshot["run"]
        if run["status"] not in {"completed", "failed", "blocked", "canceled", "cancelled"}:
            return None
        dossier_ids = sorted(item["id"] for item in snapshot.get("dossiers", []))
        binding = json.dumps({"completedAt": run["completed_at"],
                              "dossiers": dossier_ids, "runId": run["id"],
                              "status": run["status"]}, sort_keys=True)
        return "handoff-" + hashlib.sha256(binding.encode()).hexdigest()
