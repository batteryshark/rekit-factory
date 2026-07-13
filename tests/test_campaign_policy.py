from __future__ import annotations

from dataclasses import replace

import pytest

from rekit_factory.campaign_contracts import (
    CampaignCheckpoint,
    CampaignChangeRequest,
    CampaignContract,
    CheckpointSource,
    CompletionCriteria,
    ComponentVersion,
    EpochContract,
    EpochResult,
    OperatorPolicy,
    ProgressSignal,
    ResourceBudget,
    ResourceLimit,
    ResourceUsage,
    ScopeBinding,
)
from rekit_factory.campaign_policy import (
    AttemptFact,
    BudgetAccount,
    BudgetReservation,
    CampaignPolicyConfig,
    CampaignPolicyInput,
    CanonicalOutcomeTotals,
    evaluate_campaign_policy,
    validate_campaign_change_approval,
)


DIGEST_A = "a" * 64


def limit(value: int, unit: str) -> ResourceLimit:
    return ResourceLimit(value, unit)


def budget(*, work: int = 8, retries: int = 4, cost: int = 100) -> ResourceBudget:
    return ResourceBudget(
        limit(work, "items"), limit(2, "workers"), limit(retries, "attempts"),
        limit(10_000, "tokens"), limit(4_000, "tokens"), limit(cost, "cost-units"),
        limit(3_600, "seconds"), limit(20, "calls"), limit(0, "calls"),
        limit(1_000_000, "bytes"),
    )


def campaign() -> CampaignContract:
    return CampaignContract(
        "project-a", "Finish the bounded campaign",
        ScopeBinding("scope-a", 1, DIGEST_A), budget(),
        budget(work=32, retries=16, cost=400),
        CompletionCriteria(8_000, 1, 1, ("artifact-proof",)),
        OperatorPolicy(risk_threshold=60),
        (ComponentVersion("factory", "0.2.0", DIGEST_A),),
    )


def policy_input(
    *,
    signals: tuple[ProgressSignal, ...] = (),
    attempts: tuple[AttemptFact, ...] = (),
    phase: str = "recon",
    usage: ResourceUsage = ResourceUsage(work_items=1, cost_units=5),
    totals: CanonicalOutcomeTotals = CanonicalOutcomeTotals(),
    previous_totals: CanonicalOutcomeTotals = CanonicalOutcomeTotals(),
    known: tuple[str, ...] = (),
) -> CampaignPolicyInput:
    parent = campaign()
    epoch = EpochContract(parent.campaign_id, 1, ("work-a",), budget())
    checkpoint = CampaignCheckpoint(
        parent.campaign_id, epoch.epoch_id, 1,
        (CheckpointSource("factory-ledger", 1, DIGEST_A),), usage,
    )
    result = EpochResult(epoch.epoch_id, checkpoint.checkpoint_id, signals, ())
    return CampaignPolicyInput(
        parent, epoch, checkpoint, result, BudgetAccount(usage), phase, totals,
        previous_totals=previous_totals, known_progress_digests=known,
        attempts=attempts,
    )


def signal(kind: str, reference: str, digest_char: str) -> ProgressSignal:
    return ProgressSignal(kind, reference, digest_char * 64)


def attempt(number: int, outcome: str, equivalence: str | None = None) -> AttemptFact:
    return AttemptFact(f"attempt-{number}", equivalence or f"{number:064x}", outcome)


def later_policy_input(parent: CampaignContract, prior_usage: ResourceUsage,
                       current_usage: ResourceUsage) -> CampaignPolicyInput:
    first = EpochContract(parent.campaign_id, 1, ("work-a",), budget())
    prior = CampaignCheckpoint(
        parent.campaign_id, first.epoch_id, 1,
        (CheckpointSource("factory-ledger", 1, DIGEST_A),), prior_usage,
    )
    second = EpochContract(parent.campaign_id, 2, ("work-b",), budget(), prior.checkpoint_id)
    current = CampaignCheckpoint(
        parent.campaign_id, second.epoch_id, 2,
        (CheckpointSource("factory-ledger", 2, "b" * 64),), current_usage,
    )
    result = EpochResult(second.epoch_id, current.checkpoint_id, (), ())
    return CampaignPolicyInput(
        parent, second, current, result, BudgetAccount(current_usage), "recon",
        CanonicalOutcomeTotals(), previous_checkpoint=prior,
    )


