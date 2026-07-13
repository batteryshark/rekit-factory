from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from rekit_factory.notification_outbox import (
    InvalidNotificationCandidate,
    NotificationOutbox,
    NotificationStateConflict,
)
from rekit_factory.notification_policy import notification_candidates
from rekit_factory.outcomes import project_outcomes
from rekit_factory.store import FactoryLedger


class FakeClock:
    def __init__(self):
        self.value = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds: int):
        self.value += timedelta(seconds=seconds)


def _candidate(kind="operator-decision.waiting"):
    common = {"workers": (), "work_items": (), "dossiers": ()}
    old = project_outcomes(
        run={"id": "run-1", "status": "running"}, memory={}, pending_questions=(), **common,
    )
    if kind == "operator-decision.waiting":
        new = project_outcomes(
            run={"id": "run-1", "status": "running"},
            pending_questions=[{"id": "question-1", "prompt": "/private/raw prose"}],
            memory={}, **common,
        )
    else:
        status = "reproduced" if kind in {"finding.reproduced", "finding.accepted"} \
            else "candidate"
        memory = {"findings": {"finding-1": {"id": "finding-1", "status": status}}}
        if kind == "finding.accepted":
            memory["finding_operator_decisions"] = {
                "decision-1": {"id": "decision-1", "findingId": "finding-1",
                               "decision": "accepted", "_eventSeq": 1},
            }
        new = project_outcomes(
            run={"id": "run-1", "status": "running"}, memory=memory,
            pending_questions=(), **common,
        )
    return next(item for item in notification_candidates(old, new) if item["kind"] == kind)


def _transition(kind="operator-decision.waiting"):
    common = {"workers": (), "work_items": (), "dossiers": ()}
    old = project_outcomes(
        run={"id": "run-1", "status": "running"}, memory={}, pending_questions=(), **common,
    )
    if kind == "operator-decision.waiting":
        new = project_outcomes(
            run={"id": "run-1", "status": "running"}, memory={},
            pending_questions=[{"id": "question-1", "prompt": "private prose"}], **common,
        )
    else:
        new = project_outcomes(
            run={"id": "run-1", "status": "running"}, pending_questions=(),
            memory={"findings": {
                "finding-1": {"id": "finding-1", "status": "reproduced"},
            }}, **common,
        )
    return old, new


@pytest.fixture
def outbox(tmp_path):
    ledger = FactoryLedger(tmp_path / "factory.sqlite")
    clock = FakeClock()
    yield NotificationOutbox(
        ledger.conn, clock=clock, max_attempts=3, base_backoff_seconds=10,
        max_backoff_seconds=15, lease_seconds=30,
    ), clock, ledger
    ledger.close()


def test_batch_admission_is_atomic_deduplicated_and_payload_is_redacted(outbox):
    box, _, ledger = outbox
    candidates = [_candidate(), _candidate("finding.reproduced")]
    ids = box.admit(candidates)

    assert box.admit(candidates) == ids
    assert len(set(ids)) == 2
    record = box.get(ids[0])
    assert record["status"] == "queued"
    assert record["payload"]["deepLink"] == {
        "view": "mission-control", "runId": "run-1", "tab": "decisions",
        "entityType": "operator-decision", "entityId": "question-1",
    }
    assert "/private" not in json.dumps(record)
    assert ledger.conn.execute("select count(*) from factory_notification_outbox").fetchone()[0] == 2

    invalid = dict(_candidate("finding.accepted"), message="private model text")
    with pytest.raises(InvalidNotificationCandidate, match="canonical redacted"):
        box.admit([_candidate("finding.accepted"), invalid])
    assert ledger.conn.execute("select count(*) from factory_notification_outbox").fetchone()[0] == 2


def test_exact_proof_link_is_preserved_as_a_dossier_deep_link(outbox):
    box, _, _ = outbox
    old = project_outcomes(
        run={"id": "run-1", "status": "running"}, workers=(), work_items=(),
        memory={"findings": {"finding-1": {"id": "finding-1", "status": "candidate"}}},
        dossiers=(), pending_questions=(),
    )
    new = project_outcomes(
        run={"id": "run-1", "status": "running"}, workers=(), work_items=(),
        memory={"findings": {"finding-1": {"id": "finding-1", "status": "reproduced"}}},
        dossiers=[{"id": "dossier-1", "findingId": "finding-1",
                   "verificationStatus": "published"}], pending_questions=(),
    )
    [candidate] = notification_candidates(old, new)
    [notification_id] = box.admit([candidate])
    assert box.get(notification_id)["payload"]["deepLink"] == {
        "view": "mission-control", "runId": "run-1", "tab": "dossiers",
        "entityType": "proof-bundle", "entityId": "dossier-1",
    }


def test_admission_composes_with_caller_transaction_and_rolls_back(outbox):
    box, _, ledger = outbox
    ledger.conn.execute("begin")
    notification_id = box.admit([_candidate()])[0]
    assert box.get(notification_id) is not None
    ledger.conn.rollback()
    assert box.get(notification_id) is None


