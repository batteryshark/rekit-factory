from __future__ import annotations

from dataclasses import replace

import pytest

from rekit_factory.isolation_probe import (
    MAX_CANARIES,
    MAX_PACKAGE_MEMBERS,
    MAX_PROBES,
    REQUIRED_DENIAL_CHANNELS,
    CanaryRefV1,
    IsolationBindingV1,
    IsolationProbePlanV1,
    PackageMemberV1,
    ProbeResultV1,
    ProbeSpecV1,
    SealedPublicPackageV1,
    assess_probe_results,
)


def _plan() -> IsolationProbePlanV1:
    package = SealedPublicPackageV1(
        "branch-signal-public-v1", "a" * 64, 10240,
        (
            PackageMemberV1("target/branch-signal.rbvm", "b" * 64, 64),
            PackageMemberV1("task.json", "c" * 64, 128),
        ),
    )
    binding = IsolationBindingV1(
        "binding-1", "adapter-local", "v1", "sha256:" + "d" * 64,
        "e" * 64, "f" * 64, "1" * 64, package.archive_sha256, "none",
        "/input/public-package.tar", "/scratch", "/output", "destroy-recreate-v1",
    )
    kinds = ("source", "truth", "private-test", "dossier", "credential", "sibling", "residue")
    canaries = tuple(
        CanaryRefV1(f"canary-{kind}", kind, f"{index + 2:x}" * 64)
        for index, kind in enumerate(kinds)
    )
    probes = tuple(
        ProbeSpecV1(
            f"probe-{channel}", "trial-a" if index % 2 == 0 else "trial-b",
            channel, "unreachable", (canaries[index % len(canaries)].canary_id,),
        )
        for index, channel in enumerate(sorted(REQUIRED_DENIAL_CHANNELS))
    ) + (
        ProbeSpecV1("probe-public", "trial-a", "path", "public-readable", ()),
        ProbeSpecV1("probe-reset", "trial-b", "post-reset", "empty", ()),
    )
    return IsolationProbePlanV1(binding, package, canaries, probes)


def test_plan_is_canonical_round_trippable_and_binds_every_security_input():
    plan = _plan()
    assert IsolationProbePlanV1.from_dict(plan.to_dict()) == plan
    assert plan.digest == IsolationProbePlanV1.from_dict(plan.to_dict()).digest
    assert replace(plan.binding, image_digest="sha256:" + "9" * 64).digest != plan.binding.digest
    assert replace(plan.binding, scope_sha256="8" * 64).digest != plan.binding.digest
    assert replace(plan.binding, reset_policy_id="destroy-recreate-v2").digest != plan.binding.digest


def test_plan_contains_no_plaintext_canary_values_or_host_paths():
    encoded = str(_plan().to_dict())
    assert "canary-value" not in encoded
    assert "/Users/" not in encoded
    assert "/private/" not in encoded
    assert "docker.sock" not in encoded


def test_package_members_are_exact_sorted_relative_allowlist():
    plan = _plan()
    with pytest.raises(ValueError, match="sorted"):
        replace(plan.package, members=tuple(reversed(plan.package.members)))
    with pytest.raises(ValueError, match="relative POSIX"):
        replace(plan.package.members[0], path="../private/truth.json")
    with pytest.raises(ValueError, match="unique"):
        replace(plan.package, members=(plan.package.members[0], plan.package.members[0]))
    with pytest.raises(ValueError, match="relative POSIX"):
        replace(plan.package.members[0], path="a" * 1025)


def test_binding_requires_pinned_image_offline_network_and_distinct_mounts():
    binding = _plan().binding
    with pytest.raises(ValueError, match="pinned"):
        replace(binding, image_digest="ubuntu:latest")
    with pytest.raises(ValueError, match="network_policy none"):
        replace(binding, network_policy="restricted")
    with pytest.raises(ValueError, match="distinct"):
        replace(binding, output_mount=binding.scratch_mount)
    for malformed in ("//input/package.tar", "/input/./package.tar", "/input/package.tar/"):
        with pytest.raises(ValueError, match="normalized"):
            replace(binding, input_mount=malformed)
    with pytest.raises(ValueError, match="normalized"):
        replace(binding, input_mount="/" + "a" * 1025)


