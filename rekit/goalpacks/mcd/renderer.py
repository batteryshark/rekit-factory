"""The mcd goalpack's own report renderer — the deterministic adjudicator.

This is where the old `prlx-mcd` split between *finding* and *adjudicating*
collapses. In the product a deterministic scan found malicious-code shapes and a
separate model pass (``review()``) classified each; the numbers were then set by
``adjudicate.py``'s deterministic rule. In the goalpack the **brain does both the
finding and the classifying** inside the loop — it emits one ``FINDING:`` line per
behaviour carrying ``sev`` / ``conf`` / ``verdict`` / ``tier`` — and **this
renderer owns the numbers**, exactly as ``adjudicate.py`` did.

What this renderer ports from ``prlx_mcd/adjudicate.py`` + ``assess/disposition.py``:

* **The reviewed-confidence rule** (:func:`_reviewed_confidence`, from
  ``adjudicate._reviewed_confidence``): the reviewer only classifies; the number is
  set deterministically from ``(stated confidence, verdict)`` —

      confirm    -> unchanged
      escalate   -> 0.9 if the dataflow path was traced, else min(conf+0.2, 0.95)
      deescalate -> conf * 0.6
      refute     -> min(conf, 0.1)   (capped low, benign on review)
      suppress   -> 0.0              (dropped as rule noise)

* **Severity is never changed** — it is a shape property; only confidence moves.
* **Refute and suppress stay in the report, flagged** (``excludedFromDisposition``),
  but are dropped from the disposition input — same as ``_EXCLUDED_DECISIONS``.
* **The disposition is RECOMPUTED** over the reviewed confidences by the engine's
  quarantine / review / clear rule (from ``assess.disposition._disposition``):
  quarantine if any high/critical finding has reviewed confidence >= 0.65, else
  review if any mcd finding survives, else clear. The model never sets it.
* **Engine-vs-reviewed is diffable**: each finding keeps both the brain's stated
  confidence (``engineConfidence``) and the deterministically reviewed one
  (``reviewedConfidence``); the disposition keeps no model-set value at all.

The report shape (this goalpack owns it — there is no shared ``report_model``)::

    {
      "summary": {"findingCount", "highestSeverity", "disposition"},
      "findings": [ {id, title, severity, engineConfidence, verdict, tier,
                     reviewedConfidence, confidenceRule, excludedFromDisposition,
                     evidence, round, ...}, ... ],
      "disposition": {recommendation, rationale, drivers, thresholds,
                      engineTierNote, rule},
    }

Alongside :func:`render_report`, :func:`render_markdown` folds that dict into a
readable assessment (summary line, findings grouped by severity with verdict +
reviewed confidence, the disposition and the rule note).
"""

from __future__ import annotations

import re
from typing import Any

# -- the confidence rule, ported verbatim from adjudicate.py -----------------

#: Recognized reviewer verdicts (adjudicate._DECISIONS). An unrecognized verdict
#: is treated as ``confirm`` so a mistagged finding is never dropped.
VERDICTS: tuple[str, ...] = ("confirm", "escalate", "deescalate", "refute", "suppress")

#: Verdicts that keep the finding in the report but drop it from the disposition
#: input (adjudicate._EXCLUDED_DECISIONS).
_EXCLUDED_VERDICTS: frozenset[str] = frozenset({"refute", "suppress"})

# Bounds/factors — the same constants adjudicate.py uses.
_CONF_CEILING = 0.95
_REFUTE_CAP = 0.1
_DEESCALATE_FACTOR = 0.6
_ESCALATE_STEP = 0.2
_ESCALATE_PROVEN = 0.9

#: Severity rank (prlx interpret.common._SEV_RANK): "how bad if real", used only to
#: pick the highest severity and to gate the quarantine rule on high/critical.
_SEV_RANK: dict[str, int] = {
    "informational": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}
_HIGH_BAR = _SEV_RANK["high"]

