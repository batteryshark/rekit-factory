"""Durable investigation creation, supervision, permission gates, and resume."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
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
from rekit_factory.rekit_client import RekitAdapter
from rekit_factory.store import FactoryLedger


@dataclass(frozen=True)
class RunRequest:
    target: Path
    goal: str
    tools: tuple[str, ...] = ()
    model_tools: tuple[str, ...] = ()
    worker_roles: tuple[str, ...] = ("recon", "analyst")
    concurrency: int = 4
    model_profile: str | None = None

    def validate(self) -> "RunRequest":
        target = self.target.expanduser().resolve()
        if not target.exists():
            raise FileNotFoundError(target)
        if not self.goal.strip():
            raise ValueError("goal must not be empty")
        if not self.worker_roles:
            raise ValueError("at least one worker role is required")
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        return RunRequest(
            target=target,
            goal=self.goal.strip(),
            tools=tuple(dict.fromkeys(self.tools)),
            model_tools=tuple(dict.fromkeys(self.model_tools)),
            worker_roles=tuple(dict.fromkeys(self.worker_roles)),
            concurrency=self.concurrency,
            model_profile=self.model_profile,
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
        self.storage_root.mkdir(parents=True, exist_ok=True)
        worker_backend = self.worker_backend(request.model_profile)
        public_config = {
            "goal": request.goal,
            "tools": list(request.tools),
            "modelTools": list(request.model_tools),
            "workerRoles": list(request.worker_roles),
            "concurrency": request.concurrency,
            "modelProfile": worker_backend.profile.public_dict(),
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
            "project": project_meta,
            "status": "queued",
            "createdAt": utcnow(),
        }
        atomic_write(paths.run_json, json.dumps(meta, indent=2, sort_keys=True) + "\n")

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
                    payload={"toolId": tool_id, "safetyTier": manifest.safety_tier},
                    state_label="queued",
                ))
            for role in request.worker_roles:
                worker_id = ledger.add_worker(run_id, role, worker_backend.profile.name)
                ledger.enqueue(
                    run_id=run_id,
                    key=stable_key("worker", role, request.goal),
                    target=str(request.target),
                    operation="model-worker",
                    category="worker",
                    title=f"{role} worker",
                    priority=100,
                    depends_on=tool_items,
                    payload={"workerId": worker_id, "role": role, "goal": request.goal,
                             "modelProfile": worker_backend.profile.name,
                             "availableTools": list(request.model_tools)},
                    state_label="queued",
                )
        return paths.run_dir

    def run(self, request: RunRequest) -> dict[str, Any]:
        run_dir = self.create(request)
        return asyncio.run(self.drive(run_dir))

    async def drive(self, run_dir: str | Path) -> dict[str, Any]:
        paths = resolve_run_dir(run_dir)
        meta = _read_meta(paths)
        target = Path(meta["target"])
        concurrency = int(meta.get("concurrency", 4))

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
            work_item_id = ledger.answer_permission(paths.run_id, question_id, answer)
            ledger.event_log(
                paths.run_id,
                "permission.resolved",
                f"Operator answered {answer}",
                payload={"questionId": question_id, "workItemId": work_item_id},
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
            work.append(item)
        artifacts = [dict(row) for row in ledger.conn.execute(
            "select * from artifacts where run_id=? order by created_at", (paths.run_id,)
        ).fetchall()]
        return {
            "run": dict(run) if run is not None else None,
            "meta": _read_meta(paths),
            "coverage": ledger.coverage(paths.run_id),
            "workers": ledger.workers(paths.run_id),
            "workItems": work,
            "events": ledger.events(paths.run_id),
            "pendingQuestions": ledger.pending_questions(paths.run_id),
            "modelCalls": ledger.model_calls(paths.run_id),
            "workerSessions": ledger.worker_sessions(paths.run_id),
            "toolCalls": ledger.tool_calls(paths.run_id),
            "artifacts": artifacts,
        }

    async def _tool_handler(self, ctx, item: dict[str, Any]) -> None:
        ledger: FactoryLedger = ctx.deps.ledger
        payload = json.loads(item["payload_json"])
        tool_id = payload["toolId"]
        manifest = self.rekit.manifest(tool_id)
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
            ledger.link_permission(qid, ctx.state.run_id, item["id"], tool_id)
            ledger.set_work_status(
                item["id"], "blocked", error="Awaiting operator permission",
                state_label="needs_permission",
            )
            ledger.event_log(
                ctx.state.run_id, "permission.requested",
                f"{tool_id} requires operator permission",
                payload={"questionId": qid, "toolId": tool_id,
                         "safetyTier": manifest.safety_tier},
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
            ctx.state.run_id, item["id"], tool_id, manifest.safety_tier
        )
        result = await asyncio.to_thread(
            self.rekit.run,
            tool_id,
            Path(item["target"]),
            allow_dynamic=manifest.requires_permission and answer == "allow",
        )
        output_path = ctx.deps.paths.run_dir / "tool-output" / f"{item['id']}-{tool_id}.log"
        atomic_write(
            output_path,
            f"command: {result.command_label}\nexit: {result.exit_code}\n\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}\n",
        )
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
            metadata={"toolId": tool_id, "exitCode": result.exit_code},
        )
        if result.exit_code == 0:
            ledger.resolve(
                item["id"],
                result={"toolId": tool_id, "output": str(output_path)},
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

            turn = await worker_backend.analyze(
                role=role,
                goal=payload["goal"],
                target_snapshot=ctx.deps.scratch["targetSnapshot"],
                tool_context=_tool_context(ledger, ctx.state.run_id),
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
                 "toolName": call.tool_name}
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
                         "nextActionCount": len(report.next_actions)},
            )
        except Exception as exc:
            if item["attempts"] < 2:
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
            ledger.event_log(
                ctx.state.run_id, "worker.failed", f"{type(exc).__name__}: {exc}",
                worker_id=worker_id,
            )
            raise

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
            denied = result.get("decision") == "denied"
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


def _tool_context(ledger: FactoryLedger, run_id: str, max_chars: int = 30_000) -> str:
    rows = ledger.conn.execute(
        "select path, origin from artifacts where run_id=? and kind='tool-output' "
        "order by created_at", (run_id,)
    ).fetchall()
    chunks = []
    used = 0
    for row in rows:
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
