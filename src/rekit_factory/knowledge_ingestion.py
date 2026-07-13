"""Pure schemas for gated OKF knowledge contribution proposals.

This module deliberately performs no filesystem or git mutation.  It models the
boundary between append-only investigation evidence and a reviewed rekit-kb
change that a later integration layer may stage.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import re
from pathlib import PurePosixPath
from typing import Literal


class MaterialClass(str, Enum):
    DURABLE_KNOWLEDGE = "durable_knowledge"
    TARGET_SPECIFIC_EVIDENCE = "target_specific_evidence"
    BEHAVIORAL_INSTRUCTION = "behavioral_instruction"
    EXECUTABLE_CODE = "executable_code"
    RAW_LOG = "raw_log"


class ProhibitedContent(str, Enum):
    CREDENTIAL = "credential"
    PRIVATE_KEY = "private_key"
    PERSONAL_DATA = "personal_data"
    RAW_BINARY = "raw_binary"
    PERSISTENCE_PAYLOAD = "persistence_payload"
    WEAPONIZED_CHAIN = "weaponized_chain"


class ClaimKind(str, Enum):
    OBSERVATION = "observation"
    THEORY = "theory"


class DedupeDisposition(str, Enum):
    NOT_RUN = "not_run"
    NO_MATCH = "no_match"
    ENRICH_EXISTING = "enrich_existing"
    OVERLAP_DISTINCT = "overlap_distinct"


class ReviewState(str, Enum):
    EXTRACTED = "extracted"
    READY = "ready_for_operator"
    APPROVED = "approved"
    DENIED = "denied"


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONCEPT_TYPES = frozenset({
    "Technique", "Protection", "Pattern", "Tool", "Playbook", "Process",
    "Reference", "Glossary",
})
_TYPE_DIRECTORIES = {
    "Technique": "techniques",
    "Protection": "protections",
    "Pattern": "protections",
    "Tool": "references",
    "Playbook": "playbooks",
    "Process": "processes",
    "Reference": "references",
    "Glossary": "references",
}
_SENSITIVE_PATTERNS = (
    (ProhibitedContent.PRIVATE_KEY, re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (ProhibitedContent.CREDENTIAL, re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (ProhibitedContent.CREDENTIAL, re.compile(
        r"(?i)\b(?:api[_ -]?key|access[_ -]?token|password)\s*[:=]\s*['\"]?[^\s'\"]{8,}"
    )),
)


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{name} must be a stable identifier")


def _concept_path(value: str, name: str = "concept_path") -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or path.suffix != ".md" or ".." in path.parts:
        raise ValueError(f"{name} must be a bundle-relative Markdown path")
    if path.name in {"index.md", "log.md"}:
        raise ValueError(f"{name} cannot use an OKF reserved filename")


@dataclass(frozen=True)
class EvidenceCitation:
    citation_id: str
    run_id: str
    artifact_id: str | None = None
    event_id: str | None = None
    artifact_sha256: str | None = None
    locator: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.citation_id, "citation_id")
        _identifier(self.run_id, "run_id")
        if not self.artifact_id and not self.event_id:
            raise ValueError("a citation must identify an artifact or event")
        if self.artifact_id:
            _identifier(self.artifact_id, "artifact_id")
        if self.event_id:
            _identifier(self.event_id, "event_id")
        if self.artifact_sha256 is not None and not _SHA256.fullmatch(self.artifact_sha256):
            raise ValueError("artifact_sha256 must be a lowercase SHA-256 digest")
        if self.artifact_sha256 is not None and self.artifact_id is None:
            raise ValueError("artifact_sha256 requires artifact_id")

    @property
    def factory_uri(self) -> str:
        if self.event_id:
            return f"factory://runs/{self.run_id}/events/{self.event_id}"
        return f"factory://runs/{self.run_id}/artifacts/{self.artifact_id}"


@dataclass(frozen=True)
class KnowledgeClaim:
    kind: ClaimKind
    text: str
    citation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.text, "claim text")
        if not self.citation_ids:
            raise ValueError("every claim must cite durable run evidence")
        for citation_id in self.citation_ids:
            _identifier(citation_id, "citation_id")


@dataclass(frozen=True)
class DedupeQuery:
    terms: tuple[str, ...]
    concept_type: str
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.terms or any(not term.strip() for term in self.terms):
            raise ValueError("dedupe terms must contain non-empty search terms")
        if self.concept_type not in _CONCEPT_TYPES:
            raise ValueError("dedupe concept_type must use the rekit-kb taxonomy")

    @property
    def rg_arguments(self) -> tuple[str, ...]:
        """Data for a future read-only search adapter; never executes a process."""
        return ("rg", "-l", "-i", "|".join(re.escape(term) for term in self.terms), ".")


@dataclass(frozen=True)
class DedupeResult:
    query: DedupeQuery
    disposition: DedupeDisposition = DedupeDisposition.NOT_RUN
    matched_paths: tuple[str, ...] = ()
    existing_concept_path: str | None = None
    rationale: str = ""

    def __post_init__(self) -> None:
        for path in self.matched_paths:
            _concept_path(path, "matched_path")
        if self.existing_concept_path is not None:
            _concept_path(self.existing_concept_path)
        if self.disposition is DedupeDisposition.ENRICH_EXISTING:
            if self.existing_concept_path is None:
                raise ValueError("enrichment requires existing_concept_path")
        elif self.existing_concept_path is not None:
            raise ValueError("existing_concept_path is valid only for enrichment")
        if self.disposition is not DedupeDisposition.NOT_RUN:
            _text(self.rationale, "dedupe rationale")


@dataclass(frozen=True)
class KnowledgeCandidate:
    candidate_id: str
    material_class: MaterialClass
    concept_type: str
    slug: str
    title: str
    description: str
    tags: tuple[str, ...]
    observations: tuple[KnowledgeClaim, ...]
    theories: tuple[KnowledgeClaim, ...]
    citations: tuple[EvidenceCitation, ...]
    relationships: tuple[str, ...] = ()
    prohibited_content: tuple[ProhibitedContent, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "candidate_id")
        if self.material_class is not MaterialClass.DURABLE_KNOWLEDGE:
            raise ValueError(f"{self.material_class.value} cannot become a knowledge candidate")
        if self.prohibited_content:
            labels = ", ".join(sorted(item.value for item in self.prohibited_content))
            raise ValueError(f"prohibited content rejected before review: {labels}")
        if self.concept_type not in _CONCEPT_TYPES:
            raise ValueError("concept_type must use the rekit-kb taxonomy")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", self.slug):
            raise ValueError("slug must be lowercase kebab-case")
        _text(self.title, "title")
        _text(self.description, "description")
        if not self.tags or any(not tag.strip() for tag in self.tags):
            raise ValueError("candidate must contain controlled-vocabulary tags")
        if not self.observations:
            raise ValueError("candidate must contain at least one cited observation")
        if any(claim.kind is not ClaimKind.OBSERVATION for claim in self.observations):
            raise ValueError("observations must be explicitly classified as observation")
        if any(claim.kind is not ClaimKind.THEORY for claim in self.theories):
            raise ValueError("theories must be explicitly classified as theory")
        if not self.citations:
            raise ValueError("candidate must retain Factory provenance")
        citation_ids = [citation.citation_id for citation in self.citations]
        if len(set(citation_ids)) != len(citation_ids):
            raise ValueError("citation IDs must be unique")
        referenced = {
            citation_id
            for claim in (*self.observations, *self.theories)
            for citation_id in claim.citation_ids
        }
        unknown = referenced.difference(citation_ids)
        if unknown:
            raise ValueError(f"claims reference unknown citations: {sorted(unknown)!r}")
        if set(citation_ids).difference(referenced):
            raise ValueError("every candidate citation must support at least one claim")
        for relationship in self.relationships:
            if not relationship.startswith("/"):
                raise ValueError("relationships must use absolute bundle-relative links")
            _concept_path(relationship.removeprefix("/"), "relationship")
        detected = scan_prohibited_text(
            "\n".join((self.title, self.description, *(c.text for c in self.observations),
                       *(c.text for c in self.theories)))
        )
        if detected:
            labels = ", ".join(item.value for item in detected)
            raise ValueError(f"prohibited content rejected before review: {labels}")


@dataclass(frozen=True)
class OperatorDecision:
    operator_id: str
    approved: bool
    rationale: str
    decided_at: str

    def __post_init__(self) -> None:
        _text(self.operator_id, "operator_id")
        _text(self.rationale, "decision rationale")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", self.decided_at):
            raise ValueError("decided_at must be an ISO 8601 UTC timestamp")


@dataclass(frozen=True)
class CandidateReview:
    candidate: KnowledgeCandidate
    dedupe: DedupeResult
    state: ReviewState = ReviewState.EXTRACTED
    decision: OperatorDecision | None = None

    def ready(self) -> "CandidateReview":
        if self.state is not ReviewState.EXTRACTED:
            raise ValueError("only an extracted candidate can enter review")
        if self.dedupe.disposition is DedupeDisposition.NOT_RUN:
            raise ValueError("deduplication must complete before operator review")
        return replace(self, state=ReviewState.READY)

    def decide(self, decision: OperatorDecision) -> "CandidateReview":
        if self.state is not ReviewState.READY:
            raise ValueError("only a ready candidate can receive an operator decision")
        return replace(
            self,
            state=ReviewState.APPROVED if decision.approved else ReviewState.DENIED,
            decision=decision,
        )


@dataclass(frozen=True)
class StagingPlan:
    candidate_id: str
    branch_name: str
    worktree_name: str
    concept_path: str
    index_path: str
    log_path: Literal["log.md"] = "log.md"
    validation_command: tuple[str, ...] = (
        "python3", "scripts/okf_validate.py", ".", "--strict",
    )


def build_dedupe_query(candidate: KnowledgeCandidate) -> DedupeQuery:
    terms = tuple(dict.fromkeys((candidate.slug.replace("-", " "), candidate.title)))
    return DedupeQuery(terms=terms, concept_type=candidate.concept_type, tags=candidate.tags)


def build_staging_plan(review: CandidateReview) -> StagingPlan:
    """Describe approved git/filesystem work without performing any mutation."""
    if review.state is not ReviewState.APPROVED or review.decision is None:
        raise PermissionError("explicit operator approval is required before staging")
    candidate = review.candidate
    if review.dedupe.disposition is DedupeDisposition.ENRICH_EXISTING:
        concept_path = review.dedupe.existing_concept_path
        assert concept_path is not None
    else:
        concept_path = f"{_TYPE_DIRECTORIES[candidate.concept_type]}/{candidate.slug}.md"
    parent = str(PurePosixPath(concept_path).parent)
    return StagingPlan(
        candidate_id=candidate.candidate_id,
        branch_name=f"factory/kb-{candidate.candidate_id}",
        worktree_name=f"rekit-kb-{candidate.candidate_id}",
        concept_path=concept_path,
        index_path=f"{parent}/index.md" if parent != "." else "index.md",
    )


def scan_prohibited_text(value: str) -> tuple[ProhibitedContent, ...]:
    """Conservative deterministic screen; semantic classifiers add explicit flags."""
    found = []
    for category, pattern in _SENSITIVE_PATTERNS:
        if pattern.search(value) and category not in found:
            found.append(category)
    return tuple(found)