def test_productive_canonical_facts_advance_recon_then_hypothesis_to_validation():
    evidence = signal("material-evidence", "artifact-candidate", "b")
    recon = evaluate_campaign_policy(policy_input(
        signals=(evidence,), totals=CanonicalOutcomeTotals(artifact_ids=("artifact-candidate",)),
    ))
    assert (recon.action, recon.reason_code, recon.next_phase) == (
        "continue", "canonical-progress", "hypothesis",
    )

    resolved = signal("hypothesis-resolved", "hypothesis-a", "c")
    hypothesis = evaluate_campaign_policy(policy_input(
        signals=(resolved,), phase="hypothesis",
        totals=CanonicalOutcomeTotals(resolved_hypotheses=1),
    ))
    assert (hypothesis.action, hypothesis.next_phase) == ("continue", "validation")


def test_repeated_prose_is_not_progress_and_no_novelty_asks_before_unrelated_budget_exhaustion():
    repeated = signal("material-evidence", "report-prose", "b")
    value = policy_input(
        signals=(repeated,), known=(repeated.material_digest,),
        attempts=tuple(attempt(i, "no-novelty") for i in range(3)),
    )
    recommendation = evaluate_campaign_policy(value)
    assert recommendation.action == "ask-operator"
    assert recommendation.reason_code == "no-novelty-threshold"
    assert recommendation.limiting_resources == ()


def test_equivalent_reordered_inputs_have_identical_recommendation_and_explanation():
    facts = (attempt(2, "no-novelty", "f" * 64),
             attempt(1, "no-novelty", "f" * 64))
    left = evaluate_campaign_policy(policy_input(attempts=facts))
    right = evaluate_campaign_policy(policy_input(attempts=tuple(reversed(facts))))
    assert left == right
    assert (left.action, left.reason_code) == ("reprioritize", "equivalent-attempt-repeated")


def test_budget_reservation_retry_refund_and_commit_are_exact_and_idempotent():
    ceiling = campaign().cumulative_budget
    reservation = BudgetReservation("work-a:attempt-1", ResourceUsage(work_items=1, retries=1,
                                                                       cost_units=20))
    initial = BudgetAccount(ResourceUsage(cost_units=10))
    reserved = initial.reserve(reservation, ceiling)
    assert reserved.reserve(reservation, ceiling) is reserved
    with pytest.raises(ValueError, match="conflicts"):
        reserved.reserve(replace(reservation, usage=ResourceUsage(cost_units=21)), ceiling)
    assert reserved.refund(reservation.reservation_id) == initial
    committed = reserved.commit(
        reservation.reservation_id, ResourceUsage(work_items=1, retries=1, cost_units=12), ceiling,
    )
    assert committed.committed == ResourceUsage(work_items=1, retries=1, cost_units=22)
    assert committed.reservations == ()
    with pytest.raises(ValueError, match="already-consumed"):
        committed.commit(reservation.reservation_id, ResourceUsage(cost_units=1), ceiling)


@pytest.mark.parametrize(
    ("facts", "action", "reason"),
    [
        ((attempt(1, "validation-rejected"), attempt(2, "validation-rejected"),
          attempt(3, "validation-rejected")), "reprioritize", "validation-churn"),
        ((attempt(1, "dependency-blocked"), attempt(2, "dependency-blocked")),
         "ask-operator", "dependency-deadlock"),
        ((attempt(1, "environment-failed"), attempt(2, "environment-failed"),
          attempt(3, "environment-failed")), "suspend", "environment-flapping"),
        ((attempt(1, "notification-only"), attempt(2, "notification-only"),
          attempt(3, "notification-only")), "backoff", "notification-churn"),
    ],
)
def test_reason_coded_churn_detectors(facts, action, reason):
    recommendation = evaluate_campaign_policy(policy_input(attempts=facts))
    assert (recommendation.action, recommendation.reason_code) == (action, reason)


def test_success_and_hard_cumulative_exhaustion_are_canonical_and_reason_coded():
    complete = CanonicalOutcomeTotals(8_000, 1, 1, ("artifact-proof",))
    signals = (
        signal("coverage-moved", "coverage-a", "b"),
        signal("hypothesis-resolved", "hypothesis-a", "c"),
        signal("finding-reproduced", "finding-a", "d"),
        signal("material-evidence", "artifact-proof", "e"),
    )
    assert evaluate_campaign_policy(policy_input(signals=signals, totals=complete)).action == "success"

    exhausted = evaluate_campaign_policy(later_policy_input(
        campaign(), ResourceUsage(work_items=24), ResourceUsage(work_items=32),
    ))
    assert (exhausted.action, exhausted.reason_code, exhausted.limiting_resources) == (
        "exhausted", "cumulative-budget-exhausted", ("work_items",),
    )


