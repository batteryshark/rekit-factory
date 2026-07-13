"""Durable investigation creation, supervision, permission gates, and resume."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
from types import SimpleNamespace
from typing import Any

from muster import (
    AsyncWorkDispatcher,
    RunPaths,
    atomic_write,
    compute_project_id,
    compute_run_id,
    new_run_paths,
    resolve_run_dir,
    stable_key,
    utcnow,
)

from rekit_factory.models import (
    ModelActivity,
    ModelTool,
    ModelToolResult,
    WorkerBackend,
    WorkerTurn,
)
from rekit_factory.evidence import (
    EvidenceState,
    EvidenceStore,
    Provenance,
    RetentionClass,
    default_expiry,
    hash_target,
    render_tool_output,
)
from rekit_factory.memory import EvidenceRef, MemoryAction, ProjectMemoryLog, memory_context
from rekit_factory.hypotheses import (
    HypothesisMemory,
    HypothesisUpdate,
    hypothesis_snapshot,
    test_priority,
)
from rekit_factory.findings import (
    FindingMemory,
    FindingTransition,
    ReproductionAttempt,
    finding_snapshot,
)
from rekit_factory.rekit_client import RekitAdapter
from rekit_factory.scope import (
    ActionAuthority,
    AuthorizedScope,
    ScopeDecision,
    ScopeRequest,
    TargetGrant,
    decide_scope,
    legacy_local_read_only_scope,
    opaque_ref,
)
from rekit_factory.store import FactoryLedger
from rekit_factory.strategies import (
    DEFAULT_STRATEGIES,
    FollowUpProposal,
    InvestigationPlan,
    PlannedWork,
    RunCeilings,
    Strategy,
    WorkerSeed,
    plan_investigation,
    propose_follow_up,
)


@dataclass(frozen=True)
class RunRequest:
    target: Path
    goal: str
    tools: tuple[str, ...] = ()
    model_tools: tuple[str, ...] = ()
    worker_roles: tuple[str, ...] = ("recon", "analyst")
    concurrency: int = 4
    model_profile: str | None = None
    strategy: str | None = None
    retries_per_worker: int = 1
    cost_units: int = 100
    max_workers: int = 8
    scope: AuthorizedScope | None = None

    def validate(self) -> "RunRequest":
        target = self.target.expanduser().resolve()
        if not target.exists():
            raise FileNotFoundError(target)
        if not self.goal.strip():
            raise ValueError("goal must not be empty")
        if not self.worker_roles:
            raise ValueError("at least one worker role is required")
        ceilings = RunCeilings(
            concurrency=self.concurrency,
            retries_per_worker=self.retries_per_worker,
            cost_units=self.cost_units,
            max_workers=self.max_workers,
        )
        if self.strategy is not None and self.strategy not in DEFAULT_STRATEGIES:
            raise ValueError(f"unknown worker strategy {self.strategy!r}")
        return RunRequest(
            target=target,
            goal=self.goal.strip(),
            tools=tuple(dict.fromkeys(self.tools)),
            model_tools=tuple(dict.fromkeys(self.model_tools)),
            worker_roles=tuple(dict.fromkeys(self.worker_roles)),
            concurrency=self.concurrency,
            model_profile=self.model_profile,
            strategy=self.strategy,
            retries_per_worker=ceilings.retries_per_worker,
            cost_units=ceilings.cost_units,
            max_workers=ceilings.max_workers,
            scope=self.scope,
        )


class InvestigationController:
    def __init__(self, *, storage_root: str | Path, rekit: RekitAdapter,
                 workers: WorkerBackend | dict[str, WorkerBackend]):
        self.storage_root = Path(storage_root).expanduser().resolve()
        self.rekit = rekit
        if isinstance(workers, dict):
            if not workers:
                raise ValueError("at least one worker backend is required")
            self.worker_backends = dict(workers)
        else:
            self.worker_backends = {workers.profile.name: workers}
        self.default_profile = next(iter(self.worker_backends))

    @property
    def workers(self) -> WorkerBackend:
        return self.worker_backends[self.default_profile]

    def worker_backend(self, profile_name: str | None = None) -> WorkerBackend:
        name = profile_name or self.default_profile
        try:
            return self.worker_backends[name]
        except KeyError as exc:
            raise ValueError(f"unknown model profile {name!r}") from exc

    def create(self, request: RunRequest) -> Path:
        request = request.validate()
        worker_backend = self.worker_backend(request.model_profile)
        manifests = [self.rekit.manifest(tool_id)
                     for tool_id in (*request.tools, *request.model_tools)]
        manifest_contracts = {
            manifest.id: manifest.public_authority() for manifest in manifests
        }
        scope = request.scope
        if scope is None:
            non_read_only = [manifest.id for manifest in manifests
                             if _manifest_actions(manifest) != (ActionAuthority.READ_LOCAL_TARGET,)]
            if non_read_only:
                raise PermissionError(
                    "explicit engagement scope is required for non-read-only tools: "
                    + ", ".join(non_read_only)
                )
            scope = legacy_local_read_only_scope(request.target, now=utcnow())
        target_grant = TargetGrant.from_path(request.target)
        _require_scope_for_creation(scope, target_grant, manifests, now=utcnow())
        self.storage_root.mkdir(parents=True, exist_ok=True)
        plan = _request_plan(request)
        plan_payload = _plan_payload(plan)
        public_config = {
            "goal": request.goal,
            "tools": list(request.tools),
            "modelTools": list(request.model_tools),
            "workerRoles": list(request.worker_roles),
            "concurrency": request.concurrency,
            "modelProfile": worker_backend.profile.public_dict(),
            "strategyPlan": plan_payload,
            "scope": scope.envelope.public_dict(),
            "toolAuthorities": manifest_contracts,
        }
        config_json = json.dumps(public_config, sort_keys=True)
        config_hash = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
        project_id, project_meta = compute_project_id(request.target)
        run_id, run_hash = compute_run_id(project_id, request.target, config_hash)
        paths = new_run_paths(self.storage_root, project_id, run_id, run_hash)

        meta = {
            "version": 1,
            "runId": run_id,
            "projectId": project_id,
            "target": str(request.target),
            "goal": request.goal,
            "tools": list(request.tools),
            "modelTools": list(request.model_tools),
            "workerRoles": list(request.worker_roles),
            "concurrency": request.concurrency,
            "modelProfile": worker_backend.profile.public_dict(),
            "strategyPlan": plan_payload,
            "scope": scope.envelope.public_dict(),
            "toolAuthorities": manifest_contracts,
            "project": project_meta,
            "status": "queued",
            "createdAt": utcnow(),
        }
        atomic_write(paths.run_json, json.dumps(meta, indent=2, sort_keys=True) + "\n")
        atomic_write(
            paths.run_dir / "scope.json",
            json.dumps(scope.to_dict(), indent=2, sort_keys=True) + "\n",
        )

        with FactoryLedger(paths.db_path) as ledger:
            ledger.create_run(
                run_id=run_id,
                project_id=project_id,
                target_path=str(request.target),
                target_root=str(request.target),
                storage_root=str(self.storage_root),
                run_dir=str(paths.run_dir),
                config_json=config_json,
                max_iterations=100,
            )
            ledger.event_log(run_id, "run.created", "Investigation queued", payload=public_config)
            _project_memory_log(paths).append(MemoryAction(
                "goal_set",
                {
                    "text": request.goal,
                    "reason": "investigation created",
                    "scope": "target",
                    "references": [{"kind": "run-event", "id": f"{run_id}:created"}],
                },
                action_id=f"run-goal:{run_id}",
            ))
            tool_items = []
            for tool_id in request.tools:
                manifest = self.rekit.manifest(tool_id)
                tool_items.append(ledger.enqueue(
                    run_id=run_id,
                    key=stable_key("tool", tool_id, str(request.target)),
                    target=str(request.target),
                    operation="rekit-tool",
                    category="tool",
                    title=f"Run Rekit tool {tool_id}",
                    priority=200,
                    payload={"toolId": tool_id, "safetyTier": manifest.safety_tier,
                             **_manifest_work_payload(manifest)},
                    state_label="queued",
                ))
            item_ids: dict[str, str] = {}
            for planned in plan.work:
                dependencies = tool_items + [item_ids[value] for value in planned.depends_on]
                item_ids[planned.id] = self._enqueue_planned_worker(
                    ledger, run_id, str(request.target), worker_backend.profile.name,
                    request.model_tools, plan, planned, dependencies,
                )
        return paths.run_dir

    def run(self, request: RunRequest) -> dict[str, Any]:
        run_dir = self.create(request)
        return asyncio.run(self.drive(run_dir))

    async def drive(self, run_dir: str | Path) -> dict[str, Any]:
        paths = resolve_run_dir(run_dir)
        meta = _read_meta(paths)
        target = Path(meta["target"])
        plan = _plan_from_meta(meta)
        concurrency = plan.ceilings.concurrency

        with FactoryLedger(paths.db_path) as ledger:
            ledger.requeue_stale_leases(paths.run_id)
            ledger.set_run_status(paths.run_id, "running")
            _write_status(paths, "running")
            ledger.event(paths.run_id, "Drain", "enter", {"concurrency": concurrency})
            ledger.event_log(paths.run_id, "run.started", "Investigation running")
            ctx = SimpleNamespace(
                state=SimpleNamespace(run_id=paths.run_id, iteration=0),
                deps=SimpleNamespace(
                    ledger=ledger,
                    paths=paths,
                    scratch={"targetSnapshot": _target_snapshot(target)},
                ),
            )
            dispatcher = AsyncWorkDispatcher(
                {
                    "rekit-tool": self._tool_handler,
                    "model-rekit-tool": self._tool_handler,
                    "model-worker": self._worker_handler,
                },
                node_label="InvestigationDrain",
            )

            while True:
                pending_questions = ledger.pending_questions(paths.run_id)
                if pending_questions:
                    ledger.set_run_status(paths.run_id, "needs_input")
                    ledger.event_log(
                        paths.run_id, "run.needs_input",
                        f"Waiting for {len(pending_questions)} operator decision(s)",
                    )
                    _write_status(paths, "needs_input")
                    return self._snapshot_open(ledger, paths)

                batch = []
                for _ in range(concurrency):
                    item = ledger.lease_next_actionable(paths.run_id)
                    if item is None:
                        break
                    batch.append(dict(item))
                if not batch:
                    break
                await asyncio.gather(*(dispatcher.dispatch(ctx, item) for item in batch))

            termination = ledger.assess(paths.run_id)
            coverage = ledger.coverage(paths.run_id)
            if termination.verdict == "complete":
                report_path = _render_report(ledger, paths, meta)
                ledger.add_report(paths.run_id, "json", report_path)
                unsuccessful = coverage["failed"] + coverage["blocked"]
                final_status = "failed" if unsuccessful else "completed"
                ledger.finish_run(
                    paths.run_id,
                    final_status,
                    coverage=coverage,
                    summary={"workers": len(ledger.workers(paths.run_id))},
                    error=(f"{unsuccessful} terminal work item(s) were unsuccessful"
                           if unsuccessful else None),
                )
                ledger.event_log(
                    paths.run_id,
                    f"run.{final_status}",
                    (termination.message if not unsuccessful
                     else f"Coverage drained with {unsuccessful} unsuccessful item(s)"),
                )
                _write_status(paths, final_status)
            else:
                ledger.set_run_status(paths.run_id, "blocked", error=termination.message)
                ledger.event_log(paths.run_id, "run.blocked", termination.message)
                _write_status(paths, "blocked")
            return self._snapshot_open(ledger, paths)

    def answer(self, run_dir: str | Path, question_id: str, answer: str,
               *, resume: bool = True) -> dict[str, Any]:
        paths = resolve_run_dir(run_dir)
        with FactoryLedger(paths.db_path) as ledger:
            question = ledger.conn.execute(
                "select kind from questions where id=? and run_id=?",
                (question_id, paths.run_id),
            ).fetchone()
            if question is None:
                raise KeyError(question_id)
            permission = ledger.conn.execute(
                "select work_item_id from factory_permissions "
                "where question_id=? and run_id=?",
                (question_id, paths.run_id),
            ).fetchone()
            if permission is not None:
                work_item_id = ledger.answer_permission(paths.run_id, question_id, answer)
                ledger.event_log(
                    paths.run_id,
                    "permission.resolved",
                    f"Operator answered {answer}",
                    payload={"questionId": question_id, "workItemId": work_item_id},
                )
            else:
                response = answer.strip()
                if not response:
                    raise ValueError("direction answer must not be empty")
                if len(response) > 8_000:
                    raise ValueError("direction answer must be at most 8000 characters")
                ledger.record_answer(paths.run_id, question_id, response)
                ledger.event_log(
                    paths.run_id,
                    "direction.resolved",
                    "Operator supplied direction",
                    payload={"questionId": question_id, "kind": question["kind"]},
                )
            ledger.set_run_status(paths.run_id, "queued")
            _write_status(paths, "queued")
        if resume:
            return asyncio.run(self.drive(paths.run_dir))
        return self.snapshot(paths.run_dir)

    def snapshot(self, run_dir: str | Path) -> dict[str, Any]:
        paths = resolve_run_dir(run_dir)
        with FactoryLedger(paths.db_path) as ledger:
            return self._snapshot_open(ledger, paths)

    def _snapshot_open(self, ledger: FactoryLedger, paths: RunPaths) -> dict[str, Any]:
        run = ledger.get_run(paths.run_id)
        work_rows = ledger.conn.execute(
            "select * from work_items where run_id=? order by priority desc, created_at",
            (paths.run_id,),
        ).fetchall()
        work = []
        for row in work_rows:
            item = dict(row)
            for source, target in (
                ("payload_json", "payload"),
                ("depends_on_json", "dependsOn"),
                ("result_json", "result"),
            ):
                raw = item.pop(source)
                item[target] = json.loads(raw) if raw else None
            if item["payload"]:
                item["payload"] = _redact_intent(item["payload"])
            work.append(item)
        artifacts = [dict(row) for row in ledger.conn.execute(
            "select * from artifacts where run_id=? order by created_at", (paths.run_id,)
        ).fetchall()]
        project_memory = _project_memory_log(paths).replay()
        return {
            "run": dict(run) if run is not None else None,
            "meta": _read_meta(paths),
            "coverage": ledger.coverage(paths.run_id),
            "workers": ledger.workers(paths.run_id),
            "workItems": work,
            "events": ledger.events(paths.run_id),
            "pendingQuestions": ledger.pending_questions(paths.run_id),
            "modelCalls": ledger.model_calls(paths.run_id),
            "workerSessions": [
                {**session,
                 "pendingCalls": [_redact_intent(call)
                                  for call in session.get("pendingCalls", [])]}
                for session in ledger.worker_sessions(paths.run_id)
            ],
            "toolCalls": ledger.tool_calls(paths.run_id),
            "artifacts": artifacts,
            "memory": project_memory.deterministic_dict(),
            "memoryContext": memory_context(project_memory),
            "hypothesisState": hypothesis_snapshot(project_memory),
            "findingState": finding_snapshot(project_memory),
        }

    async def _tool_handler(self, ctx, item: dict[str, Any]) -> None:
        ledger: FactoryLedger = ctx.deps.ledger
        payload = json.loads(item["payload_json"])
        tool_id = payload["toolId"]
        manifest = self.rekit.manifest(tool_id)
        scope = _load_scope(ctx.deps.paths, Path(item["target"]))
        decision = None
        actions = list(_manifest_actions(manifest))
        requested_action = payload.get("requestedAction")
        if payload.get("manifestDigest") != manifest.effective_manifest_digest:
            decision = _manifest_denial(scope, item["target"], payload, "manifest.digest_changed")
        elif payload.get("endpoint") and ActionAuthority.NETWORK_ACCESS not in manifest.actions:
            decision = _manifest_denial(scope, item["target"], payload, "manifest.endpoint_not_declared")
        elif payload.get("usesCredentials") and not manifest.credential_use:
            decision = _manifest_denial(scope, item["target"], payload, "manifest.credentials_not_declared")
        elif payload.get("accountRef") and not manifest.credential_use:
            decision = _manifest_denial(scope, item["target"], payload, "manifest.account_not_declared")
        elif manifest.credential_use and not payload.get("accountRef"):
            decision = _manifest_denial(scope, item["target"], payload, "manifest.account_intent_required")
        if requested_action and decision is None:
            try:
                requested = ActionAuthority(requested_action)
                if requested not in manifest.actions:
                    decision = _manifest_denial(
                        scope, item["target"], payload, "manifest.action_not_declared"
                    )
            except ValueError:
                decision = ScopeDecision(
                    False,
                    "scope.action_invalid",
                    (f"scope:{scope.envelope.scope_id}:r{scope.envelope.revision}:"
                     f"{scope.envelope.content_digest[:12]}"),
                    TargetGrant.from_path(item["target"]).path_fingerprint,
                    (opaque_ref("endpoint", payload["endpoint"])
                     if payload.get("endpoint") else None),
                    "invalid",
                )
        for action in dict.fromkeys(actions):
            if decision is not None:
                break
            candidate = decide_scope(
                scope,
                ScopeRequest(
                    action=action,
                    target=TargetGrant.from_path(item["target"]),
                    endpoint=(payload.get("endpoint")
                              if action is ActionAuthority.NETWORK_ACCESS else None),
                    account_ref=payload.get("accountRef"),
                    uses_credentials=(manifest.credential_use
                                      or bool(payload.get("usesCredentials", False))),
                ),
                now=utcnow(),
            )
            if not candidate.allowed:
                decision = candidate
                break
        if decision is None:
            decision = candidate
        if not decision.allowed:
            ledger.resolve(
                item["id"],
                result={"toolId": tool_id, "decision": "scope-denied",
                        "reasonCode": decision.reason_code},
                evidence="Engagement authorization denied the requested action",
                state_label="scope_denied",
            )
            ledger.event_log(
                ctx.state.run_id,
                "security.scope_denied",
                "Engagement scope denied a Rekit dispatch",
                payload=decision.browser_dict(),
            )
            self._resume_model_worker_if_ready(ledger, ctx.state.run_id, payload)
            return
        qid = stable_key(ctx.state.run_id, item["id"], tool_id, "permission")
        answer = ledger.get_answer(ctx.state.run_id, qid)

        if manifest.requires_permission and answer is None:
            prompt = (
                f"Allow Rekit tool '{tool_id}'? Safety tier {manifest.safety_tier}; "
                f"executes_input={manifest.executes_input}; network={manifest.network}. "
                f"Target: {Path(item['target']).name}."
            )
            ledger.ask_question(
                ctx.state.run_id,
                qid=qid,
                node="RekitTool",
                kind="tool-permission",
                prompt=prompt,
                options=["allow", "deny"],
            )
            ledger.link_permission(
                qid, ctx.state.run_id, item["id"], tool_id,
                manifest.effective_manifest_digest,
            )
            ledger.set_work_status(
                item["id"], "blocked", error="Awaiting operator permission",
                state_label="needs_permission",
            )
            ledger.event_log(
                ctx.state.run_id, "permission.requested",
                f"{tool_id} requires operator permission",
                payload={"questionId": qid, "toolId": tool_id,
                         "safetyTier": manifest.safety_tier,
                         "manifestDigest": manifest.effective_manifest_digest,
                         "actions": [action.value for action in manifest.actions]},
            )
            return

        if manifest.requires_permission and answer == "deny":
            ledger.resolve(
                item["id"],
                result={"toolId": tool_id, "decision": "denied"},
                evidence="Operator denied permission",
                state_label="denied",
            )
            ledger.event_log(ctx.state.run_id, "tool.denied", f"{tool_id} was denied")
            self._resume_model_worker_if_ready(ledger, ctx.state.run_id, payload)
            return

        ledger.event_log(ctx.state.run_id, "tool.started", f"Running {tool_id}")
        call_id = ledger.start_tool_call(
            ctx.state.run_id, item["id"], tool_id, manifest.safety_tier,
            manifest_digest=manifest.effective_manifest_digest,
            declared_actions=tuple(action.value for action in manifest.actions),
            credential_use=manifest.credential_use,
        )
        result = await asyncio.to_thread(
            self.rekit.run,
            tool_id,
            Path(item["target"]),
            allow_dynamic=manifest.requires_permission and answer == "allow",
        )
        captured_at = utcnow()
        evidence = EvidenceStore(ctx.deps.paths.run_dir / "evidence")
        outcome = evidence.capture_tool_output(
            render_tool_output(
                result.command_label, result.exit_code, result.stdout, result.stderr
            ),
            Provenance(
                run_id=ctx.state.run_id,
                source=f"rekit:{tool_id}",
                capture_reason="tool execution proof",
                captured_at=captured_at,
                environment_id=f"local:{platform.system()}:{platform.machine()}",
                target_sha256=hash_target(Path(item["target"])),
                tool_id=tool_id,
                worker_id=payload.get("workerId"),
                invocation_id=call_id,
                work_item_id=item["id"],
            ),
            retention_class=RetentionClass.RUN,
            expires_at=default_expiry(RetentionClass.RUN),
        )
        for event in outcome.events:
            ledger.event_log(
                ctx.state.run_id,
                f"evidence.{event.action.value}",
                event.reason,
                worker_id=payload.get("workerId"),
                payload={"artifactId": event.artifact_id, **event.payload},
            )
        evidence_record = outcome.record
        if evidence_record is None:
            raise RuntimeError("required tool proof was withheld by evidence policy")
        if evidence_record.state is EvidenceState.QUARANTINED:
            raise RuntimeError("required tool proof was quarantined by evidence policy")
        output_path = evidence.root / evidence_record.display_path
        status = "done" if result.exit_code == 0 else "failed"
        ledger.finish_tool_call(
            call_id, status=status, output_path=str(output_path), exit_code=result.exit_code
        )
        ledger.add_artifact(
            run_id=ctx.state.run_id,
            kind="tool-output",
            path=output_path,
            logical_path=f"tool-output/{output_path.name}",
            origin=f"rekit:{tool_id}",
            metadata={
                "toolId": tool_id,
                "exitCode": result.exit_code,
                "evidenceArtifactId": evidence_record.artifact_id,
                "originalSha256": evidence_record.original_sha256,
                "rawSha256": evidence_record.raw_sha256,
                "displaySha256": evidence_record.display_sha256,
                "redacted": evidence_record.redacted,
                "truncated": evidence_record.truncated,
                "retentionClass": evidence_record.retention_class.value,
                "capturePolicy": evidence_record.capture_policy,
                "effectiveManifestDigest": manifest.effective_manifest_digest,
                "declaredActions": [action.value for action in manifest.actions],
                "credentialUse": manifest.credential_use,
                "provenance": asdict(evidence_record.provenance),
            },
        )
        if result.exit_code == 0:
            ledger.resolve(
                item["id"],
                result={"toolId": tool_id, "output": str(output_path),
                        "manifestDigest": manifest.effective_manifest_digest},
                evidence=str(output_path),
                state_label="completed",
            )
            ledger.event_log(ctx.state.run_id, "tool.completed", f"{tool_id} completed")
        else:
            ledger.set_work_status(
                item["id"], "failed",
                error=f"{tool_id} exited {result.exit_code}",
                evidence=str(output_path),
                state_label="failed",
            )
            ledger.event_log(
                ctx.state.run_id, "tool.failed", f"{tool_id} exited {result.exit_code}"
            )
        self._resume_model_worker_if_ready(ledger, ctx.state.run_id, payload)

    async def _worker_handler(self, ctx, item: dict[str, Any]) -> None:
        ledger: FactoryLedger = ctx.deps.ledger
        payload = json.loads(item["payload_json"])
        worker_id = payload["workerId"]
        role = payload["role"]
        hypothesis_id = payload.get("hypothesisId")
        hypothesis_test_id = payload.get("hypothesisTestId")
        if hypothesis_id and hypothesis_test_id:
            hypothesis_memory = HypothesisMemory(_project_memory_log(ctx.deps.paths))
            current = hypothesis_memory.log.replay().hypotheses.get(hypothesis_id)
            if current and current["status"] == "queued":
                hypothesis_memory.transition(HypothesisUpdate(
                    hypothesis_id=hypothesis_id, test_id=hypothesis_test_id,
                    status="testing", confidence=float(current["confidence"]),
                    reason="Discriminating test leased by durable scheduler",
                ))
            if current and current["status"] in {"queued", "testing"}:
                hypothesis_memory.update_test(
                    hypothesis_test_id, "leased", increment_attempt=True
                )
        worker_backend = self.worker_backend(payload.get("modelProfile"))
        session = ledger.worker_session(ctx.state.run_id, worker_id)
        available_tools = tuple(
            ModelTool(
                id=tool_id,
                name=self.rekit.manifest(tool_id).name,
                description=self.rekit.manifest(tool_id).description,
            )
            for tool_id in payload.get("availableTools", [])
        )
        tool_results = (
            self._model_tool_results(ledger, ctx.state.run_id, session)
            if session and session["pendingCalls"] else ()
        )
        ledger.update_worker(
            worker_id, status="running", current_step="reviewing evidence", error=None
        )
        ledger.event_log(
            ctx.state.run_id, "worker.started", f"{role} worker started", worker_id=worker_id
        )
        try:
            def event_sink(activity: ModelActivity) -> None:
                ledger.event_log(
                    ctx.state.run_id,
                    activity.kind,
                    activity.message,
                    worker_id=worker_id,
                    payload=activity.payload,
                )

            project_memory = _project_memory_log(ctx.deps.paths).replay()
            resume_context = (
                "[originating reasoning withheld for independent reproduction]"
                if payload.get("findingId") else memory_context(project_memory)
            )
            tool_context = _tool_context(
                ledger, ctx.state.run_id,
                evidence_ids=(payload.get("evidenceIds") if payload.get("findingId") else None),
            )
            bounded_context = (
                f"Project memory (bounded, cited):\n{resume_context}\n\n"
                f"Rekit tool evidence:\n{tool_context or '[no tool results]'}"
            )
            turn = await worker_backend.analyze(
                role=role,
                goal=payload["goal"],
                target_snapshot=ctx.deps.scratch["targetSnapshot"],
                tool_context=bounded_context,
                available_tools=available_tools,
                messages_json=session["messages_json"] if session else None,
                tool_results=tool_results,
                event_sink=event_sink,
            )
            # Older/custom backends can retain the original tuple contract.
            if isinstance(turn, tuple):
                report, usage = turn
                turn = WorkerTurn(report=report, usage=usage, messages_json="[]")
            ledger.record_model_call(
                ctx.state.run_id,
                worker_id,
                provider=worker_backend.profile.provider,
                model=worker_backend.profile.model,
                purpose=role,
                usage=turn.usage,
            )
            pending_calls = [
                {"callId": call.call_id, "toolId": call.tool_id,
                 "toolName": call.tool_name,
                 **_manifest_work_payload(self.rekit.manifest(call.tool_id)),
                 "endpoint": call.endpoint,
                 "accountRef": _account_intent_ref(call.account_ref),
                 "usesCredentials": call.uses_credentials,
                 "requestedAction": call.requested_action}
                for call in turn.deferred_calls
            ]
            ledger.save_worker_session(
                ctx.state.run_id,
                worker_id,
                messages_json=turn.messages_json,
                pending_calls=pending_calls,
            )
            if turn.deferred_calls:
                for call in turn.deferred_calls:
                    manifest = self.rekit.manifest(call.tool_id)
                    ledger.enqueue(
                        run_id=ctx.state.run_id,
                        key=stable_key("model-tool", worker_id, call.call_id),
                        target=item["target"],
                        operation="model-rekit-tool",
                        category="tool",
                        title=f"{role} requested Rekit tool {call.tool_id}",
                        priority=150,
                        payload={
                            "toolId": call.tool_id,
                            "toolCallId": call.call_id,
                            "workerId": worker_id,
                            "workerItemId": item["id"],
                            "safetyTier": manifest.safety_tier,
                            **_manifest_work_payload(manifest),
                            "endpoint": call.endpoint,
                            "accountRef": _account_intent_ref(call.account_ref),
                            "usesCredentials": call.uses_credentials,
                            "requestedAction": call.requested_action,
                        },
                        state_label="model_requested",
                    )
                ledger.set_work_status(
                    item["id"], "blocked",
                    error="Waiting for model-requested Rekit tool results",
                    state_label="awaiting_tools",
                )
                ledger.update_worker(
                    worker_id, status="queued", current_step="waiting for Rekit tools"
                )
                ledger.event_log(
                    ctx.state.run_id,
                    "worker.tools_requested",
                    f"{role} requested {len(turn.deferred_calls)} Rekit tool(s)",
                    worker_id=worker_id,
                    payload={"tools": [call.tool_id for call in turn.deferred_calls]},
                )
                return
            report = turn.report
            if report is None:
                raise RuntimeError("model worker returned neither report nor tool requests")
            accepted, rejected = self._append_memory_proposals(
                ctx.deps.paths,
                getattr(report, "proposed_memory_actions", ()),
            )
            hypothesis_updates = self._apply_hypothesis_updates(
                ctx.deps.paths, getattr(report, "hypothesis_updates", ()),
                expected_hypothesis_id=hypothesis_id,
                expected_test_id=hypothesis_test_id,
            )
            reproduction_results = self._apply_reproduction_results(
                ctx.deps.paths, payload, worker_backend.profile.name,
                getattr(report, "reproduction_results", ()),
            )
            if payload.get("findingId") and reproduction_results == 0:
                event_id = ledger.event_log(
                    ctx.state.run_id, "finding.validation_inconclusive",
                    "Validator completed without a matching structured reproduction result",
                    worker_id=worker_id,
                    payload={"findingId": payload["findingId"],
                             "attemptId": payload.get("reproductionAttemptId")},
                )
                self._record_inconclusive_reproduction(
                    ctx.deps.paths, payload, worker_backend.profile.name,
                    "No matching structured reproduction result was returned",
                    EvidenceRef("run-event", event_id),
                )
            hypotheses_scheduled, hypotheses_rejected = self._schedule_hypotheses(
                ledger, ctx.deps.paths, ctx.state.run_id, item,
                worker_backend.profile.name, payload.get("availableTools", ()),
                getattr(report, "proposed_hypotheses", ()),
            )
            findings_scheduled, findings_rejected = self._schedule_findings(
                ledger, ctx.deps.paths, ctx.state.run_id, item,
                worker_backend.profile.name, payload.get("availableTools", ()),
                getattr(report, "proposed_findings", ()),
            )
            if payload.get("findingId") and reproduction_results:
                self._schedule_remaining_reproduction(
                    ledger, ctx.deps.paths, ctx.state.run_id, item,
                    worker_backend.profile.name, payload.get("availableTools", ()),
                    payload["findingId"],
                )
            if hypothesis_id and hypothesis_test_id and hypothesis_updates == 0:
                current = HypothesisMemory(_project_memory_log(ctx.deps.paths)).log.replay().hypotheses[
                    hypothesis_id
                ]
                HypothesisMemory(_project_memory_log(ctx.deps.paths)).transition(HypothesisUpdate(
                    hypothesis_id=hypothesis_id, test_id=hypothesis_test_id,
                    status="blocked", confidence=float(current["confidence"]),
                    reason="Test completed without a valid explicit hypothesis outcome",
                ))
                HypothesisMemory(_project_memory_log(ctx.deps.paths)).update_test(
                    hypothesis_test_id, "blocked", outcome="missing-explicit-outcome"
                )
            ledger.resolve(
                item["id"],
                result=report.model_dump(mode="json"),
                evidence="Bounded model review over target snapshot and ledgered tool output",
                state_label="completed",
            )
            ledger.update_worker(worker_id, status="done", current_step=report.status_update)
            ledger.event_log(
                ctx.state.run_id,
                "worker.completed",
                report.status_update,
                worker_id=worker_id,
                payload={"observationCount": len(report.observations),
                         "nextActionCount": len(report.next_actions),
                         "memoryActionCount": accepted,
                         "memoryActionRejectedCount": rejected},
            )
            if hypotheses_scheduled or hypotheses_rejected or hypothesis_updates:
                ledger.event_log(
                    ctx.state.run_id, "hypothesis.activity",
                    "Processed structured hypothesis actions", worker_id=worker_id,
                    payload={"scheduled": hypotheses_scheduled,
                             "rejected": hypotheses_rejected,
                             "updates": hypothesis_updates},
                )
            if findings_scheduled or findings_rejected or reproduction_results:
                ledger.event_log(
                    ctx.state.run_id, "finding.activity",
                    "Processed structured proof-gated finding actions", worker_id=worker_id,
                    payload={"scheduled": findings_scheduled,
                             "rejected": findings_rejected,
                             "reproductionResults": reproduction_results},
                )
            self._enqueue_follow_ups(
                ledger, ctx.state.run_id, item, payload, report.next_actions,
                ctx.deps.paths, worker_backend.profile.name,
            )
        except Exception as exc:
            retry_ceiling = int(payload.get("retryCeiling", 1))
            if item["attempts"] <= retry_ceiling:
                message = f"{type(exc).__name__}: {exc}"
                ledger.need_evidence(
                    item["id"], note="Transient worker failure; retrying",
                    state_label="retrying",
                )
                ledger.update_worker(
                    worker_id, status="queued", current_step="retrying model call",
                    error=message,
                )
                ledger.event_log(
                    ctx.state.run_id, "worker.retrying", message,
                    worker_id=worker_id, payload={"attempt": item["attempts"]},
                )
                return
            ledger.update_worker(
                worker_id, status="failed", current_step="model call failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            failure_event_id = ledger.event_log(
                ctx.state.run_id, "worker.failed", f"{type(exc).__name__}: {exc}",
                worker_id=worker_id,
            )
            if payload.get("findingId"):
                self._record_inconclusive_reproduction(
                    ctx.deps.paths, payload,
                    self.worker_backend(payload.get("modelProfile")).profile.name,
                    f"Validator execution failed: {type(exc).__name__}: {exc}",
                    EvidenceRef("run-event", failure_event_id),
                )
            if hypothesis_id and hypothesis_test_id:
                hypothesis_memory = HypothesisMemory(_project_memory_log(ctx.deps.paths))
                current = hypothesis_memory.log.replay().hypotheses.get(hypothesis_id)
                if current and current["status"] == "testing":
                    hypothesis_memory.transition(HypothesisUpdate(
                        hypothesis_id=hypothesis_id, test_id=hypothesis_test_id,
                        status="blocked", confidence=float(current["confidence"]),
                        reason=(f"Stop condition exhausted after {item['attempts']} attempt(s): "
                                f"{type(exc).__name__}: {exc}"),
                    ))
                    hypothesis_memory.update_test(
                        hypothesis_test_id, "blocked", outcome="stop-condition-exhausted"
                    )
            raise

    def _append_memory_proposals(self, paths: RunPaths,
                                 proposals: Any) -> tuple[int, int]:
        """Validate structured proposals; prose is never interpreted as a memory write."""
        log = _project_memory_log(paths)
        accepted = rejected = 0
        for proposed in proposals or ():
            try:
                action = proposed if isinstance(proposed, MemoryAction) else MemoryAction(
                    type=proposed.type,
                    payload=dict(proposed.payload),
                    action_id=getattr(proposed, "action_id", None),
                )
                payload = dict(action.payload)
                references = list(payload.get("references", []))
                if not references:
                    raise ValueError(
                        "model-proposed memory actions must cite durable evidence"
                    )
                payload["references"] = references
                log.append(MemoryAction(action.type, payload, action.action_id))
                accepted += 1
            except (TypeError, ValueError):
                rejected += 1
        return accepted, rejected

    def _apply_hypothesis_updates(
        self,
        paths: RunPaths,
        updates: Any,
        *,
        expected_hypothesis_id: str | None = None,
        expected_test_id: str | None = None,
    ) -> int:
        hypotheses = HypothesisMemory(_project_memory_log(paths))
        accepted = 0
        for update in updates or ():
            try:
                if (expected_hypothesis_id is not None
                        and update.hypothesis_id != expected_hypothesis_id):
                    raise ValueError("test worker cannot update another hypothesis")
                if expected_test_id is not None and update.test_id != expected_test_id:
                    raise ValueError("test worker cannot report another discriminating test")
                hypotheses.transition(update)
                accepted += 1
            except (KeyError, TypeError, ValueError):
                continue
        return accepted

    def _schedule_hypotheses(self, ledger: FactoryLedger, paths: RunPaths, run_id: str,
                             parent_item: dict[str, Any], model_profile: str,
                             model_tools: Any, proposals: Any) -> tuple[int, int]:
        hypotheses = HypothesisMemory(_project_memory_log(paths))
        scheduled = rejected = 0
        for proposal in proposals or ():
            try:
                if proposal.scope != "target" or proposal.proposed_test.scope != "target":
                    raise ValueError("hypothesis test would broaden target scope")
                test = proposal.proposed_test
                prerequisite_rows = {
                    json.loads(row["payload_json"]).get("hypothesisTestId"): row["id"]
                    for row in ledger.conn.execute(
                        "select id, payload_json from work_items "
                        "where run_id=? and operation='model-worker'", (run_id,),
                    ).fetchall()
                }
                dependencies = [parent_item["id"]]
                for prerequisite in test.prerequisites:
                    if prerequisite not in prerequisite_rows:
                        raise ValueError(f"unknown hypothesis-test prerequisite {prerequisite}")
                    dependencies.append(prerequisite_rows[prerequisite])
                if not hypotheses.propose(proposal):
                    continue
                if not test.approved or test.authorization != "automatic":
                    rejected += 1
                    continue
                plan_id = f"hypothesis:{proposal.id}:{test.id}"
                worker_id = ledger.add_planned_worker(
                    run_id, plan_id, f"hypothesis-test:{proposal.id}", model_profile
                )
                ledger.enqueue(
                    run_id=run_id,
                    key=stable_key("hypothesis-test", proposal.id, test.id,
                                   proposal.claim, proposal.scope),
                    target=parent_item["target"], operation="model-worker",
                    category="hypothesis-test", title=f"Test hypothesis {proposal.id}",
                    priority=test_priority(proposal), depends_on=dependencies,
                    payload={
                        "workerId": worker_id, "role": f"hypothesis-test:{proposal.id}",
                        "goal": (
                            f"Discriminate hypothesis {proposal.id}: {proposal.claim}\n"
                            f"Test: {test.objective}\nExpected: {test.expected_observation}\n"
                            f"Falsifier: {test.falsifying_observation}\n"
                            "Return the outcome only through hypothesis_updates with cited observations."
                        ),
                        "modelProfile": model_profile, "availableTools": list(model_tools),
                        "toolAuthorities": _tool_authorities(self.rekit, model_tools),
                        "planId": plan_id, "dedupeKey": stable_key(plan_id),
                        "costUnits": test.cost_units, "origin": "hypothesis-test",
                        "evidenceIds": [],
                        "retryCeiling": proposal.stop_condition.max_attempts - 1,
                        "hypothesisId": proposal.id, "hypothesisTestId": test.id,
                        "authorization": test.authorization,
                    }, state_label="queued",
                )
                hypotheses.mark_scheduled(proposal.id, test.id)
                scheduled += 1
            except (KeyError, TypeError, ValueError):
                rejected += 1
        return scheduled, rejected

    def _apply_reproduction_results(self, paths: RunPaths, payload: dict[str, Any],
                                    model_profile: str, results: Any) -> int:
        """Apply only explicit validator results and bind them to controller-owned identity."""
        finding_id = payload.get("findingId")
        attempt_id = payload.get("reproductionAttemptId")
        if not finding_id or not attempt_id:
            return 0
        findings = FindingMemory(_project_memory_log(paths))
        accepted = 0
        for result in results or ():
            try:
                if result.finding_id != finding_id or result.attempt_id != attempt_id:
                    raise ValueError("reproduction result does not match assigned validation")
                findings.record_attempt(ReproductionAttempt(
                    id=attempt_id,
                    finding_id=finding_id,
                    recipe_id=payload["recipeId"],
                    outcome=result.outcome,
                    worker_id=payload["workerId"],
                    session_id=payload["validatorSessionId"],
                    environment_id=payload["validatorEnvironmentId"],
                    clean_environment=payload["cleanEnvironment"],
                    model_profile=model_profile,
                    observations=result.observations,
                    environmental_differences=result.environmental_differences,
                    references=result.references,
                ))
                accepted += 1
            except (KeyError, TypeError, ValueError):
                continue
        return accepted

    def _schedule_findings(self, ledger: FactoryLedger, paths: RunPaths, run_id: str,
                           parent_item: dict[str, Any], model_profile: str,
                           model_tools: Any, proposals: Any) -> tuple[int, int]:
        findings = FindingMemory(_project_memory_log(paths))
        parent_payload = json.loads(parent_item["payload_json"])
        scheduled = rejected = 0
        for proposal in proposals or ():
            try:
                if proposal.scope != "target":
                    raise ValueError("finding would broaden target scope")
                if not findings.propose(
                    proposal,
                    origin_worker_id=parent_payload["workerId"],
                    origin_session_id=f"session:{parent_payload['workerId']}",
                    origin_model_profile=model_profile,
                ):
                    continue
                self._enqueue_finding_validation(
                    ledger, paths, run_id, parent_item, model_profile, model_tools,
                    proposal.id, 1,
                )
                findings.mark_validation_pending(proposal.id)
                scheduled += 1
            except (KeyError, TypeError, ValueError):
                rejected += 1
        return scheduled, rejected

    def _schedule_remaining_reproduction(self, ledger: FactoryLedger, paths: RunPaths,
                                         run_id: str, parent_item: dict[str, Any],
                                         model_profile: str, model_tools: Any,
                                         finding_id: str) -> None:
        findings = FindingMemory(_project_memory_log(paths))
        memory = findings.log.replay()
        finding = memory.findings.get(finding_id)
        if finding is None or finding["status"] != "reproduction-pending":
            return
        attempts = [item for item in memory.finding_attempts.values()
                    if item["findingId"] == finding_id]
        required = int(finding["proofPolicy"]["successful_clean_reproductions"])
        if findings.qualifying_reproduction_count(finding_id) >= required or any(
            item["outcome"] in {"negative", "flaky", "contradictory", "inconclusive"}
            for item in attempts
        ):
            return
        self._enqueue_finding_validation(
            ledger, paths, run_id, parent_item, model_profile, model_tools,
            finding_id, len(attempts) + 1,
        )

    def _enqueue_finding_validation(self, ledger: FactoryLedger, paths: RunPaths,
                                    run_id: str, parent_item: dict[str, Any],
                                    model_profile: str, model_tools: Any,
                                    finding_id: str, attempt_number: int) -> str:
        finding = _project_memory_log(paths).replay().findings[finding_id]
        recipe = finding["recipe"]
        attempt_id = f"repro-{finding_id}-{attempt_number}"
        plan_id = f"finding-validation:{finding_id}:{attempt_number}"
        worker_id = ledger.add_planned_worker(
            run_id, plan_id, f"finding-validator:{finding_id}", model_profile
        )
        if worker_id == finding["originWorkerId"]:
            raise ValueError("independent validation cannot reuse the origin worker")
        environment_id = "clean:" + stable_key(
            "finding-environment", run_id, finding_id, str(attempt_number)
        )
        material_refs = sorted({
            f"{item['kind']}:{item['id']}"
            for item in recipe["staged_inputs"] + finding["references"]
        })
        goal = (
            f"Independent reproduction assignment {attempt_id}.\n"
            f"Recipe id: {recipe['id']}\n"
            f"Steps: {json.dumps(recipe['steps'], sort_keys=True)}\n"
            f"Expected observable: {recipe['expected_observation']}\n"
            f"Clean environment requirements: "
            f"{json.dumps(recipe['clean_environment_requirements'], sort_keys=True)}\n"
            f"Material evidence references: {json.dumps(material_refs)}\n"
            "Execute only the recipe and report exact observables; do not infer conclusions. "
            "Return results only through reproduction_results using the "
            f"assigned finding_id={finding_id!r} and attempt_id={attempt_id!r}."
        )
        return ledger.enqueue(
            run_id=run_id,
            key=stable_key(
                "finding-validation", finding_id, str(attempt_number),
                json.dumps(recipe, sort_keys=True, separators=(",", ":")),
            ),
            target=parent_item["target"], operation="model-worker",
            category="finding-validation", title=f"Reproduce finding {finding_id}",
            priority=220, depends_on=[parent_item["id"]],
            payload={
                "workerId": worker_id,
                "role": f"finding-validator:{finding_id}",
                "goal": goal,
                "modelProfile": model_profile,
                "availableTools": list(model_tools),
                "toolAuthorities": _tool_authorities(self.rekit, model_tools),
                "planId": plan_id,
                "dedupeKey": stable_key(plan_id),
                "costUnits": 10,
                "origin": "finding-validation",
                "evidenceIds": sorted({item["id"] for item in
                                       recipe["staged_inputs"] + finding["references"]}),
                "retryCeiling": 1,
                "findingId": finding_id,
                "recipeId": recipe["id"],
                "reproductionAttemptId": attempt_id,
                "validatorSessionId": f"session:{worker_id}",
                "validatorEnvironmentId": environment_id,
                "cleanEnvironment": True,
                "originWorkerId": finding["originWorkerId"],
            }, state_label="queued",
        )

    def _enqueue_planned_worker(self, ledger: FactoryLedger, run_id: str, target: str,
                                model_profile: str, model_tools: tuple[str, ...] | list[str],
                                plan: InvestigationPlan, planned: PlannedWork,
                                depends_on: list[str]) -> str:
        worker_id = ledger.add_planned_worker(
            run_id, planned.id, planned.role, model_profile
        )
        return ledger.enqueue(
            run_id=run_id, key=planned.dedupe_key, target=target,
            operation="model-worker", category="worker",
            title=f"{planned.role} worker", priority=100,
            depends_on=depends_on,
            payload={
                "workerId": worker_id, "role": planned.role,
                "goal": f"{plan.goal}\nAssigned objective: {planned.objective}",
                "modelProfile": model_profile, "availableTools": list(model_tools),
                "toolAuthorities": _tool_authorities(self.rekit, model_tools),
                "planId": planned.id, "dedupeKey": planned.dedupe_key,
                "costUnits": planned.cost_units, "origin": planned.origin,
                "evidenceIds": list(planned.evidence_ids),
                "retryCeiling": plan.ceilings.retries_per_worker,
            },
            state_label="queued",
        )

    def _record_inconclusive_reproduction(self, paths: RunPaths, payload: dict[str, Any],
                                          model_profile: str, observation: str,
                                          reference: EvidenceRef) -> None:
        findings = FindingMemory(_project_memory_log(paths))
        if payload["reproductionAttemptId"] in findings.log.replay().finding_attempts:
            return
        findings.record_attempt(ReproductionAttempt(
            id=payload["reproductionAttemptId"],
            finding_id=payload["findingId"],
            recipe_id=payload["recipeId"],
            outcome="inconclusive",
            worker_id=payload["workerId"],
            session_id=payload["validatorSessionId"],
            environment_id=payload["validatorEnvironmentId"],
            clean_environment=payload["cleanEnvironment"],
            model_profile=model_profile,
            observations=[observation],
            environmental_differences=["validator protocol did not yield an observable result"],
            references=[reference],
        ))

    def _enqueue_follow_ups(self, ledger: FactoryLedger, run_id: str,
                            item: dict[str, Any], payload: dict[str, Any],
                            next_actions: list[str], paths: RunPaths,
                            model_profile: str) -> None:
        """Translate explicit proposals into work; only the scheduler assesses completion."""
        if "planId" not in payload:
            return
        plan = _plan_from_meta(_read_meta(paths))
        existing = _adaptive_work(ledger, run_id)
        for action in next_actions:
            match = re.fullmatch(r"\s*\[follow-up:([^\]]+)\]\s*(.+)", action)
            if match is None:
                continue
            proposal = FollowUpProposal(
                role=match.group(1), objective=match.group(2),
                evidence_ids=(item["id"],), depends_on=(payload["planId"],),
            )
            try:
                planned = propose_follow_up(plan, proposal, existing_work=existing)
            except ValueError as exc:
                ledger.event_log(
                    run_id, "strategy.follow_up_rejected", str(exc),
                    worker_id=payload["workerId"], payload={"proposal": action},
                )
                continue
            if planned is None:
                continue
            dependency_rows = {
                json.loads(row["payload_json"]).get("planId"): row["id"]
                for row in ledger.conn.execute(
                    "select id, payload_json from work_items "
                    "where run_id=? and operation='model-worker'", (run_id,),
                ).fetchall()
            }
            work_item_id = self._enqueue_planned_worker(
                ledger, run_id, item["target"], model_profile,
                tuple(payload.get("availableTools", [])), plan, planned,
                [dependency_rows[value] for value in planned.depends_on],
            )
            existing.append(planned)
            ledger.event_log(
                run_id, "strategy.follow_up_enqueued",
                f"{planned.role} follow-up enqueued", worker_id=payload["workerId"],
                payload={"workItemId": work_item_id, "planId": planned.id,
                         "evidenceIds": list(planned.evidence_ids)},
            )

    def _resume_model_worker_if_ready(self, ledger: FactoryLedger, run_id: str,
                                      payload: dict[str, Any]) -> None:
        worker_id = payload.get("workerId")
        worker_item_id = payload.get("workerItemId")
        if not worker_id or not worker_item_id:
            return
        session = ledger.worker_session(run_id, worker_id)
        if session is None or not session["pendingCalls"]:
            return
        rows = ledger.conn.execute(
            "select status, payload_json from work_items "
            "where run_id=? and operation='model-rekit-tool'",
            (run_id,),
        ).fetchall()
        statuses = {
            json.loads(row["payload_json"]).get("toolCallId"): row["status"]
            for row in rows
            if json.loads(row["payload_json"]).get("workerId") == worker_id
        }
        if not all(statuses.get(call["callId"]) in {"done", "failed"}
                   for call in session["pendingCalls"]):
            return
        ledger.conn.execute(
            "update work_items set status='queued', state_label='resuming', error=null, "
            "result_json=null, evidence=null, terminal_at=null, updated_at=? "
            "where id=? and run_id=? and status='blocked' and state_label='awaiting_tools'",
            (utcnow(), worker_item_id, run_id),
        )
        ledger.conn.commit()
        ledger.update_worker(worker_id, status="queued", current_step="resuming with tool results")
        ledger.event_log(
            run_id, "worker.resuming", "All requested Rekit tool results are available",
            worker_id=worker_id,
        )

    def _model_tool_results(self, ledger: FactoryLedger, run_id: str,
                            session: dict[str, Any]) -> tuple[ModelToolResult, ...]:
        rows = ledger.conn.execute(
            "select status, result_json, error, payload_json from work_items "
            "where run_id=? and operation='model-rekit-tool'",
            (run_id,),
        ).fetchall()
        indexed = {}
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("workerId") == session["worker_id"]:
                indexed[payload.get("toolCallId")] = row
        results = []
        for call in session["pendingCalls"]:
            row = indexed.get(call["callId"])
            if row is None or row["status"] not in {"done", "failed"}:
                raise RuntimeError(f"tool result {call['callId']} is not ready")
            result = json.loads(row["result_json"]) if row["result_json"] else {}
            denied = result.get("decision") in {"denied", "scope-denied"}
            if denied:
                content = f"Operator denied Rekit tool {call['toolId']}."
            elif result.get("output"):
                output_path = Path(result["output"])
                content = output_path.read_text(encoding="utf-8", errors="replace")[:30_000]
            else:
                content = row["error"] or f"Rekit tool {call['toolId']} failed."
            results.append(ModelToolResult(
                call_id=call["callId"], content=content, denied=denied
            ))
        return tuple(results)


def _read_meta(paths: RunPaths) -> dict[str, Any]:
    return json.loads(paths.run_json.read_text(encoding="utf-8"))


def _project_memory_log(paths: RunPaths) -> ProjectMemoryLog:
    """Canonical project-level stream shared by every run for the same target.

    Muster lays runs out as ``<storage>/projects/<project-id>/runs/<run-id>``. Memory
    belongs at the project root beside ``runs/``; it references run/artifact/question
    records and never copies their operational tables.
    """
    project_dir = paths.run_dir.parents[1]
    return ProjectMemoryLog(project_dir)


def _manifest_actions(manifest: Any) -> tuple[ActionAuthority, ...]:
    actions = tuple(manifest.actions)
    if not actions or any(not isinstance(action, ActionAuthority) for action in actions):
        raise ValueError("tool manifest has no valid semantic action authority")
    return actions


def _manifest_work_payload(manifest: Any) -> dict[str, Any]:
    return {
        "manifestDigest": manifest.effective_manifest_digest,
        "declaredActions": [action.value for action in manifest.actions],
        "declaredCredentialUse": manifest.credential_use,
        "authorityVersion": manifest.authority_version,
    }


def _tool_authorities(rekit: RekitAdapter, tool_ids: Any) -> dict[str, Any]:
    return {tool_id: rekit.manifest(tool_id).public_authority() for tool_id in tool_ids}


def _manifest_denial(scope: AuthorizedScope, target: str, payload: dict[str, Any],
                     reason: str) -> ScopeDecision:
    return ScopeDecision(
        False, reason,
        (f"scope:{scope.envelope.scope_id}:r{scope.envelope.revision}:"
         f"{scope.envelope.content_digest[:12]}"),
        TargetGrant.from_path(target).path_fingerprint,
        (opaque_ref("endpoint", payload["endpoint"]) if payload.get("endpoint") else None),
        payload.get("requestedAction"),
    )


def _redact_intent(payload: dict[str, Any]) -> dict[str, Any]:
    public = dict(payload)
    endpoint = public.pop("endpoint", None)
    if endpoint:
        public["endpointRef"] = opaque_ref("endpoint", str(endpoint))
    # accountRef is required to be opaque by the scope matrix; never expose a
    # malformed model-supplied value before that denial is resolved.
    account = public.pop("accountRef", None)
    if account:
        public["accountRef"] = (
            account if str(account).startswith("account:")
            else opaque_ref("account", str(account))
        )
    return public


def _account_intent_ref(value: str | None) -> str | None:
    if value is None:
        return None
    if re.fullmatch(r"account:[A-Za-z0-9._-]{1,128}", value):
        return value
    return opaque_ref("account", value)


def _require_scope_for_creation(scope: AuthorizedScope, target: TargetGrant,
                                manifests: list[Any], *, now: str) -> None:
    scope.validate(now=now)
    for manifest in manifests:
        for action in _manifest_actions(manifest):
            endpoint = None
            if action is ActionAuthority.NETWORK_ACCESS and scope.envelope.endpoints:
                # Creation authorizes exposure under at least one exact endpoint.
                # Dispatch still requires the exact durable runtime endpoint.
                endpoint = scope.envelope.endpoints[0]
            decision = decide_scope(
                scope,
                ScopeRequest(
                    action=action, target=target, endpoint=endpoint,
                    account_ref=(scope.envelope.account_refs[0]
                                 if manifest.credential_use and scope.envelope.account_refs else None),
                    uses_credentials=manifest.credential_use,
                ),
                now=now,
            )
            if not decision.allowed:
                raise PermissionError(
                    f"run creation denied for {manifest.id}: {decision.reason_code}"
                )


def _load_scope(paths: RunPaths, target: Path) -> AuthorizedScope:
    scope_path = paths.run_dir / "scope.json"
    if scope_path.is_file():
        return AuthorizedScope.from_dict(json.loads(scope_path.read_text(encoding="utf-8")))
    # Pre-scope runs receive only a short-lived, exact-target, network-none grant.
    # Any non-read-only queued work will fail at the dispatch decision matrix.
    return legacy_local_read_only_scope(target, now=utcnow())


def _request_plan(request: RunRequest) -> InvestigationPlan:
    ceilings = RunCeilings(
        request.concurrency, request.retries_per_worker,
        request.cost_units, request.max_workers,
    )
    if request.strategy is not None:
        return plan_investigation(request.goal, request.strategy, ceilings=ceilings)
    strategy = Strategy(
        name="custom-roles",
        description="Explicit worker roles supplied by the operator.",
        workers=tuple(WorkerSeed(role, f"Investigate as the {role} specialist.")
                      for role in request.worker_roles),
        ceilings=ceilings,
    )
    return plan_investigation(request.goal, strategy, ceilings=ceilings)


def _plan_payload(plan: InvestigationPlan) -> dict[str, Any]:
    return {
        "strategy": plan.strategy, "goal": plan.goal,
        "ceilings": asdict(plan.ceilings),
        "work": [asdict(item) for item in plan.work],
    }


def _plan_from_payload(payload: dict[str, Any]) -> InvestigationPlan:
    return InvestigationPlan(
        strategy=payload["strategy"], goal=payload["goal"],
        ceilings=RunCeilings(**payload["ceilings"]),
        work=tuple(PlannedWork(**item) for item in payload["work"]),
    )


def _plan_from_meta(meta: dict[str, Any]) -> InvestigationPlan:
    """Read current plans while keeping pre-strategy runs resumable."""
    payload = meta.get("strategyPlan")
    if payload is not None:
        return _plan_from_payload(payload)
    roles = tuple(meta.get("workerRoles") or ("recon", "analyst"))
    concurrency = int(meta.get("concurrency", 4))
    ceilings = RunCeilings(
        concurrency=concurrency, retries_per_worker=1,
        cost_units=max(100, len(roles) * 10), max_workers=max(8, concurrency, len(roles)),
    )
    legacy = Strategy(
        name="legacy-roles", description="Compatibility plan for an existing run.",
        workers=tuple(WorkerSeed(role, f"Investigate as the {role} specialist.")
                      for role in roles),
        ceilings=ceilings,
    )
    return plan_investigation(meta["goal"], legacy, ceilings=ceilings)


def _adaptive_work(ledger: FactoryLedger, run_id: str) -> list[PlannedWork]:
    result = []
    rows = ledger.conn.execute(
        "select payload_json from work_items "
        "where run_id=? and operation='model-worker'", (run_id,),
    ).fetchall()
    for row in rows:
        payload = json.loads(row["payload_json"])
        if payload.get("origin") != "worker-proposal":
            continue
        result.append(PlannedWork(
            id=payload["planId"], dedupe_key=payload["dedupeKey"],
            role=payload["role"],
            objective=payload["goal"].split("Assigned objective: ", 1)[-1],
            cost_units=int(payload["costUnits"]),
            evidence_ids=tuple(payload.get("evidenceIds", [])),
            origin="worker-proposal",
        ))
    return result


def _write_status(paths: RunPaths, status: str) -> None:
    meta = _read_meta(paths)
    meta["status"] = status
    meta["updatedAt"] = utcnow()
    atomic_write(paths.run_json, json.dumps(meta, indent=2, sort_keys=True) + "\n")


def _target_snapshot(target: Path, *, max_files: int = 30, max_chars: int = 60_000) -> str:
    files = [target] if target.is_file() else [
        path for path in sorted(target.rglob("*"))
        if path.is_file()
        and not any(part.startswith(".") for part in path.relative_to(target).parts)
        and path.name not in {"secrets.env"}
    ]
    chunks: list[str] = []
    used = 0
    for path in files[:max_files]:
        data = path.read_bytes()[:12_000]
        if b"\x00" in data:
            chunks.append(f"--- {path.name} [binary, {path.stat().st_size} bytes] ---")
            continue
        text = data.decode("utf-8", errors="replace")
        label = path.name if target.is_file() else str(path.relative_to(target))
        chunk = f"--- {label} ---\n{text}"
        remaining = max_chars - used
        if remaining <= 0:
            break
        chunks.append(chunk[:remaining])
        used += len(chunk[:remaining])
    return "\n\n".join(chunks) or f"[no readable text files under {target}]"


def _tool_context(ledger: FactoryLedger, run_id: str, max_chars: int = 30_000,
                  evidence_ids: list[str] | None = None) -> str:
    rows = ledger.conn.execute(
        "select id, sha256, path, origin from artifacts where run_id=? and kind='tool-output' "
        "order by created_at", (run_id,)
    ).fetchall()
    allowed = set(evidence_ids) if evidence_ids is not None else None
    chunks = []
    used = 0
    for row in rows:
        if allowed is not None and row["id"] not in allowed \
                and (not row["sha256"] or f"sha256:{row['sha256']}" not in allowed):
            continue
        path = Path(row["path"])
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        chunk = f"--- {row['origin']} ---\n{text}"
        remaining = max_chars - used
        if remaining <= 0:
            break
        chunks.append(chunk[:remaining])
        used += len(chunk[:remaining])
    return "\n\n".join(chunks)


def _render_report(ledger: FactoryLedger, paths: RunPaths,
                   meta: dict[str, Any]) -> Path:
    rows = ledger.conn.execute(
        "select title, result_json, status from work_items "
        "where run_id=? and operation='model-worker' order by created_at",
        (paths.run_id,),
    ).fetchall()
    reports = []
    for row in rows:
        reports.append({
            "worker": row["title"],
            "status": row["status"],
            "report": json.loads(row["result_json"]) if row["result_json"] else None,
        })
    payload = {
        "runId": paths.run_id,
        "target": meta["target"],
        "goal": meta["goal"],
        "workers": reports,
        "coverage": ledger.coverage(paths.run_id),
        "generatedAt": utcnow(),
    }
    report_path = paths.reports_dir / "investigation.json"
    atomic_write(report_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return report_path


def default_storage_root() -> Path:
    return Path(os.environ.get("REKIT_FACTORY_HOME", "~/.rekit-factory")).expanduser()