def test_delivery_lifecycle_uses_lease_and_exact_acknowledgement(outbox):
    box, _, _ = outbox
    notification_id = box.admit([_candidate()])[0]
    [delivery] = box.claim_due("sender-1")
    assert delivery["id"] == notification_id
    assert delivery["attemptCount"] == 1
    assert box.claim_due("sender-2") == []
    with pytest.raises(NotificationStateConflict, match="lease"):
        box.record_sent(notification_id, "lease-forged")

    box.record_sent(notification_id, delivery["leaseToken"])
    assert box.get(notification_id)["status"] == "sent"
    box.acknowledge(notification_id)
    box.acknowledge(notification_id)
    assert box.get(notification_id)["status"] == "acknowledged"
    with pytest.raises(NotificationStateConflict, match="terminal"):
        box.supersede(notification_id)


def test_bounded_exponential_retry_is_deterministic(outbox):
    box, clock, _ = outbox
    notification_id = box.admit([_candidate()])[0]

    first = box.claim_due("sender")[0]
    box.record_failed(notification_id, first["leaseToken"], "transport-timeout")
    assert box.claim_due("sender") == []
    clock.advance(9)
    assert box.claim_due("sender") == []
    clock.advance(1)
    second = box.claim_due("sender")[0]
    box.record_failed(notification_id, second["leaseToken"], "transport-timeout")
    clock.advance(14)
    assert box.claim_due("sender") == []
    clock.advance(1)
    third = box.claim_due("sender")[0]
    box.record_failed(notification_id, third["leaseToken"], "transport-timeout")

    record = box.get(notification_id)
    assert record["status"] == "failed"
    assert record["attemptCount"] == 3
    assert record["nextAttemptAt"] is None
    clock.advance(999)
    assert box.claim_due("sender") == []


def test_expired_lease_is_reclaimed_and_supersede_cancels_delivery(outbox):
    box, clock, _ = outbox
    notification_id = box.admit([_candidate()])[0]
    first = box.claim_due("crashed-sender")[0]
    clock.advance(29)
    assert box.claim_due("replacement") == []
    clock.advance(1)
    second = box.claim_due("replacement")[0]
    assert second["attemptCount"] == 2
    with pytest.raises(NotificationStateConflict, match="lease"):
        box.record_sent(notification_id, first["leaseToken"])
    box.supersede(notification_id)
    assert box.get(notification_id)["status"] == "superseded"
    assert box.claim_due("replacement") == []


def test_expired_lease_cannot_report_success_or_failure_before_reclaim(outbox):
    box, clock, _ = outbox
    sent_id, failed_id = box.admit([_candidate(), _candidate("finding.reproduced")])
    deliveries = {item["id"]: item for item in box.claim_due("late-sender")}
    clock.advance(30)

    with pytest.raises(NotificationStateConflict, match="expired"):
        box.record_sent(sent_id, deliveries[sent_id]["leaseToken"])
    with pytest.raises(NotificationStateConflict, match="expired"):
        box.record_failed(
            failed_id, deliveries[failed_id]["leaseToken"], "transport-timeout",
        )
    assert box.get(sent_id)["status"] == "queued"
    assert box.get(failed_id)["status"] == "queued"


def test_final_crashed_attempt_expires_to_terminal_failure(outbox):
    box, clock, _ = outbox
    notification_id = box.admit([_candidate()])[0]
    for attempt in range(3):
        delivery = box.claim_due("crashing-sender")[0]
        assert delivery["attemptCount"] == attempt + 1
        clock.advance(30)
    assert box.claim_due("replacement") == []
    record = box.get(notification_id)
    assert record["status"] == "failed"
    assert record["attemptCount"] == 3
    assert record["lastErrorCode"] == "delivery-lease-expired"


def test_immutable_payload_integrity_fails_closed(outbox):
    box, _, ledger = outbox
    notification_id = box.admit([_candidate()])[0]
    ledger.conn.execute(
        "update factory_notification_outbox set payload_json=? where id=?",
        ('{"message":"forged"}', notification_id),
    )
    ledger.conn.commit()
    with pytest.raises(ValueError, match="integrity"):
        box.get(notification_id)


def test_identity_column_tampering_fails_closed(outbox):
    box, _, ledger = outbox
    notification_id = box.admit([_candidate()])[0]
    ledger.conn.execute(
        "update factory_notification_outbox set entity_id=? where id=?",
        ("question-forged", notification_id),
    )
    ledger.conn.commit()
    with pytest.raises(ValueError, match="identity columns"):
        box.get(notification_id)