def test_soft_cumulative_threshold_asks_instead_of_exhausting_or_silently_continuing():
    parent = campaign()
    parent = replace(parent, cumulative_budget=replace(
        parent.cumulative_budget,
        cost_units=ResourceLimit(400, "cost-units", "soft"),
    ))
    recommendation = evaluate_campaign_policy(later_policy_input(
        parent, ResourceUsage(cost_units=300), ResourceUsage(cost_units=400),
    ))
    assert (recommendation.action, recommendation.reason_code,
            recommendation.limiting_resources) == (
        "ask-operator", "soft-budget-threshold", ("cost_units",),
    )


def test_overflow_negative_stale_and_contradictory_inputs_fail_closed():
    with pytest.raises(ValueError, match="overflows"):
        BudgetAccount(reservations=(
            BudgetReservation("a", ResourceUsage(cost_units=2**63 - 1)),
            BudgetReservation("b", ResourceUsage(cost_units=1)),
        ))
    with pytest.raises(ValueError, match="integer"):
        ResourceUsage(cost_units=-1)

    parent = campaign()
    prior_epoch = EpochContract(parent.campaign_id, 1, ("work-a",), budget())
    prior = CampaignCheckpoint(
        parent.campaign_id, prior_epoch.epoch_id, 1,
        (CheckpointSource("factory-ledger", 1, DIGEST_A),), ResourceUsage(work_items=1),
    )
    epoch = EpochContract(parent.campaign_id, 2, ("work-b",), budget(), prior.checkpoint_id)
    stale = CampaignCheckpoint(
        parent.campaign_id, epoch.epoch_id, 3,
        (CheckpointSource("factory-ledger", 2, "b" * 64),), ResourceUsage(work_items=2),
    )
    stale_result = EpochResult(epoch.epoch_id, stale.checkpoint_id, (), ())
    with pytest.raises(ValueError, match="stale or discontinuous"):
        evaluate_campaign_policy(CampaignPolicyInput(
            parent, epoch, stale, stale_result, BudgetAccount(stale.cumulative_usage),
            "recon", CanonicalOutcomeTotals(), previous_checkpoint=prior,
        ))

    changed_without_fact = policy_input(totals=CanonicalOutcomeTotals(resolved_hypotheses=1))
    with pytest.raises(ValueError, match="without matching progress"):
        evaluate_campaign_policy(changed_without_fact)


def test_policy_stop_occurs_at_explicit_no_novelty_limit():
    config = CampaignPolicyConfig(no_novelty_ask_threshold=2, no_novelty_stop_threshold=4)
    value = policy_input(attempts=tuple(attempt(i, "no-novelty") for i in range(4)))
    recommendation = evaluate_campaign_policy(value, config)
    assert (recommendation.action, recommendation.reason_code) == (
        "policy-stop", "no-novelty-policy-limit",
    )


def test_signal_without_total_delta_and_malformed_canonical_identity_fail_closed():
    with pytest.raises(ValueError, match="unchanged canonical totals"):
        evaluate_campaign_policy(policy_input(
            signals=(signal("coverage-moved", "coverage-a", "b"),),
        ))
    with pytest.raises(ValueError, match="SHA-256"):
        attempt(1, "no-novelty", "not-a-digest")
    with pytest.raises(ValueError, match="SHA-256"):
        policy_input(known=("not-a-digest",))


def test_recommendation_identity_and_serialization_are_stable_under_input_order():
    facts = (attempt(2, "no-novelty", "f" * 64),
             attempt(1, "no-novelty", "f" * 64))
    left = evaluate_campaign_policy(policy_input(attempts=facts))
    right = evaluate_campaign_policy(policy_input(attempts=tuple(reversed(facts))))
    assert left.to_dict() == right.to_dict()
    assert left.recommendation_id == right.recommendation_id
    assert left.recommendation_id.startswith("policy-")


def test_scope_or_hard_ceiling_change_requires_exact_durable_approval_identity():
    current = campaign()
    proposed = replace(current, scope=ScopeBinding("scope-a", 2, "b" * 64))
    request = CampaignChangeRequest(current.campaign_id, proposed, "Approve bounded scope revision")
    with pytest.raises(ValueError, match="exact durable approval"):
        validate_campaign_change_approval(current, request, None)
    with pytest.raises(ValueError, match="exact durable approval"):
        validate_campaign_change_approval(current, request, "campaign-change-incorrect")
    assert validate_campaign_change_approval(current, request, request.request_id) == proposed
