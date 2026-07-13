from dataclasses import FrozenInstanceError, replace

import pytest

from rekit_factory.policy_contracts import (
    NamedSafetyPolicy,
    ScopePolicyBinding,
    StrategyMetadata,
    StrategyPolicyConstraints,
    StrategyRoleMetadata,
)
from rekit_factory.strategies import RunCeilings


def policy(**changes):
    value = NamedSafetyPolicy(
        name="supervised", revision=3,
        allowed_tool_ids=("inspect", "network-probe"),
        approval_mode="operator-gated",
        approval_required_tool_ids=("network-probe",),
        ceilings=RunCeilings(concurrency=2, retries_per_worker=1, cost_units=40, max_workers=4),
        scope_binding=ScopePolicyBinding(
            "authorized-scope", "scope-1", 2, "a" * 64,
        ),
    )
    return replace(value, **changes)


def test_policy_identity_is_stable_round_trip_and_binds_every_authority_field():
    original = policy()
    assert NamedSafetyPolicy.from_dict(original.to_dict()) == original
    assert NamedSafetyPolicy.from_dict(original.to_dict()).policy_id == original.policy_id

    variants = (
        replace(original, allowed_tool_ids=("network-probe",)),
        replace(original, approval_mode="automatic-only", approval_required_tool_ids=()),
        replace(original, ceilings=replace(original.ceilings, cost_units=41)),
        replace(original, scope_binding=replace(original.scope_binding, revision=3)),
    )
    assert len({original.policy_id, *(item.policy_id for item in variants)}) == 5
    with pytest.raises(FrozenInstanceError):
        original.name = "changed"


def test_same_label_different_revision_or_content_cannot_collide():
    original = policy()
    assert replace(original, revision=4).policy_id != original.policy_id
    assert replace(
        original, allowed_tool_ids=("network-probe",)
    ).policy_id != original.policy_id


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update({"surprise": True}), "unknown fields"),
        (lambda value: value["allowed_tool_ids"].reverse(), "sorted"),
        (lambda value: value["allowed_tool_ids"].append("inspect"), "duplicates"),
        (lambda value: value["ceilings"].update({"cost_units": float("nan")}), "integer"),
        (lambda value: value["ceilings"].update({"concurrency": True}), "integer"),
    ],
)
def test_policy_parser_rejects_unknown_non_finite_and_ambiguous_content(mutation, message):
    value = policy().to_dict()
    mutation(value)
    with pytest.raises(ValueError, match=message):
        NamedSafetyPolicy.from_dict(value)


def test_scope_binding_rejects_partial_or_ambiguous_authority():
    with pytest.raises(ValueError, match="must not contain"):
        ScopePolicyBinding("unbound", "scope-1", None, None)
    with pytest.raises(ValueError, match="64 lowercase"):
        ScopePolicyBinding("authorized-scope", "scope-1", 1, "A" * 64)


def test_direct_contract_construction_rejects_non_finite_ceilings():
    with pytest.raises(ValueError, match="integer"):
        replace(policy(), ceilings=RunCeilings(cost_units=float("nan")))


def test_sequence_inputs_are_detached_from_mutable_caller_lists():
    tools = ["inspect"]
    constructed = NamedSafetyPolicy(
        name="automatic", revision=1, allowed_tool_ids=tools,
        approval_mode="automatic-only", approval_required_tool_ids=[],
        ceilings=RunCeilings(), scope_binding=ScopePolicyBinding.unbound(),
    )
    identity = constructed.policy_id
    tools.append("network-probe")
    assert constructed.allowed_tool_ids == ("inspect",)
    assert constructed.policy_id == identity


def test_contract_text_and_policy_identity_are_bounded_and_well_formed():
    with pytest.raises(ValueError, match="must not exceed"):
        replace(policy(), name="x" * 257)
    with pytest.raises(ValueError, match="safety-policy-v1"):
        StrategyPolicyConstraints(("policy-by-label",))


def test_legacy_compatibility_is_explicit_and_cannot_expand_permission():
    legacy = NamedSafetyPolicy.legacy_compatibility(ceilings=RunCeilings())
    assert legacy.compatibility == "legacy-deny-all-v1"
    assert legacy.approval_mode == "deny-all"
    assert legacy.allowed_tool_ids == ()
    with pytest.raises(ValueError, match="legacy compatibility"):
        replace(legacy, approval_mode="automatic-only", allowed_tool_ids=("inspect",))
    with pytest.raises(ValueError, match="deny-all"):
        replace(legacy, allowed_tool_ids=("inspect",))


def metadata(policy_id):
    return StrategyMetadata(
        name="recon-then-analysis",
        description="Reconnaissance followed by analysis.",
        roles=(
            StrategyRoleMetadata("recon", "Map the surface."),
            StrategyRoleMetadata("analyst", "Test hypotheses.", ("recon",)),
        ),
        default_ceilings=RunCeilings(concurrency=2),
        compatible_profile_names=("local", "remote"),
        policy_constraints=StrategyPolicyConstraints(
            compatible_policy_ids=(policy_id,), required_tool_ids=("inspect",),
            requires_scope_binding=True,
        ),
    )


def test_strategy_metadata_round_trip_exposes_graph_defaults_profiles_and_constraints():
    original = metadata(policy().policy_id)
    assert StrategyMetadata.from_dict(original.to_dict()) == original
    assert original.roles[1].depends_on_roles == ("recon",)
    assert original.default_ceilings.concurrency == 2
    assert original.compatible_profile_names == ("local", "remote")
    assert original.policy_constraints.requires_scope_binding is True


def test_strategy_metadata_rejects_unknown_fields_unknown_edges_cycles_and_ambiguity():
    value = metadata(policy().policy_id).to_dict()
    value["policy_constraints"]["unexpected"] = "authority"
    with pytest.raises(ValueError, match="unknown fields"):
        StrategyMetadata.from_dict(value)

    with pytest.raises(ValueError, match="unknown dependency"):
        replace(
            metadata(policy().policy_id),
            roles=(StrategyRoleMetadata("analyst", "Analyze.", ("missing",)),),
        )
    with pytest.raises(ValueError, match="acyclic"):
        replace(
            metadata(policy().policy_id),
            roles=(
                StrategyRoleMetadata("a", "A.", ("b",)),
                StrategyRoleMetadata("b", "B.", ("a",)),
            ),
        )
    with pytest.raises(ValueError, match="sorted"):
        replace(metadata(policy().policy_id), compatible_profile_names=("remote", "local"))
