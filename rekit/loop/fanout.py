"""Orchestrator-level fan-out — run N brain invocations in parallel, fold once (E2.3).

The ralph loop (:mod:`rekit.loop.loop`) drives *one* goal one invocation at a time.
This module is the orchestrator's other lever: when there are N independent units
of work — N trees to triage, N binaries to classify — rekit can run all N brain
invocations **concurrently** and converge their results **losslessly** on the one
project ledger.

The safe pattern
----------------
The project ledger is event-sourced: every ``record_*`` appends a line to
``ledger.jsonl`` and folds it into the live in-memory :class:`~rekit.ledger.Ledger`.
That append+fold is **not** thread-safe — two threads writing at once would
interleave file writes and race the in-memory fold. So fan-out splits the work
along the one axis that is safe to parallelise:

1. **Fan out the brain invocations concurrently.** Each item's
   ``adapter.invoke(...)`` (a subprocess / pi call — I/O-bound) runs on a worker
   thread via :class:`concurrent.futures.ThreadPoolExecutor`, bounded by
   ``max_concurrency``. No worker touches the ledger.
2. **Fold the results sequentially in the parent.** Once all invocations have
   returned (or failed), the parent thread — and only the parent — folds each
   :class:`~rekit.harness.base.HarnessResult` into the ledger, one at a time, in a
   deterministic order. Zero concurrent ledger writes, so zero races.

A worker that raises does not sink the batch: its exception is captured and the
item is reported as failed, while every other item still folds.

Two delegation paths coexist
-----------------------------
This is *orchestrator-level* parallelism: rekit deciding to run N pi processes
over N trees. It is distinct from — and composes with — the *brain-level*
delegation path, the installed **pi-subagents** extension: when the loop enables
it via ``--tools``, the brain itself can spawn subagents mid-turn. Both are real;
this module is the one rekit controls directly.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..harness.base import HarnessAdapter, HarnessResult
from ..ledger.artifacts import Artifact, from_path
from ..ledger.project import Project

#: A callback that folds one item's result into the project. Mirrors the loop's
#: tagged-line protocol by default (:func:`default_fold`), but any callable with
#: this shape can be supplied for a workflow-specific fold.
FoldFn = Callable[[Project, Any, HarnessResult], int]

#: Build the per-item ``user_input`` string handed to ``adapter.invoke``.
BuildInputFn = Callable[[Any], str]


# The same tagged-line protocol the ralph loop folds (kept local so fanout does
# not import from loop.loop, which a parallel task owns). Case-insensitive, one
# item per line: FINDING / LEAD / DERIVED.
_FINDING_RE = re.compile(r"^\s*finding\s*[:\-]\s*(.+)$", re.IGNORECASE)
_LEAD_RE = re.compile(r"^\s*lead\s*[:\-]\s*(.+)$", re.IGNORECASE)
_DERIVED_RE = re.compile(r"^\s*derived\s*[:\-]\s*(.+)$", re.IGNORECASE)
_LEAD_BODY_RE = re.compile(r"^(?P<cap>[\w./-]+)\s+for\s+(?P<kind>[\w./*-]+)", re.IGNORECASE)
_DERIVED_BODY_RE = re.compile(r"^(?P<transform>[\w./-]+)\s*(?:->|=>)\s*(?P<path>.+)$")


@dataclass
class ItemResult:
    """The outcome of fanning one item out: its invocation result or the failure.

    ``ok`` is False iff the worker raised; then ``error`` holds the exception's
    message and ``result`` is None. On success ``result`` is the
    :class:`~rekit.harness.base.HarnessResult` and ``folded`` counts the ledger
    events its fold produced.
    """

    item: Any
    ok: bool
    result: HarnessResult | None = None
    error: str | None = None
    folded: int = 0


@dataclass
class FanoutSummary:
    """The aggregate outcome of one :func:`fan_out` call.

    - ``items``    every :class:`ItemResult`, in the input order.
    - ``ok``       count of items whose invocation succeeded.
    - ``failed``   count of items whose worker raised.
    - ``findings`` total ledger events folded across all successful items.
    - ``peak_concurrency`` the observed maximum of in-flight workers (bounded by
      ``max_concurrency``); populated by the executor's own accounting when
      available, else 0.
    """

    items: list[ItemResult] = field(default_factory=list)
    ok: int = 0
    failed: int = 0
    findings: int = 0
    peak_concurrency: int = 0

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def all_ok(self) -> bool:
        return self.failed == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "ok": self.ok,
            "failed": self.failed,
            "findings": self.findings,
            "peakConcurrency": self.peak_concurrency,
        }


def default_fold(project: Project, item: Any, result: HarnessResult) -> int:
    """Fold one result into the ledger using the loop's tagged-line protocol.

    Parses FINDING / LEAD / DERIVED lines out of ``result.text`` and records each
    against the project's root artifact (findings/derivations) or as a lead.
    Returns the number of ledger events recorded. Called **only** from the parent
    thread by :func:`fan_out`, so its ledger writes are serialised.

    Mirrors :func:`rekit.loop.loop._fold_result` for these three tags; the loop's
    DONE / RUN_SKILL actions are loop-control concerns and are intentionally not
    folded here (fan-out is a converge step, not a driver).
    """
    root = _root_or_none(project)
    recorded = 0
    for raw_line in (result.text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _FINDING_RE.match(line)
        if m and root is not None:
            project.record_finding(root, {"note": m.group(1).strip(), "item": str(item)})
            recorded += 1
            continue

        m = _LEAD_RE.match(line)
        if m:
            body = _LEAD_BODY_RE.match(m.group(1).strip())
            if body:
                project.record_lead(body.group("cap"), body.group("kind"))
            else:
                project.record_lead(m.group(1).strip(), "unknown")
            recorded += 1
            continue

        m = _DERIVED_RE.match(line)
        if m and root is not None:
            body = _DERIVED_BODY_RE.match(m.group(1).strip())
            if body:
                out = _artifact_for(body.group("path").strip())
                if project.record_derivation(body.group("transform"), root, [out]):
                    recorded += 1
            continue

    return recorded


def fan_out(
    project: Project,
    items: Sequence[Any],
    adapter: HarnessAdapter,
    *,
    goal: str,
    build_input: BuildInputFn,
    tier: str = "cheap",
    tools: list[str] | None = None,
    max_concurrency: int = 4,
    fold: FoldFn | None = None,
) -> FanoutSummary:
    """Run one brain invocation per item concurrently, then fold results serially.

    Parameters
    ----------
    project:
        The persistent :class:`~rekit.ledger.project.Project` — the single ledger
        every item converges on. Only the calling (parent) thread writes to it.
    items:
        The independent units of work (trees, artifacts, paths, …). One invocation
        each.
    adapter:
        Any :class:`~rekit.harness.base.HarnessAdapter`. Its
        :meth:`~rekit.harness.base.HarnessAdapter.spawn_subagent` is used per item
        so an adapter can specialise fanned work; the default delegates to
        ``invoke``.
    goal:
        The goal text, handed to every invocation as the system prompt.
    build_input:
        ``item -> user_input`` — builds each item's per-invocation ask.
    tier:
        Model tier for every invocation (fan-out is uniform-tier; the loop owns
        per-step tier escalation).
    tools:
        Optional scoped tool allowlist passed through to every invocation.
    max_concurrency:
        Upper bound on in-flight workers. The pool size is
        ``min(max_concurrency, len(items))`` so a batch never spins more threads
        than there is work.
    fold:
        ``(project, item, result) -> int`` folding one result into the ledger.
        Defaults to :func:`default_fold`. Invoked sequentially in the parent — the
        callback may assume it is the sole ledger writer.

    Returns a :class:`FanoutSummary`. A worker that raises is captured as a failed
    :class:`ItemResult` without sinking the batch; every other item still folds.
    """
    fold = fold if fold is not None else default_fold
    items = list(items)
    summary = FanoutSummary()
    if not items:
        return summary

    # Track live/peak worker concurrency so a caller (and the tests) can prove the
    # cap was respected. Instrumentation only — never gates the ledger.
    live = 0
    peak = 0
    counter_lock = threading.Lock()

    def _work(item: Any) -> HarnessResult:
        nonlocal live, peak
        with counter_lock:
            live += 1
            if live > peak:
                peak = live
        try:
            return adapter.spawn_subagent(
                goal,
                build_input(item),
                tools=tools,
                context=None,
                tier=tier,
            )
        finally:
            with counter_lock:
                live -= 1

    workers = min(max_concurrency, len(items))
    # Concurrent invocations: subprocess/pi calls are I/O-bound, so threads (not
    # processes) are the right pool. Results are collected in input order.
    results: list[tuple[Any, HarnessResult | None, str | None]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_work, item) for item in items]
        for item, fut in zip(items, futures):
            try:
                results.append((item, fut.result(), None))
            except Exception as exc:  # noqa: BLE001 — isolate one item's failure.
                results.append((item, None, f"{type(exc).__name__}: {exc}"))

    summary.peak_concurrency = peak

    # Sequential fold in the parent — the ONLY place the ledger is written, so
    # there are zero concurrent writers by construction.
    for item, result, error in results:
        if error is not None or result is None:
            summary.items.append(ItemResult(item=item, ok=False, error=error))
            summary.failed += 1
            continue
        folded = fold(project, item, result)
        summary.items.append(
            ItemResult(item=item, ok=True, result=result, folded=folded)
        )
        summary.ok += 1
        summary.findings += folded

    return summary


def _root_or_none(project: Project) -> Artifact | None:
    """The project's root artifact (the first-added entry), or None if the ledger
    is empty — mirrors the loop's root resolution."""
    for h in project.ledger.entries:
        return project.ledger.entries[h].artifact
    return None


def _artifact_for(path: str) -> Artifact:
    """Build an Artifact for a DERIVED output path: hash+classify if it exists on
    disk, else a path-addressed placeholder (the brain may name an intended
    output). Mirrors the loop's helper of the same intent."""
    from pathlib import Path

    p = Path(path)
    if p.exists():
        return from_path(p)
    import hashlib

    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
    return Artifact(kind="file", content_hash=digest, path=path, meta={"placeholder": True})
