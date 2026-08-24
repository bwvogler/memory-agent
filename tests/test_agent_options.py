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
from app.turns import Turn


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


# --- the interaction surface, which is turn-scoped ---------------------------
#
# `_options(...)` with no turn is the shape most of this file asserts on. The
# interaction options only exist when there IS a turn, so they are pinned
# separately - and the reflection surface is pinned as the ABSENCE of them,
# because that is a safety property rather than an oversight.


def _turn(*, interactive: bool = True) -> Turn:
    return Turn(id="t1", user_email="dev@localhost", interactive=interactive)


def test_asking_a_question_never_itself_raises_a_prompt(isolated):
    """A whole-tool allowlist entry auto-approves before can_use_tool is asked."""
    opts = agent._options("alice", None, "", _turn())

    assert "mcp__ask__ask_user" in opts.allowed_tools
    assert "TodoWrite" in opts.allowed_tools
    assert "Task" in opts.allowed_tools


def test_the_broken_builtin_question_tool_is_blocked(isolated):
    """With no TTY it resolves with EMPTY answers; see anthropics/claude-code#50728."""
    opts = agent._options("alice", None, "", _turn())

    assert "AskUserQuestion" in opts.disallowed_tools


def test_an_interactive_turn_can_reach_its_human(isolated):
    opts = agent._options("alice", None, "", _turn())

    assert opts.can_use_tool is not None
    # mcp_servers is a union: inline servers, or a path to a config file. Assert
    # the arm _options actually builds rather than indexing into it blind.
    servers = opts.mcp_servers
    assert isinstance(servers, dict), f"expected inline servers, got {type(servers)}"
    assert "ask" in servers


def test_a_machine_caller_gets_no_permission_prompt(isolated):
    """Nobody is watching an /mcp turn, so a prompt could only spend its timeout."""
    opts = agent._options("alice", None, "", _turn(interactive=False))

    assert opts.can_use_tool is None


def test_the_agent_cannot_grant_itself_mcp_servers(isolated):
    """cwd is the agent's own writable scratch, so a .mcp.json there is reachable."""
    opts = agent._options("alice", None, "", _turn())

    assert opts.strict_mcp_config is True


def test_the_named_subagents_are_declared_and_read_only_where_it_matters(isolated):
    opts = agent._options("alice", None, "", _turn())

    assert set(opts.agents or {}) == {"kb-query", "kb-lint"}
    query = (opts.agents or {})["kb-query"]
    assert query.tools is not None
    assert "Write" not in query.tools and "Bash" not in query.tools, (
        "kb-query must not be able to change anything"
    )


def test_reflection_gets_no_interaction_surface_at_all(isolated):
    """Reflection is signal-triggered: there is nobody to answer it."""
    opts = agent._reflection_options("alice", _turn(interactive=False), "")

    assert opts.can_use_tool is None
    assert not opts.mcp_servers
    assert not opts.agents


def test_reflection_still_reports_what_it_did(isolated):
    """The observer hooks ask nothing, so reflection keeps them."""
    opts = agent._reflection_options("alice", _turn(interactive=False), "")

    assert "PostToolUse" in (opts.hooks or {})
    assert "SubagentStop" in (opts.hooks or {})
