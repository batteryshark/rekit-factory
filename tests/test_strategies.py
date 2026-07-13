from dataclasses import replace

import pytest

from rekit_factory.strategies import (
    FollowUpProposal,
    RunCeilings,
    plan_investigation,
    propose_follow_up,
)


def test_initial_plan_is_stable_and_fans_out_independent_workers():
    first = plan_investigation("  Explain   the target ")
    second = plan_investigation("Explain the target")

    assert first == second
    assert [item.role for item in first.work] == ["recon", "analyst"]
    assert all(not item.depends_on for item in first.work)
    assert len({item.id for item in first.work}) == 2


def test_dependency_strategy_links_analysis_to_recon_by_stable_id():
    plan = plan_investigation("Explain the target", "recon-then-analysis")
    recon, analyst = plan.work

    assert analyst.depends_on == (recon.id,)


def test_evidence_follow_up_is_stable_and_deduplicates():
    plan = plan_investigation("Explain the target")
    proposal = FollowUpProposal(
        role="format-specialist",
        objective="Inspect the suspicious header",
        evidence_ids=("artifact-2", "artifact-1", "artifact-1"),
        depends_on=(plan.work[0].id,),
    )

    follow_up = propose_follow_up(plan, proposal)
    repeated = propose_follow_up(
        plan,
        replace(proposal, evidence_ids=("artifact-1", "artifact-2")),
    )

    assert follow_up == repeated
    assert follow_up is not None
    assert follow_up.origin == "worker-proposal"
    assert follow_up.evidence_ids == ("artifact-1", "artifact-2")
    assert propose_follow_up(
        plan, proposal, existing_dedupe_keys=(follow_up.dedupe_key,)
    ) is None


def test_follow_up_requires_evidence_and_known_dependencies():
    plan = plan_investigation("Explain the target")
    with pytest.raises(ValueError, match="evidence"):
        propose_follow_up(plan, FollowUpProposal("analyst", "Dig deeper", ()))
    with pytest.raises(ValueError, match="unknown work dependencies"):
        propose_follow_up(
            plan,
            FollowUpProposal("analyst", "Dig deeper", ("e1",), ("missing",)),
        )


def test_explicit_ceilings_bound_initial_and_adaptive_work():
    with pytest.raises(ValueError, match="concurrency"):
        RunCeilings(concurrency=3, max_workers=2)

    plan = plan_investigation(
        "Explain the target",
        ceilings=RunCeilings(concurrency=2, retries_per_worker=0, cost_units=20, max_workers=2),
    )
    assert plan.ceilings.retries_per_worker == 0
    with pytest.raises(ValueError, match="max_workers"):
        propose_follow_up(
            plan,
            FollowUpProposal("specialist", "Dig deeper", ("evidence-1",)),
        )


def test_existing_adaptive_work_counts_toward_cost_and_dependency_limits():
    plan = plan_investigation(
        "Explain the target",
        ceilings=RunCeilings(concurrency=2, cost_units=35, max_workers=4),
    )
    first = propose_follow_up(
        plan, FollowUpProposal("specialist", "Inspect header", ("evidence-1",), cost_units=10)
    )
    assert first is not None

    with pytest.raises(ValueError, match="cost_units"):
        propose_follow_up(
            plan,
            FollowUpProposal(
                "validator", "Validate header", ("evidence-2",), (first.id,), cost_units=10
            ),
            existing_work=(first,),
        )
