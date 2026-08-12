import re
from typing import Any

import structlog

from cbac_service.config import CHUNK_MAX_WORDS
from cbac_service.skills import SkillsCard, parse_skill_md

logger = structlog.get_logger("cbac_service.chunking")

_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def split_list_items(block: str) -> list[str]:
    """Split a paragraph block into its list items, if any.

    A bulleted/numbered line starts a new item; any following line that
    isn't itself a bullet is a wrapped continuation of that item, not a
    new unit. Blocks with no bullets at all collapse into a single item
    (this is what re-joins a hard-wrapped prose paragraph).
    """
    lines = block.splitlines()
    if not any(_BULLET_RE.match(ln) for ln in lines):
        return [" ".join(ln.strip() for ln in lines if ln.strip())]
    items: list[str] = []
    current: list[str] = []
    for line in lines:
        if _BULLET_RE.match(line):
            if current:
                items.append(" ".join(current))
            current = [line.strip()]
        else:
            stripped = line.strip()
            if stripped:
                current.append(stripped)
    if current:
        items.append(" ".join(current))
    return items


def split_by_word_budget(unit: str, max_words: int) -> list[str]:
    """Split ``unit`` only if it exceeds ``max_words``, preferring sentence
    boundaries so a chunk never straddles unrelated sentences unnecessarily."""
    words = unit.split()
    if len(words) <= max_words:
        return [unit]
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(unit) if s.strip()]
    if len(sentences) <= 1:
        return [" ".join(words[i : i + max_words]) for i in range(0, len(words), max_words)]
    out: list[str] = []
    current: list[str] = []
    current_len = 0
    for sent in sentences:
        n = len(sent.split())
        if current and current_len + n > max_words:
            out.append(" ".join(current))
            current, current_len = [], 0
        current.append(sent)
        current_len += n
    if current:
        out.append(" ".join(current))
    return out


def chunk_body_text(text: str, max_words: int = CHUNK_MAX_WORDS) -> list[str]:
    """Structure-aware, budget-capped chunking for free-form policy text.

    Paragraphs (blank-line separated) and list items are the primary
    chunk boundary — not individual lines — so a hard-wrapped sentence or
    a bullet's wrapped continuation stays in one chunk. A unit is only
    split further, on sentence boundaries, if it exceeds ``max_words``.
    Lines starting with ``#`` are dropped, matching the previous
    comment-stripping behaviour.
    """
    filtered = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
    chunks: list[str] = []
    for block in re.split(r"\n\s*\n", filtered):
        block = block.strip()
        if not block:
            continue
        for unit in split_list_items(block):
            unit = unit.strip()
            if not unit:
                continue
            chunks.extend(split_by_word_budget(unit, max_words))
    return chunks


def _flatten_mapping(mapping: dict[str, Any]) -> list[str]:
    """Flatten a key→value mapping to "key: value" chunks — one per list item
    for list-valued keys, one per scalar otherwise. ``None`` values are skipped."""
    chunks: list[str] = []
    for key, value in mapping.items():
        if isinstance(value, list):
            chunks.extend(f"{key}: {item}" for item in value)
        elif value is not None:
            chunks.append(f"{key}: {value}")
    return chunks


def flatten_policy_chunks(policy: Any) -> list[str]:
    """Flatten a policy of any shape into a list of text chunks.

    Each YAML frontmatter (or dict) entry becomes "key: value" (one chunk
    per list item for list-valued keys); the body is chunked by
    paragraph/list-item structure via :func:`chunk_body_text`, not by raw
    line. This unified path works for any skill.md schema — no hardcoded
    key names.
    """
    if isinstance(policy, SkillsCard):
        return _flatten_mapping(policy.raw_frontmatter) + chunk_body_text(policy.body)
    if isinstance(policy, str):
        stripped = policy.strip()
        if stripped.startswith("---"):
            try:
                return flatten_policy_chunks(parse_skill_md(policy))
            except Exception:
                logger.debug("policy frontmatter unparseable, chunking as plain text")
        return chunk_body_text(policy)
    if isinstance(policy, dict):
        return _flatten_mapping(policy)
    return []
