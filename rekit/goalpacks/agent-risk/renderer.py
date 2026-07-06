"""The agent-risk goalpack's own report renderer.

Reads the ledger's **generic** findings — the shared substrate — and folds them
into agent-risk's report shape. This shape belongs to the goalpack; there is no
shared ``report_model``. It translates the legacy ``prlx-agent-risk`` output
(``combos`` + ``posture`` + a review plan + recommendations) to the loop model:

    {
        "headline": "...",              # one-line verdict over the surface
        "summary": {...},               # counts + loop metadata
        "combos": [...],                # dangerous capability compositions
        "posture": [...],               # standing surface issues
        "recommendations": [...],       # concrete gate/sandbox/do-not-co-enable actions
        "reviewPlan": {...},            # what a human/agent reviewer should look at first
    }

The brain tags each finding with its kind via the loop's ``FINDING:`` protocol, e.g.
``FINDING: [combo] exfil: `reader` reads secrets and `webhook` sends them``. By the
time it reaches the ledger the loop has stripped the ``FINDING:`` prefix, so each
finding's ``note`` reads ``[combo] exfil: ...``. This renderer peels the ``[tag]``
and buckets the text under the matching section.

agent-risk *does* want a report. Alongside :func:`render_report` (the structured
dict), this module provides :func:`render_markdown` — the optional companion the
goalpack contract looks for — which folds that same dict into a readable Markdown
document. :func:`rekit.goalpacks.run_goalpack` persists both as ``report/json`` +
``report/markdown`` ledger artifacts.
"""

from __future__ import annotations

import re
from typing import Any

#: The finding kinds agent-risk buckets on, in report order.
TAGS: tuple[str, ...] = ("combo", "posture", "recommend")

#: A finding whose tag is missing/unrecognized lands here so nothing is lost. A
#: mistagged observation is still a standing surface note, so it belongs in posture.
_FALLBACK_TAG = "posture"

# "[tag] rest of the text" — case-insensitive; tolerant of surrounding space.
_TAG_RE = re.compile(r"^\s*\[\s*(?P<tag>[a-zA-Z]+)\s*\]\s*(?P<text>.*)$", re.DOTALL)

#: The dangerous abuse paths a ``[combo]`` finding names, matched at the head of the
#: combo text (``exfil: ...``) so the review plan can group compositions by path.
ABUSE_PATHS: tuple[str, ...] = ("exfil", "rce", "drop")
_PATH_RE = re.compile(r"^\s*(?P<path>[a-zA-Z]+)\s*[:\-]\s*(?P<rest>.*)$", re.DOTALL)

#: A self-sufficient composition (one skill supplies both halves) is sharper than a
#: cross-skill one; the brain says which in the text, and the review plan keys on it.
_SELF_SUFFICIENT_RE = re.compile(r"self-?sufficient", re.IGNORECASE)


def render_report(project: Any, goalpack: Any, summary: Any) -> dict[str, Any]:
    """Group the ledger's findings by tag into agent-risk's report shape.

    ``project`` gives access to the ledger (the generic findings substrate),
    ``goalpack`` carries identity/metadata, and ``summary`` is the loop's
    :class:`~rekit.loop.LoopSummary` (round/finding counts, done state).
    """
    buckets: dict[str, list[dict[str, Any]]] = {tag: [] for tag in TAGS}

    for finding in project.ledger.findings():
        note = finding.get("note") or finding.get("text") or finding.get("summary") or ""
        tag, text = _split_tag(str(note))
        entry: dict[str, Any] = {"text": text}
        if finding.get("round") is not None:
            entry["round"] = finding["round"]
        if tag == "combo":
            path, why = _split_path(text)
            entry["path"] = path
            entry["selfSufficient"] = bool(_SELF_SUFFICIENT_RE.search(text))
            if why:
                entry["why"] = why
        buckets[tag].append(entry)

    combos = buckets["combo"]
    posture = buckets["posture"]
    recommendations = [e["text"] for e in buckets["recommend"]]

    report: dict[str, Any] = {
        "headline": _headline(combos, posture),
        "summary": _summary(goalpack, summary, combos, posture, recommendations),
        "combos": combos,
        "posture": posture,
        "reviewPlan": _review_plan(combos, posture),
        "recommendations": recommendations,
    }
    return report


