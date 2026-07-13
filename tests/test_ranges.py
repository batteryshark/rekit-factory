from __future__ import annotations

from dataclasses import replace
import json

import pytest

from rekit_factory.ranges import (
    DeterministicFakeRangeAdapter,
    ImmutableInputV1,
    InjectedRangeFailure,
    NodeHandleV1,
    ProviderHandleV1,
    RANGE_STATUSES,
    RANGE_TRANSITIONS,
    RangeAccessError,
    RangeConflictError,
    RangeFailureV1,
    RangeLifecyclePolicyV1,
    RangeLinkV1,
    RangeNetworkV1,
    RangeNodeV1,
    RangeResourcesV1,
    RangeScopeV1,
    RangeServiceV1,
    RangeSpecV1,
    RangeStateError,
    RangeTemplateV1,
    RangeWorkIntentV1,
    RangeWorkRequestV1,
    benign_two_node_fixture,
    canonical_json, canonical_sha256,
    require_range_transition,
)


def _handle(state, node_id="analyzer"):
    return next(item.handle for item in state.node_handles if item.node_id == node_id)


def _work(state, *, operation_id="work-1", range_id=None, node_id="analyzer",
          handle=None, input_ids=("sample",), intent=None, output_name="output/report.json"):
    return RangeWorkRequestV1(
        1, operation_id, range_id or state.range_id, node_id,
        handle or _handle(state, node_id), input_ids, "inspect-inputs", output_name,
        intent or RangeWorkIntentV1(input_mounts=input_ids),
    )


def test_canonical_contracts_normalize_set_like_and_mapping_order():
    template, spec = benign_two_node_fixture()
    reordered_template = replace(
        template,
        nodes=tuple(reversed(template.nodes)),
        links=tuple(reversed(template.links)),
    )
    reordered_spec = replace(
        spec,
        inputs=tuple(reversed(spec.inputs)),
        scope=replace(
            spec.scope,
            actions=tuple(reversed(spec.scope.actions)),
            input_ids=tuple(reversed(spec.scope.input_ids)),
        ),
    )
    assert reordered_template.to_json() == template.to_json()
    assert reordered_template.digest == template.digest
    assert reordered_spec.to_json() == spec.to_json()
    assert reordered_spec.digest == spec.digest

    first = {"z": 1, "a": {"right": 2, "left": 1}}
    second = {"a": {"left": 1, "right": 2}, "z": 1}
    assert canonical_json(first) == canonical_json(second)


def test_all_v1_contracts_round_trip_through_strict_json_shapes():
    template, spec = benign_two_node_fixture()
    adapter = DeterministicFakeRangeAdapter(now=spec.requested_at)
    ready = adapter.provision("provision-round-trip", template, spec)
    request = _work(ready, operation_id="work-round-trip")
    output = adapter.execute(request)
    values = (
        template.nodes[1].services[0], template.nodes[0], template.links[0], template,
        spec.inputs[0], spec.scope, spec.network, spec.resources, spec.lifecycle, spec,
        ready.range_handle, ready.node_handles[0],
        RangeFailureV1("fixture-failure", "bounded fixture failure", "ready", True),
        ready, request.intent, request,
        adapter.scratch(spec.range_id)[0], output,
    )
    for value in values:
        assert type(value).from_dict(value.to_dict()) == value


