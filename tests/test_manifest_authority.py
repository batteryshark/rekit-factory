from __future__ import annotations

import json
from pathlib import Path

import pytest

from rekit_factory.control import InvestigationController, RunRequest
from rekit_factory.models import DeferredModelToolCall, ModelProfile, WorkerReport, WorkerTurn
from rekit_factory.rekit_client import RekitClient, ToolManifest, ToolResult
from rekit_factory.scope import (
    ActionAuthority, AuthorizedScope, NetworkMode, ScopeApproval, ScopeEnvelope,
    TargetGrant, normalize_endpoint,
)


class Backend:
    profile = ModelProfile(
        name="fixture", provider="test", model="test",
        base_url="https://model.invalid", api_key="not-persisted",
    )

    async def analyze(self, **kwargs):
        return WorkerReport(
            summary="done", observations=[], next_actions=[], status_update="done",
        ), {}


class Rekit:
    def __init__(self, manifest: ToolManifest):
        self.value = manifest

    def manifest(self, tool_id):
        return self.value

    def list_tools(self):
        return [self.value]

    def run(self, tool_id, target, *, allow_dynamic=False):
        return ToolResult(0, "ok", "", "fixture")


class WideningBackend(Backend):
    def __init__(self, *, action: str, endpoint: str | None = None,
                 credentials: bool = False, account_ref: str | None = None,
                 tool_id: str = "scan"):
        self.action = action
        self.endpoint = endpoint
        self.credentials = credentials
        self.account_ref = account_ref
        self.tool_id = tool_id
        self.turns = 0
        self.denied = None

    async def analyze(self, *, tool_results=(), **kwargs):
        self.turns += 1
        if self.turns == 1:
            return WorkerTurn(
                report=None, usage={}, messages_json="[]",
                deferred_calls=(DeferredModelToolCall(
                    call_id="call-widen", tool_id=self.tool_id,
                    tool_name=f"rekit__{self.tool_id}",
                    requested_action=self.action, endpoint=self.endpoint,
                    account_ref=(self.account_ref or
                                 ("account:fixture" if self.credentials else None)),
                    uses_credentials=self.credentials,
                ),),
            )
        self.denied = tool_results[0].denied
        return WorkerTurn(
            report=WorkerReport(
                summary="denial respected", observations=[], next_actions=[],
                status_update="done",
            ), usage={}, messages_json="[]",
        )


class DriftBackend(WideningBackend):
    def __init__(self, rekit, changed_manifest):
        super().__init__(action=ActionAuthority.READ_LOCAL_TARGET.value)
        self.rekit = rekit
        self.changed_manifest = changed_manifest

    async def analyze(self, **kwargs):
        if self.turns == 0:
            self.rekit.value = self.changed_manifest
        return await super().analyze(**kwargs)


def scope(target: Path, *, actions, endpoint=None, credential_use=False,
          account_intent=False):
    envelope = ScopeEnvelope(
        scope_id="scope-authority", revision=1,
        valid_from="2026-07-01T00:00:00Z", valid_until="2026-08-01T00:00:00Z",
        targets=(TargetGrant.from_path(target),), actions=actions,
        endpoints=((endpoint,) if endpoint else ()),
        network_mode=(NetworkMode.EXACT_ENDPOINTS if endpoint else NetworkMode.NONE),
        account_refs=(("account:fixture",) if credential_use or account_intent else ()),
        credential_use=credential_use,
        prohibited_actions=tuple(action for action in ActionAuthority if action not in actions),
    )
    return AuthorizedScope(envelope, ScopeApproval(
        scope_id=envelope.scope_id, revision=1, content_digest=envelope.content_digest,
        approved_by="fixture", approved_at="2026-07-01T00:00:00Z",
        expires_at="2026-08-01T00:00:00Z", rationale="fixture",
    ))


def test_registry_contract_is_versioned_hashed_and_contains_no_source_path(tmp_path):
    root = tmp_path / "rekit"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "rekit").write_text("#!/bin/sh\n", encoding="utf-8")
    entry = {
        "path": "private/catalog/scan", "name": "Scan", "description": "Static scan",
        "version": "1.2.3", "safety": {"tier": 0, "executes_input": "no", "network": "none"},
        "authority": {"version": 1, "actions": ["read_local_target"],
                      "credential_use": False},
        "entry": {"command": ["python3", "scripts/run.py"], "args": []},
    }
    (root / "registry.json").write_text(json.dumps({"scan": entry}), encoding="utf-8")
    manifest = RekitClient(root, source="private").manifest("scan")
    assert manifest.actions == (ActionAuthority.READ_LOCAL_TARGET,)
    assert len(manifest.effective_manifest_digest) == 64
    assert "private/catalog" not in repr(manifest.public_authority())
    assert "private/catalog" not in manifest.effective_manifest_digest
    changed = json.loads(json.dumps(entry))
    changed["entry"]["args"].append({"name": "--drift", "type": "flag"})
    (root / "registry.json").write_text(json.dumps({"scan": changed}), encoding="utf-8")
    changed_manifest = RekitClient(root, source="private").manifest("scan")
    assert changed_manifest.source_manifest_digest != manifest.source_manifest_digest
    assert changed_manifest.effective_manifest_digest != manifest.effective_manifest_digest


