from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from rekit_factory.knowledge import KnowledgeCatalog, KnowledgeRoot


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    _write(root / "index.md", """---
okf_version: "0.1"
---
# Knowledge
* [Protections](protections/) - Anti-debug and anti-tamper mechanisms.
* [Orphan group](orphan/) - Concepts in a directory without an index.
""")
    _write(root / "protections" / "index.md", """# Protections
* [Debugger Rungs](debugger-rungs.md) - Layered debugger detection and instrumentation guidance.
* [Unusual Thing](unusual.md) - Producer-defined knowledge type with sparse metadata.
* [Future Concept](not-written.md) - A deliberately broken index link.
""")
    _write(root / "protections" / "debugger-rungs.md", """---
type: Protection
title: Debugger Detection Rungs
description: Layered anti-debug checks used by protected Windows binaries.
tags: [anti-debug, windows, instrumentation]
producer_extension: ignored-but-tolerated
---
# Detection

Hardware breakpoints and trap-flag flooding interfere with ring-3 debuggers.
See the [instrumentation technique](/techniques/instrumentation.md), the
[neighbor](./unusual.md), and a [future note](./missing.md).

# Citations

[1] [External paper](https://example.test/anti-debug)
[2] [Local evidence](/references/evidence.md)
""")
    _write(root / "protections" / "unusual.md", """---
type: Producer Specific Artifact
---

Sparse but valid knowledge about a debugger-adjacent artifact.
""")
    _write(root / "techniques" / "instrumentation.md", """---
type: Technique
title: Inline Instrumentation
description: Observe execution without debugger breakpoints.
tags:
  - instrumentation
  - dynamic-analysis
---

Install hooks after unpacking completes.
""")
    _write(root / "references" / "evidence.md", """---
type: Reference
title: Lab Evidence
---

Observed in an authorized fixture.
""")
    # There is no orphan/index.md: the consumer must synthesize this directory.
    _write(root / "orphan" / "cache-behavior.md", """---
type: Odd Unknown Type
description: Notes on prompt cache accounting.
---

Cache read tokens are distinct from cache creation tokens.
""")
    return root


def test_search_ranks_index_shortlist_and_returns_inspectable_fields(tmp_path):
    root = _bundle(tmp_path)
    catalog = KnowledgeCatalog([KnowledgeRoot.at(root, name="fixture")])

    hits = catalog.search("anti debug debugger", limit=2)

    assert hits[0].root == "fixture"
    assert hits[0].concept_id == "protections/debugger-rungs"
    assert hits[0].title == "Debugger Detection Rungs"
    assert hits[0].description.startswith("Layered anti-debug")
    assert hits[0].type == "Protection"
    assert hits[0].tags == ("anti-debug", "windows", "instrumentation")
    assert hits[0].citations == (
        "https://example.test/anti-debug", "/references/evidence.md",
    )
    assert len(hits[0].content_hash) == 64
    assert "debugger" in hits[0].snippet.casefold()


def test_unknown_types_missing_optional_fields_and_broken_links_are_tolerated(tmp_path):
    root = _bundle(tmp_path)
    catalog = KnowledgeCatalog([KnowledgeRoot.at(root, name="fixture")])

    sparse = catalog.get("fixture", "protections/unusual")
    assert sparse is not None
    assert sparse.type == "Producer Specific Artifact"
    assert sparse.title == "Unusual"
    assert sparse.description == ""
    assert sparse.tags == ()
    assert catalog.get("fixture", "protections/not-written") is None
    assert catalog.get("fixture", "../../outside") is None

    links = catalog.related("fixture", "protections/debugger-rungs")
    missing = next(link for link in links if link.target == "./missing.md")
    assert missing.concept_id == "protections/missing"
    assert not missing.exists
    assert catalog.follow("fixture", "protections/debugger-rungs", missing) is None


def test_bundle_relative_and_relative_links_follow_only_on_demand(tmp_path):
    root = _bundle(tmp_path)
    catalog = KnowledgeCatalog([KnowledgeRoot.at(root, name="fixture")])
    links = catalog.related("fixture", "protections/debugger-rungs")

    technique_link = next(link for link in links if link.target == "/techniques/instrumentation.md")
    neighbor_link = next(link for link in links if link.target == "./unusual.md")
    external_link = next(link for link in links if link.target.startswith("https://"))

    technique = catalog.follow("fixture", "protections/debugger-rungs", technique_link)
    neighbor = catalog.follow("fixture", "protections/debugger-rungs", neighbor_link)
    assert technique is not None and technique.concept_id == "techniques/instrumentation"
    assert neighbor is not None and neighbor.type == "Producer Specific Artifact"
    assert external_link.external
    assert catalog.follow("fixture", "protections/debugger-rungs", external_link) is None


def test_missing_index_fallback_and_multiple_roots(tmp_path):
    first = _bundle(tmp_path / "one")
    second = tmp_path / "two" / "kb"
    _write(second / "only.md", """---
type: Field Note
title: Cache Accounting
---

Prompt cache reads reduce repeated input work.
""")
    catalog = KnowledgeCatalog([
        KnowledgeRoot.at(first, name="primary"),
        KnowledgeRoot.at(second, name="secondary"),
    ])

    hits = catalog.search("prompt cache", limit=4)
    assert {hit.root for hit in hits} == {"primary", "secondary"}
    assert {hit.concept_id for hit in hits} == {"orphan/cache-behavior", "only"}


def test_retrieval_never_modifies_bundle(tmp_path):
    root = _bundle(tmp_path)
    before = {
        path.relative_to(root).as_posix(): (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in root.rglob("*") if path.is_file()
    }
    catalog = KnowledgeCatalog([root])

    catalog.search("debugger instrumentation")
    catalog.related(root.name, "protections/debugger-rungs")

    after = {
        path.relative_to(root).as_posix(): (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in root.rglob("*") if path.is_file()
    }
    assert after == before


def test_result_limit_is_intentionally_bounded(tmp_path):
    catalog = KnowledgeCatalog([_bundle(tmp_path)])

    with pytest.raises(ValueError, match="between 1 and 10"):
        catalog.search("debugger", limit=11)