def test_outbox_survives_ledger_restart(tmp_path):
    path = tmp_path / "factory.sqlite"
    clock = FakeClock()
    first = FactoryLedger(path)
    notification_id = NotificationOutbox(first.conn, clock=clock).admit([_candidate()])[0]
    first.close()

    reopened = FactoryLedger(path)
    box = NotificationOutbox(reopened.conn, clock=clock)
    assert box.get(notification_id)["payload"]["deepLink"]["entityId"] == "question-1"
    assert box.claim_due("sender")[0]["id"] == notification_id
    reopened.close()


def test_two_connections_cannot_claim_same_delivery(tmp_path):
    path = tmp_path / "factory.sqlite"
    clock = FakeClock()
    setup = FactoryLedger(path)
    notification_id = NotificationOutbox(setup.conn, clock=clock).admit([_candidate()])[0]
    setup.close()
    barrier = Barrier(2)

    def claim(worker_id):
        ledger = FactoryLedger(path)
        try:
            barrier.wait()
            return NotificationOutbox(ledger.conn, clock=clock).claim_due(worker_id)
        finally:
            ledger.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("sender-a", "sender-b")))
    claims = [item for result in results for item in result]
    assert [item["id"] for item in claims] == [notification_id]


def test_canonical_projection_boundary_hydrates_then_admits_exactly_once(tmp_path):
    path = tmp_path / "run.db"
    old, new = _transition()
    first = FactoryLedger(path)
    assert first.admit_notification_projection("run-1", old) == []
    [notification_id] = first.admit_notification_projection("run-1", new)
    assert first.admit_notification_projection("run-1", new) == []
    assert first.conn.execute(
        "select count(*) from factory_notification_outbox",
    ).fetchone()[0] == 1
    first.close()

    reopened = FactoryLedger(path)
    assert reopened.admit_notification_projection("run-1", new) == []
    assert NotificationOutbox(reopened.conn).get(notification_id) is not None
    reopened.close()


def test_projection_and_outbox_rollback_together_at_every_boundary(tmp_path):
    old, new = _transition("finding.reproduced")
    for boundary in ("outbox-admitted", "baseline-advanced"):
        path = tmp_path / f"{boundary}.db"
        ledger = FactoryLedger(path)
        assert ledger.admit_notification_projection("run-1", old) == []

        def crash(observed):
            if observed == boundary:
                raise RuntimeError(f"crash after {boundary}")

        with pytest.raises(RuntimeError, match=boundary):
            ledger.admit_notification_projection(
                "run-1", new, failure_injector=crash,
            )
        assert ledger.conn.execute(
            "select count(*) from factory_notification_outbox",
        ).fetchone()[0] == 0
        baseline = ledger.conn.execute(
            "select semantic_sha256 from factory_notification_projection_state where run_id=?",
            ("run-1",),
        ).fetchone()[0]
        assert baseline == old["semanticSha256"]
        assert len(ledger.admit_notification_projection("run-1", new)) == 1
        ledger.close()


def test_degraded_observation_does_not_erase_last_trusted_baseline(tmp_path):
    old, new = _transition()
    # Changing public content invalidates the semantic identity, so derive a genuinely degraded
    # projection instead of forging one.
    degraded = project_outcomes(
        run={"id": "run-1", "status": "future-unsupported"}, workers=(), work_items=(),
        memory={"findings": {}}, dossiers=(), pending_questions=(),
    )
    ledger = FactoryLedger(tmp_path / "run.db")
    ledger.admit_notification_projection("run-1", old)
    assert ledger.admit_notification_projection("run-1", degraded) == []
    assert len(ledger.admit_notification_projection("run-1", new)) == 1
    ledger.close()


def test_projection_boundary_rejects_cross_run_and_corrupt_baseline_atomically(tmp_path):
    old, new = _transition()
    ledger = FactoryLedger(tmp_path / "run.db")
    with pytest.raises(ValueError, match="exact run"):
        ledger.admit_notification_projection("run-forged", old)
    ledger.admit_notification_projection("run-1", old)
    ledger.conn.execute(
        "update factory_notification_projection_state set projection_json='{}' where run_id=?",
        ("run-1",),
    )
    ledger.conn.commit()
    with pytest.raises(ValueError, match="integrity"):
        ledger.admit_notification_projection("run-1", new)
    assert ledger.conn.execute(
        "select count(*) from factory_notification_outbox",
    ).fetchone()[0] == 0
    ledger.close()


def test_concurrent_transition_observers_converge_on_one_admission(tmp_path):
    path = tmp_path / "run.db"
    old, new = _transition()
    setup = FactoryLedger(path)
    setup.admit_notification_projection("run-1", old)
    setup.close()
    barrier = Barrier(2)

    def observe(_):
        ledger = FactoryLedger(path)
        try:
            barrier.wait()
            return ledger.admit_notification_projection("run-1", new)
        finally:
            ledger.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(observe, range(2)))
    admitted = [item for result in results for item in result]
    verify = FactoryLedger(path)
    assert len(admitted) == 1
    assert verify.conn.execute(
        "select count(*) from factory_notification_outbox",
    ).fetchone()[0] == 1
    verify.close()
