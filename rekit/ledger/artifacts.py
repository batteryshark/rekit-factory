"""Content-addressed artifacts — the nodes the ledger records.

An :class:`Artifact` is ``(kind, content_hash, path, meta)``. The content hash is
what makes the ledger's derivation cache safe: re-deriving the same bytes is a
no-op, so a second goal over an already-analyzed target re-derives nothing and a
re-entrant "unpack this again" dedupes. ``kind`` is a coarse bucket
(``archive/asar``, ``binary/native``, ``source/python``, ``tree``, …) that skills
and transforms declare interest in.

This is *rekit's own* artifact identification: ``classify()`` lives here in the
kernel, re-derived cleanly and imported from nothing — rekit depends on nothing.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CHUNK = 1 << 20  # 1 MiB streaming reads

# Coarse kind buckets. The value before the slash is the family a transform/skill
# can match on ("archive", "binary", "source"); the part after refines it.
KIND_TREE = "tree"
KIND_FILE = "file"          # a plain/unclassified file
KIND_TEXT = "text"

# Magic-number table: leading bytes -> kind. Checked before the extension table so
# a mislabelled file is still classified by its actual content.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x7fELF", "binary/native"),           # ELF (Linux/BSD)
    (b"\xfe\xed\xfa\xce", "binary/native"),  # Mach-O 32
    (b"\xfe\xed\xfa\xcf", "binary/native"),  # Mach-O 64
    (b"\xcf\xfa\xed\xfe", "binary/native"),  # Mach-O 64 (LE)
    (b"\xce\xfa\xed\xfe", "binary/native"),  # Mach-O 32 (LE)
    (b"dex\n", "binary/dex"),                # Android DEX
    (b"PK\x03\x04", "archive/zip"),          # zip family (jar/apk/aar/…)
    (b"PK\x05\x06", "archive/zip"),          # empty zip
    (b"\x1f\x8b", "archive/gzip"),           # gzip
    (b"MZ", "binary/pe"),                    # Windows PE/DLL/EXE
    (b"\xca\xfe\xba\xbe", "binary/jvm"),     # Java .class / Mach-O fat (ambiguous)
)

# Extension table: refines or overrides where content alone is ambiguous (a zip
# that is really an .apk) or where the payload is plain text (source).
_EXT_KIND: dict[str, str] = {
    ".asar": "archive/asar",
    ".apk": "archive/apk",
    ".aar": "archive/aar",
    ".jar": "archive/jar",
    ".war": "archive/jar",
    ".ipa": "archive/ipa",
    ".zip": "archive/zip",
    ".tar": "archive/tar",
    ".gz": "archive/gzip",
    ".tgz": "archive/gzip",
    ".dex": "binary/dex",
    ".dll": "binary/pe",
    ".exe": "binary/pe",
    ".so": "binary/native",
    ".dylib": "binary/native",
    ".class": "binary/jvm",
    ".py": "source/python",
    ".js": "source/javascript",
    ".mjs": "source/javascript",
    ".cjs": "source/javascript",
    ".ts": "source/typescript",
    ".tsx": "source/typescript",
    ".jsx": "source/javascript",
    ".java": "source/java",
    ".kt": "source/kotlin",
    ".swift": "source/swift",
    ".c": "source/c",
    ".h": "source/c",
    ".cc": "source/cpp",
    ".cpp": "source/cpp",
    ".go": "source/go",
    ".rb": "source/ruby",
    ".rs": "source/rust",
    ".sh": "source/shell",
    ".json": "text/json",
    ".md": "text/markdown",
    ".txt": "text",
}


@dataclass(frozen=True)
class Artifact:
    kind: str
    content_hash: str
    path: str
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Short, stable identity — the family plus a hash prefix. Used to key the
        ledger's derivation cache together with the transform id."""
        return f"{self.kind}:{self.content_hash[:16]}"

    @property
    def family(self) -> str:
        """The part of the kind a transform matches on (``archive`` for
        ``archive/asar``)."""
        return self.kind.split("/", 1)[0]


def hash_file(path: str | os.PathLike) -> str:
    """Streaming sha256 of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_tree(root: str | os.PathLike) -> str:
    """Content hash of a directory: sha256 over sorted ``(relpath, filehash)``.

    Order-independent — deterministic regardless of walk order — so two trees
    with the same files hash the same. Symlinks are recorded by target, not
    followed, so a link can't make two different trees collide.
    """
    root = Path(root)
    entries: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = Path(dirpath) / name
            rel = str(full.relative_to(root))
            if full.is_symlink():
                entries.append((rel, "link:" + os.readlink(full)))
            else:
                entries.append((rel, hash_file(full)))
    entries.sort()
    digest = hashlib.sha256()
    for rel, file_hash in entries:
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def classify(path: str | os.PathLike) -> str:
    """Best-effort ``kind`` for a path: directory → tree; otherwise magic bytes,
    then extension, then a text/binary sniff, then ``file``."""
    p = Path(path)
    if p.is_dir():
        return KIND_TREE
    ext = p.suffix.lower()
    try:
        with open(p, "rb") as fh:
            head = fh.read(16)
    except OSError:
        head = b""

    magic_kind = None
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            magic_kind = kind
            break

    # A zip by magic that has a more specific archive extension (.apk/.jar/.asar)
    # takes the extension's refinement; otherwise magic wins over extension.
    if magic_kind:
        if magic_kind == "archive/zip" and ext in _EXT_KIND and _EXT_KIND[ext].startswith("archive/"):
            return _EXT_KIND[ext]
        return magic_kind
    if ext in _EXT_KIND:
        return _EXT_KIND[ext]
    if not head:
        return KIND_FILE
    # Heuristic text sniff: no NUL byte in the head → treat as text.
    return KIND_TEXT if b"\x00" not in head else KIND_FILE


def to_dict(artifact: Artifact) -> dict[str, Any]:
    """Serialize an Artifact for the event log / project index."""
    return {
        "kind": artifact.kind,
        "contentHash": artifact.content_hash,
        "path": artifact.path,
        "meta": dict(artifact.meta),
    }


def from_dict(data: dict[str, Any]) -> Artifact:
    """Rebuild an Artifact from :func:`to_dict` output."""
    return Artifact(
        kind=data["kind"],
        content_hash=data["contentHash"],
        path=data["path"],
        meta=dict(data.get("meta") or {}),
    )


def from_path(path: str | os.PathLike, *, kind: str | None = None,
              meta: dict[str, Any] | None = None) -> Artifact:
    """Build an :class:`Artifact` from a filesystem path, classifying and hashing
    it. Pass ``kind`` to override classification (e.g. a skill that already knows
    what it produced)."""
    p = Path(path)
    resolved_kind = kind or classify(p)
    content_hash = hash_tree(p) if p.is_dir() else hash_file(p)
    return Artifact(kind=resolved_kind, content_hash=content_hash,
                    path=str(p), meta=dict(meta or {}))
