"""A minimal, dependency-free parser for SKILL.md frontmatter.

SKILL.md files open with a ``---`` fenced block of ``key: value`` frontmatter,
then a Markdown body. The frontmatter in the wild is *simple* — scalars, inline
lists (``[a, b]``), block lists (``- item``), and folded/literal block scalars
(``>-`` / ``|`` for long descriptions). That is a tiny, closed subset of YAML, so
rekit parses it by hand rather than taking a PyYAML dependency (the kernel stays
dependency-free; see the epic's dependency-direction section).

Supported constructs (everything the shipped SKILL.md dialects use):

* ``key: scalar`` — a string; surrounding quotes stripped.
* ``key: [a, b, c]`` — an inline flow list.
* multi-line block lists::

      accepts:
        - binary/dex
        - archive/apk

* folded / literal block scalars (``>``, ``>-``, ``|``, ``|-``) — joined into one
  string (folded joins with spaces, literal keeps newlines).

Anything fancier (nested maps, anchors, multi-doc) is out of scope by design; a
SKILL.md that needs them is doing too much. Returns ``(metadata, body)``.
"""

from __future__ import annotations

from typing import Any

_FENCE = "---"


def split_frontmatter(text: str) -> tuple[str, str]:
    """Split a document into ``(frontmatter_text, body_text)``.

    Frontmatter is the block between a leading ``---`` line and the next ``---``
    line. If the document does not open with a fence, everything is body and the
    frontmatter is empty.
    """
    lines = text.splitlines()
    # Skip a leading UTF-8 BOM / blank lines before the opening fence.
    idx = 0
    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1
    if idx >= len(lines) or lines[idx].strip() != _FENCE:
        return "", text
    start = idx + 1
    for end in range(start, len(lines)):
        if lines[end].strip() == _FENCE:
            fm = "\n".join(lines[start:end])
            body = "\n".join(lines[end + 1:])
            return fm, body
    # Unterminated fence: treat the whole thing as body (be forgiving).
    return "", text


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Parse a SKILL.md into ``(metadata, body)``.

    ``metadata`` values are ``str`` or ``list[str]``. Unknown/garbled lines are
    skipped rather than raising — a malformed skill degrades to a lead, it does
    not crash discovery.
    """
    fm_text, body = split_frontmatter(text)
    return _parse_mapping(fm_text.splitlines()), body


def _parse_mapping(lines: list[str]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue

        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()

        if not key:
            i += 1
            continue

        # Block scalar: ``>``, ``>-``, ``|``, ``|-`` (chomp/keep indicators tolerated).
        if rest and rest[0] in "|>":
            fold = rest[0] == ">"
            block, i = _consume_block_scalar(lines, i + 1)
            meta[key] = _join_block(block, fold=fold)
            continue

        # Inline flow list: ``[a, b]``.
        if rest.startswith("[") and rest.endswith("]"):
            meta[key] = _parse_flow_list(rest)
            i += 1
            continue

        # Block list on following ``- item`` lines (key line itself is empty).
        if rest == "":
            items, next_i = _consume_block_list(lines, i + 1)
            if items is not None:
                meta[key] = items
                i = next_i
                continue
            meta[key] = ""
            i += 1
            continue

        # Plain scalar.
        meta[key] = _unquote(rest)
        i += 1
    return meta


def _consume_block_scalar(lines: list[str], start: int) -> tuple[list[str], int]:
    """Gather an indented block following a ``>``/``|`` key, returning the raw
    (de-indented) lines and the index of the first line past the block."""
    block: list[str] = []
    i = start
    n = len(lines)
    # Establish the block indent from the first non-blank continuation line.
    indent = None
    while i < n:
        line = lines[i]
        if line.strip() == "":
            block.append("")
            i += 1
            continue
        cur_indent = len(line) - len(line.lstrip())
        if indent is None:
            if cur_indent == 0:
                break  # nothing indented under the key -> empty block
            indent = cur_indent
        if cur_indent < indent:
            break
        block.append(line[indent:])
        i += 1
    # Trim trailing blank lines that belong to spacing, not content.
    while block and block[-1] == "":
        block.pop()
    return block, i


def _join_block(block: list[str], *, fold: bool) -> str:
    if not fold:
        return "\n".join(block)
    # Folded: blank lines become paragraph breaks; other newlines become spaces.
    out: list[str] = []
    paragraph: list[str] = []
    for line in block:
        if line.strip() == "":
            if paragraph:
                out.append(" ".join(paragraph))
                paragraph = []
        else:
            paragraph.append(line.strip())
    if paragraph:
        out.append(" ".join(paragraph))
    return "\n".join(out)


def _consume_block_list(lines: list[str], start: int):
    """If the lines beginning at ``start`` are ``- item`` entries, return
    ``(items, next_index)``; otherwise ``(None, start)``."""
    items: list[str] = []
    i = start
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if stripped == "" or stripped.startswith("#"):
            i += 1
            continue
        if stripped.startswith("- "):
            items.append(_unquote(stripped[2:].strip()))
            i += 1
            continue
        if stripped == "-":
            items.append("")
            i += 1
            continue
        break
    if not items:
        return None, start
    return items, i


def _parse_flow_list(rest: str) -> list[str]:
    inner = rest[1:-1].strip()
    if not inner:
        return []
    return [_unquote(part.strip()) for part in inner.split(",") if part.strip()]


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value