def test_security_relevant_fields_change_template_or_spec_identity():
    template, spec = benign_two_node_fixture()
    analyzer, helper = template.nodes
    template_variants = (
        replace(template, template_id="benign-two-node-other"),
        replace(template, template_version="v2"),
        replace(template, nodes=(replace(analyzer, platform="windows"), helper)),
        replace(template, nodes=(replace(analyzer, architecture="arm64"), helper)),
        replace(template, nodes=(replace(analyzer, image_sha256="d" * 64), helper)),
        replace(template, nodes=(replace(analyzer, capabilities=("dynamic-inspection",)), helper)),
        replace(template, nodes=(
            analyzer,
            replace(helper, services=(RangeServiceV1("artifact-index", "tcp", 9443),)),
        )),
        replace(template, links=()),
    )
    assert all(item.digest != template.digest for item in template_variants)

    changed_input = replace(spec.inputs[0], sha256="e" * 64)
    variants = (
        replace(spec, range_id="range-other"),
        replace(spec, template_sha256="f" * 64),
        replace(spec, inputs=(changed_input,)),
        replace(spec, inputs=(replace(spec.inputs[0], size=13),)),
        replace(spec, inputs=(replace(spec.inputs[0], media_type="application/x-fixture"),)),
        replace(spec, inputs=(replace(spec.inputs[0], mount_path="input/renamed.bin"),)),
        replace(spec, scope=replace(spec.scope, scope_id="scope-other")),
        replace(spec, scope=replace(spec.scope, revision=2)),
        replace(spec, scope=replace(
            spec.scope, actions=("mount_input", "network_access"),
        )),
        replace(spec, resources=replace(spec.resources, max_nodes=3)),
        replace(spec, resources=replace(spec.resources, max_vcpus_per_node=3)),
        replace(spec, resources=replace(spec.resources, max_memory_mb_per_node=4096)),
        replace(spec, resources=replace(spec.resources, max_scratch_bytes=999_999)),
        replace(spec, resources=replace(spec.resources, max_output_bytes=999_999)),
        replace(spec, resources=replace(spec.resources, max_work_items=9)),
        replace(spec, lifecycle=replace(spec.lifecycle, max_lifetime_seconds=7200)),
        replace(spec, requested_at="2026-07-13T12:00:01Z"),
        replace(spec, expires_at="2026-07-13T12:30:00Z"),
    )
    assert all(item.digest != spec.digest for item in variants)

    network_scope = RangeScopeV1(
        "scope-benign", 1, ("mount_input", "network_access"),
        ("https://updates.invalid:443",), (), ("sample",),
    )
    networked = replace(
        spec, scope=network_scope,
        network=RangeNetworkV1("isolated", ("https://updates.invalid:443",)),
    )
    assert networked.digest != spec.digest
    credentialed = replace(spec, scope=RangeScopeV1(
        "scope-benign", 1, ("mount_input", "credential_use"), (),
        ("credential:fixture",), ("sample",),
    ))
    assert credentialed.digest != spec.digest


def test_contracts_reject_private_paths_secrets_unknown_fields_and_bad_topology():
    template, spec = benign_two_node_fixture()
    with pytest.raises(ValueError, match="relative POSIX"):
        replace(spec, inputs=(replace(spec.inputs[0], mount_path="/private/tmp/sample"),))
    with pytest.raises(ValueError, match="provider handle"):
        ProviderHandleV1("node", "https://provider.invalid/node/secret")
    with pytest.raises(ValueError, match="credential: references"):
        RangeScopeV1(
            "scope", 1, ("credential_use",), (), ("raw-api-key-value",), (),
        )
    with pytest.raises(ValueError, match="outside the scope"):
        replace(
            spec,
            network=RangeNetworkV1("isolated", ("https://egress.invalid:443",)),
        )
    with pytest.raises(ValueError, match="undeclared destination service"):
        replace(template, links=(RangeLinkV1("analyzer", "helper", ("missing",)),))

    decoded = spec.to_dict()
    decoded["api_key"] = "must-not-exist"
    with pytest.raises(ValueError, match="unknown fields"):
        RangeSpecV1.from_dict(decoded)
    serialized = template.to_json() + spec.to_json()
    for forbidden in ("api_key", "credential-must", "/private/", "C:\\"):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("contract", "field"),
    [
        (lambda template, spec: template.nodes[0], "capabilities"),
        (lambda template, spec: template.links[0], "service_ids"),
        (lambda template, spec: template, "nodes"),
        (lambda template, spec: spec.scope, "actions"),
        (lambda template, spec: spec.scope, "endpoints"),
        (lambda template, spec: spec.scope, "credential_refs"),
        (lambda template, spec: spec.scope, "input_ids"),
        (lambda template, spec: spec.network, "allowed_egress"),
    ],
)
def test_decoders_reject_strings_for_every_sequence_field(contract, field):
    template, spec = benign_two_node_fixture()
    value = contract(template, spec)
    malformed = value.to_dict()
    malformed[field] = "not-an-array"
    with pytest.raises(ValueError, match="JSON array"):
        type(value).from_dict(malformed)


