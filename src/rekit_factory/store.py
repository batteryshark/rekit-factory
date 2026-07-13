"""Factory's domain tables layered onto muster's per-run ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from muster import Ledger, new_id, utcnow


_UNSET = object()


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

create table if not exists factory_tool_calls (
    id             text primary key,
    run_id         text not null,
    work_item_id   text not null,
    tool_id        text not null,
    safety_tier    integer not null,
    status         text not null,
    output_path    text,
    exit_code      integer,
    created_at     text not null,
    completed_at   text
);
create index if not exists idx_factory_tool_calls_run on factory_tool_calls(run_id);

create table if not exists factory_permissions (
    question_id    text primary key,
    run_id         text not null,
    work_item_id   text not null,
    tool_id        text not null,
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
"""


class FactoryLedger(Ledger):
    def __init__(self, db_path: str | Path):
        # Factory tables are operational history and deliberately survive resume.
        super().__init__(db_path, extra_schema=FACTORY_SCHEMA)

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

    def start_tool_call(self, run_id: str, work_item_id: str, tool_id: str,
                        safety_tier: int) -> str:
        call_id = new_id("tool")
        self.conn.execute(
            "insert into factory_tool_calls "
            "(id, run_id, work_item_id, tool_id, safety_tier, status, created_at) "
            "values (?,?,?,?,?,?,?)",
            (call_id, run_id, work_item_id, tool_id, safety_tier, "running", utcnow()),
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

    def link_permission(self, qid: str, run_id: str, work_item_id: str, tool_id: str) -> None:
        self.conn.execute(
            "insert or ignore into factory_permissions "
            "(question_id, run_id, work_item_id, tool_id, created_at) values (?,?,?,?,?)",
            (qid, run_id, work_item_id, tool_id, utcnow()),
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
        return [dict(row) for row in self.conn.execute(
            "select * from factory_tool_calls where run_id=? order by created_at", (run_id,)
        ).fetchall()]
