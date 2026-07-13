"""Factory's domain tables layered onto muster's per-run ledger."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from muster import Ledger, new_id, utcnow


_UNSET = object()


def _worker_semantic_key(*, run_id: str, work_item_id: str, worker_id: str,
                         report_sha256: str, usage_json: str, provider: str,
                         model: str, purpose: str) -> str:
    binding = json.dumps({
        "model": model, "provider": provider, "purpose": purpose,
        "reportSha256": report_sha256, "runId": run_id,
        "usage": json.loads(usage_json), "workerId": worker_id,
        "workItemId": work_item_id,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(binding.encode("utf-8")).hexdigest()


FACTORY_SCHEMA = """
create table if not exists factory_workers (
    id             text primary key,
    run_id         text not null,
    role           text not null,
    status         text not null,
    model_profile  text not null,
    current_step   text,
    created_at     text not null,
    updated_at     text not null,
    completed_at   text,
    error          text
);
create index if not exists idx_factory_workers_run on factory_workers(run_id);

create table if not exists factory_strategy_workers (
    run_id         text not null,
    plan_work_id   text not null,
    worker_id      text not null unique,
    primary key (run_id, plan_work_id)
);

create table if not exists factory_events (
    id            text primary key,
    run_id        text not null,
    worker_id     text,
    kind          text not null,
    message       text not null,
    payload_json  text not null default '{}',
    created_at    text not null
);
create index if not exists idx_factory_events_run on factory_events(run_id, created_at);

create table if not exists factory_event_dedupe (
    run_id       text not null,
    dedupe_key   text not null,
    event_id     text not null unique,
    primary key (run_id, dedupe_key)
);

create table if not exists factory_model_calls (
    id             text primary key,
    run_id         text not null,
    worker_id      text not null,
    provider       text not null,
    model          text not null,
    purpose        text not null,
    usage_json     text not null default '{}',
    created_at     text not null
);
create index if not exists idx_factory_model_calls_run on factory_model_calls(run_id);

create table if not exists factory_worker_sessions (
    worker_id           text primary key,
    run_id              text not null,
    messages_json       text not null default '[]',
    pending_calls_json  text not null default '[]',
    updated_at          text not null
);
create index if not exists idx_factory_worker_sessions_run
    on factory_worker_sessions(run_id);

create table if not exists factory_worker_semantic_commits (
    commit_key      text primary key,
    run_id          text not null,
    work_item_id    text not null unique,
    worker_id       text not null,
    report_sha256   text not null,
    report_json     text not null,
    usage_json      text not null,
    provider        text not null,
    model           text not null,
    purpose         text not null,
    status          text not null default 'staged',
    created_at      text not null,
    completed_at    text
);
create index if not exists idx_factory_worker_semantic_commits_run
    on factory_worker_semantic_commits(run_id, status);

create table if not exists factory_tool_calls (
    id             text primary key,
    run_id         text not null,
    work_item_id   text not null,
    tool_id        text not null,
    safety_tier    integer not null,
    manifest_digest text not null,
    declared_actions_json text not null default '[]',
    credential_use integer not null default 0,
    status         text not null,
    output_path    text,
    exit_code      integer,
    created_at     text not null,
    completed_at   text
);
create index if not exists idx_factory_tool_calls_run on factory_tool_calls(run_id);

create table if not exists factory_evidence_publications (
    reconciliation_key text primary key,
    run_id             text not null,
    tool_call_id       text not null unique,
    work_item_id       text not null,
    result_sha256      text not null,
    result_size        integer not null,
    exit_code          integer not null,
    expected_evidence_artifact_id text not null,
    authority_json     text not null,
    evidence_artifact_id text,
    ledger_artifact_id text,
    status             text not null default 'staged',
    created_at         text not null,
    completed_at       text
);
create index if not exists idx_factory_evidence_publications_run
    on factory_evidence_publications(run_id, status);

create table if not exists factory_permissions (
    question_id    text primary key,
    run_id         text not null,
    work_item_id   text not null,
    tool_id        text not null,
    manifest_digest text not null,
    created_at     text not null
);

create table if not exists factory_knowledge_references (
    run_id          text not null,
    root_name       text not null,
    concept_id      text not null,
    query_rationale text not null,
    citations_json  text not null default '[]',
    provenance_json text not null default '{}',
    content_hash    text not null,
    selected_at     text not null,
    primary key (run_id, root_name, concept_id, content_hash)
);
create index if not exists idx_factory_knowledge_refs_run
    on factory_knowledge_references(run_id, selected_at);

create table if not exists factory_notification_outbox (
    id                text primary key,
    dedupe_key        text not null unique,
    run_id            text not null,
    kind              text not null,
    severity          text not null,
    entity_type       text not null,
    entity_id         text not null,
    payload_json      text not null,
    payload_sha256    text not null,
    status            text not null,
    attempt_count     integer not null default 0,
    next_attempt_at   text,
    lease_token       text,
    lease_expires_at  text,
    last_error_code   text,
    created_at        text not null,
    updated_at        text not null,
    sent_at           text,
    acknowledged_at   text,
    superseded_at     text
);
create index if not exists idx_factory_notification_outbox_due
    on factory_notification_outbox(status, next_attempt_at, created_at);

create table if not exists factory_notification_schedules (
    schedule_id          text primary key,
    notification_id      text not null unique,
    preferences_id       text not null,
    preferences_revision text not null,
    preferences_json     text not null,
    preferences_sha256   text not null,
    schedule_json        text not null,
    schedule_sha256      text not null,
    disposition          text not null,
    created_at           text not null,
    updated_at           text not null
);