def test_remaining_nested_decoders_require_json_arrays():
    template, spec = benign_two_node_fixture()
    adapter = DeterministicFakeRangeAdapter(now=spec.requested_at)
    ready = adapter.provision("provision-arrays", template, spec)
    request = _work(ready, operation_id="work-arrays")
    output = adapter.execute(request)
    values = (
        (template.nodes[1], "services"),
        (template, "links"),
        (spec, "inputs"),
        (ready, "node_handles"),
        (request.intent, "network_endpoints"),
        (request.intent, "credential_refs"),
        (request.intent, "input_mounts"),
        (request, "input_ids"),
        (output, "input_sha256"),
    )
    for value, field in values:
        malformed = value.to_dict()
        malformed[field] = "not-an-array"
        with pytest.raises(ValueError, match="JSON array"):
            type(value).from_dict(malformed)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.invalid",
        "https://user@example.invalid",
        "https://example.invalid/",
        "https://example.invalid/path",
        "https://example.invalid?query=yes",
        "https://example.invalid#fragment",
        "https://example.invalid:0",
        "https://example.invalid:65536",
        "https://-bad.invalid",
        "https://bad-.invalid",
        "https://bad..invalid",
        "https://EXAMPLE.invalid",
    ],
)
def test_exact_https_origins_reject_malformed_or_noncanonical_values(endpoint):
    with pytest.raises(ValueError, match="HTTPS origin"):
        RangeNetworkV1("isolated", (endpoint,))


def test_transition_matrix_has_exact_states_owners_predecessors_and_terminality():
    assert set(RANGE_TRANSITIONS) == RANGE_STATUSES
    assert RANGE_TRANSITIONS["requested"].owner == "requester"
    assert RANGE_TRANSITIONS["requested"].allowed_predecessors == (None,)
    assert RANGE_TRANSITIONS["in-use"].owner == "scheduler"
    assert RANGE_TRANSITIONS["expired"].owner == "clock"
    assert RANGE_TRANSITIONS["destroyed"].terminal is True
    assert all(
        not rule.terminal for state, rule in RANGE_TRANSITIONS.items()
        if state != "destroyed"
    )
    require_range_transition("ready", "in-use", "scheduler")
    require_range_transition("in-use", "resetting", "adapter")
    require_range_transition("expired", "destroyed", "adapter")
    with pytest.raises(RangeStateError):
        require_range_transition("ready", "destroyed", "scheduler")
    with pytest.raises(RangeStateError):
        require_range_transition("destroyed", "ready", "adapter")


def test_two_node_offline_lifecycle_records_outputs_resets_and_destroys():
    template, spec = benign_two_node_fixture()
    adapter = DeterministicFakeRangeAdapter(now=spec.requested_at)
    ready = adapter.provision("provision-1", template, spec)
    assert ready.status == "ready"
    assert ready.range_handle.kind == "range"
    assert [item.node_id for item in ready.node_handles] == ["analyzer", "helper"]
    assert [item.status for item in adapter.history(spec.range_id)] == [
        "requested", "provisioning", "ready",
    ]

    original_handle = _handle(ready)
    output = adapter.execute(_work(ready))
    assert adapter.state(spec.range_id).status == "in-use"
    assert len(adapter.scratch(spec.range_id)) == 1
    assert adapter.scratch(spec.range_id)[0].generation == 1
    assert output.verified is True
    assert output.input_sha256 == ("c" * 64,)
    assert adapter.output(spec.range_id, output.output_id) == output
    assert adapter.outputs(spec.range_id) == (output,)
    assert adapter.evidence(spec.range_id) == (output,)

    reset = adapter.reset("reset-1", spec.range_id)
    assert reset.status == "ready"
    assert reset.generation == 2
    assert adapter.scratch(spec.range_id) == ()
    assert adapter.outputs(spec.range_id) == ()
    assert adapter.evidence(spec.range_id) == (output,)
    with pytest.raises(RangeAccessError, match="range generation"):
        adapter.execute(_work(
            reset, operation_id="stale-handle", handle=original_handle,
        ))

    destroyed = adapter.destroy("destroy-1", spec.range_id)
    assert destroyed.status == "destroyed"
    assert destroyed.range_handle is None and destroyed.node_handles == ()
    assert adapter.destroy("destroy-1", spec.range_id) == destroyed
    assert adapter.destroy("destroy-2", spec.range_id) == destroyed
    with pytest.raises(RangeStateError, match="not ready"):
        adapter.execute(_work(reset, operation_id="work-after-destroy"))


