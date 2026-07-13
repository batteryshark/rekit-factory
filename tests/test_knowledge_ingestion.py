from __future__ import annotations

import unittest

from rekit_factory.knowledge_ingestion import (
    CandidateReview,
    ClaimKind,
    DedupeDisposition,
    DedupeResult,
    EvidenceCitation,
    KnowledgeCandidate,
    KnowledgeClaim,
    MaterialClass,
    OperatorDecision,
    ProhibitedContent,
    ReviewState,
    build_dedupe_query,
    build_staging_plan,
    scan_prohibited_text,
)


class KnowledgeIngestionSchemaTests(unittest.TestCase):
    def candidate(self, **overrides) -> KnowledgeCandidate:
        citations = (
            EvidenceCitation(
                citation_id="evidence-event",
                run_id="run-123",
                event_id="event-17",
                locator="model.final.observations[0]",
            ),
            EvidenceCitation(
                citation_id="evidence-artifact",
                run_id="run-123",
                artifact_id="artifact-report",
                artifact_sha256="a" * 64,
            ),
        )
        values = {
            "candidate_id": "candidate-123",
            "material_class": MaterialClass.DURABLE_KNOWLEDGE,
            "concept_type": "Technique",
            "slug": "bounded-symbol-recovery",
            "title": "Bounded symbol recovery",
            "description": "Correlates independent evidence before assigning recovered names.",
            "tags": ("static", "reporting"),
            "observations": (
                KnowledgeClaim(
                    kind=ClaimKind.OBSERVATION,
                    text="Two independent evidence sources reduced ambiguous name assignments.",
                    citation_ids=("evidence-event", "evidence-artifact"),
                ),
            ),
            "theories": (
                KnowledgeClaim(
                    kind=ClaimKind.THEORY,
                    text="Requiring corroboration may generalize to other stripped binaries.",
                    citation_ids=("evidence-event",),
                ),
            ),
            "citations": citations,
            "relationships": ("/techniques/symbol-recovery.md",),
        }
        values.update(overrides)
        return KnowledgeCandidate(**values)

    def dedupe(self, candidate, **overrides) -> DedupeResult:
        values = {
            "query": build_dedupe_query(candidate),
            "disposition": DedupeDisposition.NO_MATCH,
            "rationale": "No concept covers the bounded corroboration rule.",
        }
        values.update(overrides)
        return DedupeResult(**values)

    def decision(self, approved: bool) -> OperatorDecision:
        return OperatorDecision(
            operator_id="operator@example",
            approved=approved,
            rationale="Reviewed provenance and reusable scope.",
            decided_at="2026-07-13T05:00:00Z",
        )

    def test_review_requires_dedupe_and_explicit_approval_before_staging(self):
        candidate = self.candidate()
        unsearched = CandidateReview(
            candidate=candidate,
            dedupe=DedupeResult(query=build_dedupe_query(candidate)),
        )
        with self.assertRaisesRegex(ValueError, "Deduplication|deduplication"):
            unsearched.ready()
        with self.assertRaisesRegex(PermissionError, "approval"):
            build_staging_plan(unsearched)

        ready = CandidateReview(candidate, self.dedupe(candidate)).ready()
        self.assertEqual(ReviewState.READY, ready.state)
        with self.assertRaisesRegex(PermissionError, "approval"):
            build_staging_plan(ready)

        approved = ready.decide(self.decision(True))
        plan = build_staging_plan(approved)
        self.assertEqual("factory/kb-candidate-123", plan.branch_name)
        self.assertEqual("techniques/bounded-symbol-recovery.md", plan.concept_path)
        self.assertEqual("techniques/index.md", plan.index_path)
        self.assertEqual("log.md", plan.log_path)
        self.assertEqual(
            ("python3", "scripts/okf_validate.py", ".", "--strict"),
            plan.validation_command,
        )

    def test_denial_is_terminal_and_cannot_produce_mutation_plan(self):
        candidate = self.candidate()
        denied = CandidateReview(candidate, self.dedupe(candidate)).ready().decide(
            self.decision(False)
        )
        self.assertEqual(ReviewState.DENIED, denied.state)
        with self.assertRaisesRegex(PermissionError, "approval"):
            build_staging_plan(denied)
        with self.assertRaisesRegex(ValueError, "ready candidate"):
            denied.decide(self.decision(True))

    def test_enrichment_targets_the_deduplicated_existing_concept(self):
        candidate = self.candidate()
        dedupe = self.dedupe(
            candidate,
            disposition=DedupeDisposition.ENRICH_EXISTING,
            matched_paths=("techniques/symbol-recovery.md",),
            existing_concept_path="techniques/symbol-recovery.md",
            rationale="Same scope; add the new evidence and bounded rule.",
        )
        approved = CandidateReview(candidate, dedupe).ready().decide(self.decision(True))
        plan = build_staging_plan(approved)
        self.assertEqual("techniques/symbol-recovery.md", plan.concept_path)
        self.assertEqual("techniques/index.md", plan.index_path)

    def test_non_durable_and_prohibited_material_is_rejected_before_review(self):
        with self.assertRaisesRegex(ValueError, "raw_log cannot become"):
            self.candidate(material_class=MaterialClass.RAW_LOG)
        with self.assertRaisesRegex(ValueError, "weaponized_chain"):
            self.candidate(prohibited_content=(ProhibitedContent.WEAPONIZED_CHAIN,))
        with self.assertRaisesRegex(ValueError, "private_key"):
            self.candidate(description="-----BEGIN PRIVATE KEY----- do not retain")
        self.assertEqual(
            (ProhibitedContent.CREDENTIAL,),
            scan_prohibited_text("api_key=super-secret-value"),
        )

    def test_observations_theories_and_citations_cannot_blur(self):
        theory = KnowledgeClaim(
            kind=ClaimKind.THEORY,
            text="This might generalize.",
            citation_ids=("evidence-event",),
        )
        with self.assertRaisesRegex(ValueError, "classified as observation"):
            self.candidate(observations=(theory,))

        unknown = KnowledgeClaim(
            kind=ClaimKind.OBSERVATION,
            text="Observed in the fixture.",
            citation_ids=("missing-citation",),
        )
        with self.assertRaisesRegex(ValueError, "unknown citations"):
            self.candidate(observations=(unknown,))

        with self.assertRaisesRegex(ValueError, "artifact or event"):
            EvidenceCitation(citation_id="bad", run_id="run-123")

    def test_relationships_and_dedupe_paths_are_bundle_safe(self):
        with self.assertRaisesRegex(ValueError, "bundle-relative links"):
            self.candidate(relationships=("techniques/symbol-recovery.md",))
        candidate = self.candidate()
        with self.assertRaisesRegex(ValueError, "bundle-relative Markdown"):
            self.dedupe(
                candidate,
                disposition=DedupeDisposition.ENRICH_EXISTING,
                existing_concept_path="../outside.md",
            )


if __name__ == "__main__":
    unittest.main()
