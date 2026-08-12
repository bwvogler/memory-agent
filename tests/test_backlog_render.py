"""Rendering the bead graph into memory/backlog.md.

This page is the durable, human-readable copy of the backlog: the bead graph
lives on an unreplicated volume, while this lands in the Postgres-backed KB and
renders in /kb. It is generated after every turn, so a crash here would break
ordinary turns rather than anything obviously backlog-shaped.
"""

from __future__ import annotations

from app.kb import _render_backlog


def test_empty_graph_says_so():
    out = _render_backlog([])

    assert "Nothing open." in out
    assert out.startswith("# Backlog")


def test_warns_against_hand_editing():
    # The page is overwritten every turn; an edit here is silently lost.
    assert "Do not edit by hand" in _render_backlog([])


def test_groups_by_status_and_orders_by_priority():
    out = _render_backlog([
        {"id": "kb-c", "title": "Low", "status": "open", "priority": 4},
        {"id": "kb-a", "title": "Urgent", "status": "open", "priority": 0},
        {"id": "kb-b", "title": "Doing", "status": "in_progress", "priority": 2},
    ])

    # in_progress sorts ahead of open.
    assert out.index("## In Progress") < out.index("## Open")
    assert out.index("kb-a") < out.index("kb-c")


def test_unknown_status_is_kept_not_dropped():
    """A status bd adds later must still appear rather than vanish silently."""
    out = _render_backlog([
        {"id": "kb-z", "title": "Odd", "status": "superseded", "priority": 1},
    ])

    assert "kb-z" in out
    assert "Superseded" in out


def test_blocker_count_is_shown():
    out = _render_backlog([
        {"id": "kb-b", "title": "Blocked", "status": "open", "priority": 2,
         "dependency_count": 2},
    ])

    assert "blocked by 2" in out


def test_long_description_is_truncated_and_marked():
    out = _render_backlog([
        {"id": "kb-l", "title": "Long", "status": "open", "priority": 2,
         "description": "x" * 500},
    ])

    assert "..." in out
    assert "x" * 300 not in out


def test_only_the_first_description_line_is_used():
    out = _render_backlog([
        {"id": "kb-m", "title": "Multi", "status": "open", "priority": 2,
         "description": "first line\nsecond line"},
    ])

    assert "first line" in out
    assert "second line" not in out


def test_missing_optional_fields_do_not_crash():
    # bd omits `description` entirely when it is empty.
    out = _render_backlog([{"id": "kb-n", "status": "open"}])

    assert "kb-n" in out
