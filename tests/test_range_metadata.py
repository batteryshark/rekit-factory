from __future__ import annotations

from dataclasses import replace
import json

import pytest

from rekit_factory.range_attachments import (
    RangeAttachmentPolicyV1, RangeAttachmentRequestV1, authorize_range_attachment,
)
from rekit_factory.range_metadata import (
    MAX_ATTACHMENTS,
    RangeNodeRuntimeV1,
    RangeToolIdentityV1,
    bind_range_execution_identity,
    project_range_health,
    range_benchmark_comparison_key,
    range_proof_metadata,
)
from rekit_factory.ranges import DeterministicFakeRangeAdapter, benign_two_node_fixture


def _fixture():
    template, spec = benign_two_node_fixture(range_id="range-metadata")
    adapter = DeterministicFakeRangeAdapter(now=spec.requested_at)
    lease = adapter.provision("provision-metadata", template, spec)
    nodes = tuple(RangeNodeRuntimeV1(
        node.node_id, node.image_sha256, ("d" if node.node_id == "analyzer" else "e") * 64,
        (RangeToolIdentityV1("rekit", "v1.2.3", "f" * 64),),
    ) for node in template.nodes)
    identity = bind_range_execution_identity(
        template, spec, lease, adapter_id="adapter-local",
        adapter_version="v1", nodes=nodes,
    )
    policy = RangeAttachmentPolicyV1(
        1, "observer", spec.scope.digest, "1" * 64,
        ("analyzer",), ("observe-console",), 60,
    )
    handle = next(item.handle for item in lease.node_handles if item.node_id == "analyzer")
    request = RangeAttachmentRequestV1(
        1, "attach-metadata", spec.range_id, "analyzer", handle,
        "observe-console", policy.digest, "operator-1", spec.requested_at,
    )
    audit = authorize_range_attachment(request, policy, spec, lease, now=spec.requested_at)
    return template, spec, lease, identity, audit


def test_one_identity_binds_proof_metadata_benchmark_key_and_health_projection():
    template, spec, lease, identity, audit = _fixture()
    proof = range_proof_metadata(identity)
    health = project_range_health(
        template, spec, lease, identity=identity, attachments=(audit,),
    )
    assert proof["rangeExecutionSha256"] == health["executionSha256"] == identity.digest
    assert health["benchmarkComparisonKey"] == range_benchmark_comparison_key(identity)
    assert proof["topologySha256"] == health["topologySha256"]
    assert health["attachments"][0] == {
        "auditId": audit.audit_id, "nodeId": "analyzer", "action": "observe-console",
        "requestedBy": "operator-1", "disposition": "allowed",
        "reasonCode": "authorized", "createdAt": spec.requested_at,
        "expiresAt": "2026-07-13T12:01:00Z",
    }


def test_environment_image_tool_and_topology_changes_change_comparison_identity():
    template, spec, lease, identity, _ = _fixture()
    changed_node = replace(identity.nodes[0], environment_sha256="9" * 64)
    changed = bind_range_execution_identity(
        template, spec, lease, adapter_id=identity.adapter_id,
        adapter_version=identity.adapter_version,
        nodes=(changed_node,) + identity.nodes[1:],
    )
    assert changed.digest != identity.digest
    assert range_benchmark_comparison_key(changed) != range_benchmark_comparison_key(identity)
    with pytest.raises(ValueError, match="image identities"):
        bind_range_execution_identity(
            template, spec, lease, adapter_id="adapter-local", adapter_version="v1",
            nodes=(replace(identity.nodes[0], image_sha256="8" * 64),) + identity.nodes[1:],
        )


def test_health_projection_is_bounded_redacted_and_has_no_handles_or_provider_secrets():
    template, spec, lease, identity, audit = _fixture()
    projection = project_range_health(
        template, spec, lease, identity=identity, attachments=(audit,),
    )
    encoded = json.dumps(projection, sort_keys=True)
    for forbidden in (
        "opaque_id", "fake-node:", "credential:", "password", "/Users/", "/private/",
        "providerEndpoint", "failure reason",
    ):
        assert forbidden not in encoded
    assert len(projection["nodes"]) == 2
    with pytest.raises(ValueError, match="unbounded"):
        project_range_health(
            template, spec, lease, identity=identity,
            attachments=(audit,) * (MAX_ATTACHMENTS + 1),
        )
    with pytest.raises(ValueError, match="stable identifier"):
        replace(identity, adapter_id="credential:provider-secret")


def test_cross_range_generation_and_runtime_forgery_fail_closed():
    template, spec, lease, identity, audit = _fixture()
    with pytest.raises(ValueError, match="another lease generation"):
        project_range_health(
            template, spec, lease, identity=identity,
            attachments=(replace(audit, range_id="range-other"),),
        )
    with pytest.raises(ValueError, match="conflicts"):
        project_range_health(
            template, spec, lease, identity=replace(identity, spec_sha256="8" * 64),
        )
    with pytest.raises(ValueError, match="active exact lease"):
        bind_range_execution_identity(
            template, spec, replace(lease, status="destroyed", range_handle=None,
                                    node_handles=(), terminal_reason="destroyed"),
            adapter_id="adapter-local", adapter_version="v1", nodes=identity.nodes,
        )