def test_plan_fails_closed_without_each_channel_parallelism_or_reset_probe():
    plan = _plan()
    with pytest.raises(ValueError, match="missing required denial channels"):
        replace(plan, probes=tuple(item for item in plan.probes if item.channel != "credential"))
    with pytest.raises(ValueError, match="at least two trials"):
        replace(plan, probes=tuple(replace(item, trial_id="trial-a") for item in plan.probes))
    with pytest.raises(ValueError, match="post-reset"):
        replace(plan, probes=tuple(item for item in plan.probes if item.channel != "post-reset"))


def test_strict_decoders_reject_unknown_fields_and_non_arrays():
    value = _plan().to_dict()
    value["host_home"] = "/Users/example"
    with pytest.raises(ValueError, match="exactly"):
        IsolationProbePlanV1.from_dict(value)
    value = _plan().to_dict()
    value["probes"] = "probe-path"
    with pytest.raises(ValueError, match="JSON array"):
        IsolationProbePlanV1.from_dict(value)


def test_strict_decoders_reject_boolean_versions_and_unhashable_literals():
    value = _plan().to_dict()
    value["schema_version"] = True
    with pytest.raises(ValueError, match="schema_version"):
        IsolationProbePlanV1.from_dict(value)
    value = _plan().to_dict()
    value["canaries"][0]["kind"] = []
    with pytest.raises(ValueError, match="canary kind"):
        IsolationProbePlanV1.from_dict(value)
    value = _plan().to_dict()
    value["probes"][0]["channel"] = []
    with pytest.raises(ValueError, match="probe channel"):
        IsolationProbePlanV1.from_dict(value)
    value = _plan().to_dict()
    value["probes"][0]["expectation"] = []
    with pytest.raises(ValueError, match="probe expectation"):
        IsolationProbePlanV1.from_dict(value)


def test_hostile_collection_sizes_are_rejected_before_nested_decode():
    plan = _plan()
    package = plan.package.to_dict()
    package["members"] = [{}] * (MAX_PACKAGE_MEMBERS + 1)
    with pytest.raises(ValueError, match="at most"):
        SealedPublicPackageV1.from_dict(package)
    value = plan.to_dict()
    value["canaries"] = [{}] * (MAX_CANARIES + 1)
    with pytest.raises(ValueError, match="at most"):
        IsolationProbePlanV1.from_dict(value)
    value = plan.to_dict()
    value["probes"] = [{}] * (MAX_PROBES + 1)
    with pytest.raises(ValueError, match="at most"):
        IsolationProbePlanV1.from_dict(value)


def test_result_assessment_requires_complete_verified_passing_observations():
    plan = _plan()
    results = tuple(
        ProbeResultV1(plan.digest, item.probe_id, "passed", "7" * 64) for item in plan.probes
    )
    assert assess_probe_results(plan, results) == ()
    assert assess_probe_results(plan, results[:-1]) == ("incomplete-results",)
    not_run = (replace(results[0], outcome="not-run", evidence_sha256=None),) + results[1:]
    assert assess_probe_results(plan, not_run) == ("probe-not-run",)
    mismatched = (replace(results[0], plan_sha256="6" * 64),) + results[1:]
    assert assess_probe_results(plan, mismatched) == ("plan-mismatch",)


def test_any_known_or_unknown_canary_leak_blocks_qualification():
    plan = _plan()
    results = tuple(
        ProbeResultV1(plan.digest, item.probe_id, "passed", "7" * 64) for item in plan.probes
    )
    leaked = replace(
        results[0], outcome="failed", leaked_canary_ids=(plan.canaries[0].canary_id,),
    )
    assert assess_probe_results(plan, (leaked,) + results[1:]) == (
        "canary-leak", "probe-failed",
    )
    unknown = replace(leaked, leaked_canary_ids=("canary-unknown",))
    assert assess_probe_results(plan, (unknown,) + results[1:]) == (
        "unknown-canary-reference", "canary-leak", "probe-failed",
    )


def test_contracts_never_claim_that_a_passing_matrix_is_environmental_proof():
    assert "necessary, not sufficient" in (assess_probe_results.__doc__ or "")
