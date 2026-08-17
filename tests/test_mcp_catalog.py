"""The outbound MCP catalog: what reaches the agent, and what never can.

Two properties here are safety properties rather than behaviour, and both are
easy to delete by accident:

* A catalog entry names an environment variable and never holds its value. The
  structural test at the bottom enforces that against the real source, because
  a reviewer reading a diff full of opaque strings is exactly who this design
  is protecting.
* A tool the entry does not pre-approve must stay OUT of `allowed_tools`, so it
  falls through to `can_use_tool` and a person decides. That is the whole
  safety story for a write-capable server, and nothing else in the codebase
  would notice it being lost.
"""

from __future__ import annotations

import ast
import pathlib
import re
import types

import pytest

from app import agent, mcp_catalog
from app.mcp_catalog import Server
from app.turns import Turn


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


@pytest.fixture(autouse=True)
def _forget_warnings():
    """Reset the log-once memo, which is module state shared between tests."""
    mcp_catalog._warned.clear()


def _catalog(monkeypatch, *servers: Server) -> None:
    monkeypatch.setattr(mcp_catalog, "CATALOG", servers)


def _server_names(opts) -> set[str]:
    """The names in `mcp_servers`, asserting the shape rather than assuming it.

    `ClaudeAgentOptions.mcp_servers` is a union of a dict, a str and a Path -
    the last two being paths to a config file, which this deployment never uses
    and `strict_mcp_config` exists to keep unused. Same reasoning as `_appended`
    in test_agent_options.py: if the shape drifts, fail saying so.
    """
    servers = opts.mcp_servers
    assert isinstance(servers, dict), f"expected a dict, got {type(servers)}"
    return set(servers)


CALENDAR = Server(
    name="calendar",
    summary="Google Calendar for the household account.",
    command="npx",
    args=("-y", "@example/calendar-mcp@1.0.0"),
    secrets={"GOOGLE_REFRESH_TOKEN": "MCP_GOOGLE_REFRESH_TOKEN"},
    auto_approve=("list_events",),
)


# --- the shipped state -------------------------------------------------------


def test_the_catalog_ships_empty():
    """Adding a server is a reviewed, deployed change - never a default."""
    assert mcp_catalog.CATALOG == ()
    assert mcp_catalog.resolved() == {}
    assert mcp_catalog.auto_approved_tools() == []
    assert mcp_catalog.summaries() == ""


def test_an_empty_catalog_leaves_the_turn_exactly_as_it_was(isolated):
    """Nothing about a deploy changes until someone adds an entry."""
    turn = Turn(id="t1", user_email="alice@example.com")
    opts = agent._options("alice", None, "", turn)

    assert _server_names(opts) == {"ask"}


# --- resolution --------------------------------------------------------------


def test_a_server_with_its_secret_set_resolves_to_a_stdio_config(monkeypatch):
    monkeypatch.setenv("MCP_GOOGLE_REFRESH_TOKEN", "s3cret-value")
    _catalog(monkeypatch, CALENDAR)

    resolved = mcp_catalog.resolved()

    assert set(resolved) == {"calendar"}
    assert resolved["calendar"] == {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@example/calendar-mcp@1.0.0"],
        # The server's own variable name, carrying OUR variable's value.
        "env": {"GOOGLE_REFRESH_TOKEN": "s3cret-value"},
    }


def test_a_server_with_a_missing_secret_is_dropped_not_launched(monkeypatch, caplog):
    """Launching it would fail on first use, which is far later and quieter."""
    monkeypatch.delenv("MCP_GOOGLE_REFRESH_TOKEN", raising=False)
    _catalog(monkeypatch, CALENDAR)

    with caplog.at_level("WARNING"):
        assert mcp_catalog.resolved() == {}

    assert "MCP_GOOGLE_REFRESH_TOKEN" in caplog.text, (
        "the log has to name the variable, or the operator has nothing to act on"
    )


def test_a_disabled_server_warns_once_per_turn_not_once_per_lookup(monkeypatch, caplog):
    """_live() runs three times per turn; three identical lines train people to
    skip the line, which is the opposite of what a warning is for."""
    monkeypatch.delenv("MCP_GOOGLE_REFRESH_TOKEN", raising=False)
    _catalog(monkeypatch, CALENDAR)

    with caplog.at_level("WARNING"):
        mcp_catalog.auto_approved_tools()
        mcp_catalog.resolved()
        mcp_catalog.summaries()

    disabled = [r for r in caplog.records if "disabled" in r.getMessage()]
    assert len(disabled) == 1, f"expected one warning, got {len(disabled)}"


def test_a_secret_set_to_whitespace_counts_as_missing(monkeypatch):
    """`fly secrets set FOO=` is a plausible way to arrive here."""
    monkeypatch.setenv("MCP_GOOGLE_REFRESH_TOKEN", "   ")
    _catalog(monkeypatch, CALENDAR)

    assert mcp_catalog.resolved() == {}