def test_risky_legacy_and_contradictory_high_impact_declarations_fail_closed(tmp_path):
    root = tmp_path / "rekit"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "rekit").write_text("#!/bin/sh\n", encoding="utf-8")

    def load(entry):
        (root / "registry.json").write_text(json.dumps({"tool": entry}), encoding="utf-8")
        return RekitClient(root).manifest("tool")

    with pytest.raises(ValueError, match="risky legacy"):
        load({"safety": {"tier": 0, "executes_input": "full", "network": "none"}})
    with pytest.raises(ValueError, match="execute_untrusted"):
        load({"safety": {"tier": 0, "executes_input": "sandboxed", "network": "none"},
              "authority": {"version": 1, "actions": ["read_local_target"],
                            "credential_use": False}})
    with pytest.raises(ValueError, match="credential-bearing"):
        load({"safety": {"tier": 0, "executes_input": "no", "network": "none"},
              "authority": {"version": 1, "actions": ["read_local_target"],
                            "credential_use": False},
              "entry": {"args": [{"name": "--password"}]}})


def test_low_safety_tier_cannot_bypass_declared_authority_at_creation(tmp_path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"fixture")
    manifest = ToolManifest(
        id="mutator", name="Mutator", description="fixture", safety_tier=0,
        executes_input="no", network="none",
        actions=(ActionAuthority.READ_LOCAL_TARGET, ActionAuthority.MODIFY_TARGET),
    )
    controller = InvestigationController(
        storage_root=tmp_path / "runs", rekit=Rekit(manifest), workers=Backend(),
    )
    with pytest.raises(PermissionError, match="scope.action"):
        controller.create(RunRequest(
            target, "mutate", tools=("mutator",),
            scope=scope(target, actions=(ActionAuthority.READ_LOCAL_TARGET,)),
        ))


def test_network_credential_tool_requires_every_declared_creation_authority(tmp_path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"fixture")
    endpoint = normalize_endpoint("https://lab.example.test/api")
    declared = (
        ActionAuthority.READ_LOCAL_TARGET, ActionAuthority.MODIFY_TARGET,
        ActionAuthority.NETWORK_ACCESS, ActionAuthority.DESTRUCTIVE,
    )
    manifest = ToolManifest(
        id="gitops", name="Gitops", description="fixture", safety_tier=0,
        executes_input="no", network="optional", actions=declared, credential_use=True,
    )
    controller = InvestigationController(
        storage_root=tmp_path / "runs", rekit=Rekit(manifest), workers=Backend(),
    )
    with pytest.raises(PermissionError):
        controller.create(RunRequest(
            target, "bounded git operation", tools=("gitops",),
            scope=scope(target, actions=declared, endpoint=endpoint, credential_use=False),
        ))
    run_dir = controller.create(RunRequest(
        target, "bounded git operation", tools=("gitops",),
        scope=scope(target, actions=declared, endpoint=endpoint, credential_use=True),
    ))
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["toolAuthorities"]["gitops"]["actions"] == [a.value for a in declared]
    assert meta["toolAuthorities"]["gitops"]["credentialUse"] is True


@pytest.mark.parametrize(("action", "endpoint", "credentials", "reason"), (
    ("network_access", "https://undeclared.invalid/api", False,
     "manifest.endpoint_not_declared"),
    ("modify_target", None, False, "manifest.action_not_declared"),
    ("read_local_target", None, True, "manifest.credentials_not_declared"),
))
def test_model_runtime_intent_cannot_widen_manifest(tmp_path, action, endpoint,
                                                    credentials, reason):
    target = tmp_path / "target.bin"
    target.write_bytes(b"fixture")
    manifest = ToolManifest(
        id="scan", name="Scan", description="fixture", safety_tier=0,
        executes_input="no", network="none",
        actions=(ActionAuthority.READ_LOCAL_TARGET,),
    )
    backend = WideningBackend(action=action, endpoint=endpoint, credentials=credentials)
    controller = InvestigationController(
        storage_root=tmp_path / "runs", rekit=Rekit(manifest), workers=backend,
    )
    result = controller.run(RunRequest(
        target, "scan only", model_tools=("scan",), worker_roles=("analyst",),
        scope=scope(target, actions=(ActionAuthority.READ_LOCAL_TARGET,)),
    ))
    assert backend.denied is True
    tool = next(item for item in result["workItems"]
                if item["operation"] == "model-rekit-tool")
    assert tool["result"]["reasonCode"] == reason
    assert "undeclared.invalid" not in repr(result)