#: Quarantine confidence bar (disposition._QUARANTINE_MIN_CONF).
_QUARANTINE_MIN_CONF = 0.65

#: The verbatim rule note carried on the report so the deterministic contract is
#: legible in the output (condensed from adjudicate._REVIEW_RULE_NOTE +
#: disposition._THRESHOLD_NOTE).
_REVIEW_RULE_NOTE = (
    "Review confidence rule (deterministic; the reviewer classifies, the engine sets the number): "
    "confirm keeps the finding's confidence; escalate raises it (to 0.9 when the dataflow path was "
    "traced, otherwise +0.2 bounded at 0.95); deescalate attenuates it to 0.6x; refute caps it at 0.1; "
    "suppress sets it to 0. Refute and suppress keep the finding in the report but drop it from the "
    "malicious-code disposition. Severity is never changed."
)

_DISPOSITION_RULE_NOTE = (
    "Disposition rule (deterministic; severity and confidence kept separate): quarantine if any "
    "high/critical-severity finding has reviewed confidence >= 0.65; review if malicious-code findings "
    "exist but do not meet that bar; clear if there are no surviving findings. The model never sets the "
    "disposition; it is recomputed over the reviewed confidences."
)

# "[sev:high conf:0.8 verdict:confirm tier:3 path:proven] <title> :: <evidence>"
# The bracket carries the structured fields; the rest is "title :: evidence".
_TAG_RE = re.compile(r"^\s*\[(?P<fields>[^\]]*)\]\s*(?P<rest>.*)$", re.DOTALL)
# key:value tokens inside the bracket (value may be a float, a word, or bare).
_FIELD_RE = re.compile(r"([A-Za-z_]+)\s*:\s*([^\s\]]+)")


def _reviewed_confidence(base: float, verdict: str, path_proven: bool) -> tuple[float, str]:
    """Map ``(stated confidence, verdict)`` to a reviewed confidence + the rule text
    that produced it. Ported from ``adjudicate._reviewed_confidence`` — bounded and
    deterministic; the reviewer never emits this number."""
    if verdict == "suppress":
        return 0.0, "suppress -> 0.0 (dropped from the disposition as noise)"
    if verdict == "refute":
        return round(min(base, _REFUTE_CAP), 2), f"refute -> min({base}, {_REFUTE_CAP}) (capped low, benign on review)"
    if verdict == "deescalate":
        return round(base * _DEESCALATE_FACTOR, 2), f"deescalate -> {base} x {_DEESCALATE_FACTOR}"
    if verdict == "escalate":
        if path_proven:
            return _ESCALATE_PROVEN, f"escalate (path proven) -> {_ESCALATE_PROVEN}"
        return (
            round(min(_CONF_CEILING, base + _ESCALATE_STEP), 2),
            f"escalate -> min({base} + {_ESCALATE_STEP}, {_CONF_CEILING})",
        )
    # confirm (and any unknown verdict, treated as confirm)
    return round(base, 2), "confirm -> unchanged"


def render_report(project: Any, goalpack: Any, summary: Any) -> dict[str, Any]:
    """Fold the ledger's findings into the mcd assessment, applying the
    deterministic rule.

    Each ledger finding's ``note`` is the brain's line minus the ``FINDING:`` prefix
    (the loop strips it), so it reads ``[sev:.. conf:.. verdict:.. tier:..] title ::
    evidence``. This parses those fields, sets the reviewed confidence by the ported
    rule, keeps severity untouched, flags refute/suppress as excluded, and recomputes
    the disposition over the surviving reviewed confidences.
    """
    findings: list[dict[str, Any]] = []
    for i, finding in enumerate(project.ledger.findings()):
        note = finding.get("note") or finding.get("text") or finding.get("summary") or ""
        findings.append(_build_finding(str(note), i, finding))

    disposition = _recompute_disposition(findings)
    highest = _highest_severity(findings)

    return {
        "summary": {
            "findingCount": len(findings),
            "highestSeverity": highest,
            "disposition": disposition["recommendation"],
        },
        "findings": findings,
        "disposition": disposition,
    }


