"""Recovery from TigerFS's Create()-then-fchmod bug.

See ADR 0007's amendment and timescale/tigerfs#74: Write/Edit sometimes fail
with `ENOENT: no such file or directory, fchmod` even though the underlying
write would have been fine - TigerFS's `OpsNode.Create` hard-codes a new
file's mode to 0644 and ignores whatever mode the caller asked for, so the
client's fchmod call was never doing anything real. `kb.write_kb_file_safely`
and `kb.apply_kb_edit_safely` reconstruct the write without calling fchmod at
all, and `guards.fchmod_recovery_for` is the PostToolUseFailure hook that
calls them automatically and tells the model so.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from app import guards, kb

FCHMOD_ERROR = "ENOENT: no such file or directory, fchmod"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point kb at a temp KB_MOUNT, matching tests/test_checklist.py."""
    monkeypatch.setattr(
        kb, "config", types.SimpleNamespace(work_dir="/work", kb_mount=str(tmp_path))
    )
    root = tmp_path / "memory"
    root.mkdir()
    return root


# --- guards.is_fchmod_enoent -------------------------------------------


def test_the_exact_observed_message_is_recognised():
    assert guards.is_fchmod_enoent(FCHMOD_ERROR)


def test_an_unrelated_error_is_not():
    assert not guards.is_fchmod_enoent("Write verification failed: ...")
    assert not guards.is_fchmod_enoent("")


def test_fchmod_alone_without_enoent_is_not_this_bug():
    # A different fchmod failure (e.g. a real permission error) should not
    # be silently "recovered" - only the specific ENOENT signature is safe
    # to reconstruct without calling fchmod.
    assert not guards.is_fchmod_enoent("EACCES: permission denied, fchmod")


# --- kb.write_kb_file_safely ---------------------------------------------


def test_writes_a_brand_new_file(workspace):
    target = workspace / "wiki" / "recipes" / "new.md"
    result = kb.write_kb_file_safely(str(target), "# New\n")
    assert result == target
    assert target.read_text() == "# New\n"


def test_overwrites_an_existing_file_whole(workspace):
    target = workspace / "notes.md"
    target.write_text("old\n")
    result = kb.write_kb_file_safely(str(target), "new content\n")
    assert result == target
    assert target.read_text() == "new content\n"


def test_a_relative_path_is_rejected(workspace):
    assert kb.write_kb_file_safely("wiki/x.md", "content") is None


def test_a_path_outside_the_workspace_is_rejected(workspace):
    outside = workspace.parent / "escape.md"
    assert kb.write_kb_file_safely(str(outside), "content") is None
    assert not outside.exists()


def test_traversal_out_of_the_workspace_is_rejected(workspace):
    escaping = str(workspace / ".." / ".." / "etc" / "passwd")
    assert kb.write_kb_file_safely(escaping, "content") is None


# --- kb.apply_kb_edit_safely ----------------------------------------------


def test_applies_a_unique_replacement(workspace):
    target = workspace / "guide.md"
    target.write_text("before\nold line\nafter\n")
    result = kb.apply_kb_edit_safely(str(target), "old line", "new line")
    assert result == target
    assert target.read_text() == "before\nnew line\nafter\n"


def test_refuses_an_ambiguous_match_without_replace_all(workspace):
    target = workspace / "guide.md"
    target.write_text("x\nx\n")
    assert kb.apply_kb_edit_safely(str(target), "x", "y") is None
    assert target.read_text() == "x\nx\n"  # untouched


def test_replace_all_handles_every_occurrence(workspace):
    target = workspace / "guide.md"
    target.write_text("x\nx\n")
    result = kb.apply_kb_edit_safely(str(target), "x", "y", replace_all=True)
    assert result == target
    assert target.read_text() == "y\ny\n"


def test_refuses_a_missing_match(workspace):
    target = workspace / "guide.md"
    target.write_text("hello\n")
    assert kb.apply_kb_edit_safely(str(target), "goodbye", "hi") is None


def test_refuses_a_nonexistent_file(workspace):
    target = workspace / "missing.md"
    assert kb.apply_kb_edit_safely(str(target), "a", "b") is None


# --- the hook --------------------------------------------------------------


def _run(tool_name: str, tool_input: dict, error: str) -> dict:
    hook = guards.fchmod_recovery_for()
    return asyncio.run(
        hook(
            {"tool_name": tool_name, "tool_input": tool_input, "error": error},
            "tu_1",
            None,
        )
    )


def test_recovers_a_failed_write_and_tells_the_model(workspace):
    target = workspace / "recipes" / "kebabs.md"
    out = _run(
        "Write",
        {"file_path": str(target), "content": "# Kebabs\n"},
        FCHMOD_ERROR,
    )
    assert target.read_text() == "# Kebabs\n"
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "already been written" in context
    assert "do not fall back to a shell command" in context.lower()


def test_recovers_a_failed_edit(workspace):
    target = workspace / "guide.md"
    target.write_text("before\nold\nafter\n")
    out = _run(
        "Edit",
        {"file_path": str(target), "old_string": "old", "new_string": "new"},
        FCHMOD_ERROR,
    )
    assert target.read_text() == "before\nnew\nafter\n"
    assert "additionalContext" in out["hookSpecificOutput"]


def test_ignores_an_unrelated_failure(workspace):
    target = workspace / "notes.md"
    target.write_text("original\n")
    out = _run(
        "Write",
        {"file_path": str(target), "content": "clobbered\n"},
        "Write verification failed: 10 bytes on disk, expected 9",
    )
    assert out == {}
    assert target.read_text() == "original\n"  # never touched


def test_ignores_tools_it_does_not_know_how_to_reconstruct(workspace):
    out = _run("MultiEdit", {"file_path": str(workspace / "x.md")}, FCHMOD_ERROR)
    assert out == {}


def test_leaves_the_failure_alone_when_it_cannot_reconstruct_the_edit(workspace):
    target = workspace / "guide.md"
    target.write_text("nothing matches\n")
    out = _run(
        "Edit",
        {"file_path": str(target), "old_string": "absent", "new_string": "x"},
        FCHMOD_ERROR,
    )
    assert out == {}
    assert target.read_text() == "nothing matches\n"


def test_a_malformed_payload_does_not_break_the_turn():
    """A broken hook must not take down the turn it was meant to help."""
    hook = guards.fchmod_recovery_for()
    assert asyncio.run(hook({}, None, None)) == {}
    assert asyncio.run(hook({"tool_name": "Write"}, None, None)) == {}
