from __future__ import annotations

from dataclasses import replace
import json

import pytest

from rekit_factory.notification_delivery import (
    DesktopChannel,
    InvalidDeliveryConfiguration,
    WebhookChannel,
    build_test_webhook_request,
    build_webhook_request,
    channel_test_preview,
    deliver_desktop,
    deliver_test_desktop,
    deliver_webhook,
    delivery_preview,
)
from rekit_factory.notification_policy import notification_candidates
from rekit_factory.outcomes import project_outcomes


def _record():
    common = {"workers": (), "work_items": (), "dossiers": ()}
    old = project_outcomes(
        run={"id": "run-1", "status": "running"}, memory={}, pending_questions=(), **common,
    )
    new = project_outcomes(
        run={"id": "run-1", "status": "running"}, memory={},
        pending_questions=[{"id": "question-1", "prompt": "/Users/private/secret"}], **common,
    )
    candidate = notification_candidates(old, new)[0]
    return {
        "id": "notification-" + candidate["dedupeKey"].removeprefix("sha256:"),
        "payload": {
            "schemaVersion": 1,
            "policyVersion": candidate["policyVersion"],
            "dedupeKey": candidate["dedupeKey"],
            "kind": candidate["kind"],
            "severity": candidate["severity"],
            "message": candidate["message"],
            "deepLink": {
                "view": "mission-control", "runId": candidate["runId"], "tab": "decisions",
                "entityType": candidate["entity"]["entityType"],
                "entityId": candidate["entity"]["entityId"],
            },
        },
    }


class FakeResolver:
    def __init__(self, result="super-secret-token", error=None):
        self.result = result
        self.error = error
        self.refs = []

    def resolve(self, credential_ref):
        self.refs.append(credential_ref)
        if self.error:
            raise self.error
        return self.result


class FakeWebhookTransport:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def send(self, request, *, bearer_token):
        self.calls.append((request, bearer_token))
        if self.error:
            raise self.error