def test_exact_retry_and_checkpoint_restart_do_not_duplicate_ranges_or_operations():
    template, spec = benign_two_node_fixture()
    adapter = DeterministicFakeRangeAdapter(now=spec.requested_at)
    ready = adapter.provision("provision-stable", template, spec)
    output_request = _work(ready, operation_id="work-stable")
    output = adapter.execute(output_request)
    checkpoint = adapter.checkpoint()
    restarted = DeterministicFakeRangeAdapter.from_checkpoint(checkpoint)

    assert restarted.checkpoint() == checkpoint
    assert restarted.provision("provision-stable", template, spec) == ready
    assert restarted.execute(output_request) == output
    assert len(json.loads(restarted.checkpoint())["ranges"]) == 1
    assert len(json.loads(restarted.checkpoint())["operations"]) == 2

    conflict = replace(output_request, output_name="output/different.json")
    with pytest.raises(RangeConflictError, match="operation ID"):
        restarted.execute(conflict)
    conflicting_spec = replace(spec, scope=replace(spec.scope, revision=2))
    with pytest.raises(RangeConflictError, match="range ID"):
        restarted.provision("provision-conflict", template, conflicting_spec)

    reordered_checkpoint = json.dumps(
        json.loads(checkpoint), sort_keys=False, separators=(",", ":"),
    )
    assert DeterministicFakeRangeAdapter.from_checkpoint(reordered_checkpoint).checkpoint() == checkpoint


def test_expiry_cancel_destroy_and_cleanup_are_idempotent_and_reject_new_work():
    template, spec = benign_two_node_fixture()
    adapter = DeterministicFakeRangeAdapter(now=spec.requested_at)
    ready = adapter.provision("provision-expiry", template, spec)
    adapter.advance(3600)
    expired = adapter.expire("expire-1", spec.range_id)
    assert expired.status == "expired"
    assert adapter.expire("expire-1", spec.range_id) == expired
    with pytest.raises(RangeStateError, match="not ready"):
        adapter.execute(_work(ready, operation_id="work-expired"))
    destroyed = adapter.destroy("cleanup-expired", spec.range_id)
    assert destroyed.status == "destroyed"
    assert adapter.destroy("cleanup-expired-again", spec.range_id) == destroyed

    second_template, second_spec = benign_two_node_fixture(range_id="range-cancelled")
    second = DeterministicFakeRangeAdapter(now=second_spec.requested_at)
    cancelled = second.cancel(
        "cancel-1", second.provision("provision-cancel", second_template, second_spec).range_id,
    )
    assert cancelled.status == "destroyed"
    assert cancelled.terminal_reason == "cancelled"
    assert second.cancel("cancel-1", second_spec.range_id) == cancelled
    with pytest.raises(RangeStateError, match="not ready"):
        second.execute(_work(
            ready, operation_id="work-cancelled", range_id=second_spec.range_id,
        ))


def test_lifetime_is_enforced_before_provision_and_across_failed_recovery():
    template, spec = benign_two_node_fixture()
    early = DeterministicFakeRangeAdapter(now="2026-07-13T11:59:59Z")
    with pytest.raises(RangeStateError, match="not started"):
        early.provision("too-early", template, spec)
    assert json.loads(early.checkpoint())["ranges"] == {}

    late = DeterministicFakeRangeAdapter(now=spec.expires_at)
    expired = late.provision("already-expired", template, spec)
    assert expired.status == "expired"
    assert "ready" not in [item.status for item in late.history(spec.range_id)]

    failed_template, failed_spec = benign_two_node_fixture(range_id="range-failed-expiry")
    failed = DeterministicFakeRangeAdapter(now=failed_spec.requested_at)
    failed_ready = failed.provision("provision-failed-expiry", failed_template, failed_spec)
    failed.inject_failure("in-use")
    with pytest.raises(InjectedRangeFailure):
        failed.execute(_work(failed_ready, operation_id="fail-before-expiry"))
    failed.advance(3600)
    with pytest.raises(RangeStateError, match="cannot reset an expired"):
        failed.reset("expired-recovery", failed_spec.range_id)
    assert failed.state(failed_spec.range_id).status == "expired"


