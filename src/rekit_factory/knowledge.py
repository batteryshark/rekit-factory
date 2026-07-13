"""Read-only, progressive-disclosure retrieval over OKF v0.1 bundles.

The catalog reads directory indexes first and opens concept documents only for a
small shortlist.  Bundles without indexes remain consumable through a local
frontmatter scan, as required by OKF's permissive consumer contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit


_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")
_WORD = re.compile(r"[a-z0-9]+")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_RESERVED = {"index.md", "log.md"}


@dataclass(frozen=True)
class KnowledgeRoot:
    """A named OKF bundle root."""

    name: str
    path: Path

    @classmethod
    def at(cls, path: str | Path, *, name: str | None = None) -> "KnowledgeRoot":
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise FileNotFoundError(resolved)
        root_name = name or resolved.name
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", root_name):
            raise ValueError("knowledge root name must be a short opaque label")
        return cls(root_name, resolved)


@dataclass(frozen=True)
class KnowledgeLink:
    label: str
    target: str
    concept_id: str | None
    external: bool
    exists: bool


@dataclass(frozen=True)
class KnowledgeConcept:
    root: str
    concept_id: str
    title: str
    description: str
    type: str
    tags: tuple[str, ...]
    citations: tuple[str, ...]
    content_hash: str
    snippet: str
    links: tuple[KnowledgeLink, ...]
    body: str


@dataclass(frozen=True)
class KnowledgeHit:
    root: str
    concept_id: str
    title: str
    description: str
    type: str
    tags: tuple[str, ...]
    citations: tuple[str, ...]
    content_hash: str
    snippet: str
    score: float


@dataclass(frozen=True)
class _IndexEntry:
    root: KnowledgeRoot
    concept_id: str
    title: str
    description: str


class KnowledgeCatalog:
    """Federated, read-only OKF retrieval with bounded concept loading."""

    def __init__(self, roots: Iterable[KnowledgeRoot | str | Path]):
        configured: list[KnowledgeRoot] = []
        for root in roots:
            configured.append(
                KnowledgeRoot.at(root.path, name=root.name)
                if isinstance(root, KnowledgeRoot) else KnowledgeRoot.at(root)
            )
        if not configured:
            raise ValueError("at least one knowledge root is required")
        names = [root.name for root in configured]
        if len(names) != len(set(names)):
            raise ValueError("knowledge root names must be unique")
        self.roots = tuple(configured)

    def search(self, query: str, *, limit: int = 4) -> tuple[KnowledgeHit, ...]:
        """Return a small ranked result set without eagerly loading the corpus."""
        terms = _terms(query)
        if not terms:
            return ()
        if not 1 <= limit <= 10:
            raise ValueError("limit must be between 1 and 10")
        entries = [entry for root in self.roots for entry in _catalog_entries(root)]
        ranked = sorted(entries, key=lambda entry: (-_entry_score(entry, terms), entry.root.name, entry.concept_id))
        # Frontmatter/body are loaded only for a bounded shortlist.  Zero-score
        # entries remain eligible because an index description is optional.
        shortlist = ranked[: max(limit * 5, 12)]
        hits: list[KnowledgeHit] = []
        for entry in shortlist:
            concept = self.get(entry.root.name, entry.concept_id, query=query)
            if concept is None:
                continue
            score = _concept_score(concept, terms)
            if score > 0:
                hits.append(KnowledgeHit(
                    root=concept.root, concept_id=concept.concept_id, title=concept.title,
                    description=concept.description, type=concept.type, tags=concept.tags,
                    citations=concept.citations, content_hash=concept.content_hash,
                    snippet=concept.snippet, score=score,
                ))
        hits.sort(key=lambda hit: (-hit.score, hit.root, hit.concept_id))
        return tuple(hits[:limit])

    def get(self, root_name: str, concept_id: str, *, query: str = "") -> KnowledgeConcept | None:
        """Open one concept by bundle-relative ID; invalid or missing IDs return ``None``."""
        root = self._root(root_name)
        path = _concept_path(root, concept_id)
        if path is None or not path.is_file() or path.name in _RESERVED:
            return None
        try:
            raw = path.read_bytes()
            source = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        metadata, body = _frontmatter(source)
        normalized_id = path.relative_to(root.path).with_suffix("").as_posix()
        title = _scalar(metadata.get("title")) or _title_from_id(normalized_id)
        description = _scalar(metadata.get("description"))
        kind = _scalar(metadata.get("type")) or "Concept"
        tags = _strings(metadata.get("tags"))
        links = tuple(_links(root, path, body))
        citations = tuple(link.target for link in _citation_links(root, path, body))
        return KnowledgeConcept(
            root=root.name, concept_id=normalized_id, title=title,
            description=description, type=kind, tags=tags, citations=citations,
            content_hash=hashlib.sha256(raw).hexdigest(),
            snippet=_snippet(body, _terms(query)), links=links, body=body,
        )

    def related(self, root_name: str, concept_id: str) -> tuple[KnowledgeLink, ...]:
        """List links from one concept without following them."""
        concept = self.get(root_name, concept_id)
        return concept.links if concept else ()

    def follow(self, root_name: str, source_id: str, link: KnowledgeLink, *,
               expected_source_hash: str | None = None) -> KnowledgeConcept | None:
        """Follow one already-discovered bundle link on demand."""
        if link.external or not link.exists or link.concept_id is None:
            return None
        # Re-resolve from the source so callers cannot manufacture an escaping link.
        source = self.get(root_name, source_id)
        if (source is None or link not in source.links
                or (expected_source_hash is not None
                    and source.content_hash != expected_source_hash)):
            return None
        return self.get(root_name, link.concept_id)

    def _root(self, name: str) -> KnowledgeRoot:
        for root in self.roots:
            if root.name == name:
                return root
        raise KeyError(f"unknown knowledge root {name!r}")


def _catalog_entries(root: KnowledgeRoot) -> tuple[_IndexEntry, ...]:
    root_index = root.path / "index.md"
    if not root_index.is_file():
        return tuple(_scan_concepts(root, root.path))
    entries: dict[str, _IndexEntry] = {}
    pending = [root_index]
    visited: set[Path] = set()
    while pending:
        index = pending.pop(0)
        try:
            index = index.resolve(strict=True)
            index.relative_to(root.path)
        except (OSError, ValueError):
            continue
        if index in visited or index.name != "index.md":
            continue
        visited.add(index)
        try:
            source = index.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for label, target, description in _index_links(source):
            resolved = _resolve_target(root, index, target)
            if resolved is None:
                continue
            if resolved.is_dir():
                child_index = resolved / "index.md"
                if child_index.is_file():
                    pending.append(child_index)
                else:
                    for entry in _scan_concepts(root, resolved):
                        entries.setdefault(entry.concept_id, entry)
                continue
            if resolved.name == "index.md":
                if resolved.is_file():
                    pending.append(resolved)
                elif resolved.parent.is_dir():
                    for entry in _scan_concepts(root, resolved.parent):
                        entries.setdefault(entry.concept_id, entry)
                continue
            if resolved.suffix.lower() != ".md" or resolved.name in _RESERVED:
                continue
            concept_id = resolved.relative_to(root.path).with_suffix("").as_posix()
            entries.setdefault(concept_id, _IndexEntry(root, concept_id, label, description))
    return tuple(entries.values())


def _scan_concepts(root: KnowledgeRoot, directory: Path) -> Iterable[_IndexEntry]:
    """Best-effort fallback used only where progressive-disclosure indexes are absent."""
    for path in sorted(directory.rglob("*.md")):
        if path.name in _RESERVED:
            continue
        try:
            path.resolve().relative_to(root.path)
            metadata, _ = _frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        concept_id = path.relative_to(root.path).with_suffix("").as_posix()
        yield _IndexEntry(
            root, concept_id, _scalar(metadata.get("title")) or _title_from_id(concept_id),
            _scalar(metadata.get("description")),
        )


def _index_links(source: str) -> Iterable[tuple[str, str, str]]:
    _, body = _frontmatter(source)
    for line in body.splitlines():
        match = _LINK.search(line)
        if not match:
            continue
        trailing = line[match.end():].strip().lstrip("-–—: ").strip()
        yield match.group(1).strip(), match.group(2).strip(), trailing


def _links(root: KnowledgeRoot, source: Path, body: str) -> Iterable[KnowledgeLink]:
    for match in _LINK.finditer(body):
        label, target = match.group(1).strip(), match.group(2).strip()
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or target.startswith("#"):
            yield KnowledgeLink(label, target, None, True, False)
            continue
        resolved = _resolve_target(root, source, target)
        concept_id = None
        exists = False
        if resolved is not None and resolved.suffix.lower() == ".md" and resolved.name not in _RESERVED:
            concept_id = resolved.relative_to(root.path).with_suffix("").as_posix()
            exists = resolved.is_file()
        yield KnowledgeLink(label, target, concept_id, False, exists)


def _citation_links(root: KnowledgeRoot, source: Path, body: str) -> Iterable[KnowledgeLink]:
    section = _section(body, "citations")
    return _links(root, source, section) if section else ()


def _section(body: str, heading: str) -> str:
    matches = list(_HEADING.finditer(body))
    for index, match in enumerate(matches):
        if match.group(1).strip().casefold() != heading.casefold():
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        return body[match.end():end]
    return ""


def _resolve_target(root: KnowledgeRoot, source: Path, target: str) -> Path | None:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None
    clean = unquote(parsed.path)
    if not clean:
        return None
    candidate = root.path / clean.lstrip("/") if clean.startswith("/") else source.parent / clean
    if clean.endswith("/"):
        candidate = candidate / "index.md"
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root.path)
        return resolved
    except (OSError, ValueError):
        return None


def _concept_path(root: KnowledgeRoot, concept_id: str) -> Path | None:
    clean = PurePosixPath(concept_id.removesuffix(".md"))
    if clean.is_absolute() or ".." in clean.parts or not clean.parts:
        return None
    candidate = root.path.joinpath(*clean.parts).with_suffix(".md")
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root.path)
        return resolved
    except (OSError, ValueError):
        return None


def _frontmatter(source: str) -> tuple[dict[str, Any], str]:
    lines = source.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, source
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return {}, source
    return _parse_mapping(lines[1:end]), "\n".join(lines[end + 1:])


def _parse_mapping(lines: list[str]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if ":" not in line or not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if not key:
            index += 1
            continue
        if value.startswith("[") and value.endswith("]"):
            metadata[key] = [_unquote(item.strip()) for item in value[1:-1].split(",") if item.strip()]
        elif value and value[0] in ">|":
            block: list[str] = []
            index += 1
            while index < len(lines) and (not lines[index].strip() or lines[index][:1].isspace()):
                block.append(lines[index].strip())
                index += 1
            metadata[key] = (" " if value[0] == ">" else "\n").join(item for item in block if item)
            continue
        elif not value:
            items: list[str] = []
            cursor = index + 1
            while cursor < len(lines) and lines[cursor].strip().startswith("-"):
                items.append(_unquote(lines[cursor].strip().lstrip("-").strip()))
                cursor += 1
            metadata[key] = items if items else ""
            if items:
                index = cursor
                continue
        else:
            metadata[key] = _unquote(value.split(" #", 1)[0].strip())
        index += 1
    return metadata


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _scalar(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    return ()


def _terms(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_WORD.findall(value.casefold())))


def _entry_score(entry: _IndexEntry, terms: tuple[str, ...]) -> float:
    return _weighted_score(terms, ((entry.title, 8), (entry.description, 5), (entry.concept_id, 2)))


def _concept_score(concept: KnowledgeConcept, terms: tuple[str, ...]) -> float:
    return _weighted_score(terms, (
        (concept.title, 8), (concept.description, 5), (" ".join(concept.tags), 4),
        (concept.type, 2), (concept.concept_id, 2), (concept.body, 1),
    ))


def _weighted_score(terms: tuple[str, ...], fields: Iterable[tuple[str, int]]) -> float:
    score = 0.0
    for source, weight in fields:
        words = _WORD.findall(source.casefold())
        text = " ".join(words)
        for term in terms:
            score += words.count(term) * weight
            if len(term) > 2 and term in text:
                score += weight * 0.25
    return score


def _snippet(body: str, terms: tuple[str, ...], *, limit: int = 280) -> str:
    plain = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    plain = re.sub(r"[#>*_`|]", " ", plain)
    plain = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", plain)
    plain = " ".join(plain.split())
    if not plain:
        return ""
    lowered = plain.casefold()
    offsets = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max(0, (min(offsets) if offsets else 0) - 70)
    snippet = plain[start:start + limit].strip()
    return ("…" if start else "") + snippet + ("…" if start + limit < len(plain) else "")


def _title_from_id(concept_id: str) -> str:
    return concept_id.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").title()