class FakeDesktopTransport:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def notify(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error


def test_webhook_request_has_exact_idempotency_and_no_credential_material():
    record = _record()
    channel = WebhookChannel(
        "webhook-primary", "https://hooks.example.test/factory?format=v1",
        "credential:notifications/primary",
    )
    request = build_webhook_request(channel, record)

    assert request.method == "POST"
    assert request.headers["Idempotency-Key"] == record["payload"]["dedupeKey"]
    assert "Authorization" not in request.headers
    body = json.loads(request.body)
    assert body["idempotencyKey"] == record["payload"]["dedupeKey"]
    assert body["message"] == "Operator decision is waiting in Mission Control."
    serialized = repr(request) + request.body.decode()
    assert "credential:" not in serialized
    assert "super-secret" not in serialized
    assert "/Users/private" not in serialized


def test_delivery_rejects_forged_notification_identity():
    record = _record()
    record["id"] = "notification-" + "0" * 64
    channel = WebhookChannel("webhook-1", "https://example.test/hook", "credential:one")
    with pytest.raises(InvalidDeliveryConfiguration, match="idempotency identity"):
        build_webhook_request(channel, record)


def test_delivery_rejects_cross_kind_entity_and_extra_payload_fields():
    channel = WebhookChannel("webhook-1", "https://example.test/hook", "credential:one")
    crossed = _record()
    crossed["payload"]["deepLink"]["entityType"] = "finding"
    crossed["payload"]["deepLink"]["tab"] = "findings"
    with pytest.raises(InvalidDeliveryConfiguration, match="inconsistent"):
        build_webhook_request(channel, crossed)

    extra = _record()
    extra["payload"]["rawText"] = "/Users/private/secret"
    with pytest.raises(InvalidDeliveryConfiguration, match="payload is invalid"):
        build_webhook_request(channel, extra)


def test_webhook_delivery_resolves_transient_secret_and_returns_only_bounded_results():
    channel = WebhookChannel(
        "webhook-primary", "https://hooks.example.test/factory", "credential:primary",
    )
    request = build_webhook_request(channel, _record())
    resolver = FakeResolver()
    transport = FakeWebhookTransport()

    assert deliver_webhook(channel, request, resolver, transport).sent is True
    assert resolver.refs == ["credential:primary"]
    assert transport.calls == [(request, "super-secret-token")]

    hostile = RuntimeError("token=super-secret-token /Users/private/key https://internal")
    unavailable = deliver_webhook(
        channel, request, FakeResolver(error=hostile), FakeWebhookTransport())
    failed = deliver_webhook(
        channel, request, FakeResolver(), FakeWebhookTransport(error=hostile))
    assert unavailable.error_code == "credential-unavailable"
    assert failed.error_code == "transport-failed"
    assert "secret" not in repr(unavailable) + repr(failed)


def test_webhook_rejects_cross_channel_or_forged_request_before_resolving_secret():
    first = WebhookChannel("webhook-1", "https://one.example.test/hook", "credential:one")
    second = WebhookChannel("webhook-2", "https://two.example.test/hook", "credential:two")
    request = build_webhook_request(first, _record())
    resolver = FakeResolver()
    transport = FakeWebhookTransport()

    crossed = deliver_webhook(second, request, resolver, transport)
    forged = deliver_webhook(
        first, replace(request, headers={**request.headers, "Idempotency-Key": "sha256:" + "0" * 64}),
        resolver, transport,
    )
    assert crossed.error_code == "request-invalid"
    assert forged.error_code == "request-invalid"
    assert resolver.refs == []
    assert transport.calls == []


def test_delivery_does_not_swallow_process_control_exceptions():
    channel = WebhookChannel("webhook-1", "https://example.test/hook", "credential:one")
    request = build_webhook_request(channel, _record())
    with pytest.raises(KeyboardInterrupt):
        deliver_webhook(
            channel, request, FakeResolver(error=KeyboardInterrupt()), FakeWebhookTransport())

    desktop = FakeDesktopTransport(error=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        deliver_desktop(DesktopChannel("desktop-local"), _record(), desktop)


@pytest.mark.parametrize("secret", ["", "abc\r\nInjected: yes", "x" * 8193, b"bytes"])
def test_webhook_rejects_invalid_resolved_secrets_without_calling_transport(secret):
    channel = WebhookChannel("webhook-1", "https://example.test/hook", "credential:one")
    transport = FakeWebhookTransport()
    result = deliver_webhook(
        channel, build_webhook_request(channel, _record()), FakeResolver(secret), transport)
    assert result.error_code == "credential-invalid"
    assert transport.calls == []


@pytest.mark.parametrize("endpoint", [
    "http://hooks.example.test/x",
    "https://user:secret@hooks.example.test/x",
    "https://hooks.example.test/x#credential:secret",
    "file:///Users/private/key",
])
def test_webhook_channel_rejects_unsafe_endpoint_shapes(endpoint):
    with pytest.raises(InvalidDeliveryConfiguration):
        WebhookChannel("webhook-1", endpoint, "credential:one")


@pytest.mark.parametrize("credential_ref", [
    "secret-token", "credential:", "credential:bad ref", "credential:\nprivate",
])
def test_webhook_channel_accepts_only_opaque_credential_references(credential_ref):
    with pytest.raises(InvalidDeliveryConfiguration, match="opaque"):
        WebhookChannel("webhook-1", "https://hooks.example.test/x", credential_ref)


def test_preview_and_test_channel_are_fixed_bounded_and_redacted():
    preview = delivery_preview(_record())
    test = channel_test_preview("webhook-primary", "attempt-7")

    assert set(preview) == {
        "kind", "severity", "title", "message", "deepLink", "idempotencyKey",
    }
    assert test["kind"] == "channel.test"
    assert test["message"] == (
        "Test notification from Mission Control. No investigation content is included.")
    assert test == channel_test_preview("webhook-primary", "attempt-7")
    assert test != channel_test_preview("webhook-primary", "attempt-8")
    serialized = json.dumps({"preview": preview, "test": test})
    assert "/Users/private" not in serialized
    assert "credential:" not in serialized
    assert len(serialized) < 1400


def test_test_webhook_body_matches_preview_and_uses_stable_test_idempotency():
    channel = WebhookChannel("webhook-primary", "https://example.test/x", "credential:one")
    request = build_test_webhook_request(channel, "attempt-7")
    body = json.loads(request.body)
    preview = channel_test_preview(channel.channel_id, "attempt-7")
    assert request.headers["Idempotency-Key"] == preview["idempotencyKey"]
    assert {key: body[key] for key in preview} == preview


def test_desktop_delivery_is_best_effort_and_never_forwards_entity_identifiers():
    channel = DesktopChannel("desktop-local")
    transport = FakeDesktopTransport()
    record = _record()
    result = deliver_desktop(channel, record, transport)

    assert result.sent is True
    assert transport.calls == [{
        "title": "Rekit Factory needs you",
        "message": "Operator decision is waiting in Mission Control.",
        "deep_link": "rekit-factory://mission-control",
        "idempotency_key": record["payload"]["dedupeKey"],
    }]
    error = RuntimeError("/Users/private/key token=secret")
    failed = deliver_desktop(channel, record, FakeDesktopTransport(error))
    assert failed.error_code == "transport-failed"
    assert "private" not in repr(failed)


def test_desktop_test_delivery_is_fixed_and_stable():
    channel = DesktopChannel("desktop-local")
    first = FakeDesktopTransport()
    second = FakeDesktopTransport()
    assert deliver_test_desktop(channel, "attempt-1", first).sent is True
    assert deliver_test_desktop(channel, "attempt-1", second).sent is True
    assert first.calls == second.calls
    assert first.calls[0]["message"] == (
        "Test notification from Mission Control. No investigation content is included.")
