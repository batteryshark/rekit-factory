from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from muster import resolve_run_dir
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rekit_factory.api import FactoryServer
from rekit_factory.control import InvestigationController, RunRequest
from rekit_factory.models import ModelProfile, WorkerReport
from rekit_factory.policy_runtime import (
    builtin_policy_catalog,
    policy_from_record,
    policy_record,
    strategy_from_record,
    strategy_metadata_catalog,
    strategy_record,
    validate_policy_authority,
    validate_strategy_authority,
)
from rekit_factory.strategies import DEFAULT_STRATEGIES, RunCeilings
from rekit_factory.rekit_client import ToolManifest
from rekit_factory.scope import ActionAuthority, author_scope


def manifest(tool_id: str, *, gated: bool = False):
    return SimpleNamespace(id=tool_id, requires_permission=gated)


class Backend:
    def __init__(self):
        self.profile = ModelProfile(
            name="fixture", provider="test", model="fixture",
            base_url="https://invalid.test", api_key="secret",
        )
        self.calls = 0

    async def analyze(self, *, role, **kwargs):
        self.calls += 1
        return WorkerReport(
            summary=f"{role} complete", observations=[], next_actions=[],
            status_update="complete",
        ), {"inputTokens": 1, "outputTokens": 1}


class EmptyRekit:
    def list_tools(self):
        return []


class GatedRekit:
    def __init__(self):
        self.tool = ToolManifest(
            id="exec", name="exec", description="fixture", safety_tier=3,
            executes_input="full", network="none",
            actions=(ActionAuthority.READ_LOCAL_TARGET, ActionAuthority.EXECUTE_UNTRUSTED),
        )

    def list_tools(self):
        return [self.tool]

    def manifest(self, tool_id):
        if tool_id != self.tool.id:
            raise KeyError(tool_id)
        return self.tool


def target(tmp_path: Path) -> Path:
    result = tmp_path / "target"
    result.mkdir()
    (result / "fixture.txt").write_text("fixture", encoding="utf-8")
    return result


def test_builtin_catalog_is_order_independent_and_separates_gated_authority():
    first = builtin_policy_catalog((manifest("exec", gated=True), manifest("inspect")))
    second = builtin_policy_catalog((manifest("inspect"), manifest("exec", gated=True)))
    assert first.public_dicts() == second.public_dicts()
    supervised, automatic = first.policies
    assert supervised.allowed_tool_ids == ("exec", "inspect")
    assert supervised.approval_required_tool_ids == ("exec",)
    assert automatic.allowed_tool_ids == ("inspect",)
    assert first.resolve(None) == supervised
    with pytest.raises(ValueError, match="unknown or stale"):
        first.resolve("safety-policy-v1-" + "0" * 64)


def test_policy_record_binds_the_exact_document_and_runtime_authority():
    catalog = builtin_policy_catalog((manifest("exec", gated=True), manifest("inspect")))
    supervised, automatic = catalog.policies
    assert policy_from_record(policy_record(supervised)) == supervised
    changed = policy_record(supervised)
    changed["document"] = {**changed["document"], "revision": 2}
    with pytest.raises(ValueError, match="does not match"):
        policy_from_record(changed)

    with pytest.raises(PermissionError, match="does not allow"):
        validate_policy_authority(
            automatic, requested_tool_ids=("exec",),
            manifests={"exec": manifest("exec", gated=True)},
            ceilings=RunCeilings(), scope=None,
        )
    with pytest.raises(PermissionError, match="concurrency"):
        validate_policy_authority(
            supervised, requested_tool_ids=(), manifests={},
            ceilings=replace(supervised.ceilings, concurrency=5, max_workers=8),
            scope=None,
        )


