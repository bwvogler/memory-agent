"""app/search.py's chunker - pure, so it is fully covered here.

The property these tests rest on: every chunk's content carries the path,
title and frontmatter it was chunked from. That prefix is the entire reason
hybrid search earns itself on a household wiki (docs/decisions/0020) - the
lexical half gets proper nouns for free from it.
"""

from __future__ import annotations

from app import search


def test_a_headingless_file_is_one_chunk():
    chunks = search.chunk_document("wiki/note.md", "A Note", {}, "Just some text.")

    assert len(chunks) == 1
    assert chunks[0].heading == ""


def test_an_empty_body_yields_no_chunks():
    assert search.chunk_document("wiki/empty.md", None, {}, "") == []
    assert search.chunk_document("wiki/empty.md", None, {}, "   \n  ") == []


def test_backlog_is_never_chunked():
    assert search.chunk_document("backlog.md", None, {}, "some ready work") == []


def test_heading_splits_report_the_most_recent_heading():
    body = "# Title\n\nIntro text.\n\n## Method\n\nBrown the soffritto slowly."
    chunks = search.chunk_document("wiki/recipes/ragu.md", "Ragù", {}, body)

    headings = [c.heading for c in chunks]
    assert any("Method" in h for h in headings)


def test_a_small_merged_section_still_keeps_its_heading_text_in_content():
    """Two small sections merge into one chunk - the heading TEXT must survive
    even though the merged chunk's `.heading` metadata reports only the first
    section's trail (see the module docstring on why this loses no retrieval
    signal: the markdown heading line itself stays in `content`)."""
    body = "# Title\n\nIntro text.\n\n## Method\n\nBrown the soffritto slowly."
    chunks = search.chunk_document("wiki/recipes/ragu.md", "Ragù", {}, body)

    assert any("## Method" in c.content for c in chunks)


def test_heading_trail_is_the_full_path_not_just_the_nearest_heading():
    body = (
        "# Ragù alla Bolognese\n\n"
        "## Method\n\n"
        "### Browning\n\n"
        "Brown the soffritto slowly, then add the wine and let it reduce "
        "before adding the tomatoes and milk, simmering for three hours "
        "on the lowest possible heat so nothing catches on the bottom."
    )
    chunks = search.chunk_document("wiki/recipes/ragu.md", "Ragù", {}, body)

    assert any("Ragù alla Bolognese > Method > Browning" in c.heading for c in chunks)


def test_every_chunk_carries_path_title_and_frontmatter():
    body = "# Method\n\n" + ("Brown the soffritto. " * 30)
    headers = {"cuisine": "italian", "time": "3h"}
    chunks = search.chunk_document(
        "wiki/recipes/ragu.md", "Ragù alla Bolognese", headers, body
    )

    assert chunks
    for chunk in chunks:
        assert "wiki/recipes/ragu.md" in chunk.content
        assert "Ragù alla Bolognese" in chunk.content
        assert "cuisine: italian" in chunk.content
        assert "time: 3h" in chunk.content


def test_an_oversized_section_splits_on_paragraph_boundaries():
    paragraphs = [
        f"Paragraph number {i} with some real content in it." for i in range(200)
    ]
    body = "# Long Section\n\n" + "\n\n".join(paragraphs)
    chunks = search.chunk_document("wiki/long.md", "Long", {}, body)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.content) <= search.MAX_CHUNK_CHARS + 500  # prefix headroom


def test_a_short_fragment_is_merged_forward_not_left_alone():
    body = "# One\n\nShort.\n\n# Two\n\n" + ("Real content here. " * 20)
    chunks = search.chunk_document("wiki/merge.md", "Merge", {}, body)

    # The short "One" section should not survive as its own tiny chunk.
    assert not any(c.content.strip().endswith("Short.") for c in chunks)


def test_frontmatter_list_values_render_as_comma_joined():
    headers = {"holder": ["brian", "laura"]}
    body = "# Section\n\n" + ("Enough text to be a real chunk. " * 10)
    chunks = search.chunk_document("wiki/shared.md", "Shared", headers, body)

    assert any("holder: brian, laura" in c.content for c in chunks)


def test_no_title_omits_the_dash_separator():
    chunks = search.chunk_document("wiki/note.md", None, {}, "Some body text here.")

    assert chunks[0].content.startswith("wiki/note.md\n")