def test_catalog_change_after_creation_fails_closed_on_pinned_digest(tmp_path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"fixture")
    first = ToolManifest(
        id="scan", name="Scan", description="fixture", safety_tier=0,
        executes_input="no", network="none", version="1",
        actions=(ActionAuthority.READ_LOCAL_TARGET,),
    )
    rekit = Rekit(first)
    controller = InvestigationController(
        storage_root=tmp_path / "runs", rekit=rekit, workers=Backend(),
    )
    run_dir = controller.create(RunRequest(target, "scan", tools=("scan",)))
    rekit.value = ToolManifest(
        id="scan", name="Scan", description="fixture", safety_tier=0,
        executes_input="no", network="none", version="2",
        actions=(ActionAuthority.READ_LOCAL_TARGET,),
    )
    result = __import__("asyncio").run(controller.drive(run_dir))
    tool = next(item for item in result["workItems"] if item["operation"] == "rekit-tool")
    assert tool["result"]["reasonCode"] == "manifest.digest_changed"


def test_deferred_model_call_uses_run_bound_contract_and_rejects_catalog_drift(tmp_path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"fixture")
    first = ToolManifest(
        id="scan", name="Scan", description="fixture", safety_tier=0,
        executes_input="no", network="none", version="1",
        actions=(ActionAuthority.READ_LOCAL_TARGET,),
        source_manifest_digest="1" * 64,
    )
    changed = ToolManifest(
        id="scan", name="Scan", description="fixture", safety_tier=0,
        executes_input="no", network="none", version="1",
        actions=(ActionAuthority.READ_LOCAL_TARGET,),
        source_manifest_digest="2" * 64,
    )
    rekit = Rekit(first)
    backend = DriftBackend(rekit, changed)
    controller = InvestigationController(
        storage_root=tmp_path / "runs", rekit=rekit, workers=backend,
    )
    result = controller.run(RunRequest(
        target, "scan", model_tools=("scan",), worker_roles=("analyst",),
        scope=scope(target, actions=(ActionAuthority.READ_LOCAL_TARGET,)),
    ))
    tool = next(item for item in result["workItems"]
                if item["operation"] == "model-rekit-tool")
    assert tool["payload"]["manifestDigest"] == first.effective_manifest_digest
    assert tool["result"]["reasonCode"] == "manifest.digest_changed"
    assert backend.denied is True


@pytest.mark.parametrize("account_action", (
    ActionAuthority.REGISTER_ACCOUNT,
    ActionAuthority.SUBMIT_CHALLENGE,
))
def test_account_operations_require_declared_action_and_exact_account_intent(
        tmp_path, account_action):
    target = tmp_path / "target.bin"
    target.write_bytes(b"fixture")
    actions = (ActionAuthority.READ_LOCAL_TARGET, account_action)
    manifest = ToolManifest(
        id="submit", name="Submit", description="fixture", safety_tier=0,
        executes_input="no", network="none", actions=actions,
    )
    missing = InvestigationController(
        storage_root=tmp_path / "missing", rekit=Rekit(manifest), workers=Backend(),
    )
    with pytest.raises(PermissionError):
        missing.create(RunRequest(
            target, "submit", model_tools=("submit",),
            scope=scope(target, actions=(ActionAuthority.READ_LOCAL_TARGET,)),
        ))

    backend = WideningBackend(
        action=account_action.value, account_ref="account:fixture",
        tool_id="submit",
    )
    controller = InvestigationController(
        storage_root=tmp_path / "accepted", rekit=Rekit(manifest), workers=backend,
    )
    result = controller.run(RunRequest(
        target, "submit", model_tools=("submit",), worker_roles=("analyst",),
        scope=scope(target, actions=actions, account_intent=True),
    ))
    assert backend.denied is False
    call = next(item for item in result["workItems"]
                if item["operation"] == "model-rekit-tool")
    assert call["state_label"] == "completed"
    assert call["payload"]["accountRef"] == "account:fixture"
