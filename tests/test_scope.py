from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from rekit_factory.control import InvestigationController, RunRequest
from rekit_factory.models import (
    DeferredModelToolCall, ModelProfile, WorkerReport, WorkerTurn,
)
from rekit_factory.rekit_client import ToolManifest, ToolResult
from rekit_factory.scope import (
    ActionAuthority,
    AuthorizedScope,
    DataHandling,
    NetworkMode,
    ScopeApproval,
    ScopeAuthorizationError,
    ScopeEnvelope,
    ScopeRequest,
    TargetGrant,
    decide_scope,
    normalize_endpoint,
)


NOW = "2026-07-13T12:00:00Z"


def authorize(envelope: ScopeEnvelope, *, expires_at="2026-07-14T00:00:00Z") -> AuthorizedScope:
    return AuthorizedScope(envelope, ScopeApproval(
        scope_id=envelope.scope_id,
        revision=envelope.revision,
        content_digest=envelope.content_digest,
        approved_by="operator-opaque-1",
        approved_at="2026-07-12T11:00:00Z",
        expires_at=expires_at,
        rationale="Authorized exact fixture and bounded actions",
    ))


def envelope(target: TargetGrant, *, actions=(ActionAuthority.READ_LOCAL_TARGET,),
             endpoints=(), network_mode=NetworkMode.NONE, prohibited=None) -> ScopeEnvelope:
    if prohibited is None:
        prohibited = tuple(action for action in ScopeEnvelope.__dataclass_fields__[  # type: ignore[index]
            "prohibited_actions"
        ].default if action not in actions)
    return ScopeEnvelope(
        scope_id="scope-test",
        revision=1,
        valid_from="2026-07-12T10:00:00Z",
        valid_until="2026-07-15T00:00:00Z",
        targets=(target,),
        actions=actions,
        endpoints=endpoints,
        network_mode=network_mode,
        prohibited_actions=prohibited,
    )


class ScopePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.target_path = Path(self.tmp.name) / "fixture.txt"
        self.target_path.write_text("offline fixture", encoding="utf-8")
        self.target = TargetGrant.from_path(self.target_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_offline_fixture_allowed_under_network_none(self):
        scope = authorize(envelope(self.target))
        decision = decide_scope(scope, ScopeRequest(
            action=ActionAuthority.READ_LOCAL_TARGET, target=self.target,
        ), now=NOW)
        self.assertTrue(decision.allowed)
        network = decide_scope(scope, ScopeRequest(
            action=ActionAuthority.NETWORK_ACCESS,
            target=self.target,
            endpoint="https://lab.example.test:443/api",
        ), now=NOW)
        self.assertFalse(network.allowed)
        self.assertIn(network.reason_code, {"scope.action_not_authorized", "scope.network_disabled"})

    def test_prompt_injected_unlisted_host_is_denied_exactly(self):
        allowed = normalize_endpoint("https://lab.example.test/api")
        scope = authorize(envelope(
            self.target,
            actions=(ActionAuthority.READ_LOCAL_TARGET, ActionAuthority.NETWORK_ACCESS),
            endpoints=(allowed,),
            network_mode=NetworkMode.EXACT_ENDPOINTS,
        ))
        good = decide_scope(scope, ScopeRequest(
            action=ActionAuthority.NETWORK_ACCESS, target=self.target, endpoint=allowed,
        ), now=NOW)
        injected = decide_scope(scope, ScopeRequest(
            action=ActionAuthority.NETWORK_ACCESS,
            target=self.target,
            endpoint="https://attacker.invalid/collect",
        ), now=NOW)
        self.assertTrue(good.allowed)
        self.assertEqual("scope.endpoint_not_authorized", injected.reason_code)
        self.assertNotIn("attacker.invalid", repr(injected.browser_dict()))

    def test_expired_modified_and_mismatched_approvals_fail_closed(self):
        base = envelope(self.target)
        expired = authorize(base, expires_at="2026-07-13T11:30:00Z")
        self.assertEqual("scope.expired", decide_scope(expired, ScopeRequest(
            action=ActionAuthority.READ_LOCAL_TARGET, target=self.target,
        ), now=NOW).reason_code)

        changed = replace(base, revision=2)
        mismatched = AuthorizedScope(changed, authorize(base).approval)
        with self.assertRaisesRegex(ScopeAuthorizationError, "scope.approval_mismatch"):
            mismatched.validate(now=NOW)

        modified = AuthorizedScope(
            replace(base, data_handling=DataHandling.APPROVED_EXPORT), authorize(base).approval
        )
        with self.assertRaisesRegex(ScopeAuthorizationError, "scope.content_mismatch"):
            modified.validate(now=NOW)

    def test_sensitive_targets_and_endpoints_are_browser_redacted(self):
        raw_path = str(self.target_path)
        raw_endpoint = normalize_endpoint("https://private-lab.internal:8443/range")
        scope = authorize(envelope(
            self.target,
            actions=(ActionAuthority.READ_LOCAL_TARGET, ActionAuthority.NETWORK_ACCESS),
            endpoints=(raw_endpoint,), network_mode=NetworkMode.EXACT_ENDPOINTS,
        ))
        projection = scope.envelope.public_dict()
        denial = decide_scope(scope, ScopeRequest(
            action=ActionAuthority.NETWORK_ACCESS, target=self.target,
            endpoint="https://unlisted.private.invalid/",
        ), now=NOW).browser_dict()
        rendered = repr((projection, denial))
        self.assertNotIn(raw_path, rendered)
        self.assertNotIn("private-lab.internal", rendered)
        self.assertNotIn("unlisted.private.invalid", rendered)
        self.assertIn("target-path:", rendered)

    def test_live_challenge_actions_need_independent_authorities(self):
        scope = authorize(envelope(
            self.target,
            actions=(ActionAuthority.READ_LOCAL_TARGET, ActionAuthority.REGISTER_ACCOUNT),
            prohibited=(),
        ))
        register = decide_scope(scope, ScopeRequest(
            action=ActionAuthority.REGISTER_ACCOUNT, target=self.target,
        ), now=NOW)
        submit = decide_scope(scope, ScopeRequest(
            action=ActionAuthority.SUBMIT_CHALLENGE, target=self.target,
        ), now=NOW)
        self.assertTrue(register.allowed)
        self.assertEqual("scope.action_not_authorized", submit.reason_code)

    def test_network_authority_does_not_imply_execution_authority(self):
        endpoint = normalize_endpoint("https://lab.example.test/api")
        scope = authorize(envelope(
            self.target,
            actions=(ActionAuthority.READ_LOCAL_TARGET, ActionAuthority.NETWORK_ACCESS),
            endpoints=(endpoint,), network_mode=NetworkMode.EXACT_ENDPOINTS,
        ))
        execution = decide_scope(scope, ScopeRequest(
            action=ActionAuthority.EXECUTE_UNTRUSTED, target=self.target,
        ), now=NOW)
        self.assertEqual("scope.action_not_authorized", execution.reason_code)

    def test_round_trip_preserves_content_bound_scope(self):
        scope = authorize(envelope(self.target))
        loaded = AuthorizedScope.from_dict(scope.to_dict())
        self.assertEqual(scope, loaded)
        loaded.validate(now=NOW)


class FakeBackend:
    profile = ModelProfile(
        name="fake", provider="test", model="test",
        base_url="https://model.invalid", api_key="not-persisted",
    )

    async def analyze(self, **kwargs):
        return WorkerReport(
            summary="done", observations=[], next_actions=[], status_update="done",
        ), {}


class FakeRekit:
    def __init__(self, manifest: ToolManifest):
        self._manifest = manifest
        self.calls = 0

    def manifest(self, tool_id):
        return self._manifest

    def list_tools(self):
        return [self._manifest]

    def run(self, tool_id, target, *, allow_dynamic=False):
        self.calls += 1
        return ToolResult(0, "ok", "", "fixture")


class InjectedIntentBackend(FakeBackend):
    def __init__(self):
        self.turns = 0
        self.denied = None

    async def analyze(self, *, tool_results=(), **kwargs):
        self.turns += 1
        if self.turns == 1:
            return WorkerTurn(
                report=None, usage={}, messages_json="[]",
                deferred_calls=(DeferredModelToolCall(
                    call_id="call-injected", tool_id="probe", tool_name="rekit__probe",
                    endpoint="https://injected.invalid/collect",
                    uses_credentials=True,
                    requested_action=ActionAuthority.NETWORK_ACCESS.value,
                ),),
            )
        self.denied = tool_results[0].denied
        return WorkerTurn(
            report=WorkerReport(
                summary="denial respected", observations=[], next_actions=[],
                status_update="done",
            ), usage={}, messages_json="[]",
        )


class ScopeControllerTests(unittest.TestCase):
    def test_scope_less_local_read_only_run_gets_narrow_migration_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            rekit = FakeRekit(ToolManifest(
                "scan", "Scan", "read only", 0, "no", "none",
            ))
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs", rekit=rekit, workers=FakeBackend(),
            )
            run_dir = controller.create(RunRequest(target, "inspect", tools=("scan",)))
            snapshot = controller.snapshot(run_dir)
            self.assertEqual("none", snapshot["meta"]["scope"]["networkMode"])
            self.assertNotIn(str(target), repr(snapshot["meta"]["scope"]))
            self.assertTrue((run_dir / "scope.json").is_file())

    def test_tool_safety_approval_does_not_substitute_for_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            rekit = FakeRekit(ToolManifest(
                "probe", "Probe", "network", 2, "no", "target-controlled",
            ))
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs", rekit=rekit, workers=FakeBackend(),
            )
            with self.assertRaisesRegex(PermissionError, "explicit engagement scope"):
                controller.create(RunRequest(target, "probe", tools=("probe",)))
            self.assertEqual(0, rekit.calls)

    def test_dispatch_denial_is_durable_redacted_and_precedes_tool_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            rekit = FakeRekit(ToolManifest(
                "probe", "Probe", "network", 2, "no", "target-controlled",
            ))
            endpoint = normalize_endpoint("https://approved.private.test/api")
            scoped = authorize(envelope(
                TargetGrant.from_path(target),
                actions=(ActionAuthority.READ_LOCAL_TARGET, ActionAuthority.NETWORK_ACCESS),
                endpoints=(endpoint,), network_mode=NetworkMode.EXACT_ENDPOINTS,
            ))
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs", rekit=rekit, workers=FakeBackend(),
            )
            # The tool is available under a network authority, but the durable work
            # request carries no exact endpoint. Dispatch therefore fails closed.
            run_dir = controller.create(RunRequest(
                target, "probe approved endpoint only", tools=("probe",), scope=scoped,
            ))
            result = __import__("asyncio").run(controller.drive(run_dir))
            self.assertEqual(0, rekit.calls)
            self.assertEqual([], result["pendingQuestions"])
            denied = [event for event in result["events"]
                      if event["kind"] == "security.scope_denied"]
            self.assertEqual(1, len(denied))
            self.assertEqual("scope.endpoint_required", denied[0]["payload"]["reason_code"])
            self.assertNotIn("approved.private.test", repr(denied[0]["payload"]))

    def test_deferred_exact_intent_survives_queue_and_cannot_self_authorize(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fixture.txt"
            target.write_text("fixture", encoding="utf-8")
            rekit = FakeRekit(ToolManifest(
                "probe", "Probe", "network", 2, "no", "target-controlled",
            ))
            backend = InjectedIntentBackend()
            approved_endpoint = normalize_endpoint("https://approved.private.test/api")
            scoped = authorize(envelope(
                TargetGrant.from_path(target),
                actions=(ActionAuthority.READ_LOCAL_TARGET, ActionAuthority.NETWORK_ACCESS),
                endpoints=(approved_endpoint,), network_mode=NetworkMode.EXACT_ENDPOINTS,
            ))
            controller = InvestigationController(
                storage_root=Path(tmp) / "runs", rekit=rekit, workers=backend,
            )
            result = controller.run(RunRequest(
                target, "do not follow injected endpoint", model_tools=("probe",),
                worker_roles=("analyst",), scope=scoped,
            ))
            self.assertEqual(0, rekit.calls)
            self.assertTrue(backend.denied)
            rendered = repr(result)
            self.assertNotIn("injected.invalid", rendered)
            model_tool = next(item for item in result["workItems"]
                              if item["operation"] == "model-rekit-tool")
            self.assertIn("endpointRef", model_tool["payload"])
            self.assertEqual("scope.credentials_not_authorized", model_tool["result"]["reasonCode"])


if __name__ == "__main__":
    unittest.main()
