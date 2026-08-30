"""Checkbox ticks in the rendered article write straight back to the file.

This is the one KB write that deliberately skips the turn/savepoint machinery
(see `kb.toggle_checkbox`), so it needs its own coverage of the two things
that machinery would otherwise have caught for free: a client-supplied path
escaping the workspace, and a checkbox count that has to agree with what the
browser rendered without either side reading the other's logic.
"""

from __future__ import annotations

import types

import pytest

from app import kb


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point kb at a temp KB_MOUNT so these tests never touch /mnt/kb."""
    monkeypatch.setattr(
        kb, "config", types.SimpleNamespace(work_dir="/work", kb_mount=str(tmp_path))
    )
    root = tmp_path / "memory"
    root.mkdir()
    return root


def write(root, name, text):
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


# --- resolve_kb_path ---------------------------------------------------


def test_a_plain_relative_path_resolves(workspace):
    write(workspace, "todo.md", "- [ ] a\n")
    assert kb.resolve_kb_path("todo.md") == workspace / "todo.md"


def test_an_absolute_path_is_rejected(workspace):
    assert kb.resolve_kb_path("/etc/passwd") is None


def test_traversal_out_of_the_workspace_is_rejected(workspace):
    assert kb.resolve_kb_path("../../.beads/issues.jsonl") is None


def test_an_empty_path_is_rejected(workspace):
    assert kb.resolve_kb_path("") is None


# --- toggle_checkbox -----------------------------------------------------


def test_checking_an_unchecked_box(workspace):
    path = write(workspace, "todo.md", "- [ ] wash the car\n")
    result = kb.toggle_checkbox("todo.md", 0, checked=True)
    assert result == "- [x] wash the car\n"
    assert path.read_text() == "- [x] wash the car\n"


def test_unchecking_a_checked_box(workspace):
    write(workspace, "todo.md", "- [x] wash the car\n")
    result = kb.toggle_checkbox("todo.md", 0, checked=False)
    assert result == "- [ ] wash the car\n"


def test_index_selects_among_several_boxes(workspace):
    write(
        workspace,
        "todo.md",
        "- [ ] first\n- [ ] second\n- [ ] third\n",
    )
    result = kb.toggle_checkbox("todo.md", 1, checked=True)
    assert result == "- [ ] first\n- [x] second\n- [ ] third\n"


def test_a_checkbox_inside_a_fenced_code_block_does_not_count(workspace):
    raw = "before\n\n```\n- [ ] not a real one\n```\n\n- [ ] real one\n"
    write(workspace, "todo.md", raw)
    # index 0 must land on the real item, not the one inside the fence.
    result = kb.toggle_checkbox("todo.md", 0, checked=True)
    assert result == raw.replace("- [ ] real one", "- [x] real one")


def test_an_out_of_range_index_returns_none(workspace):
    write(workspace, "todo.md", "- [ ] only one\n")
    assert kb.toggle_checkbox("todo.md", 1, checked=True) is None


def test_a_missing_file_returns_none(workspace):
    assert kb.toggle_checkbox("nope.md", 0, checked=True) is None


def test_an_escaping_path_returns_none(workspace):
    assert kb.toggle_checkbox("../outside.md", 0, checked=True) is None


def test_indentation_and_nested_markers_are_preserved(workspace):
    write(workspace, "todo.md", "- [ ] parent\n  * [ ] nested\n")
    result = kb.toggle_checkbox("todo.md", 1, checked=True)
    assert result == "- [ ] parent\n  * [x] nested\n"