def test_status_names_the_missing_variable_and_never_a_value(monkeypatch):
    monkeypatch.delenv("MCP_GOOGLE_REFRESH_TOKEN", raising=False)
    _catalog(monkeypatch, CALENDAR)

    assert mcp_catalog.status() == {"calendar": "missing MCP_GOOGLE_REFRESH_TOKEN"}

    monkeypatch.setenv("MCP_GOOGLE_REFRESH_TOKEN", "s3cret-value")
    assert mcp_catalog.status() == {"calendar": "ready"}
    assert "s3cret-value" not in str(mcp_catalog.status())


def test_status_reports_a_configured_server_even_while_it_is_disabled(monkeypatch):
    """/healthz must distinguish "not configured" from "configured and dark"."""
    monkeypatch.delenv("MCP_GOOGLE_REFRESH_TOKEN", raising=False)
    _catalog(monkeypatch, CALENDAR)

    assert "calendar" in mcp_catalog.status()
    assert "calendar" not in mcp_catalog.resolved()


# --- the two-tier approval rule ----------------------------------------------


def test_pre_approved_tools_reach_allowed_tools_fully_qualified(isolated, monkeypatch):
    monkeypatch.setenv("MCP_GOOGLE_REFRESH_TOKEN", "s3cret-value")
    _catalog(monkeypatch, CALENDAR)

    opts = agent._options("alice", None, "", Turn(id="t1", user_email="a@e.com"))

    assert "mcp__calendar__list_events" in opts.allowed_tools
    assert _server_names(opts) == {"ask", "calendar"}


def test_a_tool_that_is_not_pre_approved_must_fall_through_to_the_human(
    isolated, monkeypatch
):
    """The safety story for a write-capable server, and nothing else pins it.

    `create_event` is deliberately absent from `auto_approve`. If it leaked into
    `allowed_tools` the CLI would run it before `can_use_tool` was ever
    consulted, and the agent would be writing to a real calendar with nobody
    asked.
    """
    monkeypatch.setenv("MCP_GOOGLE_REFRESH_TOKEN", "s3cret-value")
    _catalog(monkeypatch, CALENDAR)

    opts = agent._options("alice", None, "", Turn(id="t1", user_email="a@e.com"))

    assert "mcp__calendar__create_event" not in opts.allowed_tools
    assert any("calendar" in rule for rule in opts.allowed_tools), (
        "sanity check: the read-only tool really was allowlisted"
    )
    # A wildcard would satisfy the assertion above while pre-approving every
    # tool the server has - including whichever one it learns to delete with in
    # its next version. Checked explicitly because it is the obvious shortcut
    # for anyone who finds listing tool names tedious.
    assert not any("*" in rule for rule in opts.allowed_tools if "calendar" in rule), (
        "catalog tools must be allowlisted by name, never by wildcard"
    )


def test_a_disabled_server_contributes_no_allowlist_entries(isolated, monkeypatch):
    monkeypatch.delenv("MCP_GOOGLE_REFRESH_TOKEN", raising=False)
    _catalog(monkeypatch, CALENDAR)

    opts = agent._options("alice", None, "", Turn(id="t1", user_email="a@e.com"))

    assert not any("calendar" in rule for rule in opts.allowed_tools)


# --- who does and does not get a connected service ---------------------------


def test_reflection_never_gets_a_connected_service(isolated, monkeypatch):
    """Nobody is watching, so `can_use_tool` is absent and Allow cannot happen."""
    monkeypatch.setenv("MCP_GOOGLE_REFRESH_TOKEN", "s3cret-value")
    _catalog(monkeypatch, CALENDAR)

    opts = agent._reflection_options(
        "alice", Turn(id="t1", user_email="r@e.com", interactive=False), ""
    )

    assert not opts.mcp_servers
    assert not any("calendar" in rule for rule in opts.allowed_tools)


def test_the_agent_is_told_the_service_is_shared_and_not_its_to_change(monkeypatch):
    monkeypatch.setenv("MCP_GOOGLE_REFRESH_TOKEN", "s3cret-value")
    _catalog(monkeypatch, CALENDAR)

    text = mcp_catalog.summaries()

    assert "calendar" in text
    assert "household" in text
    assert "bead" in text, "wanting a new server has to route to the ledger"
    assert "s3cret-value" not in text


# --- structural --------------------------------------------------------------


def test_no_catalog_entry_can_hard_code_a_secret():
    """Structural, in the spirit of test_no_entry_point_bypasses_begin.

    Every value in a `secrets` mapping must be an environment VARIABLE NAME, so
    that a reviewer can tell at a glance that a diff adds no credential. Read
    from the real source rather than from the imported object, because the point
    is what a human reviewing this file would see.
    """
    source = pathlib.Path(mcp_catalog.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Server"
        ):
            continue
        for kw in node.keywords:
            if kw.arg != "secrets" or not isinstance(kw.value, ast.Dict):
                continue
            for value in kw.value.values:
                if not isinstance(value, ast.Constant) or not isinstance(
                    value.value, str
                ):
                    offenders.append(ast.dump(value))
                elif not re.fullmatch(r"[A-Z][A-Z0-9_]*", value.value):
                    offenders.append(value.value)

    assert offenders == [], (
        f"a `secrets` value must be an env var NAME, not a credential: {offenders}"
    )