def test_strategy_metadata_is_strict_and_binds_graph_profiles_policies_and_defaults():
    policies = builtin_policy_catalog((manifest("inspect"),))
    metadata = strategy_metadata_catalog(
        DEFAULT_STRATEGIES.values(), profile_names=("remote", "local"),
        policy_ids=(policy.policy_id for policy in policies.policies),
    )
    recon_analysis, sequential = metadata
    assert recon_analysis.compatible_profile_names == ("local", "remote")
    assert recon_analysis.policy_constraints.compatible_policy_ids == tuple(sorted(
        policy.policy_id for policy in policies.policies
    ))
    assert sequential.roles[1].depends_on_roles == ("recon",)
    assert sequential.default_ceilings == DEFAULT_STRATEGIES["recon-then-analysis"].ceilings
    assert strategy_from_record(strategy_record(sequential)) == sequential

    changed = strategy_record(sequential)
    changed["document"] = {**changed["document"], "description": "changed"}
    with pytest.raises(ValueError, match="does not match"):
        strategy_from_record(changed)

    with pytest.raises(PermissionError, match="model profile"):
        validate_strategy_authority(
            sequential, profile_name="missing", policy=policies.policies[0], scope=None,
        )


def test_controller_persists_exact_policy_and_rechecks_it_after_restart(tmp_path):
    storage = tmp_path / "runs"
    controller = InvestigationController(
        storage_root=storage, rekit=EmptyRekit(), workers=Backend(),
    )
    run_dir = controller.create(RunRequest(target(tmp_path), "inspect"))
    before = controller.snapshot(run_dir)
    record = before["meta"]["safetyPolicy"]
    assert record["policyId"] == controller.safety_policies.default_policy_id
    assert policy_from_record(record).name == "supervised"

    restarted = InvestigationController(
        storage_root=storage, rekit=EmptyRekit(), workers=Backend(),
    )
    restarted.validate_run_concurrency(run_dir)
    assert restarted.snapshot(run_dir)["meta"]["safetyPolicy"] == record


def test_controller_applies_named_strategy_defaults_and_persists_exact_metadata(tmp_path):
    controller = InvestigationController(
        storage_root=tmp_path / "runs", rekit=EmptyRekit(), workers=Backend(),
    )
    run_dir = controller.create(RunRequest(
        target(tmp_path), "sequential inspection", strategy="recon-then-analysis",
    ))
    snapshot = controller.snapshot(run_dir)
    plan = snapshot["meta"]["strategyPlan"]
    metadata = strategy_from_record(snapshot["meta"]["strategyMetadata"])
    assert plan["ceilings"] == {
        "concurrency": 2, "retries_per_worker": 2,
        "cost_units": 100, "max_workers": 8,
    }
    assert metadata.name == plan["strategy"] == "recon-then-analysis"
    assert metadata.roles[1].depends_on_roles == ("recon",)
    assert metadata.compatible_profile_names == ("fixture",)
    assert set(metadata.policy_constraints.compatible_policy_ids) == {
        policy.policy_id for policy in controller.safety_policies.policies
    }


def test_resume_rejects_incompatible_persisted_strategy_before_mutation(tmp_path):
    controller = InvestigationController(
        storage_root=tmp_path / "runs", rekit=EmptyRekit(), workers=Backend(),
    )
    run_dir = controller.create(RunRequest(
        target(tmp_path), "bound strategy", strategy="recon-analysis",
    ))
    before = controller.snapshot(run_dir)
    paths = resolve_run_dir(run_dir)
    meta = json.loads(paths.run_json.read_text(encoding="utf-8"))
    metadata = strategy_from_record(meta["strategyMetadata"])
    meta["strategyMetadata"] = strategy_record(replace(
        metadata, compatible_profile_names=("other",),
    ))
    paths.run_json.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(PermissionError, match="model profile"):
        controller.validate_run_concurrency(run_dir)
    after = controller.snapshot(run_dir)
    assert after["run"]["status"] == before["run"]["status"]
    assert after["events"] == before["events"]