create table if not exists factory_notification_deliveries (
    id                text primary key,
    schedule_id       text not null,
    notification_id   text not null,
    channel_ref       text not null,
    phase             text not null,
    status            text not null,
    due_at            text,
    attempt_count     integer not null default 0,
    next_attempt_at   text,
    lease_token       text,
    lease_expires_at  text,
    last_error_code   text,
    created_at        text not null,
    updated_at        text not null,
    sent_at           text,
    superseded_at     text,
    unique(schedule_id, channel_ref, phase)
);
create index if not exists idx_factory_notification_deliveries_due
    on factory_notification_deliveries(status, next_attempt_at, due_at, created_at);

create table if not exists factory_notification_projection_state (
    run_id             text primary key,
    semantic_sha256    text not null,
    projection_json    text not null,
    projection_sha256  text not null,
    updated_at         text not null
);
"""


class FactoryLedger(Ledger):
    def __init__(self, db_path: str | Path):
        # Factory tables are operational history and deliberately survive resume.
        super().__init__(db_path, extra_schema=FACTORY_SCHEMA)
        self._migrate_manifest_authority_columns()

    def _migrate_manifest_authority_columns(self) -> None:
        migrations = {
            "factory_tool_calls": (
                ("manifest_digest", "text not null default ''"),
                ("declared_actions_json", "text not null default '[]'"),
                ("credential_use", "integer not null default 0"),
            ),
            "factory_permissions": (
                ("manifest_digest", "text not null default ''"),
            ),
        }
        with self.conn:
            for table, columns in migrations.items():
                existing = {row[1] for row in self.conn.execute(f"pragma table_info({table})")}
                for name, declaration in columns:
                    if name not in existing:
                        self.conn.execute(f"alter table {table} add column {name} {declaration}")

    def admit_notification_projection(
            self, run_id: str, projection: dict[str, Any], *,
            proof_resolver: Callable[[str, str], tuple[str, str] | None] | None = None,
            failure_injector: Callable[[str], None] | None = None) -> list[str]:
        """Atomically advance the trusted outcome baseline and admit notifications.

        This is the sole durable boundary between Factory's canonical outcome projection and
        its lossy notification subsystem.  Initial hydration establishes a baseline without
        emitting.  Reconnects with the same semantic identity are no-ops.  Degraded projections
        never replace the last trustworthy baseline, so recovery cannot invent a transition or
        erase the last point from which a real transition can be observed.
        """
        from rekit_factory.notification_outbox import NotificationOutbox
        from rekit_factory.notification_policy import (
            notification_candidates,
            notification_supersession_ids,
        )

        # This validates the supported schema, vocabulary, semantic digest, and canonical bytes
        # even when there is no previous projection.
        notification_candidates(None, projection)
        run_ids = sorted(
            item.get("entityId") for item in projection.get("entities", ())
            if item.get("entityType") == "run"
        )
        if run_ids != [run_id]:
            raise ValueError("notification projection does not bind the exact run")
        if projection.get("degraded") is not False:
            return []

        projection_json = json.dumps(
            projection, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        )
        projection_sha256 = hashlib.sha256(projection_json.encode("utf-8")).hexdigest()
        semantic_sha256 = projection["semanticSha256"]
        admitted: list[str] = []
        self.conn.execute("begin immediate")
        try:
            previous = self.conn.execute(
                "select * from factory_notification_projection_state where run_id=?",
                (run_id,),
            ).fetchone()
            if previous is not None:
                if hashlib.sha256(previous["projection_json"].encode("utf-8")).hexdigest() \
                        != previous["projection_sha256"]:
                    raise ValueError(
                        "notification projection baseline failed integrity verification"
                    )
                old = json.loads(previous["projection_json"])
                if old.get("semanticSha256") != previous["semantic_sha256"]:
                    raise ValueError(
                        "notification projection baseline identity conflicts with content"
                    )
                # Revalidate stored content before it is allowed to influence policy.
                notification_candidates(None, old)
                if previous["semantic_sha256"] == semantic_sha256:
                    self.conn.rollback()
                    return []
                candidates = notification_candidates(
                    old, projection, proof_resolver=proof_resolver,
                )
                supersession_ids = notification_supersession_ids(
                    old, projection, proof_resolver=proof_resolver,
                )
                outbox = NotificationOutbox(self.conn)
                admitted = outbox.admit(candidates)
                if failure_injector:
                    failure_injector("outbox-admitted")
                now = utcnow()
                for notification_id in supersession_ids:
                    notification = outbox.get(notification_id)
                    if notification is None:
                        continue
                    delivered = self.conn.execute(
                        "select 1 from factory_notification_deliveries "
                        "where notification_id=? and status='sent' limit 1",
                        (notification_id,),
                    ).fetchone()
                    if notification["status"] in {"queued", "failed"} and delivered is None:
                        self.conn.execute(
                            "update factory_notification_outbox set status='superseded',"
                            "next_attempt_at=null,lease_token=null,lease_expires_at=null,"
                            "superseded_at=?,updated_at=? where id=? "
                            "and status in ('queued','failed')",
                            (now, now, notification_id),
                        )
                    # An invitation that already escaped preserves its outbox audit lifecycle,
                    # while every still-pending delivery or escalation is no longer actionable.
                    self.conn.execute(
                        "update factory_notification_deliveries set status='superseded',"
                        "next_attempt_at=null,lease_token=null,lease_expires_at=null,"
                        "superseded_at=?,updated_at=? where notification_id=? "
                        "and status in ('queued','failed')",
                        (now, now, notification_id),
                    )
                if failure_injector:
                    failure_injector("notifications-reconciled")
                self.conn.execute(
                    "update factory_notification_projection_state set semantic_sha256=?,"
                    "projection_json=?,projection_sha256=?,updated_at=? where run_id=?",
                    (semantic_sha256, projection_json, projection_sha256, utcnow(), run_id),
                )
            else:
                self.conn.execute(
                    "insert into factory_notification_projection_state "
                    "(run_id,semantic_sha256,projection_json,projection_sha256,updated_at) "
                    "values (?,?,?,?,?)",
                    (run_id, semantic_sha256, projection_json, projection_sha256, utcnow()),
                )
            if failure_injector:
                failure_injector("baseline-advanced")
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return admitted

    def reconcile_stale_operator_decisions(
            self, run_id: str, *, threshold_seconds: int,
            clock: Callable[[], datetime],
            failure_injector: Callable[[str], None] | None = None) -> list[str]:
        """Admit due stale decisions and atomically retire no-longer-actionable ones.

        The question ledger owns both creation and pending/resolved truth.  The caller owns the
        explicit threshold and clock.  Consequently replay and restart can re-evaluate elapsed
        policy time without mutating the canonical outcome projection or consulting wall time.
        """
        from rekit_factory.notification_outbox import NotificationOutbox
        from rekit_factory.notification_policy import stale_operator_decision_candidate

        if type(threshold_seconds) is not int or not 1 <= threshold_seconds <= 31_536_000:
            raise ValueError("stale-decision threshold must be 1..31536000 seconds")
        now = clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("stale-decision clock must return a timezone-aware datetime")
        now = now.astimezone(timezone.utc)
        now_text = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        def derive() -> tuple[list[dict[str, Any]], set[str]]:
            candidates: list[dict[str, Any]] = []
            active_question_ids: set[str] = set()
            rows = self.conn.execute(
                "select q.id,q.created_at,a.id as answer_id from questions q "
                "left join answers a on a.id=q.id where q.run_id=? order by q.created_at,q.id",
                (run_id,),
            ).fetchall()
            for row in rows:
                raw_created = row["created_at"]
                if type(raw_created) is not str:
                    raise ValueError("question creation timestamp is invalid")
                try:
                    created = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("question creation timestamp is invalid") from exc
                if created.tzinfo is None or created.utcoffset() is None:
                    raise ValueError("question creation timestamp must be timezone-aware")
                if row["answer_id"] is None and now >= (
                        created.astimezone(timezone.utc) + timedelta(seconds=threshold_seconds)):
                    active_question_ids.add(row["id"])
                    candidates.append(stale_operator_decision_candidate(
                        run_id=run_id, question_id=row["id"],
                        threshold_seconds=threshold_seconds,
                    ))
            return candidates, active_question_ids

        admitted: list[str] = []
        self.conn.execute("begin immediate")
        try:
            candidates, active_question_ids = derive()
            outbox = NotificationOutbox(self.conn, clock=lambda: now)
            admitted = outbox.admit(candidates)
            if failure_injector:
                failure_injector("stale-decisions-admitted")
            active_ids = set(admitted)
            stale_rows = self.conn.execute(
                "select id,status,entity_id from factory_notification_outbox "
                "where run_id=? and kind='operator-decision.stale'",
                (run_id,),
            ).fetchall()
            for row in stale_rows:
                if row["id"] in active_ids and row["entity_id"] in active_question_ids:
                    continue
                delivered = self.conn.execute(
                    "select 1 from factory_notification_deliveries "
                    "where notification_id=? and status='sent' limit 1", (row["id"],),
                ).fetchone()
                if row["status"] in {"queued", "failed"} and delivered is None:
                    self.conn.execute(
                        "update factory_notification_outbox set status='superseded',"
                        "next_attempt_at=null,lease_token=null,lease_expires_at=null,"
                        "superseded_at=?,updated_at=? where id=? and status in ('queued','failed')",
                        (now_text, now_text, row["id"]),
                    )
                self.conn.execute(
                    "update factory_notification_deliveries set status='superseded',"
                    "next_attempt_at=null,lease_token=null,lease_expires_at=null,"
                    "superseded_at=?,updated_at=? where notification_id=? "
                    "and status in ('queued','failed')",
                    (now_text, now_text, row["id"]),
                )
            if failure_injector:
                failure_injector("stale-decisions-reconciled")
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return admitted

    def set_run_status(self, run_id: str, status: str, *, error: str | None = None) -> None:
        self.conn.execute(
            "update runs set status=?, error=?, updated_at=? where id=?",
            (status, error, utcnow(), run_id),
        )
        self.conn.commit()

    def add_worker(self, run_id: str, role: str, model_profile: str) -> str:
        worker_id = new_id("worker")
        now = utcnow()
        self.conn.execute(
            "insert into factory_workers "
            "(id, run_id, role, status, model_profile, created_at, updated_at) "
            "values (?,?,?,?,?,?,?)",
            (worker_id, run_id, role, "queued", model_profile, now, now),
        )
        self.conn.commit()
        return worker_id

    def add_planned_worker(self, run_id: str, plan_work_id: str, role: str,
                           model_profile: str) -> str:
        """Return one durable worker for a deterministic strategy work id."""
        existing = self.conn.execute(
            "select worker_id from factory_strategy_workers "
            "where run_id=? and plan_work_id=?", (run_id, plan_work_id),
        ).fetchone()
        if existing is not None:
            return existing["worker_id"]
        worker_id = new_id("worker")
        now = utcnow()
        with self.conn:
            self.conn.execute(
                "insert into factory_workers "
                "(id, run_id, role, status, model_profile, created_at, updated_at) "
                "values (?,?,?,?,?,?,?)",
                (worker_id, run_id, role, "queued", model_profile, now, now),
            )
            self.conn.execute(
                "insert into factory_strategy_workers (run_id, plan_work_id, worker_id) "
                "values (?,?,?)", (run_id, plan_work_id, worker_id),
            )
        return worker_id

    def update_worker(self, worker_id: str, *, status: str | None = None,
                      current_step: str | None = None, error: object = _UNSET) -> None:
        row = self.conn.execute(
            "select status, current_step, error from factory_workers where id=?", (worker_id,)
        ).fetchone()
        if row is None:
            raise KeyError(worker_id)
        next_status = status if status is not None else row["status"]
        completed = utcnow() if next_status in {"done", "failed", "cancelled"} else None
        self.conn.execute(
            "update factory_workers set status=?, current_step=?, error=?, updated_at=?, "
            "completed_at=coalesce(?, completed_at) where id=?",
            (next_status,
             current_step if current_step is not None else row["current_step"],
             row["error"] if error is _UNSET else error, utcnow(), completed, worker_id),
        )
        self.conn.commit()

    def event_log(self, run_id: str, kind: str, message: str, *,
                  worker_id: str | None = None, payload: dict[str, Any] | None = None) -> str:
        event_id = new_id("event")
        self.conn.execute(
            "insert into factory_events "
            "(id, run_id, worker_id, kind, message, payload_json, created_at) "
            "values (?,?,?,?,?,?,?)",
            (event_id, run_id, worker_id, kind, message,
             json.dumps(payload or {}, sort_keys=True), utcnow()),
        )
        self.conn.commit()
        return event_id

    def event_log_once(self, run_id: str, dedupe_key: str, kind: str, message: str, *,
                       worker_id: str | None = None,
                       payload: dict[str, Any] | None = None) -> str:
        """Append one event for a stable semantic key, including after crash/retry."""
        existing = self.conn.execute(
            "select event_id from factory_event_dedupe where run_id=? and dedupe_key=?",
            (run_id, dedupe_key),
        ).fetchone()
        if existing is not None:
            return existing["event_id"]
        event_id = new_id("event")
        now = utcnow()
        with self.conn:
            claimed = self.conn.execute(
                "insert or ignore into factory_event_dedupe "
                "(run_id,dedupe_key,event_id) values (?,?,?)",
                (run_id, dedupe_key, event_id),
            )
            if claimed.rowcount == 0:
                return self.conn.execute(
                    "select event_id from factory_event_dedupe "
                    "where run_id=? and dedupe_key=?", (run_id, dedupe_key),
                ).fetchone()["event_id"]
            self.conn.execute(
                "insert into factory_events "
                "(id,run_id,worker_id,kind,message,payload_json,created_at) "
                "values (?,?,?,?,?,?,?)",
                (event_id, run_id, worker_id, kind, message,
                 json.dumps(payload or {}, sort_keys=True), now),
            )
        return event_id

    def record_model_call(self, run_id: str, worker_id: str, *, provider: str,
                          model: str, purpose: str, usage: dict[str, Any]) -> str:
        call_id = new_id("model")
        self.conn.execute(
            "insert into factory_model_calls "
            "(id, run_id, worker_id, provider, model, purpose, usage_json, created_at) "
            "values (?,?,?,?,?,?,?,?)",
            (call_id, run_id, worker_id, provider, model, purpose,
             json.dumps(usage, sort_keys=True), utcnow()),
        )
        self.conn.commit()
        return call_id

    def save_worker_session(self, run_id: str, worker_id: str, *, messages_json: str,
                            pending_calls: list[dict[str, Any]]) -> None:
        self.conn.execute(
            "insert into factory_worker_sessions "
            "(worker_id, run_id, messages_json, pending_calls_json, updated_at) "
            "values (?,?,?,?,?) on conflict(worker_id) do update set "
            "messages_json=excluded.messages_json, "
            "pending_calls_json=excluded.pending_calls_json, "
            "updated_at=excluded.updated_at",
            (worker_id, run_id, messages_json,
             json.dumps(pending_calls, sort_keys=True), utcnow()),
        )
        self.conn.commit()

    def worker_session(self, run_id: str, worker_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "select * from factory_worker_sessions where run_id=? and worker_id=?",
            (run_id, worker_id),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["pendingCalls"] = json.loads(item.pop("pending_calls_json"))
        return item

    def worker_sessions(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "select worker_id, run_id, pending_calls_json, updated_at "
            "from factory_worker_sessions where run_id=? order by updated_at",
            (run_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["pendingCalls"] = json.loads(item.pop("pending_calls_json"))
            result.append(item)
        return result

    def stage_worker_semantic_commit(
            self, run_id: str, work_item_id: str, worker_id: str, *,
            report: dict[str, Any], usage: dict[str, Any], provider: str,
            model: str, purpose: str) -> str:
        """Durably bind one returned model report before cross-store projection."""
        work = self.get_work_item(work_item_id)
        worker = self.conn.execute(
            "select * from factory_workers where id=?", (worker_id,),
        ).fetchone()
        if work is None or work["run_id"] != run_id or worker is None \
                or worker["run_id"] != run_id:
            raise ValueError("worker semantic authority does not match run/work identity")
        work_payload = json.loads(work["payload_json"])
        if work_payload.get("workerId") != worker_id:
            raise ValueError("worker semantic authority conflicts with work payload")
        report_json = json.dumps(report, sort_keys=True, separators=(",", ":"))
        usage_json = json.dumps(usage, sort_keys=True, separators=(",", ":"))
        report_sha256 = hashlib.sha256(report_json.encode("utf-8")).hexdigest()
        commit_key = _worker_semantic_key(
            run_id=run_id, work_item_id=work_item_id, worker_id=worker_id,
            report_sha256=report_sha256, usage_json=usage_json,
            provider=provider, model=model, purpose=purpose,
        )
        existing = self.conn.execute(
            "select * from factory_worker_semantic_commits where work_item_id=?",
            (work_item_id,),
        ).fetchone()
        if existing is not None:
            exact = (
                existing["commit_key"] == commit_key
                and existing["worker_id"] == worker_id
                and existing["report_json"] == report_json
                and existing["usage_json"] == usage_json
                and existing["provider"] == provider
                and existing["model"] == model
                and existing["purpose"] == purpose
            )
            if not exact:
                raise ValueError("worker semantic commit conflicts with staged report")
            return commit_key
        self.conn.execute(
            "insert into factory_worker_semantic_commits "
            "(commit_key,run_id,work_item_id,worker_id,report_sha256,report_json,"
            "usage_json,provider,model,purpose,created_at) values (?,?,?,?,?,?,?,?,?,?,?)",
            (commit_key, run_id, work_item_id, worker_id, report_sha256, report_json,
             usage_json, provider, model, purpose, utcnow()),
        )
        self.conn.commit()
        return commit_key

    def worker_semantic_commit(self, work_item_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "select * from factory_worker_semantic_commits where work_item_id=?",
            (work_item_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        actual_report_sha256 = hashlib.sha256(
            result["report_json"].encode("utf-8")
        ).hexdigest()
        actual_key = _worker_semantic_key(
            run_id=result["run_id"], work_item_id=result["work_item_id"],
            worker_id=result["worker_id"], report_sha256=actual_report_sha256,
            usage_json=result["usage_json"], provider=result["provider"],
            model=result["model"], purpose=result["purpose"],
        )
        if actual_report_sha256 != result["report_sha256"] \
                or actual_key != result["commit_key"]:
            raise ValueError("staged worker semantic commit failed integrity verification")
        result["report"] = json.loads(result.pop("report_json"))
        result["usage"] = json.loads(result.pop("usage_json"))
        return result

    def complete_worker_semantic_commit(
            self, commit_key: str, *, completion_payload: dict[str, Any],
            activity_events: list[dict[str, Any]],
            failure_injector: Callable[[str], None] | None = None) -> None:
        """Atomically expose report, usage, events, and worker/work completion."""
        staged = self.conn.execute(
            "select * from factory_worker_semantic_commits where commit_key=?",
            (commit_key,),
        ).fetchone()
        if staged is None:
            raise KeyError(commit_key)
        actual_report_sha256 = hashlib.sha256(
            staged["report_json"].encode("utf-8")
        ).hexdigest()
        actual_key = _worker_semantic_key(
            run_id=staged["run_id"], work_item_id=staged["work_item_id"],
            worker_id=staged["worker_id"], report_sha256=actual_report_sha256,
            usage_json=staged["usage_json"], provider=staged["provider"],
            model=staged["model"], purpose=staged["purpose"],
        )
        if actual_report_sha256 != staged["report_sha256"] or actual_key != commit_key:
            raise ValueError("staged worker semantic commit failed integrity verification")
        if staged["status"] == "complete":
            return
        report = json.loads(staged["report_json"])
        work = self.get_work_item(staged["work_item_id"])
        worker = self.conn.execute(
            "select * from factory_workers where id=?", (staged["worker_id"],),
        ).fetchone()
        if work is None or worker is None or work["run_id"] != staged["run_id"] \
                or worker["run_id"] != staged["run_id"]:
            raise ValueError("staged worker semantic authority no longer matches")
        now = utcnow()
        model_call_id = "model-semantic-" + commit_key
        completed_event_id = "event-worker-" + commit_key
        with self.conn:
            self.conn.execute(
                "insert into factory_model_calls "
                "(id,run_id,worker_id,provider,model,purpose,usage_json,created_at) "
                "values (?,?,?,?,?,?,?,?)",
                (model_call_id, staged["run_id"], staged["worker_id"],
                 staged["provider"], staged["model"], staged["purpose"],
                 staged["usage_json"], staged["created_at"]),
            )
            if failure_injector:
                failure_injector("model-call")
            self.conn.execute(
                "update work_items set status='done',state_label='completed',updated_at=?,"
                "terminal_at=?,result_json=?,error=null,evidence=? where id=? and run_id=?",
                (now, now, staged["report_json"],
                 "Bounded model review over target snapshot and ledgered tool output",
                 staged["work_item_id"], staged["run_id"]),
            )
            if failure_injector:
                failure_injector("report-projection")
            self.conn.execute(
                "update factory_workers set status='done',current_step=?,error=null,"
                "updated_at=?,completed_at=coalesce(completed_at,?) where id=? and run_id=?",
                (report["status_update"], now, now, staged["worker_id"], staged["run_id"]),
            )
            if failure_injector:
                failure_injector("worker-completion")
            self.conn.execute(
                "insert into factory_events "
                "(id,run_id,worker_id,kind,message,payload_json,created_at) "
                "values (?,?,?,?,?,?,?)",
                (completed_event_id, staged["run_id"], staged["worker_id"],
                 "worker.completed", report["status_update"],
                 json.dumps(completion_payload, sort_keys=True), now),
            )
            for index, event in enumerate(activity_events):
                self.conn.execute(
                    "insert into factory_events "
                    "(id,run_id,worker_id,kind,message,payload_json,created_at) "
                    "values (?,?,?,?,?,?,?)",
                    (f"event-worker-{commit_key}-{index}", staged["run_id"],
                     staged["worker_id"], event["kind"], event["message"],
                     json.dumps(event.get("payload", {}), sort_keys=True), now),
                )
            if failure_injector:
                failure_injector("semantic-events")
            self.conn.execute(
                "update factory_worker_semantic_commits set status='complete',completed_at=? "
                "where commit_key=?", (now, commit_key),
            )
            if failure_injector:
                failure_injector("commit-marker")

    def start_tool_call(self, run_id: str, work_item_id: str, tool_id: str,
                        safety_tier: int, *, manifest_digest: str,
                        declared_actions: tuple[str, ...], credential_use: bool) -> str:
        existing = self.conn.execute(
            "select * from factory_tool_calls where run_id=? and work_item_id=? "
            "and status='running' order by created_at desc limit 1",
            (run_id, work_item_id),
        ).fetchone()
        if existing is not None:
            exact = (
                existing["tool_id"] == tool_id
                and existing["safety_tier"] == safety_tier
                and existing["manifest_digest"] == manifest_digest
                and json.loads(existing["declared_actions_json"]) == list(declared_actions)
                and bool(existing["credential_use"]) == credential_use
            )
            if not exact:
                raise ValueError("running tool call authority conflicts with retry")
            return existing["id"]
        call_id = new_id("tool")
        self.conn.execute(
            "insert into factory_tool_calls "
            "(id, run_id, work_item_id, tool_id, safety_tier, manifest_digest, "
            "declared_actions_json, credential_use, status, created_at) "
            "values (?,?,?,?,?,?,?,?,?,?)",
            (call_id, run_id, work_item_id, tool_id, safety_tier, manifest_digest,
             json.dumps(declared_actions), credential_use, "running", utcnow()),
        )
        self.conn.commit()
        return call_id

    def finish_tool_call(self, call_id: str, *, status: str, output_path: str | None,
                         exit_code: int | None) -> None:
        self.conn.execute(
            "update factory_tool_calls set status=?, output_path=?, exit_code=?, "
            "completed_at=? where id=?",
            (status, output_path, exit_code, utcnow(), call_id),
        )
        self.conn.commit()

    def stage_tool_evidence_publication(
            self, call_id: str, *, result_bytes: bytes, exit_code: int,
            authority: dict[str, Any]) -> str:
        """Persist the exact authorized result identity before evidence capture."""
        call = self.conn.execute(
            "select * from factory_tool_calls where id=?", (call_id,),
        ).fetchone()
        if call is None:
            raise KeyError(call_id)
        canonical_authority = json.dumps(authority, sort_keys=True, separators=(",", ":"))
        result_sha256 = hashlib.sha256(result_bytes).hexdigest()
        expected_evidence_artifact_id = "artifact-" + hashlib.sha256(
            b"tool-output\0" + result_bytes
        ).hexdigest()
        binding = {
            "authority": json.loads(canonical_authority), "exitCode": exit_code,
            "evidenceArtifactId": expected_evidence_artifact_id,
            "resultSha256": result_sha256, "resultSize": len(result_bytes),
            "runId": call["run_id"], "toolCallId": call_id,
            "workItemId": call["work_item_id"],
        }
        reconciliation_key = hashlib.sha256(json.dumps(
            binding, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        existing = self.conn.execute(
            "select * from factory_evidence_publications where tool_call_id=?", (call_id,),
        ).fetchone()
        if existing is not None:
            exact = (
                existing["reconciliation_key"] == reconciliation_key
                and existing["result_sha256"] == result_sha256
                and existing["result_size"] == len(result_bytes)
                and existing["exit_code"] == exit_code
                and existing["expected_evidence_artifact_id"] == expected_evidence_artifact_id
                and existing["authority_json"] == canonical_authority
            )
            if not exact:
                raise ValueError("conflicting result or authority for tool publication")
            return reconciliation_key
        self.conn.execute(
            "insert into factory_evidence_publications "
            "(reconciliation_key,run_id,tool_call_id,work_item_id,result_sha256,"
            "result_size,exit_code,expected_evidence_artifact_id,authority_json,created_at) "
            "values (?,?,?,?,?,?,?,?,?,?)",
            (reconciliation_key, call["run_id"], call_id, call["work_item_id"],
             result_sha256, len(result_bytes), exit_code, expected_evidence_artifact_id,
             canonical_authority, utcnow()),
        )
        self.conn.commit()
        return reconciliation_key

    def evidence_publication(self, call_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "select * from factory_evidence_publications where tool_call_id=?", (call_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["authority"] = json.loads(result.pop("authority_json"))
        return result

    def complete_tool_evidence_publication(
            self, reconciliation_key: str, *, evidence_artifact_id: str,
            evidence_original_sha256: str, evidence_path: str | Path,
            evidence_size: int, evidence_metadata: dict[str, Any],
            worker_id: str | None = None,
            related_evidence: list[dict[str, Any]] | None = None,
            evidence_events: list[dict[str, Any]] | None = None,
            failure_injector: Callable[[str], None] | None = None) -> str:
        """Atomically expose evidence, its events, and the Muster tool completion."""
        publication = self.conn.execute(
            "select * from factory_evidence_publications where reconciliation_key=?",
            (reconciliation_key,),
        ).fetchone()
        if publication is None:
            raise KeyError(reconciliation_key)
        if evidence_original_sha256 != publication["result_sha256"]:
            raise ValueError("evidence bytes conflict with staged tool result")
        if evidence_artifact_id != publication["expected_evidence_artifact_id"]:
            raise ValueError("evidence identity conflicts with staged tool result")
        existing_artifact = publication["ledger_artifact_id"]
        if publication["status"] == "complete":
            if publication["evidence_artifact_id"] != evidence_artifact_id:
                raise ValueError("conflicting evidence identity for completed publication")
            return existing_artifact
        call = self.conn.execute(
            "select * from factory_tool_calls where id=?", (publication["tool_call_id"],),
        ).fetchone()
        if call is None or call["run_id"] != publication["run_id"] \
                or call["work_item_id"] != publication["work_item_id"]:
            raise ValueError("tool invocation authority no longer matches publication")
        now = utcnow()
        artifact_id = "art-evidence-" + reconciliation_key
        terminal_event_id = "event-tool-" + reconciliation_key
        output_path = str(evidence_path)
        success = publication["exit_code"] == 0
        with self.conn:
            self.conn.execute(
                "insert into artifacts "
                "(id,run_id,kind,path,logical_path,sha256,size_bytes,media_type,language,"
                "origin,metadata_json,created_at) values (?,?,?,?,?,?,?,?,?,?,?,?)",
                (artifact_id, publication["run_id"], "tool-output", output_path,
                 f"tool-output/{Path(output_path).name}",
                 evidence_metadata.get("displaySha256", evidence_original_sha256),
                 evidence_size, "text/plain; charset=utf-8", None,
                 f"rekit:{call['tool_id']}", json.dumps({
                     **evidence_metadata, "reconciliationKey": reconciliation_key,
                 }, sort_keys=True), now),
            )
            for index, related in enumerate(related_evidence or ()):
                self.conn.execute(
                    "insert into artifacts "
                    "(id,run_id,kind,path,logical_path,sha256,size_bytes,media_type,language,"
                    "origin,metadata_json,created_at) values (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"art-evidence-{reconciliation_key}-{index}", publication["run_id"],
                     related["kind"], str(related["path"]), related["logicalPath"],
                     related["sha256"], related["sizeBytes"], related.get("mediaType"),
                     None, f"rekit:{call['tool_id']}",
                     json.dumps({**related["metadata"],
                                 "reconciliationKey": reconciliation_key}, sort_keys=True), now),
                )
            if failure_injector:
                failure_injector("artifacts")
            projected_events = evidence_events or [{
                "kind": "evidence.captured", "message": "Tool result evidence reconciled",
                "payload": {"artifactId": evidence_artifact_id},
            }]
            for index, event in enumerate(projected_events):
                self.conn.execute(
                    "insert into factory_events "
                    "(id,run_id,worker_id,kind,message,payload_json,created_at) "
                    "values (?,?,?,?,?,?,?)",
                    (f"event-evidence-{reconciliation_key}-{index}", publication["run_id"],
                     worker_id, event["kind"], event["message"],
                     json.dumps({**event.get("payload", {}),
                                 "reconciliationKey": reconciliation_key}, sort_keys=True), now),
                )
            if failure_injector:
                failure_injector("events")
            self.conn.execute(
                "update factory_tool_calls set status=?,output_path=?,exit_code=?,completed_at=? "
                "where id=?",
                ("done" if success else "failed", output_path,
                 publication["exit_code"], now, publication["tool_call_id"]),
            )
            if failure_injector:
                failure_injector("tool-completion")
            if success:
                self.conn.execute(
                    "update work_items set status='done',state_label='completed',updated_at=?,"
                    "terminal_at=?,result_json=?,error=null,evidence=? where id=? and run_id=?",
                    (now, now, json.dumps({"toolId": call["tool_id"], "output": output_path,
                                          "manifestDigest": call["manifest_digest"]}),
                     output_path, publication["work_item_id"], publication["run_id"]),
                )
            else:
                self.conn.execute(
                    "update work_items set status='failed',state_label='failed',updated_at=?,"
                    "terminal_at=?,result_json=null,error=?,evidence=? where id=? and run_id=?",
                    (now, now, f"{call['tool_id']} exited {publication['exit_code']}",
                     output_path, publication["work_item_id"], publication["run_id"]),
                )
            if failure_injector:
                failure_injector("work-resolution")
            self.conn.execute(
                "insert into factory_events "
                "(id,run_id,worker_id,kind,message,payload_json,created_at) "
                "values (?,?,?,?,?,?,?)",
                (terminal_event_id, publication["run_id"], worker_id,
                 "tool.completed" if success else "tool.failed",
                 (f"{call['tool_id']} completed" if success else
                  f"{call['tool_id']} exited {publication['exit_code']}"),
                 json.dumps({"reconciliationKey": reconciliation_key}, sort_keys=True), now),
            )
            if failure_injector:
                failure_injector("terminal-event")
            self.conn.execute(
                "update factory_evidence_publications set status='complete',"
                "evidence_artifact_id=?,ledger_artifact_id=?,completed_at=? "
                "where reconciliation_key=?",
                (evidence_artifact_id, artifact_id, now, reconciliation_key),
            )
        return artifact_id

    def link_permission(self, qid: str, run_id: str, work_item_id: str, tool_id: str,
                        manifest_digest: str) -> None:
        self.conn.execute(
            "insert or ignore into factory_permissions "
            "(question_id, run_id, work_item_id, tool_id, manifest_digest, created_at) "
            "values (?,?,?,?,?,?)",
            (qid, run_id, work_item_id, tool_id, manifest_digest, utcnow()),
        )
        self.conn.commit()

    def select_knowledge_reference(self, run_id: str, *, root_name: str, concept_id: str,
                                   query_rationale: str, citations: list[str],
                                   provenance: dict[str, Any], content_hash: str) -> None:
        """Record a stable selection without copying the concept body into the ledger."""
        self.conn.execute(
            "insert or ignore into factory_knowledge_references "
            "(run_id,root_name,concept_id,query_rationale,citations_json,provenance_json,"
            "content_hash,selected_at) values (?,?,?,?,?,?,?,?)",
            (run_id, root_name, concept_id, query_rationale,
             json.dumps(citations, sort_keys=True), json.dumps(provenance, sort_keys=True),
             content_hash, utcnow()),
        )
        self.conn.commit()

    def knowledge_references(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "select * from factory_knowledge_references where run_id=? "
            "order by selected_at, root_name, concept_id", (run_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["runId"] = item.pop("run_id")
            item["root"] = item.pop("root_name")
            item["conceptId"] = item.pop("concept_id")
            item["queryRationale"] = item.pop("query_rationale")
            item["citations"] = json.loads(item.pop("citations_json"))
            item["provenance"] = json.loads(item.pop("provenance_json"))
            item["contentHash"] = item.pop("content_hash")
            item["selectedAt"] = item.pop("selected_at")
            result.append(item)
        return result

    def publish_dossier(self, run_id: str, *, finding_id: str, manifest_sha256: str,
                        records: list[dict[str, Any]], metadata: dict[str, Any]) -> list[str]:
        """Expose a fully materialized dossier and its event in one SQLite transaction."""
        existing = self.conn.execute(
            "select id from artifacts where run_id=? and kind='proof-bundle' "
            "and json_extract(metadata_json,'$.manifestSha256')=?",
            (run_id, manifest_sha256),
        ).fetchone()
        if existing is not None:
            return [existing["id"]]
        now = utcnow()
        artifact_ids = [new_id("art") for _ in records]
        linked = {record["kind"]: artifact_id
                  for record, artifact_id in zip(records, artifact_ids)}
        with self.conn:
            for record, artifact_id in zip(records, artifact_ids):
                record_metadata = {**metadata, **record.get("metadata", {}),
                                   "artifactIds": linked}
                self.conn.execute(
                    "insert into artifacts "
                    "(id,run_id,kind,path,logical_path,sha256,size_bytes,media_type,language,"
                    "origin,metadata_json,created_at) values (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (artifact_id, run_id, record["kind"], str(record["path"]),
                     record["logical_path"], record["sha256"], record["size_bytes"],
                     record["media_type"], record.get("language"), "proof-dossier",
                     json.dumps(record_metadata, sort_keys=True), now),
                )
            self.conn.execute(
                "insert into factory_events "
                "(id,run_id,worker_id,kind,message,payload_json,created_at) "
                "values (?,?,?,?,?,?,?)",
                (new_id("event"), run_id, None, "dossier.published",
                 f"Proof dossier published for {finding_id}",
                 json.dumps({"findingId": finding_id, "manifestSha256": manifest_sha256,
                             "artifactIds": linked}, sort_keys=True), now),
            )
        return artifact_ids

    def answer_permission(self, run_id: str, qid: str, answer: str) -> str:
        if answer not in {"allow", "deny"}:
            raise ValueError("permission answer must be 'allow' or 'deny'")
        link = self.conn.execute(
            "select work_item_id from factory_permissions where question_id=? and run_id=?",
            (qid, run_id),
        ).fetchone()
        if link is None:
            raise KeyError(qid)
        self.record_answer(run_id, qid, answer)
        self.conn.execute(
            "update work_items set status='queued', state_label=null, error=null, "
            "result_json=null, terminal_at=null, updated_at=? "
            "where id=? and run_id=? and status='blocked' and state_label='needs_permission'",
            (utcnow(), link["work_item_id"], run_id),
        )
        self.conn.commit()
        return link["work_item_id"]

    def workers(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(
            "select * from factory_workers where run_id=? order by created_at", (run_id,)
        ).fetchall()]

    def events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "select * from factory_events where run_id=? order by created_at", (run_id,)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def model_calls(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "select * from factory_model_calls where run_id=? order by created_at", (run_id,)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["usage"] = json.loads(item.pop("usage_json"))
            result.append(item)
        return result

    def tool_calls(self, run_id: str) -> list[dict[str, Any]]:
        result = []
        for row in self.conn.execute(
            "select * from factory_tool_calls where run_id=? order by created_at", (run_id,)
        ).fetchall():
            item = dict(row)
            item["declaredActions"] = json.loads(item.pop("declared_actions_json"))
            item["credentialUse"] = bool(item.pop("credential_use"))
            result.append(item)
        return result
