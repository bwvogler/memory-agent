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
    out = _render_backlog(
        [
            {"id": "kb-c", "title": "Low", "status": "open", "priority": 4},
            {"id": "kb-a", "title": "Urgent", "status": "open", "priority": 0},
            {"id": "kb-b", "title": "Doing", "status": "in_progress", "priority": 2},
        ]
    )

    # in_progress sorts ahead of open.
    assert out.index("## In Progress") < out.index("## Open")
    assert out.index("kb-a") < out.index("kb-c")


def test_unknown_status_is_kept_not_dropped():
    """A status bd adds later must still appear rather than vanish silently."""
    out = _render_backlog(
        [
            {"id": "kb-z", "title": "Odd", "status": "superseded", "priority": 1},
        ]
    )

    assert "kb-z" in out
    assert "Superseded" in out


def test_blocker_count_is_shown():
    out = _render_backlog(
        [
            {
                "id": "kb-b",
                "title": "Blocked",
                "status": "open",
                "priority": 2,
                "dependency_count": 2,
            },
        ]
    )

    assert "blocked by 2" in out


def test_long_descriptions_are_reproduced_in_full():
    """Truncating here once made the durable copy useless for reconstruction.

    This file is the only copy of the ledger that reaches Postgres. The bead
    graph lives on an unreplicated volume, so if a summary is all this holds,
    a lost volume takes every design note and acceptance criterion with it.
    """
    out = _render_backlog(
        [
            {
                "id": "kb-l",
                "title": "Long",
                "status": "open",
                "priority": 2,
                "description": "x" * 500,
            },
        ]
    )

    assert "x" * 500 in out


def test_every_description_line_survives():
    out = _render_backlog(
        [
            {
                "id": "kb-m",
                "title": "Multi",
                "status": "open",
                "priority": 2,
                "description": "first line\n\nsecond line\n  indented detail",
            },
        ]
    )

    assert "first line" in out
    assert "second line" in out
    assert "indented detail" in out


def test_labels_are_shown_so_signal_beads_are_identifiable():
    out = _render_backlog(
        [
            {
                "id": "kb-s",
                "title": "Revert",
                "status": "deferred",
                "priority": 1,
                "labels": ["signal", "revert"],
            },
        ]
    )

    assert "signal" in out


def test_missing_optional_fields_do_not_crash():
    # bd omits `description` entirely when it is empty.
    out = _render_backlog([{"id": "kb-n", "status": "open"}])

    assert "kb-n" in out