def _split_tag(note: str) -> tuple[str, str]:
    """Peel a ``[tag]`` prefix off a finding note.

    Returns ``(tag, text)``; an unrecognized or missing tag falls back to
    ``posture`` with the whole note as text, so a mistagged finding is never dropped.
    """
    m = _TAG_RE.match(note)
    if m:
        tag = m.group("tag").strip().lower()
        if tag in TAGS:
            return tag, m.group("text").strip()
    return _FALLBACK_TAG, note.strip()


def _split_path(text: str) -> tuple[str | None, str]:
    """Peel a leading ``<path>:`` abuse-path label (``exfil``/``rce``/``drop``) off a
    combo finding. Returns ``(path_or_None, rest)``; text with no recognized label
    yields ``(None, text)`` so an unlabelled composition is still reported."""
    m = _PATH_RE.match(text)
    if m:
        path = m.group("path").strip().lower()
        if path in ABUSE_PATHS:
            return path, m.group("rest").strip()
    return None, text.strip()


def _headline(combos: list[dict], posture: list[dict]) -> str:
    """A one-line verdict over the surface, keyed on the sharpest thing found.

    A self-sufficient composition (one skill completes an abuse path alone) is the
    sharpest signal; then any composition at all; then posture-only; then nothing.
    """
    self_sufficient = sum(1 for c in combos if c.get("selfSufficient"))
    if self_sufficient:
        verb = "completes" if self_sufficient == 1 else "complete"
        return (
            f"Elevated agent risk: {_count(self_sufficient, 'composition')} "
            f"{verb} an abuse path alone, across {_count(len(combos), 'dangerous composition')} "
            f"and {_count(len(posture), 'posture issue')}."
        )
    if combos:
        return (
            f"Agent risk on this surface: {_count(len(combos), 'dangerous capability composition')} "
            f"reachable (by co-enablement), plus {_count(len(posture), 'posture issue')}."
        )
    if posture:
        return (
            f"No dangerous capability compositions found, but "
            f"{_count(len(posture), 'posture issue')} on this surface still need a decision."
        )
    return "No dangerous capability compositions or posture issues found on this surface."


def _review_plan(combos: list[dict], posture: list[dict]) -> dict[str, Any]:
    """What a reviewer should look at first: self-sufficient compositions before
    cross-skill ones, then posture. Groups compositions by abuse path so the queue
    reads as 'here are the exfil paths, here are the rce paths', etc."""
    self_sufficient = [c["text"] for c in combos if c.get("selfSufficient")]
    cross_skill = [c["text"] for c in combos if not c.get("selfSufficient")]

    by_path: dict[str, int] = {p: 0 for p in ABUSE_PATHS}
    unlabelled = 0
    for c in combos:
        path = c.get("path")
        if path in by_path:
            by_path[path] += 1
        else:
            unlabelled += 1

    tiers = [
        {
            "id": "must_review",
            "why": "Compositions where a single skill completes an abuse path on its own.",
            "count": len(self_sufficient),
            "items": self_sufficient,
        },
        {
            "id": "should_review",
            "why": "Compositions that complete an abuse path only once two skills are co-enabled.",
            "count": len(cross_skill),
            "items": cross_skill,
        },
        {
            "id": "posture_review",
            "why": "Standing surface issues (over-broad grants, steerable inputs, undeclared reach).",
            "count": len(posture),
            "items": [p["text"] for p in posture],
        },
    ]
    return {
        "policy": "tiered-composition-review",
        "byAbusePath": {p: by_path[p] for p in ABUSE_PATHS if by_path[p]},
        "unlabelledCompositions": unlabelled,
        "tiers": tiers,
    }