@pytest.mark.parametrize("transition", ["requested", "provisioning", "ready"])
def test_provision_transition_failures_survive_restart_without_false_ready(transition):
    template, spec = benign_two_node_fixture(range_id=f"range-fail-{transition}")
    adapter = DeterministicFakeRangeAdapter(now=spec.requested_at)
    adapter.inject_failure(transition)
    with pytest.raises(InjectedRangeFailure, match=transition):
        adapter.provision("provision-fails", template, spec)
    assert adapter.state(spec.range_id).status == "failed"
    assert "ready" not in [item.status for item in adapter.history(spec.range_id)]

    restarted = DeterministicFakeRangeAdapter.from_checkpoint(adapter.checkpoint())
    with pytest.raises(InjectedRangeFailure, match=transition):
        restarted.provision("provision-fails", template, spec)
    assert len(json.loads(restarted.checkpoint())["ranges"]) == 1
    recovered = restarted.reset("recover-provision", spec.range_id)
    assert recovered.status == "ready"


@pytest.mark.parametrize("transition", ["in-use", "resetting", "destroyed", "expired"])
def test_runtime_transition_failures_are_checkpointed_and_recoverable(transition):
    template, spec = benign_two_node_fixture(range_id=f"range-fail-{transition}")
    adapter = DeterministicFakeRangeAdapter(now=spec.requested_at)
    ready = adapter.provision("provision-ok", template, spec)
    if transition == "resetting":
        adapter.execute(_work(ready, operation_id="work-before-reset"))
    if transition == "expired":
        adapter.advance(3600)
    adapter.inject_failure(transition)
    operation_id = f"inject-{transition}"
    with pytest.raises(InjectedRangeFailure, match=transition):
        if transition == "in-use":
            adapter.execute(_work(ready, operation_id=operation_id))
        elif transition == "resetting":
            adapter.reset(operation_id, spec.range_id)
        elif transition == "destroyed":
            adapter.destroy(operation_id, spec.range_id)
        else:
            adapter.expire(operation_id, spec.range_id)
    assert adapter.state(spec.range_id).status == "failed"
    assert adapter.state(spec.range_id).failure.transition == transition

    restarted = DeterministicFakeRangeAdapter.from_checkpoint(adapter.checkpoint())
    with pytest.raises(InjectedRangeFailure, match=transition):
        if transition == "in-use":
            restarted.execute(_work(ready, operation_id=operation_id))
        elif transition == "resetting":
            restarted.reset(operation_id, spec.range_id)
        elif transition == "destroyed":
            restarted.destroy(operation_id, spec.range_id)
        else:
            restarted.expire(operation_id, spec.range_id)
    recovered = (
        restarted.destroy("recover-destroy", spec.range_id)
        if transition in {"destroyed", "expired"}
        else restarted.reset("recover-reset", spec.range_id)
    )
    assert recovered.status in {"ready", "destroyed"}


def test_cross_lease_handles_inputs_outputs_and_undeclared_intents_fail_closed():
    template_a, spec_a = benign_two_node_fixture(range_id="range-a")
    template_b, spec_b = benign_two_node_fixture(range_id="range-b")
    spec_b = replace(
        spec_b,
        inputs=(replace(spec_b.inputs[0], input_id="other", mount_path="input/other.bin"),),
        scope=replace(spec_b.scope, input_ids=("other",)),
    )
    adapter = DeterministicFakeRangeAdapter(now=spec_a.requested_at)
    ready_a = adapter.provision("provision-a", template_a, spec_a)
    ready_b = adapter.provision("provision-b", template_b, spec_b)

    with pytest.raises(RangeAccessError, match="node handle"):
        adapter.execute(_work(
            ready_a, operation_id="cross-handle", handle=_handle(ready_b),
        ))
    with pytest.raises(RangeAccessError, match="input outside"):
        adapter.execute(_work(
            ready_a, operation_id="cross-input", input_ids=("other",),
            intent=RangeWorkIntentV1(input_mounts=("other",)),
        ))
    with pytest.raises(RangeAccessError, match="network intent"):
        adapter.execute(_work(
            ready_a, operation_id="undeclared-network",
            intent=RangeWorkIntentV1(
                network_endpoints=("https://egress.invalid:443",), input_mounts=("sample",),
            ),
        ))
    with pytest.raises(RangeAccessError, match="credential intent"):
        adapter.execute(_work(
            ready_a, operation_id="undeclared-credential",
            intent=RangeWorkIntentV1(
                credential_refs=("credential:lab",), input_mounts=("sample",),
            ),
        ))
    with pytest.raises(ValueError, match="exactly match"):
        replace(_work(ready_a, operation_id="undeclared-mount"), intent=RangeWorkIntentV1())

    output_b = adapter.execute(_work(
        ready_b, operation_id="work-b", input_ids=("other",),
        intent=RangeWorkIntentV1(input_mounts=("other",)),
    ))
    with pytest.raises(RangeAccessError, match="not owned"):
        adapter.output(spec_a.range_id, output_b.output_id)


