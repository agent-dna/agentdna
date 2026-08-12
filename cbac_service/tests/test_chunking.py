"""Chunking + policy-flattening: the text path that feeds the embedding cache.

No models, no DB — pure text in, chunks out.
"""

from cbac_service.chunking import chunk_body_text, flatten_policy_chunks
from cbac_service.skills import parse_skill_md

SKILL_MD = """---
agent-did: did:agent:test
agent-name: tester
issued-by: did:user:boss
issued-at: 2026-01-01T00:00:00Z
expires-at: 2027-01-01T00:00:00Z
allowed-actions:
  - read pull requests
  - list issues
forbidden-actions:
  - delete the repo
---
The agent may summarise repository activity.
"""


# ── chunk_body_text ───────────────────────────────────────────────────────────


def test_comment_lines_are_dropped():
    assert chunk_body_text("# a comment\nreal content") == ["real content"]


def test_hard_wrapped_paragraph_rejoins_into_one_chunk():
    # A wrapped sentence must not become three chunks — that was the whole
    # point of chunking by structure rather than by line.
    assert chunk_body_text("the agent may\nread pull requests\nand nothing else") == [
        "the agent may read pull requests and nothing else"
    ]


def test_blank_line_separates_paragraphs():
    assert chunk_body_text("first para\n\nsecond para") == ["first para", "second para"]


def test_bullets_split_but_wrapped_continuation_stays_attached():
    chunks = chunk_body_text("- read pull requests\n  for any repo\n- list issues")
    assert chunks == ["- read pull requests for any repo", "- list issues"]


def test_long_unit_splits_on_sentence_boundaries():
    text = "One two three four. Five six seven eight. Nine ten eleven twelve."
    chunks = chunk_body_text(text, max_words=5)
    assert len(chunks) > 1
    # every sentence survives intact somewhere — splitting must not lose words
    assert " ".join(chunks).split() == text.split()


def test_unsplittable_long_unit_falls_back_to_word_budget():
    text = " ".join(str(i) for i in range(12))  # no sentence boundaries at all
    chunks = chunk_body_text(text, max_words=5)
    assert [len(c.split()) for c in chunks] == [5, 5, 2]


# ── flatten_policy_chunks ─────────────────────────────────────────────────────


def test_dict_expands_lists_and_skips_none():
    chunks = flatten_policy_chunks({"allowed": ["read", "write"], "owner": "boss", "expires": None})
    assert chunks == ["allowed: read", "allowed: write", "owner: boss"]


def test_plain_string_is_chunked_as_body_text():
    assert flatten_policy_chunks("just prose\n\nmore prose") == ["just prose", "more prose"]


def test_skill_md_string_yields_frontmatter_and_body():
    chunks = flatten_policy_chunks(SKILL_MD)
    assert "allowed-actions: read pull requests" in chunks
    assert "forbidden-actions: delete the repo" in chunks
    assert "The agent may summarise repository activity." in chunks


def test_skillscard_object_matches_its_source_string():
    assert flatten_policy_chunks(parse_skill_md(SKILL_MD)) == flatten_policy_chunks(SKILL_MD)


def test_malformed_frontmatter_falls_back_instead_of_raising():
    # Starts with --- so it takes the skill.md branch, but has no closing
    # delimiter. Must degrade to plain-text chunking, not blow up the
    # precompute pipeline.
    broken = "---\nnot: [valid\n"
    assert flatten_policy_chunks(broken) == chunk_body_text(broken)


def test_unsupported_type_returns_empty():
    assert flatten_policy_chunks(42) == []
    assert flatten_policy_chunks(None) == []