def _summary(
    goalpack: Any,
    summary: Any,
    combos: list[dict],
    posture: list[dict],
    recommendations: list[str],
) -> dict[str, Any]:
    """A small header: goalpack identity, per-tag counts, and loop metadata."""
    out: dict[str, Any] = {
        "goalpack": getattr(goalpack, "name", "agent-risk"),
        "title": getattr(goalpack, "title", ""),
        "total": len(combos) + len(posture) + len(recommendations),
        "counts": {
            "combos": len(combos),
            "posture": len(posture),
            "recommendations": len(recommendations),
            "selfSufficient": sum(1 for c in combos if c.get("selfSufficient")),
        },
    }
    if summary is not None:
        out["done"] = getattr(summary, "done", None)
        out["rounds"] = getattr(summary, "round_count", None)
    return out


def _count(n: int, noun: str) -> str:
    """``n noun`` with a naive plural (``composition`` -> ``compositions``)."""
    if n == 1:
        return f"1 {noun}"
    plural = noun + ("es" if noun.endswith(("s", "x", "z", "sh", "ch")) else "s")
    return f"{n} {plural}"


#: Human-readable heading per abuse path, for the markdown grouping.
_PATH_TITLES: dict[str, str] = {
    "exfil": "Exfil (read secrets, then send them off-host)",
    "rce": "RCE (outside/agent-directed input drives code execution)",
    "drop": "Drop (write a file, then execute it)",
}


def render_markdown(report: dict[str, Any]) -> str:
    """Render agent-risk's structured report to a readable Markdown document.

    The contract: the goalpack's :func:`render_report` returns the structured dict;
    this companion takes that same dict and returns a string. Headline, then the
    dangerous compositions grouped by abuse path, then posture, then recommendations
    and the review plan. A short header carries the title and per-tag counts.
    """
    header = report.get("summary") or {}
    title = header.get("title") or header.get("goalpack") or "agent-risk"

    lines: list[str] = [f"# {title}", ""]
    if report.get("headline"):
        lines.append(f"> {report['headline']}")
        lines.append("")

    counts = header.get("counts") or {}
    total = header.get("total")
    if total is not None:
        lines.append(
            f"_{total} finding(s) — "
            f"{counts.get('combos', 0)} composition(s), "
            f"{counts.get('posture', 0)} posture issue(s), "
            f"{counts.get('recommendations', 0)} recommendation(s)._"
        )
        lines.append("")
    lines.append(
        "_A composition is dangerous whether one skill supplies both halves or two "
        "co-enabled skills each supply one. Capability is separate from intent._"
    )
    lines.append("")

    # Dangerous compositions, grouped by abuse path (labelled first, then the rest).
    lines.append("## Dangerous capability compositions")
    lines.append("")
    combos = report.get("combos") or []
    if not combos:
        lines.append("_None found._")
        lines.append("")
    else:
        by_path: dict[str, list[dict]] = {}
        for c in combos:
            by_path.setdefault(c.get("path") or "other", []).append(c)
        for path in list(ABUSE_PATHS) + ["other"]:
            entries = by_path.get(path)
            if not entries:
                continue
            lines.append(f"### {_PATH_TITLES.get(path, 'Other compositions')}")
            lines.append("")
            for entry in entries:
                text = str(entry.get("why") or entry.get("text", "")).strip()
                mark = " **(self-sufficient)**" if entry.get("selfSufficient") else ""
                lines.append(f"- {text or '(no text)'}{mark}")
            lines.append("")

    lines.append("## Posture")
    lines.append("")
    posture = report.get("posture") or []
    if not posture:
        lines.append("_No standing surface issues recorded._")
        lines.append("")
    else:
        for entry in posture:
            text = str(entry.get("text", "")).strip()
            lines.append(f"- {text or '(no text)'}")
        lines.append("")

    lines.append("## Recommendations")
    lines.append("")
    recs = report.get("recommendations") or []
    if not recs:
        lines.append("_None recorded._")
        lines.append("")
    else:
        for rec in recs:
            lines.append(f"- {str(rec).strip()}")
        lines.append("")

    plan = report.get("reviewPlan") or {}
    if plan.get("tiers"):
        lines.append("## Review plan")
        lines.append("")
        for tier in plan["tiers"]:
            heading = str(tier.get("id", "")).replace("_", " ").title()
            lines.append(f"- **{heading} ({tier.get('count', 0)})** — {tier.get('why', '')}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