def test_controller_rejects_gated_tool_under_automatic_only_before_creation(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr("rekit_factory.control.utcnow", lambda: "2026-07-13T12:30:00Z")
    storage = tmp_path / "runs"
    rekit = GatedRekit()
    controller = InvestigationController(
        storage_root=storage, rekit=rekit, workers=Backend(),
    )
    fixture = target(tmp_path)
    scope = author_scope(
        fixture, scope_id="policy-test", revision=1,
        actions=(ActionAuthority.READ_LOCAL_TARGET, ActionAuthority.EXECUTE_UNTRUSTED),
        approved_by="test", rationale="exact fixture authority",
        approved_at="2026-07-13T12:00:00Z",
        valid_until="2026-07-14T12:00:00Z",
        expires_at="2026-07-14T12:00:00Z",
    )
    automatic = next(
        policy for policy in controller.safety_policies.policies
        if policy.name == "automatic-only"
    )
    with pytest.raises(PermissionError, match="does not allow requested tools"):
        controller.create(RunRequest(
            fixture, "reject gated", model_tools=("exec",), scope=scope,
            safety_policy_id=automatic.policy_id,
        ))
    assert not storage.exists()


def test_stale_policy_fails_before_creation_and_legacy_projection_is_deny_all(tmp_path):
    storage = tmp_path / "runs"
    controller = InvestigationController(
        storage_root=storage, rekit=EmptyRekit(), workers=Backend(),
    )
    fixture = target(tmp_path)
    with pytest.raises(ValueError, match="unknown or stale"):
        controller.create(RunRequest(
            fixture, "reject stale", safety_policy_id="safety-policy-v1-" + "f" * 64,
        ))
    assert not storage.exists()

    run_dir = controller.create(RunRequest(fixture, "legacy readable"))
    paths = resolve_run_dir(run_dir)
    meta = json.loads(paths.run_json.read_text(encoding="utf-8"))
    meta.pop("safetyPolicy")
    paths.run_json.write_text(json.dumps(meta), encoding="utf-8")
    legacy = policy_from_record(controller.snapshot(run_dir)["meta"]["safetyPolicy"])
    assert legacy.compatibility == "legacy-deny-all-v1"
    assert legacy.allowed_tool_ids == ()


def test_api_projects_policy_catalog_and_rejects_stale_identity(tmp_path):
    storage = tmp_path / "runs"
    controller = InvestigationController(
        storage_root=storage, rekit=EmptyRekit(), workers=Backend(),
    )
    fixture = target(tmp_path)
    server = FactoryServer(("127.0.0.1", 0), controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/api/config", timeout=5) as response:
            config = json.loads(response.read())
        assert config["defaultSafetyPolicyId"] == controller.safety_policies.default_policy_id
        assert config["safetyPolicies"] == controller.public_safety_policies()
        assert config["strategies"] == controller.public_strategy_metadata()
        assert config["navigationRoute"] == {
            "schemaVersion": 1, "queryMarker": "mc-v1", "maxLength": 512,
            "routes": [
                {"entityType": "campaign", "surface": "campaigns", "requiresRun": False},
                {"entityType": "finding", "surface": "outcomes", "requiresRun": True},
                {"entityType": "operator-decision", "surface": "decisions", "requiresRun": True},
                {"entityType": "proof-bundle", "surface": "dossiers", "requiresRun": True},
            ],
        }
        sequential = next(
            item for item in config["strategies"]
            if item["name"] == "recon-then-analysis"
        )
        assert sequential["roles"][1]["depends_on_roles"] == ["recon"]
        assert sequential["compatible_profile_names"] == ["fixture"]
        assert set(sequential["policy_constraints"]["compatible_policy_ids"]) == {
            policy.policy_id for policy in controller.safety_policies.policies
        }

        body = json.dumps({
            "target": str(fixture), "goal": "crafted stale request",
            "safetyPolicyId": "safety-policy-v1-" + "e" * 64,
        }).encode("utf-8")
        request = Request(
            base + "/api/runs", data=body, headers={"Content-Type": "application/json"},
        )
        with pytest.raises(HTTPError) as denied:
            urlopen(request, timeout=5)
        assert denied.value.code == 400
        assert "unknown or stale safety policy identity" in denied.value.read().decode("utf-8")
        assert not storage.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
