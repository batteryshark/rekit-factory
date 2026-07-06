"""The understand goalpack's own report renderer.

Reads the ledger's **generic** findings — the shared substrate — and folds them
into understand's four-section shape. This shape belongs to the goalpack; there is
no shared ``report_model``. It mirrors the legacy ``prlx-understand`` output:

    {
        "does": [...],          # what the code does
        "decides": [...],       # the decisions it makes
        "brittle": [...],       # where it is fragile
        "surprising": [...],    # anything unexpected
        "summary": {...},       # counts + loop metadata
    }

The brain tags each finding with its lens via the loop's `FINDING:` protocol, e.g.
``FINDING: [brittle] assumes config.json exists``. By the time it reaches the ledger
the loop has stripped the ``FINDING:`` prefix, so each finding's ``note`` reads
``[brittle] assumes config.json exists``. This renderer peels the ``[lens]`` tag and
buckets the text under the matching section.
"""

from __future__ import annotations

import re
from typing import Any

#: The four lenses understand reports on, in report order.
LENSES: tuple[str, ...] = ("does", "decides", "brittle", "surprising")

#: Findings whose lens tag is missing/unrecognized land here so nothing is lost.
_FALLBACK_LENS = "does"

# "[lens] rest of the text" — case-insensitive; tolerant of surrounding space.
_LENS_RE = re.compile(r"^\s*\[\s*(?P<lens>[a-zA-Z]+)\s*\]\s*(?P<text>.*)$", re.DOTALL)


def render_report(project: Any, goalpack: Any, summary: Any) -> dict[str, Any]:
    """Group the ledger's findings by lens into understand's four-section report.

    ``project`` gives access to the ledger (the generic findings substrate),
    ``goalpack`` carries identity/metadata, and ``summary`` is the loop's
    :class:`~rekit.loop.LoopSummary` (round/finding counts, done state).
    """
    sections: dict[str, list[dict[str, Any]]] = {lens: [] for lens in LENSES}

    for finding in project.ledger.findings():
        note = finding.get("note") or finding.get("text") or finding.get("summary") or ""
        lens, text = _split_lens(str(note))
        entry: dict[str, Any] = {"text": text}
        # Carry through the artifact provenance the ledger annotated onto the finding.
        if finding.get("artifact"):
            entry["artifact"] = finding["artifact"]
        if finding.get("artifactPath"):
            entry["path"] = finding["artifactPath"]
        if finding.get("round") is not None:
            entry["round"] = finding["round"]
        sections[lens].append(entry)

    report: dict[str, Any] = {lens: sections[lens] for lens in LENSES}
    report["summary"] = _summary(goalpack, summary, sections)
    return report


def _split_lens(note: str) -> tuple[str, str]:
    """Peel a ``[lens]`` prefix off a finding note.

    Returns ``(lens, text)``; an unrecognized or missing lens falls back to
    ``does`` with the whole note as text, so a mistagged finding is never dropped.
    """
    m = _LENS_RE.match(note)
    if m:
        lens = m.group("lens").strip().lower()
        if lens in LENSES:
            return lens, m.group("text").strip()
    return _FALLBACK_LENS, note.strip()


def _summary(goalpack: Any, summary: Any, sections: dict[str, list]) -> dict[str, Any]:
    """A small header: goalpack identity, per-lens counts, and loop metadata."""
    out: dict[str, Any] = {
        "goalpack": getattr(goalpack, "name", "understand"),
        "title": getattr(goalpack, "title", ""),
        "total": sum(len(v) for v in sections.values()),
        "counts": {lens: len(sections[lens]) for lens in LENSES},
    }
    if summary is not None:
        out["done"] = getattr(summary, "done", None)
        out["rounds"] = getattr(summary, "round_count", None)
    return out
