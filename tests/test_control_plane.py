from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rekit_factory.api import FactoryServer, _event_batch
from rekit_factory.control import InvestigationController, RunRequest
from rekit_factory.dossiers import dossier_list as canonical_dossier_list
from rekit_factory.evidence import EvidenceStore, Provenance, hash_target
from rekit_factory.models import (
    DeferredModelToolCall,
    ModelActivity,
    ModelProfile,
    WorkerReport,
    WorkerTurn,
)
from rekit_factory.rekit_client import ToolManifest, ToolResult
from rekit_factory.remote import InvocationRequest, LocalRekitWorker, WorkerLeaseRequest
from rekit_factory.store import FactoryLedger
from muster import resolve_run_dir
from rekit_factory.scope import (
    ActionAuthority, AuthorizedScope, ScopeApproval, ScopeEnvelope, TargetGrant,
)


def authorized_dynamic_scope(target):
    envelope = ScopeEnvelope(
        scope_id="scope-control-test", revision=1,
        valid_from="2026-07-01T00:00:00Z", valid_until="2026-08-01T00:00:00Z",
        targets=(TargetGrant.from_path(target),),
        actions=(ActionAuthority.READ_LOCAL_TARGET, ActionAuthority.EXECUTE_UNTRUSTED),
    )
    return AuthorizedScope(envelope, ScopeApproval(
        scope_id=envelope.scope_id, revision=envelope.revision,
        content_digest=envelope.content_digest, approved_by="test-operator",
        approved_at="2026-07-01T00:00:00Z", expires_at="2026-08-01T00:00:00Z",
        rationale="Deterministic test authorization for the exact fixture",
    ))


class FakeBackend:
    def __init__(self, *, name="fake", model="deterministic"):
        self.profile = ModelProfile(
            name=name, provider="test", model=model,
            base_url="https://model.invalid/v1", api_key="never-persist-this-key",
        )
        self.active = 0
        self.peak = 0

    async def analyze(self, *, role, goal, target_snapshot, tool_context, **kwargs):
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return WorkerReport(
            summary=f"{role} reviewed the target",
            observations=[f"goal={goal}", f"snapshot={len(target_snapshot)} chars"],
            next_actions=["inspect the next artifact"],
            status_update=f"{role} review complete",
        ), {"inputTokens": 10, "outputTokens": 5}


class DeferredBackend(FakeBackend):
    def __init__(self, *, tool_id="fixture-scan"):
        super().__init__()
        self.tool_id = tool_id
        self.turns = 0
        self.returned_tool_results = ()

    async def analyze(self, *, role, goal, target_snapshot, tool_context,
                      messages_json=None, tool_results=(), event_sink=None, **kwargs):
        self.turns += 1
        if event_sink:
            event_sink(ModelActivity(
                kind="model.thinking.streamed",
                message="Finished streaming thinking",
                payload={"characters": 42},
            ))
        if messages_json is None:
            return WorkerTurn(
                report=None,
                usage={"inputTokens": 10, "outputTokens": 2},
                messages_json='[{"fixture":"first-turn"}]',
                deferred_calls=(DeferredModelToolCall(
                    call_id="call-fixture",
                    tool_id=self.tool_id,
                    tool_name="rekit__fixture_scan",
                ),),
            )
        self.returned_tool_results = tool_results
        return WorkerTurn(
            report=WorkerReport(
                summary="Reviewed the returned Rekit evidence",
                observations=["tool result received"],
                next_actions=[],
                status_update="deferred tool round trip complete",
            ),
            usage={"inputTokens": 5, "outputTokens": 5},
            messages_json='[{"fixture":"complete"}]',
        )


class FailingBackend(FakeBackend):
    async def analyze(self, **kwargs):
        raise RuntimeError("provider unavailable")


class FlakyBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def analyze(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary provider error")
        return await super().analyze(**kwargs)


class FakeRekit:
    def __init__(self, *, risky=False):
        self.risky = risky
        self.calls = []

    def manifest(self, tool_id):
        return ToolManifest(
            id=tool_id,
            name=tool_id,
            description="fixture tool",
            safety_tier=3 if self.risky else 0,
            executes_input="full" if self.risky else "no",
            network="none",
            actions=((ActionAuthority.READ_LOCAL_TARGET, ActionAuthority.EXECUTE_UNTRUSTED)
                     if self.risky else (ActionAuthority.READ_LOCAL_TARGET,)),
        )

    def list_tools(self):
        return [self.manifest("fixture-scan"), self.manifest("exec-observe")]

    def run(self, tool_id, target, *, allow_dynamic=False,
            expected_manifest_digest=None):
        self.calls.append((tool_id, Path(target), allow_dynamic))
        return ToolResult(
            exit_code=0,
            stdout='{"ok": true}',
            stderr="",
            command_label=f"rekit run {tool_id} <target>",
            manifest_digest=expected_manifest_digest,
        )


class SecretOutputRekit(FakeRekit):
    def run(self, tool_id, target, *, allow_dynamic=False,
            expected_manifest_digest=None):
        result = super().run(
            tool_id, target, allow_dynamic=allow_dynamic,
            expected_manifest_digest=expected_manifest_digest,
        )
        return ToolResult(
            exit_code=result.exit_code,
            stdout="api_key=fixture-secret-value\nproof: ok",
            stderr="Authorization: Bearer fixture-bearer-token",
            command_label=result.command_label,
            manifest_digest=result.manifest_digest,
        )


class ControlPlaneTests(unittest.TestCase):
    def _fixture(self, tmp):
        target = Path(tmp) / "target"
        target.mkdir()
        (target / "main.py").write_text("def run(value):\n    return value + 1\n", encoding="utf-8")
        return target

    def test_workers_fan_out_and_everything_is_ledgered(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._fixture(tmp)
            backend = FakeBackend()
            rekit = FakeRekit()
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs", rekit=rekit, workers=backend
            )
            result = controller.run(RunRequest(
                target=target,
                goal="Explain the code and identify research leads",
                tools=("fixture-scan",),
                worker_roles=("recon", "analyst"),
                concurrency=2,
            ))

            self.assertEqual("completed", result["run"]["status"])
            self.assertEqual(0, result["coverage"]["pending"])
            self.assertEqual(2, backend.peak)
            self.assertEqual(2, len(result["modelCalls"]))
            self.assertEqual(1, len(result["toolCalls"]))
            self.assertEqual(1, len(rekit.calls))
            self.assertTrue(any(event["kind"] == "worker.completed" for event in result["events"]))
            self.assertTrue(any(a["kind"] == "tool-output" for a in result["artifacts"]))

            run_dir = Path(result["run"]["run_dir"])
            for path in run_dir.rglob("*"):
                if path.is_file():
                    self.assertNotIn(b"never-persist-this-key", path.read_bytes())

    def test_tool_evidence_projects_redacted_output_and_never_captures_cli_screenshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._fixture(tmp)
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs", rekit=SecretOutputRekit(),
                workers=FakeBackend(),
            )
            result = controller.run(RunRequest(
                target=target, goal="Capture the fixture proof",
                tools=("fixture-scan",), worker_roles=("analyst",),
            ))

            artifact = next(item for item in result["artifacts"] if item["kind"] == "tool-output")
            projected = Path(artifact["path"]).read_text(encoding="utf-8")
            assert "fixture-secret-value" not in projected
            assert "fixture-bearer-token" not in projected
            assert "[REDACTED:CREDENTIAL]" in projected
            assert any(event["kind"] == "evidence.redacted" for event in result["events"])
            evidence = EvidenceStore(Path(result["run"]["run_dir"]) / "evidence")
            evidence_id = json.loads(artifact["metadata_json"])["evidenceArtifactId"]
            candidate = evidence.knowledge_candidate_text(evidence_id)
            assert candidate is not None and "fixture-secret-value" not in candidate
            assert not list(Path(result["run"]["run_dir"]).rglob("*.png"))

    def test_risky_tool_suspends_until_durable_deny_then_workers_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._fixture(tmp)
            backend = FakeBackend()
            rekit = FakeRekit(risky=True)
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs", rekit=rekit, workers=backend
            )
            run_dir = controller.create(RunRequest(
                target=target,
                goal="Observe behavior only if the operator permits it",
                tools=("exec-observe",),
                worker_roles=("analyst",),
                scope=authorized_dynamic_scope(target),
            ))
            suspended = asyncio.run(controller.drive(run_dir))

            self.assertEqual("needs_input", suspended["run"]["status"])
            self.assertEqual(1, len(suspended["pendingQuestions"]))
            self.assertEqual([], rekit.calls)
            self.assertEqual("queued", suspended["workers"][0]["status"])
            qid = suspended["pendingQuestions"][0]["id"]

            completed = controller.answer(run_dir, qid, "deny")
            self.assertEqual("completed", completed["run"]["status"])
            self.assertEqual([], rekit.calls)
            self.assertEqual("done", completed["workers"][0]["status"])
            denied = [item for item in completed["workItems"]
                      if item["operation"] == "rekit-tool"][0]
            self.assertEqual("denied", denied["state_label"])
            self.assertEqual([], completed["pendingQuestions"])

    def test_allow_requeues_and_passes_explicit_dynamic_consent(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._fixture(tmp)
            backend = FakeBackend()
            rekit = FakeRekit(risky=True)
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs", rekit=rekit, workers=backend
            )
            run_dir = controller.create(RunRequest(
                target=target,
                goal="Run the approved observation",
                tools=("exec-observe",),
                worker_roles=("analyst",),
                scope=authorized_dynamic_scope(target),
            ))
            suspended = asyncio.run(controller.drive(run_dir))
            qid = suspended["pendingQuestions"][0]["id"]
            manifest = rekit.manifest("exec-observe")
            with FactoryLedger(resolve_run_dir(run_dir).db_path) as ledger:
                permission = ledger.conn.execute(
                    "select manifest_digest from factory_permissions where question_id=?",
                    (qid,),
                ).fetchone()
                self.assertEqual(manifest.effective_manifest_digest,
                                 permission["manifest_digest"])
            completed = controller.answer(run_dir, qid, "allow")

            self.assertEqual("completed", completed["run"]["status"])
            self.assertEqual(1, len(rekit.calls))
            self.assertTrue(rekit.calls[0][2])
            self.assertTrue(any(event["kind"] == "permission.resolved"
                                for event in completed["events"]))
            self.assertEqual(manifest.effective_manifest_digest,
                             completed["toolCalls"][0]["manifest_digest"])
            self.assertEqual(["read_local_target", "execute_untrusted"],
                             completed["toolCalls"][0]["declaredActions"])
            self.assertIn(manifest.effective_manifest_digest,
                          completed["artifacts"][0]["metadata_json"])

    def test_terminal_failed_worker_makes_run_failed_not_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._fixture(tmp)
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs",
                rekit=FakeRekit(),
                workers=FailingBackend(),
            )
            result = controller.run(RunRequest(
                target=target,
                goal="Exercise provider failure",
                worker_roles=("analyst",),
            ))
            self.assertEqual(0, result["coverage"]["pending"])
            self.assertEqual(1, result["coverage"]["failed"])
            self.assertEqual("failed", result["run"]["status"])
            self.assertTrue(any(event["kind"] == "run.failed" for event in result["events"]))

    def test_transient_worker_failure_is_durably_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._fixture(tmp)
            backend = FlakyBackend()
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs",
                rekit=FakeRekit(),
                workers=backend,
            )
            result = controller.run(RunRequest(
                target=target,
                goal="Retry transient provider failures",
                worker_roles=("analyst",),
            ))
            self.assertEqual("completed", result["run"]["status"])
            self.assertEqual(2, backend.calls)
            worker_item = [item for item in result["workItems"]
                           if item["operation"] == "model-worker"][0]
            self.assertEqual(2, worker_item["attempts"])
            self.assertTrue(any(event["kind"] == "worker.retrying"
                                for event in result["events"]))

    def test_run_selects_a_named_model_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._fixture(tmp)
            default = FakeBackend()
            alternate = FakeBackend(name="local", model="local-research-model")
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs",
                rekit=FakeRekit(),
                workers={"fake": default, "local": alternate},
            )
            result = controller.run(RunRequest(
                target=target,
                goal="Use the selected profile",
                worker_roles=("analyst",),
                model_profile="local",
            ))
            self.assertEqual("local", result["meta"]["modelProfile"]["name"])
            self.assertEqual("local", result["workers"][0]["model_profile"])
            self.assertEqual("local-research-model", result["modelCalls"][0]["model"])

    def test_model_requested_tool_round_trips_through_durable_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._fixture(tmp)
            backend = DeferredBackend()
            rekit = FakeRekit()
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs", rekit=rekit, workers=backend
            )
            result = controller.run(RunRequest(
                target=target,
                goal="Request evidence, then finish",
                model_tools=("fixture-scan",),
                worker_roles=("analyst",),
            ))

            self.assertEqual("completed", result["run"]["status"])
            self.assertEqual(2, backend.turns)
            self.assertEqual(1, len(rekit.calls))
            self.assertIn("stdout", backend.returned_tool_results[0].content)
            self.assertEqual(2, len(result["modelCalls"]))
            self.assertEqual([], result["workerSessions"][0]["pendingCalls"])
            kinds = {event["kind"] for event in result["events"]}
            self.assertIn("worker.tools_requested", kinds)
            self.assertIn("worker.resuming", kinds)
            self.assertIn("model.thinking.streamed", kinds)

    def test_model_requested_gated_tool_suspends_and_resumes_after_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._fixture(tmp)
            backend = DeferredBackend(tool_id="exec-observe")
            rekit = FakeRekit(risky=True)
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs", rekit=rekit, workers=backend
            )
            run_dir = controller.create(RunRequest(
                target=target,
                goal="Request gated evidence",
                model_tools=("exec-observe",),
                worker_roles=("analyst",),
                scope=authorized_dynamic_scope(target),
            ))
            suspended = asyncio.run(controller.drive(run_dir))

            self.assertEqual("needs_input", suspended["run"]["status"])
            self.assertEqual([], rekit.calls)
            self.assertEqual(1, len(suspended["pendingQuestions"]))
            qid = suspended["pendingQuestions"][0]["id"]
            # Recreate both controller and backend to prove the continuation state lives
            # in the run ledger rather than in Python process memory.
            resumed_backend = DeferredBackend(tool_id="exec-observe")
            resumed_controller = InvestigationController(
                storage_root=Path(tmp) / "runs", rekit=rekit, workers=resumed_backend
            )
            completed = resumed_controller.answer(run_dir, qid, "allow")

            self.assertEqual("completed", completed["run"]["status"])
            self.assertEqual(1, backend.turns)
            self.assertEqual(1, resumed_backend.turns)
            self.assertTrue(resumed_backend.returned_tool_results)
            self.assertEqual(1, len(rekit.calls))
            self.assertTrue(rekit.calls[0][2])

    def test_anthropic_compatible_profile_loads_from_environment(self):
        environment = {
            "RESEARCH_API_KEY": "do-not-persist",
            "RESEARCH_API_BASEURL": "https://api.minimax.io/anthropic",
            "RESEARCH_API_MODEL": "MiniMax-M2.7",
            "RESEARCH_API_FORMAT": "anthropic",
        }
        with patch.dict("os.environ", environment, clear=False):
            profile = ModelProfile.from_env("RESEARCH")

        self.assertEqual("anthropic", profile.api_format)
        self.assertEqual("anthropic-compatible", profile.provider)
        self.assertEqual("anthropic", profile.public_dict()["apiFormat"])
        self.assertNotIn("api_key", profile.public_dict())

    def test_unknown_model_api_format_is_rejected(self):
        environment = {
            "RESEARCH_API_KEY": "do-not-persist",
            "RESEARCH_API_BASEURL": "https://model.invalid",
            "RESEARCH_API_MODEL": "model",
            "RESEARCH_API_FORMAT": "mystery",
        }
        with patch.dict("os.environ", environment, clear=False):
            with self.assertRaisesRegex(ValueError, "RESEARCH_API_FORMAT"):
                ModelProfile.from_env("RESEARCH")

    def test_mission_control_exposes_strategy_and_safety_composer(self):
        ui = Path(__file__).parents[1] / "src" / "rekit_factory" / "ui"
        page = (ui / "index.html").read_text(encoding="utf-8")
        script = (ui / "mission-control.js").read_text(encoding="utf-8")

        self.assertIn('id="strategySelect"', page)
        self.assertIn('id="retriesPerWorker"', page)
        self.assertIn('value="automatic-only"', page)
        self.assertIn('id="evidenceLifecycle"', page)
        for field in ("strategy", "concurrency", "retriesPerWorker", "costUnits", "maxWorkers"):
            self.assertIn(field, script)
        self.assertIn("cannot bypass server-side gates", page)
        self.assertIn("updateEvidence", script)
        self.assertNotIn("rawPath", script)

    def test_mission_control_renders_canonical_memory_and_scope_projections(self):
        ui = Path(__file__).parents[1] / "src" / "rekit_factory" / "ui"
        page = (ui / "index.html").read_text(encoding="utf-8")
        script = (ui / "mission-control.js").read_text(encoding="utf-8")
        style = (ui / "mission-control.css").read_text(encoding="utf-8")

        for marker in ('data-tab="memory"', 'id="memoryRibbon"', 'id="memoryGroups"',
                       'id="memoryContext"', 'id="scopeSummary"'):
            self.assertIn(marker, page)
        for field in ("snapshot.memory", "snapshot.memoryContext", "meta.scope",
                      "workstreams", "attempts", "decisions", "theories",
                      "questions", "next_actions", "missing_references"):
            self.assertIn(field, script)
        self.assertIn("prefers-reduced-motion", style)
        self.assertIn("memory-scan", style)
        self.assertNotIn("state.memory", script)

    def test_mission_control_fleet_keeps_e7_operational_cadence_and_keyboard_tabs(self):
        ui = Path(__file__).parents[1] / "src" / "rekit_factory" / "ui"
        script = (ui / "mission-control.js").read_text(encoding="utf-8")
        style = (ui / "mission-control.css").read_text(encoding="utf-8")

        for behavior in ("elapsedLabel", "targetKind", "coverage-track", "attention-cue",
                         "fleetSearch", "fleetHealth", "state.query",
                         '"ArrowLeft"', '"ArrowRight"',
                         "event.target.matches('[role=\"tab\"]')"):
            self.assertIn(behavior, script)
        for responsive in (".target-kind", ".card-foot", ".attention-cue", ".fleet-search",
                           "@media(max-width:560px)", ".health{display:none}"):
            self.assertIn(responsive, style)
        self.assertIn("@media(prefers-reduced-motion:reduce)", style)

    def test_loopback_service_restart_is_explicit_and_stops_the_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs",
                rekit=FakeRekit(),
                workers=FakeBackend(),
            )
            server = FactoryServer(("127.0.0.1", 0), controller, allow_restart=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                config = self._request(base + "/api/config")
                self.assertTrue(config["restartAvailable"])
                self.assertEqual(server.instance_id, config["serviceInstance"])
                result = self._request(base + "/api/restart", {}, expected=202)
                self.assertTrue(result["restarting"])
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                self.assertTrue(server.restart_requested.is_set())
            finally:
                server.server_close()

    def test_loopback_api_launches_exposes_and_answers_a_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._fixture(tmp)
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs",
                rekit=FakeRekit(risky=True),
                workers=FakeBackend(),
            )
            server = FactoryServer(("127.0.0.1", 0), controller)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(base + "/", timeout=5) as response:
                    page = response.read()
                    self.assertIn(b"Mission Control", page)
                    self.assertIn(b'/ui/mission-control.css', page)
                    self.assertIn(b'/ui/mission-outcomes.js', page)
                    self.assertIn(b'/ui/mission-control.js', page)
                    self.assertIn(b'data-tab="outcomes"', page)
                    self.assertIn(b'data-tab="reports"', page)
                    self.assertIn(b'data-tab="usage"', page)
                    self.assertIn(b'id="strategySelect"', page)
                    self.assertIn(b'id="retriesPerWorker"', page)
                    self.assertIn(b'value="automatic-only"', page)
                    self.assertIn(b'id="restartService"', page)
                    self.assertIn(b'data-tab="memory"', page)
                    self.assertIn(b'id="scopeSummary"', page)
                with urlopen(base + "/ui/mission-control.css", timeout=5) as response:
                    self.assertEqual("text/css; charset=utf-8", response.headers["Content-Type"])
                    self.assertIn(b"prefers-reduced-motion", response.read())
                with urlopen(base + "/ui/mission-control.js", timeout=5) as response:
                    self.assertEqual(
                        "text/javascript; charset=utf-8", response.headers["Content-Type"]
                    )
                    script = response.read()
                    self.assertIn(b"async function boot()", script)
                    self.assertIn(b"renderDecision", script)
                    self.assertIn(b"cacheReadTokens", script)
                    self.assertIn(b"retriesPerWorker", script)
                    self.assertIn(b"costUnits", script)
                with urlopen(base + "/ui/mission-outcomes.js", timeout=5) as response:
                    self.assertEqual(
                        "text/javascript; charset=utf-8", response.headers["Content-Type"]
                    )
                    outcomes = response.read()
                    self.assertIn(b"factory-outcomes/semantic-sha256/v1", outcomes)
                    self.assertIn(b"createSemanticTracker", outcomes)
                    self.assertIn(b"maxWorkers", script)
                    self.assertIn(b"restartService", script)
                config = self._request(base + "/api/config")
                self.assertFalse(config["restartAvailable"])
                self.assertEqual("deterministic", config["modelProfile"]["model"])
                self.assertEqual("prompted", config["modelProfile"]["structuredOutputMode"])
                self.assertIn("recon-analysis", {item["name"] for item in config["strategies"]})
                self.assertEqual(2, len(config["tools"]))
                for tool in config["tools"]:
                    self.assertNotIn("path", tool)
                    self.assertRegex(tool["authority"]["digest"], r"^[0-9a-f]{64}$")
                    self.assertTrue(tool["authority"]["actions"])
                launched = self._request(base + "/api/runs", {
                    "target": str(target),
                    "goal": "Exercise the API permission path",
                    "tools": ["exec-observe"],
                    "workerRoles": ["analyst"],
                    "scope": authorized_dynamic_scope(target).to_dict(),
                }, expected=202)
                run_id = launched["run"]["id"]
                self.assertEqual(1, launched["outcomeProjection"]["schemaVersion"])
                self.assertEqual(
                    "factory-outcomes/v1", launched["outcomeProjection"]["vocabularyVersion"]
                )
                self.assertIsInstance(
                    launched["outcomeProjection"]["sourceWatermarks"]["factoryEventRowid"], int,
                )
                self.assertFalse(
                    launched["outcomeProjection"]["consistency"]
                    ["watermarksAreProjectionIdentity"]
                )
                with FactoryLedger(Path(launched["runDir"]) / "run.db") as watermark_ledger:
                    current_watermark = watermark_ledger.conn.execute(
                        "select max(rowid) from factory_events where run_id=?", (run_id,),
                    ).fetchone()[0]
                self.assertLessEqual(
                    launched["outcomeProjection"]["sourceWatermarks"]["factoryEventRowid"],
                    current_watermark,
                )
                suspended = self._wait_status(base, run_id, "needs_input")
                self.assertEqual(1, len(suspended["pendingQuestions"]))

                fleet = self._request(base + "/api/fleet")
                self.assertEqual(1, fleet["runs"][0]["needsYou"])
                for field in ("createdAt", "updatedAt", "completedAt", "iteration", "maxIterations"):
                    self.assertIn(field, fleet["runs"][0])
                qid = suspended["pendingQuestions"][0]["id"]
                answered = self._request(
                    base + f"/api/runs/{run_id}/answers",
                    {"questionId": qid, "answer": "allow"},
                    expected=202,
                )
                self.assertTrue(answered["started"])
                completed = self._wait_status(base, run_id, "completed")
                run_outcome = next(
                    item for item in completed["outcomeProjection"]["entities"]
                    if item["entityType"] == "run" and item["entityId"] == run_id
                )
                self.assertEqual("terminal", run_outcome["facets"]["execution"]["state"])
                self.assertEqual("completed", run_outcome["facets"]["completion"]["state"])
                self.assertEqual("successful", run_outcome["facets"]["disposition"]["state"])
                self.assertEqual(0, completed["coverage"]["pending"])
                self.assertEqual("Exercise the API permission path", completed["memory"]["goals"][-1]["text"])
                self.assertIn("GOAL: Exercise the API permission path", completed["memoryContext"])
                self.assertEqual("none", completed["meta"]["scope"]["networkMode"])
                reports = self._request(base + f"/api/runs/{run_id}/reports")
                self.assertEqual(1, len(reports["reports"]))
                self.assertEqual("analyst", reports["reports"][0]["role"])
                self.assertEqual(1, len(completed["artifacts"]))
                artifact = completed["artifacts"][0]
                with urlopen(
                    base + f"/api/runs/{run_id}/artifacts/{artifact['id']}", timeout=5
                ) as response:
                    self.assertIn("attachment", response.headers["Content-Disposition"])
                    self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])
                    self.assertIn(b"stdout", response.read())
                evidence_store = EvidenceStore(Path(launched["runDir"]) / "evidence")
                evidence_record = evidence_store.capture_tool_output(
                    b"operator-visible proof",
                    Provenance(
                        run_id=run_id, source="fixture", capture_reason="API lifecycle proof",
                        captured_at="2026-07-13T12:00:00Z", environment_id="local:test",
                        target_sha256=hash_target(target),
                    ),
                ).record
                self.assertIsNotNone(evidence_record)
                evidence = self._request(base + f"/api/runs/{run_id}/evidence")
                public_record = next(
                    item for item in evidence["records"]
                    if item["artifactId"] == evidence_record.artifact_id
                )
                self.assertNotIn("rawPath", public_record)
                pinned = self._request(
                    base + f"/api/runs/{run_id}/evidence/{evidence_record.artifact_id}/pin",
                    {"citationId": "operator:test"},
                )
                self.assertIn("operator:test", pinned["record"]["citations"])
                conflict = self._request(
                    base + f"/api/runs/{run_id}/evidence/{evidence_record.artifact_id}/delete", {}
                )
                self.assertEqual("retention_conflict", conflict["record"]["state"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_snapshot_uses_one_ledger_read_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._fixture(tmp)
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs", rekit=FakeRekit(), workers=FakeBackend()
            )
            run_dir = controller.create(RunRequest(
                target=target, goal="Capture one untorn ledger snapshot", worker_roles=("analyst",)
            ))
            run_id = resolve_run_dir(run_dir).run_id
            inserted = threading.Event()
            committed = threading.Event()
            writer_errors = []

            def publish_concurrently():
                try:
                    connection = sqlite3.connect(run_dir / "run.db", timeout=5)
                    connection.execute("begin immediate")
                    metadata = json.dumps({
                        "dossierId": "dossier-concurrent", "findingId": "finding-concurrent",
                        "manifestSha256": "a" * 64, "findingStateSha256": "b" * 64,
                        "verdict": "SUPPORTED", "findingStatus": "reproduced",
                        "artifactIds": {"proof-bundle": "artifact-concurrent"},
                    }, sort_keys=True)
                    connection.execute(
                        "insert into artifacts "
                        "(id,run_id,kind,path,logical_path,sha256,size_bytes,media_type,"
                        "language,origin,metadata_json,created_at) values (?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("artifact-concurrent", run_id, "proof-bundle", str(target),
                         "dossier/proof.json", "c" * 64, 1, "application/json", None,
                         "proof-dossier", metadata, "2026-07-13T08:40:00Z"),
                    )
                    connection.execute(
                        "insert into factory_events "
                        "(id,run_id,worker_id,kind,message,payload_json,created_at) "
                        "values (?,?,?,?,?,?,?)",
                        ("event-concurrent", run_id, None, "dossier.published",
                         "Concurrent dossier published", "{}", "2026-07-13T08:40:00Z"),
                    )
                    inserted.set()
                    connection.commit()
                    committed.set()
                    connection.close()
                except Exception as exc:  # pragma: no cover - asserted below
                    writer_errors.append(exc)
                    inserted.set()

            writer = None

            def dossier_read(read_ledger, selected_run_id, **kwargs):
                nonlocal writer
                self.assertTrue(read_ledger.conn.in_transaction)
                writer = threading.Thread(target=publish_concurrently, daemon=True)
                writer.start()
                self.assertTrue(inserted.wait(timeout=2))
                self.assertFalse(committed.is_set())
                return canonical_dossier_list(read_ledger, selected_run_id, **kwargs)

            with patch("rekit_factory.control.dossier_list", side_effect=dossier_read):
                before = controller.snapshot(run_dir)
            self.assertIsNotNone(writer)
            writer.join(timeout=5)
            self.assertFalse(writer.is_alive())
            self.assertEqual([], writer_errors)
            self.assertNotIn("artifact-concurrent", {item["id"] for item in before["artifacts"]})
            self.assertEqual([], before["dossiers"])

            after = controller.snapshot(run_dir)
            self.assertIn("artifact-concurrent", {item["id"] for item in after["artifacts"]})
            self.assertEqual("dossier-concurrent", after["dossiers"][0]["id"])
            self.assertEqual(
                "canonical-source-state",
                after["outcomeProjection"]["consistency"]["mode"],
            )
            self.assertEqual(
                "external-to-projection",
                after["outcomeProjection"]["consistency"]["sourceRead"],
            )

    def test_general_direction_answer_is_bounded_and_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._fixture(tmp)
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs", rekit=FakeRekit(), workers=FakeBackend()
            )
            run_dir = controller.create(RunRequest(
                target=target, goal="Wait for operator direction", worker_roles=("analyst",)
            ))
            paths = resolve_run_dir(run_dir)
            with FactoryLedger(paths.db_path) as ledger:
                ledger.ask_question(
                    paths.run_id, qid="direction-fixture", node="Strategy",
                    kind="direction", prompt="Which component should be prioritized?",
                )
            snapshot = controller.answer(
                run_dir, "direction-fixture", "Prioritize the parser boundary.", resume=False
            )
            self.assertEqual([], snapshot["pendingQuestions"])
            self.assertTrue(any(
                event["kind"] == "direction.resolved" for event in snapshot["events"]
            ))
            with self.assertRaisesRegex(ValueError, "must not be empty"):
                with FactoryLedger(paths.db_path) as ledger:
                    ledger.ask_question(
                        paths.run_id, qid="empty-direction", node="Strategy",
                        kind="direction", prompt="Anything else?",
                    )
                controller.answer(run_dir, "empty-direction", "   ", resume=False)

    def test_event_cursor_distinguishes_initial_continuation_and_reset(self):
        events = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        self.assertEqual(([{"id": "c"}], None), _event_batch(events, "b"))
        self.assertEqual((events, None), _event_batch(events, None))
        self.assertEqual(([], "c"), _event_batch(events, "foreign-run-event"))
        self.assertEqual(([], None), _event_batch([], "stale-event"))

        client_a, client_b = _event_batch(events, "b"), _event_batch(events, "b")
        self.assertEqual(client_a, client_b)
        self.assertEqual([{"id": "c"}], client_a[0])

        later = [*events, {"id": "terminal", "kind": "run.completed"}]
        self.assertEqual(
            ([{"id": "terminal", "kind": "run.completed"}], None),
            _event_batch(later, "c"),
        )
        self.assertEqual(([], None), _event_batch(later, "terminal"))
        reset_batch, reset_cursor = _event_batch(events, "event-from-other-run")
        self.assertEqual([], reset_batch)
        self.assertEqual(
            ([{"id": "terminal", "kind": "run.completed"}], None),
            _event_batch(later, reset_cursor),
        )

    def test_worker_envelope_requires_approval_for_gated_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = self._fixture(tmp)
            rekit = FakeRekit(risky=True)
            worker = LocalRekitWorker(rekit)
            scope = authorized_dynamic_scope(target)
            common = {
                "target_sha256": TargetGrant.from_path(target).content_sha256,
                "scope_digest": scope.envelope.content_digest,
                "scope_revision": scope.to_dict(),
                "requested_actions": (
                    ActionAuthority.READ_LOCAL_TARGET.value,
                    ActionAuthority.EXECUTE_UNTRUSTED.value,
                ),
            }
            request = InvocationRequest(
                run_id="run-1", work_item_id="work-1", tool_id="exec-observe",
                target_path=str(target), **common,
            )
            with self.assertRaises(PermissionError):
                worker.invoke(request)
            allowed = InvocationRequest(
                run_id="run-1", work_item_id="work-1", tool_id="exec-observe",
                target_path=str(target), approval_id="question-answered-allow",
                lease_id="lease-local", **common,
            )
            worker.setup_lease(WorkerLeaseRequest(
                lease_id="lease-local", run_id="run-1", work_item_id="work-1",
                worker_id="local", route_sha256="a" * 64,
            ))
            result = worker.invoke(allowed)
            self.assertEqual("done", result.status)
            self.assertTrue(rekit.calls[0][2])

    def _request(self, url, payload=None, *, expected=200):
        data = None if payload is None else __import__("json").dumps(payload).encode()
        request = Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=5) as response:
                self.assertEqual(expected, response.status)
                return __import__("json").loads(response.read())
        except HTTPError as exc:
            self.fail(f"HTTP {exc.code}: {exc.read().decode()}")

    def _wait_status(self, base, run_id, wanted):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = self._request(base + f"/api/runs/{run_id}")
            if snapshot["run"]["status"] == wanted:
                return snapshot
            time.sleep(0.02)
        self.fail(f"run {run_id} did not reach {wanted}")


if __name__ == "__main__":
    unittest.main()
