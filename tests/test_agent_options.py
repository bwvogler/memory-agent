"""Session options that are load-bearing and easy to delete by accident.

`permission_mode="acceptEdits"` auto-accepts file writes but NOT Bash. Without
an explicit allowlist the agent writes to the wiki perfectly and is silently
blocked from ever running `bd` - it asks for an approval nobody is present to
give, and files nothing. That failure looks exactly like a working system, so
it is pinned here.

This is a proxy: it catches the line being removed, not the SDK changing what
`acceptEdits` covers. Only the live tier catches the latter.
"""

from __future__ import annotations

import types
from typing import cast

import pytest

from app import agent


def _appended(opts) -> str:
    """The text _options appends to the preset system prompt.

    ClaudeAgentOptions.system_prompt is a four-way union (str, preset dict,
    file dict, None) and only one arm carries "append". _options always builds
    the preset arm, so assert that rather than indexing into the union blind -
    if the shape ever drifts, this fails saying so instead of TypeErroring.
    """
    prompt = opts.system_prompt
    assert isinstance(prompt, dict), f"expected a preset dict, got {type(prompt)}"
    assert prompt.get("type") == "preset", prompt
    return cast("str", prompt["append"])


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point config at a temp dir so _options does not touch /work or /mnt/kb."""
    fake = types.SimpleNamespace(
        work_dir=str(tmp_path / "work"),
        kb_mount=str(tmp_path / "kb"),
        agent_model="claude-sonnet-4-6",
        max_turns=30,
    )
    monkeypatch.setattr(agent, "config", fake)
    monkeypatch.setattr(agent.kb, "scratch_dir_for", lambda slug: tmp_path / slug)
    (tmp_path / "alice").mkdir(exist_ok=True)
    return fake


def test_bd_is_allowed_without_a_permission_prompt(isolated):
    opts = agent._options("alice", None)

    assert any("bd" in rule for rule in opts.allowed_tools), (
        "bd must be allowlisted or the agent can never file a bead headlessly"
    )


def test_bash_is_not_opened_up_wholesale(isolated):
    """The allowlist should stay scoped to bd, not grant Bash generally."""
    opts = agent._options("alice", None)

    assert "Bash" not in opts.allowed_tools


def test_filesystem_settings_are_never_loaded(isolated):
    """Multi-tenant isolation: one user's settings must not reach another."""
    opts = agent._options("alice", None)

    assert opts.setting_sources == []


def test_auto_memory_stays_disabled(isolated):
    """Auto memory loads regardless of setting_sources, leaking between users."""
    opts = agent._options("alice", None)

    assert opts.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_each_user_gets_their_own_config_dir(isolated):
    a = agent._options("alice", None)
    b = agent._options("bob", None)

    assert a.env["CLAUDE_CONFIG_DIR"] != b.env["CLAUDE_CONFIG_DIR"]


def test_beads_overrides_are_appended_when_bd_context_is_present(isolated):
    """bd's own instructions contradict two decisions this deployment made."""
    opts = agent._options("alice", None, "# Beads Workflow Context")
    appended = _appended(opts)

    assert "memory/CLAUDE.md" in appended
    assert "Never run git commands" in appended


def test_no_beads_overrides_without_bd_context(isolated):
    """If bd is unavailable, do not tell the agent to override absent rules."""
    opts = agent._options("alice", None)

    assert "Overrides to the beads instructions" not in _appended(opts)