def test_scope_derived_exact_exceptions_are_accepted_as_inert_intent_only():
    template, base = benign_two_node_fixture(range_id="range-scoped-exception")
    endpoint = "https://updates.invalid:443"
    credential = "credential:range-readonly"
    scope = RangeScopeV1(
        "scope-exception", 7,
        ("mount_input", "network_access", "credential_use"),
        (endpoint,), (credential,), ("sample",),
    )
    spec = replace(
        base, scope=scope, network=RangeNetworkV1("isolated", (endpoint,)),
    )
    adapter = DeterministicFakeRangeAdapter(now=spec.requested_at)
    ready = adapter.provision("provision-exception", template, spec)
    output = adapter.execute(_work(
        ready,
        operation_id="bounded-exception",
        intent=RangeWorkIntentV1((endpoint,), (credential,), ("sample",)),
    ))
    assert output.verified
    checkpoint = adapter.checkpoint()
    assert endpoint in checkpoint and credential in checkpoint
    assert "private-model" not in checkpoint and "api_key" not in checkpoint


def test_checkpoint_rejects_unknown_fields_and_inconsistent_range_identity():
    template, spec = benign_two_node_fixture()
    adapter = DeterministicFakeRangeAdapter(now=spec.requested_at)
    adapter.provision("provision", template, spec)
    checkpoint = json.loads(adapter.checkpoint())
    checkpoint["unknown"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        DeterministicFakeRangeAdapter.from_checkpoint(json.dumps(checkpoint))

    checkpoint = json.loads(adapter.checkpoint())
    record = checkpoint["ranges"].pop(spec.range_id)
    checkpoint["ranges"]["different-range-key"] = record
    with pytest.raises(ValueError, match="range identity"):
        DeterministicFakeRangeAdapter.from_checkpoint(json.dumps(checkpoint))


def test_fake_enforces_scratch_output_and_work_item_ceilings():
    template, base = benign_two_node_fixture(range_id="range-small-ceilings")
    spec = replace(
        base,
        resources=replace(
            base.resources, max_scratch_bytes=1, max_output_bytes=1, max_work_items=1,
        ),
    )
    adapter = DeterministicFakeRangeAdapter(now=spec.requested_at)
    ready = adapter.provision("provision-small", template, spec)
    with pytest.raises(RangeStateError, match="scratch ceiling"):
        adapter.execute(_work(ready, operation_id="too-large"))
    assert adapter.scratch(spec.range_id) == ()
    assert adapter.outputs(spec.range_id) == ()

    normal_template, normal_spec = benign_two_node_fixture(range_id="range-one-work")
    normal_spec = replace(
        normal_spec, resources=replace(normal_spec.resources, max_work_items=1),
    )
    normal = DeterministicFakeRangeAdapter(now=normal_spec.requested_at)
    normal_ready = normal.provision("provision-one", normal_template, normal_spec)
    normal.execute(_work(normal_ready, operation_id="only-work"))
    normal_ready = normal.reset("reset-one", normal_spec.range_id)
    with pytest.raises(RangeStateError, match="work-item ceiling"):
        normal.execute(_work(normal_ready, operation_id="extra-work"))


def test_checkpoint_operation_requests_are_persisted_and_bound_to_results():
    template, spec = benign_two_node_fixture()
    adapter = DeterministicFakeRangeAdapter(now=spec.requested_at)
    ready = adapter.provision("provision-bound", template, spec)
    output = adapter.execute(_work(ready, operation_id="work-bound"))
    checkpoint = json.loads(adapter.checkpoint())
    operation = checkpoint["operations"]["work-bound"]
    assert operation["request"]["kind"] == "execute"
    assert operation["request"]["operation_id"] == "work-bound"

    forged = json.loads(adapter.checkpoint())
    for record in (
        forged["operations"]["work-bound"]["result"],
        forged["ranges"][spec.range_id]["outputs"][output.output_id],
        forged["ranges"][spec.range_id]["evidence"][output.output_id],
    ):
        record["logical_path"] = "output/forged.json"
    with pytest.raises(ValueError, match="not bound to its request"):
        DeterministicFakeRangeAdapter.from_checkpoint(json.dumps(forged))

    forged = json.loads(adapter.checkpoint())
    envelope = forged["operations"]["work-bound"]["request"]
    envelope["request"]["range_id"] = "range-other"
    forged["operations"]["work-bound"]["request_sha256"] = canonical_sha256(envelope)
    with pytest.raises(ValueError, match="outside its request range"):
        DeterministicFakeRangeAdapter.from_checkpoint(json.dumps(forged))


def test_checkpoint_rejects_generation_handle_and_artifact_forgery():
    template, spec = benign_two_node_fixture()
    adapter = DeterministicFakeRangeAdapter(now=spec.requested_at)
    ready = adapter.provision("provision-forgery", template, spec)
    output = adapter.execute(_work(ready, operation_id="work-forgery"))
    adapter.reset("reset-forgery", spec.range_id)

    forged = json.loads(adapter.checkpoint())
    record = forged["ranges"][spec.range_id]
    record["state"]["generation"] = 1
    record["history"][-1]["generation"] = 1
    with pytest.raises(ValueError, match="generation does not match reset history"):
        DeterministicFakeRangeAdapter.from_checkpoint(json.dumps(forged))

    forged = json.loads(adapter.checkpoint())
    record = forged["ranges"][spec.range_id]
    record["state"]["range_handle"]["opaque_id"] = "fake-range:forged"
    record["history"][-1]["range_handle"]["opaque_id"] = "fake-range:forged"
    with pytest.raises(ValueError, match="invalid deterministic handles"):
        DeterministicFakeRangeAdapter.from_checkpoint(json.dumps(forged))

    active = DeterministicFakeRangeAdapter(now=spec.requested_at)
    ready = active.provision("provision-artifact", template, spec)
    output = active.execute(_work(ready, operation_id="work-artifact"))
    forged = json.loads(active.checkpoint())
    record = forged["ranges"][spec.range_id]
    for value in (
        forged["operations"]["work-artifact"]["result"],
        record["scratch"][next(iter(record["scratch"]))],
        record["outputs"][output.output_id], record["evidence"][output.output_id],
    ):
        value["sha256"] = "f" * 64
    with pytest.raises(ValueError, match="not bound to its request"):
        DeterministicFakeRangeAdapter.from_checkpoint(json.dumps(forged))


def test_checkpoint_requires_utf8_unique_keys_and_nonempty_credential_refs():
    template, spec = benign_two_node_fixture()
    adapter = DeterministicFakeRangeAdapter(now=spec.requested_at)
    adapter.provision("provision-json", template, spec)
    checkpoint = adapter.checkpoint()
    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        DeterministicFakeRangeAdapter.from_checkpoint(checkpoint.encode("utf-16"))
    duplicate = checkpoint.replace(
        '"schema_version":1', '"schema_version":1,"schema_version":1', 1,
    )
    with pytest.raises(ValueError, match="duplicate JSON key"):
        DeterministicFakeRangeAdapter.from_checkpoint(duplicate)
    with pytest.raises(ValueError, match="credential: references"):
        RangeScopeV1("scope", 1, ("credential_use",), (), ("credential:",), ())
    with pytest.raises(ValueError, match="credential: references"):
        RangeWorkIntentV1(credential_refs=("credential:",))
