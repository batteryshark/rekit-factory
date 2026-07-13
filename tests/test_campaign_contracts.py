from __future__ import annotations

from dataclasses import replace
import json

import pytest

from rekit_factory.campaign_contracts import (
    CampaignChangeRequest,
    CampaignCheckpoint,
    CampaignContract,
    CheckpointSource,
    CompletionCriteria,
    ComponentVersion,
    EpochContract,
    OperatorPolicy,
    ResourceBudget,
    ResourceLimit,
    ResourceUsage,
    ScopeBinding,
    TerminalOutcome,
    requires_operator_decision,
    validate_campaign_transition,
)


DIGEST = "a" * 64


def limit(value, unit, enforcement="hard"):
    return ResourceLimit(value, unit, enforcement)


def budget(*, work=8, concurrency=2, retries=2, input_tokens=10_000,
           output_tokens=4_000, cost=100, seconds=3600, tools=10,
           network=0, artifacts=1_000_000):
    return ResourceBudget(
        limit(work, "items"), limit(concurrency, "workers"),
        limit(retries, "attempts"), limit(input_tokens, "tokens"),
        limit(output_tokens, "tokens"), limit(cost, "cost-units"),
        limit(seconds, "seconds"), limit(tools, "calls"),
        limit(network, "calls"), limit(artifacts, "bytes"),
    )


def campaign(**changes):
    values = dict(
        project_id="project-a", goal="Prove the bounded fixture",
        scope=ScopeBinding("scope-a", 1, DIGEST),
        epoch_budget=budget(), cumulative_budget=budget(
            work=32, concurrency=4, retries=8, input_tokens=40_000,
            output_tokens=16_000, cost=400, seconds=14_400, tools=40,
            network=0, artifacts=4_000_000,
        ),
        completion=CompletionCriteria(8000, 1, 1, ("artifact-proof",)),
        operator_policy=OperatorPolicy(risk_threshold=60),
        components=(ComponentVersion("factory", "0.2.0", DIGEST),),
    )
    values.update(changes)
    return CampaignContract(**values)


def test_campaign_contract_round_trip_is_canonical_and_content_bound():
    value = campaign()
    decoded = CampaignContract.from_dict(json.loads(value.canonical_bytes()))
    assert decoded == value
    assert decoded.canonical_bytes() == value.canonical_bytes()
    assert value.campaign_id == "campaign-" + value.digest
    assert campaign(goal="Prove a different fixture").campaign_id != value.campaign_id
    assert campaign(scope=ScopeBinding("scope-a", 2, DIGEST)).campaign_id != value.campaign_id
    assert campaign(operator_policy=OperatorPolicy(risk_threshold=40)).campaign_id != value.campaign_id
    changed_budget = replace(value.epoch_budget, cost_units=limit(99, "cost-units"))
    assert campaign(epoch_budget=changed_budget).campaign_id != value.campaign_id
    assert campaign(completion=CompletionCriteria(9000, 1, 1)).campaign_id != value.campaign_id
    assert campaign(components=(ComponentVersion("factory", "0.2.1", DIGEST),)).campaign_id != value.campaign_id


def test_epoch_is_finite_bound_and_requires_checkpoint_after_first():
    parent = campaign()
    epoch = EpochContract(parent.campaign_id, 1, ("work-b", "work-a"), budget())
    epoch.validate_for(parent)
    assert EpochContract.from_dict(epoch.to_dict()) == epoch
    assert epoch.work_ids == ("work-a", "work-b")
    with pytest.raises(ValueError, match="previous checkpoint"):
        EpochContract(parent.campaign_id, 2, ("work-a",), budget())
    with pytest.raises(ValueError, match="per-epoch budget"):
        replace(epoch, budget=budget(work=9)).validate_for(parent)


def test_budget_rejects_unit_confusion_unbounded_values_and_inconsistent_totals():
    with pytest.raises(ValueError, match="workItems must use unit items"):
        replace(budget(), work_items=limit(8, "bytes"))
    with pytest.raises(ValueError, match="finite"):
        budget(work=0)
    with pytest.raises(ValueError, match="integer"):
        budget(cost=float("inf"))
    with pytest.raises(ValueError, match="integer"):
        budget(artifacts=2**63)
    with pytest.raises(ValueError, match="epoch budget"):
        campaign(epoch_budget=budget(work=33))


def test_unknown_fields_tampered_identity_and_ambiguous_success_fail_closed():
    raw = campaign().to_dict()
    raw["future"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        CampaignContract.from_dict(raw)
    raw = campaign().to_dict()
    raw["goal"] = "Tampered goal"
    with pytest.raises(ValueError, match="identity"):
        CampaignContract.from_dict(raw)
    with pytest.raises(ValueError, match="at least one canonical outcome"):
        CompletionCriteria(0, 0, 0)
    with pytest.raises(ValueError, match="terminal evidence"):
        TerminalOutcome(campaign().campaign_id, "completed", "success", (), "checkpoint-a")


def test_transition_matrix_names_exact_authority_and_terminal_states_do_not_reopen():
    validate_campaign_transition("requested", "running", authority="factory-scheduler")
    validate_campaign_transition("running", "stopped", authority="operator")
    validate_campaign_transition("running", "policy-stopped", authority="validator-policy")
    with pytest.raises(ValueError, match="requires operator"):
        validate_campaign_transition("running", "stopped", authority="factory-scheduler")
    with pytest.raises(ValueError, match="invalid campaign transition"):
        validate_campaign_transition("completed", "running", authority="factory-scheduler")


def test_scope_or_hard_ceiling_increase_requires_exact_operator_decision():
    current = campaign()
    assert not requires_operator_decision(current, current)
    assert requires_operator_decision(
        current, campaign(scope=ScopeBinding("scope-a", 2, "b" * 64))
    )
    larger = replace(
        current,
        cumulative_budget=replace(
            current.cumulative_budget,
            cost_units=limit(current.cumulative_budget.cost_units.value + 1, "cost-units"),
        ),
    )
    assert requires_operator_decision(current, larger)
    request = CampaignChangeRequest(current.campaign_id, larger, "Raise bounded cost ceiling")
    assert CampaignChangeRequest.from_dict(request.to_dict()) == request
    assert request.request_id.startswith("campaign-change-")
    with pytest.raises(ValueError, match="cannot be revised"):
        requires_operator_decision(current, campaign(goal="A new campaign goal"))


def test_operator_policy_cannot_disable_v1_authority_expansion_gates():
    with pytest.raises(ValueError, match="must require approval"):
        OperatorPolicy(scope_expansion_requires_approval=False)


def test_checkpoint_is_content_bound_to_sources_epoch_and_cumulative_usage():
    parent = campaign()
    epoch = EpochContract(parent.campaign_id, 1, ("work-a",), budget())
    checkpoint = CampaignCheckpoint(
        parent.campaign_id, epoch.epoch_id, 1,
        (CheckpointSource("project-memory", 4, DIGEST),
         CheckpointSource("factory-ledger", 9, "b" * 64)),
        ResourceUsage(work_items=1, cost_units=10, wall_seconds=30),
    )
    assert CampaignCheckpoint.from_dict(checkpoint.to_dict()) == checkpoint
    assert replace(
        checkpoint, cumulative_usage=ResourceUsage(work_items=2, cost_units=10)
    ).checkpoint_id != checkpoint.checkpoint_id
    raw = checkpoint.to_dict()
    raw["sequence"] = 2
    with pytest.raises(ValueError, match="checkpoint identity"):
        CampaignCheckpoint.from_dict(raw)