def _build_finding(note: str, index: int, raw: dict[str, Any]) -> dict[str, Any]:
    """Parse one finding note and adjudicate it by the deterministic rule."""
    fields, title, evidence = _parse_note(note)

    severity = _norm_severity(fields.get("sev") or fields.get("severity"))
    base = _as_float(fields.get("conf") or fields.get("confidence"), default=0.0)
    verdict = _norm_verdict(fields.get("verdict") or fields.get("decision"))
    tier = _norm_tier(fields.get("tier"))
    path_proven = _is_truthy(fields.get("path")) or _is_truthy(fields.get("pathProven"))

    reviewed, rule = _reviewed_confidence(base, verdict, path_proven)
    excluded = verdict in _EXCLUDED_VERDICTS

    entry: dict[str, Any] = {
        "id": f"mcd-{index + 1:03d}",
        "title": title or note.strip(),
        "severity": severity,          # never changed — a shape property
        "engineConfidence": round(base, 2),   # what the brain stated (diffable)
        "verdict": verdict,
        "responseTier": tier,
        "reviewedConfidence": reviewed,        # what the rule set
        "confidenceRule": rule,
        "excludedFromDisposition": excluded,
    }
    if verdict == "escalate":
        entry["pathProven"] = path_proven
    if evidence:
        entry["evidence"] = evidence
    if raw.get("round") is not None:
        entry["round"] = raw["round"]
    return entry


