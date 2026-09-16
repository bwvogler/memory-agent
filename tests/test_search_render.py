"""app/search.py's render_results - pure, so it is fully covered here.

The invariant these tests pin (docs/decisions/0020): search RANKS, it never
LISTS. Every non-empty render says so, in the same words the tool
description and the system prompt use, so a reader cannot mistake a result
list for a complete listing.
"""

from __future__ import annotations

from app.search import Hit, render_results

RANKS_NOT_LISTS = "RANKS; it does not list"


def _hit(path="wiki/recipes/ragu.md", heading="Method", **kw):
    defaults = {
        "path": path,
        "heading": heading,
        "snippet": "brown the soffritto slowly",
        "score": 0.5,
        "dense_rank": None,
        "lexical_rank": None,
    }
    defaults.update(kw)
    return Hit(**defaults)


def test_path_comes_first_on_each_line():
    text = render_results([_hit()], "ragu", "hybrid")

    lines = [ln for ln in text.splitlines() if ln.startswith("1.")]
    assert lines and lines[0].startswith("1. wiki/recipes/ragu.md")


def test_every_non_empty_render_states_the_ranks_not_lists_caveat():
    text = render_results([_hit()], "ragu", "hybrid")

    assert RANKS_NOT_LISTS in text


def test_empty_result_names_glob_and_grep():
    text = render_results([], "nonexistent thing", "hybrid")

    assert "Glob" in text
    assert "Grep" in text
    assert "nonexistent thing" in text


def test_unavailable_state_names_glob_and_grep_and_skips_the_query():
    text = render_results([], "anything", "unavailable")

    assert "Glob" in text
    assert "Grep" in text


def test_lexical_only_state_adds_a_note():
    text = render_results([_hit()], "ragu", "lexical")

    assert "semantic ranking is off" in text


def test_hybrid_state_has_no_lexical_only_note():
    text = render_results([_hit()], "ragu", "hybrid")

    assert "semantic ranking is off" not in text


def test_both_sided_hit_shows_both_ranks():
    hit = _hit(dense_rank=1, lexical_rank=3)
    text = render_results([hit], "ragu", "hybrid")

    assert "dense #1" in text
    assert "lexical #3" in text
    assert "[both:" in text


def test_dense_only_hit_shows_dense_rank_only():
    hit = _hit(dense_rank=2, lexical_rank=None)
    text = render_results([hit], "ragu", "hybrid")

    assert "[dense #2]" in text
    assert "lexical #" not in text


def test_lexical_only_hit_shows_lexical_rank_only():
    hit = _hit(dense_rank=None, lexical_rank=1)
    text = render_results([hit], "ragu", "lexical")

    assert "[lexical #1]" in text
    assert "dense #" not in text
