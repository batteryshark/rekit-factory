from __future__ import annotations

from dataclasses import replace

import pytest

from rekit_factory.range_attachments import (
    RangeAttachmentPolicyV1,
    RangeAttachmentRequestV1,
    authorize_range_attachment,
)
from rekit_factory.ranges import DeterministicFakeRangeAdapter, benign_two_node_fixture


def _fixture():
    template, spec = benign_two_node_fixture(range_id="range-attach")
    adapter = DeterministicFakeRangeAdapter(now=spec.requested_at)
    lease = adapter.provision("provision-attach", template, spec)
    handles = {item.node_id: item.handle for item in lease.node_handles}
    policy = RangeAttachmentPolicyV1(
        1, "read-only-observer-v1", spec.scope.digest, "d" * 64,
        ("analyzer",), ("observe-console", "capture-screen"), 300,
    )
    request = RangeAttachmentRequestV1(
        1, "attach-1", spec.range_id, "analyzer", handles["analyzer"],
        "observe-console", policy.digest, "operator-1", spec.requested_at,
    )
    return template, spec, adapter, lease, policy, request


def test_exact_ready_lease_produces_bounded_restart_stable_authorization():
    _, spec, adapter, lease, policy, request = _fixture()
    audit = authorize_range_attachment(request, policy, spec, lease, now=spec.requested_at)
    assert audit.disposition == "allowed"
    assert audit.reason_code == "authorized"
    assert audit.expires_at == "2026-07-13T12:05:00Z"
    assert audit.request_sha256 == request.digest

    restarted = DeterministicFakeRangeAdapter.from_checkpoint(adapter.checkpoint())
    assert authorize_range_attachment(
        request, policy, spec, restarted.state(spec.range_id), now=spec.requested_at,
    ) == audit


@pytest.mark.parametrize("mutation,reason", [
    ("policy", "policy-mismatch"),
    ("scope", "scope-mismatch"),
    ("node", "node-mismatch"),
    ("action", "action-denied"),
])
def test_policy_scope_node_and_action_mismatches_fail_closed(mutation, reason):
    _, spec, _, lease, policy, request = _fixture()
    if mutation == "policy":
        request = replace(request, policy_sha256="e" * 64)
    elif mutation == "scope":
        policy = replace(policy, scope_sha256="e" * 64)
        request = replace(request, policy_sha256=policy.digest)
    elif mutation == "node":
        request = replace(request, node_id="helper")
    else:
        policy = replace(policy, allowed_actions=("capture-screen",))
        request = replace(request, policy_sha256=policy.digest)
    audit = authorize_range_attachment(request, policy, spec, lease, now=spec.requested_at)
    assert (audit.disposition, audit.reason_code, audit.expires_at) == (
        "denied", reason, None,
    )


def test_expired_destroyed_and_cross_lease_handles_never_authorize():
    template, spec, adapter, lease, policy, request = _fixture()
    destroyed = adapter.destroy("destroy-attach", spec.range_id, reason="test cleanup")
    denied = authorize_range_attachment(
        request, policy, spec, destroyed, now=spec.requested_at,
    )
    assert denied.reason_code == "lease-unavailable"

    other_template, other_spec = benign_two_node_fixture(range_id="range-other")
    other = DeterministicFakeRangeAdapter(now=other_spec.requested_at)
    other_lease = other.provision("provision-other", other_template, other_spec)
    forged = replace(request, node_handle=other_lease.node_handles[0].handle)
    assert authorize_range_attachment(
        forged, policy, spec, lease, now=spec.requested_at,
    ).reason_code == "node-mismatch"
    assert authorize_range_attachment(
        request, policy, spec, lease, now=spec.expires_at,
    ).reason_code == "expired"


def test_contracts_have_no_provider_credentials_paths_or_interactive_input_fields():
    _, _, _, _, policy, request = _fixture()
    encoded = str({"policy": policy.to_dict(), "request": request.to_dict()})
    for forbidden in (
        "credential", "password", "endpoint", "host_path", "/Users/", "/private/",
        "keyboard", "command", "stdin",
    ):
        assert forbidden not in encoded
    hostile = request.to_dict()
    hostile["command"] = "type arbitrary provider input"
    with pytest.raises(ValueError, match="exactly"):
        RangeAttachmentRequestV1.from_dict(hostile)


def test_strict_round_trip_and_policy_bounds():
    _, _, _, _, policy, request = _fixture()
    assert RangeAttachmentPolicyV1.from_dict(policy.to_dict()) == policy
    assert RangeAttachmentRequestV1.from_dict(request.to_dict()) == request
    with pytest.raises(ValueError, match="read-only"):
        replace(policy, allowed_actions=("interactive-shell",))
    with pytest.raises(ValueError, match="between 1 and 3600"):
        replace(policy, max_session_seconds=3601)