def _recompute_disposition(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Recompute clear / review / quarantine over the REVIEWED confidences.

    Ported from ``assess.disposition._disposition`` (the single-artifact case, no
    cross-file reachability): quarantine if any surviving high/critical finding has
    reviewed confidence >= 0.65; review if any surviving mcd finding remains; clear
    otherwise. Refuted/suppressed findings are dropped from the input first.
    """
    surviving = [f for f in findings if not f.get("excludedFromDisposition")]

    if not surviving:
        return {
            "recommendation": "clear",
            "rationale": (
                "No surviving malicious-code (mcd) findings. This is 'clear of mcd findings', not a "
                "guarantee of safety."
            ),
            "drivers": [],
            "thresholds": _DISPOSITION_RULE_NOTE,
            "rule": _REVIEW_RULE_NOTE,
        }

    high_conf = [
        f for f in surviving
        if _SEV_RANK.get(f.get("severity"), 0) >= _HIGH_BAR
        and (f.get("reviewedConfidence") or 0) >= _QUARANTINE_MIN_CONF
    ]
    if high_conf:
        drivers = [
            f"{len(high_conf)} high/critical-severity finding(s) at reviewed confidence "
            f">= {_QUARANTINE_MIN_CONF}"
        ]
        return {
            "recommendation": "quarantine",
            "rationale": (
                "Recommend quarantine: " + "; ".join(drivers) + ". Hold the artifact and do not install "
                "or run it until the verification questions are answered. This is a recommended "
                "next-action from the evidence, not a maliciousness verdict; severity and confidence "
                "are reported separately."
            ),
            "drivers": drivers,
            "thresholds": _DISPOSITION_RULE_NOTE,
            "rule": _REVIEW_RULE_NOTE,
        }

    if any(_SEV_RANK.get(f.get("severity"), 0) >= _HIGH_BAR for f in surviving):
        why = "high-severity findings exist but below the quarantine confidence bar"
    else:
        why = "no high/critical-severity findings"
    return {
        "recommendation": "review",
        "rationale": (
            f"Recommend review: malicious-code findings are present but do not meet the quarantine bar "
            f"({why}). Have an engineer resolve the verification questions before relying on the code."
        ),
        "drivers": [why],
        "thresholds": _DISPOSITION_RULE_NOTE,
        "rule": _REVIEW_RULE_NOTE,
    }


# -- note parsing ------------------------------------------------------------


def _parse_note(note: str) -> tuple[dict[str, str], str, str]:
    """Split a finding note into ``(fields, title, evidence)``.

    ``[sev:.. conf:.. verdict:.. tier:..] <title> :: <evidence>`` — a missing bracket
    yields empty fields (so the finding still lands, with defaults); a missing ``::``
    yields empty evidence.
    """
    m = _TAG_RE.match(note)
    if not m:
        title, evidence = _split_evidence(note.strip())
        return {}, title, evidence
    fields = {k.lower(): v for k, v in _FIELD_RE.findall(m.group("fields"))}
    title, evidence = _split_evidence(m.group("rest").strip())
    return fields, title, evidence


def _split_evidence(rest: str) -> tuple[str, str]:
    """Split ``title :: evidence`` on the first ``::``; no ``::`` means all title."""
    title, sep, evidence = rest.partition("::")
    return title.strip(), evidence.strip() if sep else ""


def _norm_severity(value: Any) -> str:
    s = str(value or "").strip().lower()
    return s if s in _SEV_RANK else "informational"


def _norm_verdict(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in VERDICTS else "confirm"


def _norm_tier(value: Any) -> int | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return max(0, min(5, int(float(s))))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any, *, default: float) -> float:
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, f))


def _is_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"proven", "true", "yes", "1", "path"}


def _highest_severity(findings: list[dict[str, Any]]) -> str | None:
    sevs = [f.get("severity") for f in findings if f.get("severity")]
    return max(sevs, key=lambda s: _SEV_RANK.get(s, 0)) if sevs else None


# -- markdown companion ------------------------------------------------------

#: Severity order for grouping (highest first).
_SEV_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low", "informational")


def render_markdown(report: dict[str, Any]) -> str:
    """Render the mcd assessment to a readable Markdown document.

    Summary line (finding count, highest severity, disposition), findings grouped by
    severity with each finding's verdict and reviewed confidence (engine-stated shown
    when it differs, so the review is diffable), then the disposition and the
    deterministic rule note.
    """
    summary = report.get("summary") or {}
    disposition = report.get("disposition") or {}
    findings = report.get("findings") or []

    lines: list[str] = ["# Malicious-code assessment", ""]
    lines.append(
        f"_{summary.get('findingCount', 0)} finding(s); highest severity "
        f"{summary.get('highestSeverity') or 'none'}; disposition "
        f"**{(summary.get('disposition') or 'clear').upper()}**._"
    )
    lines.append("")

    by_sev: dict[str, list[dict[str, Any]]] = {s: [] for s in _SEV_ORDER}
    for f in findings:
        by_sev.setdefault(f.get("severity") or "informational", []).append(f)

    for sev in _SEV_ORDER:
        entries = by_sev.get(sev) or []
        if not entries:
            continue
        lines.append(f"## {sev.capitalize()}")
        lines.append("")
        for f in entries:
            title = str(f.get("title") or "").strip() or "(untitled)"
            verdict = f.get("verdict")
            reviewed = f.get("reviewedConfidence")
            engine = f.get("engineConfidence")
            flag = " — dropped from disposition" if f.get("excludedFromDisposition") else ""
            conf_bit = f"reviewed confidence {reviewed}"
            if engine is not None and engine != reviewed:
                conf_bit += f" (brain stated {engine})"
            lines.append(f"- **{title}** — {verdict}, {conf_bit}{flag}")
            evidence = str(f.get("evidence") or "").strip()
            if evidence:
                lines.append(f"  - evidence: {evidence}")
        lines.append("")

    lines.append("## Disposition")
    lines.append("")
    lines.append(f"**{(disposition.get('recommendation') or 'clear').upper()}** — {disposition.get('rationale', '')}")
    drivers = disposition.get("drivers") or []
    if drivers:
        lines.append("")
        for d in drivers:
            lines.append(f"- {d}")
    lines.append("")
    if disposition.get("rule"):
        lines.append(f"_{disposition['rule']}_")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
