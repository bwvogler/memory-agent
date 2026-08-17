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
* A tool in `deny` must reach `disallowed_tools`. The Google token grants the
  send that `deny` refuses, so this list is the only thing standing between the
  agent and mail leaving the house.

The last section pins the REAL entries rather than a fixture, which is unusual
here and deliberate: those tool names are a claim about two pinned npm packages,
and a version bump can add tools that no tuple in this repo mentions.
"""

from __future__ import annotations

import ast
import dataclasses
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
    """Reset the log-once memos, which are module state shared between tests."""
    mcp_catalog._warned.clear()
    mcp_catalog._materialised.clear()


@pytest.fixture(autouse=True)
def _private_state_dir(tmp_path, monkeypatch):
    """Never let a test write credential files to the real /tmp/mcp-catalog.

    `Config` is frozen, so this replaces the object rather than setting on it -
    which also keeps every other field at its real value.
    """
    monkeypatch.setattr(
        mcp_catalog,
        "config",
        dataclasses.replace(mcp_catalog.config, mcp_state_dir=str(tmp_path / "state")),
    )


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


def _stdio_env(resolved, name: str) -> dict[str, str]:
    """The `env` of one resolved server, asserting the shape rather than
    assuming it. `McpServerConfig` is a union of four TypedDicts and only the
    stdio one has `env` - so index it blind and a type checker is right to
    object. Sibling of `_server_names` above."""
    entry = resolved[name]
    assert entry["type"] == "stdio", f"expected a stdio server, got {entry['type']}"
    env = entry.get("env")
    assert isinstance(env, dict), f"expected an env dict, got {type(env)}"
    return env


CALENDAR = Server(
    name="calendar",
    summary="Google Calendar for the household account.",
    command="npx",
    args=("-y", "@example/calendar-mcp@1.0.0"),
    secrets={"GOOGLE_REFRESH_TOKEN": "MCP_GOOGLE_REFRESH_TOKEN"},
    auto_approve=("list_events",),
)


# --- the shipped state -------------------------------------------------------


def test_a_catalogued_server_stays_dark_until_its_secrets_are_set(monkeypatch):
    """The deployed default: entries exist, nothing runs, nothing is allowed.

    Adding an entry is a reviewed change and setting its secrets is a second,
    separate one. Until the second happens a fresh deploy behaves as if the
    catalog were still empty.
    """
    for server in mcp_catalog.CATALOG:
        for var in server.needs():
            monkeypatch.delenv(var, raising=False)

    assert mcp_catalog.resolved() == {}
    assert mcp_catalog.auto_approved_tools() == []
    assert mcp_catalog.summaries() == ""


def test_an_empty_catalog_leaves_the_turn_exactly_as_it_was(isolated, monkeypatch):
    """Nothing about a deploy changes until someone adds an entry."""
    _catalog(monkeypatch)
    turn = Turn(id="t1", user_email="alice@example.com")
    opts = agent._options("alice", None, "", turn)

    assert _server_names(opts) == {"ask"}
    assert opts.disallowed_tools == ["AskUserQuestion"]


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


# --- credentials that are files ----------------------------------------------


FILEY = Server(
    name="filey",
    summary="A server that reads its credentials from disk.",
    command="filey-mcp",
    files={"token.json": "MCP_FILEY_TOKEN"},
    env={"FILEY_TOKEN_PATH": "{dir}/token.json", "FILEY_MODE": "readonly"},
    auto_approve=("look",),
)


def test_a_files_entry_lands_on_disk_and_the_path_reaches_the_server(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MCP_FILEY_TOKEN", '{"refresh_token": "abc"}')
    _catalog(monkeypatch, FILEY)

    resolved = mcp_catalog.resolved()

    directory = tmp_path / "state" / "filey"
    written = directory / "token.json"
    assert written.read_text(encoding="utf-8") == '{"refresh_token": "abc"}'
    # `{dir}` is substituted; a literal is passed straight through.
    env = _stdio_env(resolved, "filey")
    assert env["FILEY_TOKEN_PATH"] == str(written)
    assert env["FILEY_MODE"] == "readonly"


def test_credential_files_are_not_world_readable(monkeypatch, tmp_path):
    """Belt and braces. The containment that matters is that this directory is
    container-local and outside `add_dirs`, but a live OAuth token should not
    also be sitting at 0644 for anything else sharing the box."""
    monkeypatch.setenv("MCP_FILEY_TOKEN", "secret")
    _catalog(monkeypatch, FILEY)

    mcp_catalog.resolved()

    directory = tmp_path / "state" / "filey"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "token.json").stat().st_mode & 0o777 == 0o600


def test_a_missing_file_variable_drops_the_server_like_a_missing_secret(monkeypatch):
    """`files` and `secrets` are two spellings of "this server needs a value"."""
    monkeypatch.delenv("MCP_FILEY_TOKEN", raising=False)
    _catalog(monkeypatch, FILEY)

    assert mcp_catalog.resolved() == {}
    assert mcp_catalog.status() == {"filey": "missing MCP_FILEY_TOKEN"}


def test_files_are_written_once_per_process_not_once_per_turn(monkeypatch, tmp_path):
    """The servers write REFRESHED tokens back to these files. Rewriting every
    turn would throw that away and force another refresh immediately."""
    monkeypatch.setenv("MCP_FILEY_TOKEN", "original")
    _catalog(monkeypatch, FILEY)

    mcp_catalog.resolved()
    written = tmp_path / "state" / "filey" / "token.json"
    written.write_text("refreshed-by-the-server", encoding="utf-8")

    mcp_catalog.resolved()  # a second turn

    assert written.read_text(encoding="utf-8") == "refreshed-by-the-server"


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


# --- the third tier: deny ----------------------------------------------------


DENIER = Server(
    name="post",
    summary="A server that can put something in front of another person.",
    command="post-mcp",
    secrets={"POST_TOKEN": "MCP_POST_TOKEN"},
    auto_approve=("read_outbox",),
    deny=("send",),
)


def test_denied_tools_reach_disallowed_tools_fully_qualified(isolated, monkeypatch):
    monkeypatch.setenv("MCP_POST_TOKEN", "s3cret-value")
    _catalog(monkeypatch, DENIER)

    opts = agent._options("alice", None, "", Turn(id="t1", user_email="a@e.com"))

    assert "mcp__post__send" in opts.disallowed_tools
    assert "AskUserQuestion" in opts.disallowed_tools, "the existing entry survives"
    assert "mcp__post__send" not in opts.allowed_tools
    # Same reasoning as the allowlist: a wildcard here would over-deny silently
    # today and is the obvious shortcut tomorrow. Names only, both directions.
    assert not any("*" in rule for rule in opts.disallowed_tools)


def test_a_tool_is_denied_even_when_its_server_is_dark(isolated, monkeypatch):
    """`denied_tools` reads CATALOG, not `_live()`.

    Deriving it from liveness would mean an unset secret quietly SHORTENED the
    deny list - the wrong direction for the one list whose job is to stay long.
    """
    monkeypatch.delenv("MCP_POST_TOKEN", raising=False)
    _catalog(monkeypatch, DENIER)

    opts = agent._options("alice", None, "", Turn(id="t1", user_email="a@e.com"))

    assert "mcp__post__send" in opts.disallowed_tools
    assert _server_names(opts) == {"ask"}


def test_a_tool_cannot_be_both_pre_approved_and_denied():
    """Not a leak - `disallowed_tools` wins, which test_live_turn.py measures -
    but two fields disagreeing about the author's intent, which no later reader
    can resolve. Cheaper to refuse at import than to debug."""
    with pytest.raises(ValueError, match="both"):
        Server(
            name="post",
            summary="x",
            command="post-mcp",
            auto_approve=("send",),
            deny=("send",),
        )


# --- the real entries, which are claims about two pinned packages ------------


def _entry(name: str) -> Server:
    return next(s for s in mcp_catalog.CATALOG if s.name == name)


SENDING_TOOLS = frozenset(
    {"send_email", "send_draft", "reply_all", "reply_to_email", "forward_email"}
)


def test_every_gmail_tool_that_sends_mail_is_denied():
    """The one test that would catch a version bump re-enabling send.

    The token carries gmail.compose, which grants sending - Google's scope, not
    the package's - so this list is the only thing between the agent and mail
    leaving the house. `draft_email` is deliberately NOT here: a draft sits in
    the mailbox for a person to read, and is the reason compose was worth its
    cost at all.
    """
    gmail = _entry("gmail")

    assert set(gmail.deny) >= SENDING_TOOLS
    assert "draft_email" in gmail.deny or "draft_email" not in gmail.auto_approve, (
        "drafting must either be forbidden or ask a person - never silent"
    )


def test_no_write_shaped_tool_runs_without_asking():
    """Reads are free; anything that changes something asks. Spelled out per
    tool because `auto_approve` is a list of strings and a plausible-looking
    name is easy to add without noticing which side of the line it falls on."""
    writes = {
        "calendar": {
            "create-event",
            "create-events",
            "update-event",
            "delete-event",
            # Visible to the organiser, so it is a write even though it changes
            # nothing on a calendar we own.
            "respond-to-event",
            "manage-accounts",
        },
        "gmail": {
            "draft_email",
            "download_email",
            "download_attachment",
            *SENDING_TOOLS,
        },
    }
    for name, tools in writes.items():
        approved = set(_entry(name).auto_approve)
        assert not (tools & approved), (
            f"{name}: {sorted(tools & approved)} runs silently"
        )


# What each pinned server actually registers, MEASURED by booting it inside the
# real image against fake credentials and calling tools/list - not read off its
# source, which is how `manage-accounts` and `respond-to-event` were missed the
# first time. Update these together with the versions in the Dockerfile.
INVENTORY = {
    "calendar": {  # @cocal/google-calendar-mcp 2.6.2, scope: calendar
        "list-calendars",
        "list-events",
        "search-events",
        "get-event",
        "get-freebusy",
        "list-colors",
        "get-current-time",
        "create-event",
        "create-events",
        "update-event",
        "delete-event",
        "respond-to-event",
        "manage-accounts",
    },
    "gmail": {  # @klodr/gmail-mcp 1.3.3, scopes: gmail.readonly + gmail.compose
        "search_emails",
        "read_email",
        "get_thread",
        "list_inbox_threads",
        "get_inbox_with_threads",
        "list_email_labels",
        "list_drafts",
        "get_draft",
        "draft_email",
        "download_email",
        "download_attachment",
        "pair_recipient",
        "send_email",
        "send_draft",
        "reply_all",
        "reply_to_email",
        "forward_email",
    },
}


def test_no_entry_names_a_tool_its_server_does_not_have():
    """A typo in `deny` denies nothing, and says nothing while doing it.

    `mcp__gmail__send_emails` would sail through every other test in this file
    and leave the real `send_email` reachable - the failure is invisible exactly
    where it is least affordable. Same for `auto_approve`, where a typo fails
    the safe way but still means a tool nobody reviewed is prompting.
    """
    for name, inventory in INVENTORY.items():
        server = _entry(name)
        unknown = (set(server.auto_approve) | set(server.deny)) - inventory
        assert not unknown, f"{name}: {sorted(unknown)} is not a tool this server has"


def test_every_tool_is_deliberately_placed():
    """Nothing arrives by default. A tool absent from both tuples falls through
    to a human, which is the safe landing spot - but it should be a choice, so
    this spells out the ones we mean to leave there."""
    asks = {
        "calendar": {
            "create-event",
            "create-events",
            "update-event",
            "delete-event",
            "respond-to-event",
        },
        "gmail": {
            "draft_email",
            "download_email",
            "download_attachment",
            "pair_recipient",
        },
    }
    for name, inventory in INVENTORY.items():
        server = _entry(name)
        silent, refused, prompts = (
            set(server.auto_approve),
            set(server.deny),
            asks[name],
        )
        placed = silent | refused | prompts
        assert placed == inventory, (
            f"{name}: unclassified {sorted(inventory - placed)}, "
            f"stale {sorted(placed - inventory)}"
        )
        # The three tiers must PARTITION the inventory, not merely cover it.
        # Without this, promoting a tool out of `asks` into `auto_approve`
        # leaves the union unchanged and every assertion above still passes -
        # which is exactly how a write starts running with nobody asked.
        assert not (prompts & silent), (
            f"{name}: {sorted(prompts & silent)} is meant to ask and runs silently"
        )
        assert not (prompts & refused), f"{name}: {sorted(prompts & refused)}"


def test_both_google_entries_share_one_oauth_client():
    """Three secrets, not four. If these diverge, scripts/google-auth.sh is
    printing the wrong `fly secrets set` line."""
    shared = "MCP_GOOGLE_OAUTH_KEYS"

    assert _entry("calendar").files["gcp-oauth.keys.json"] == shared
    assert _entry("gmail").files["gcp-oauth.keys.json"] == shared
    assert {v for s in mcp_catalog.CATALOG for v in s.needs()} == {
        shared,
        "MCP_GCAL_TOKEN",
        "MCP_GMAIL_TOKEN",
    }


def test_the_pinned_versions_match_the_dockerfile():
    """The catalog names a bare binary, so the pin lives in the image. A drift
    here means the server that runs is not the one these tool lists describe."""
    dockerfile = pathlib.Path(mcp_catalog.__file__).parents[1] / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")

    assert "GCAL_MCP_VERSION=2.6.2" in text
    assert "GMAIL_MCP_VERSION=1.3.3" in text
    assert "@cocal/google-calendar-mcp@${GCAL_MCP_VERSION}" in text
    assert "@klodr/gmail-mcp@${GMAIL_MCP_VERSION}" in text
    for server in mcp_catalog.CATALOG:
        assert f"command -v {server.command}" in text, (
            f"the Dockerfile must prove {server.command} is on PATH after install"
        )


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

    Every value in a `secrets` or `files` mapping must be an environment
    VARIABLE NAME, so that a reviewer can tell at a glance that a diff adds no
    credential. `files` matters more than `secrets` here, not less: its values
    are whole JSON documents, so an inlined one would be a long opaque blob in
    a diff - the easiest kind of thing to skim past.

    Read from the real source rather than from the imported object, because the
    point is what a human reviewing this file would see.
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
            if kw.arg not in ("secrets", "files") or not isinstance(kw.value, ast.Dict):
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
