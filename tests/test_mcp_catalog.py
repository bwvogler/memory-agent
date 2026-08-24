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
import asyncio
import dataclasses
import json
import pathlib
import re
import types
from datetime import UTC, date, datetime, timedelta

import httpx
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
    # The probe cache is the same kind of state, and leaking it across tests
    # would make a verdict from one test the starting point of the next.
    mcp_catalog._refresh_state.clear()
    mcp_catalog._refresh_checked_at = 0.0
    mcp_catalog._refresh_task = None


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

    assert mcp_catalog.status() == {
        "calendar": {"state": "missing", "missing": ["MCP_GOOGLE_REFRESH_TOKEN"]}
    }

    monkeypatch.setenv("MCP_GOOGLE_REFRESH_TOKEN", "s3cret-value")
    assert mcp_catalog.status() == {"calendar": {"state": "ready"}}
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
    assert mcp_catalog.status() == {
        "filey": {"state": "missing", "missing": ["MCP_FILEY_TOKEN"]}
    }


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


# --- present is not alive -----------------------------------------------------
#
# `missing()` answers a question about our own config. These cover the two
# signals that answer the other one - whether Google still honours the token -
# and the two rules that keep the answer from doing damage: a dead credential
# must not remove the server, and /healthz must not wait on Google.


# Bound separately from the Server rather than reached for as `OAUTHY.oauth`,
# which is `OAuthCheck | None` and so cannot be passed to anything that wants a
# check. Same reason `_stdio_env` exists: assert the shape once, here.
OAUTHY_CHECK = mcp_catalog.OAuthCheck(
    keys_var="MCP_OAUTHY_KEYS",
    token_var="MCP_OAUTHY_TOKEN",  # noqa: S106 - a variable name, not a value
    token_path=("tokens",),
)

OAUTHY = Server(
    name="oauthy",
    summary="A server whose credential can go stale while remaining present.",
    command="oauthy-mcp",
    files={"keys.json": "MCP_OAUTHY_KEYS", "token.json": "MCP_OAUTHY_TOKEN"},
    env={"OAUTHY_TOKEN_PATH": "{dir}/token.json"},
    auto_approve=("look",),
    oauth=OAUTHY_CHECK,
)

CLIENT_SECRET = "cs-do-not-log-me"  # noqa: S105 - a fixture, not a credential
REFRESH_TOKEN = "rt-do-not-log-me"  # noqa: S105 - a fixture, not a credential

KEYS_JSON = json.dumps(
    {
        "installed": {
            "client_id": "cid.apps.googleusercontent.com",
            "client_secret": CLIENT_SECRET,
        }
    }
)


def _token_json(granted_days_ago: int, *, nest: str | None = "tokens") -> str:
    """A stored Google token whose grant is `granted_days_ago` days old.

    The file records the ACCESS token's expiry, an hour after it was issued, so
    that is what gets written here - the code under test is what has to work back
    from it to the grant.

    Whole days only, on purpose: the predicted expiry is a DATE, so a half-day
    offset lands on either side of midnight depending on the hour the suite runs
    and `days_left` comes out one higher or lower. That is a flaky test rather
    than a finding.
    """
    granted = datetime.now(tz=UTC) - timedelta(days=granted_days_ago)
    inner = {
        "refresh_token": REFRESH_TOKEN,
        "access_token": "at-whatever",
        "expiry_date": int((granted + timedelta(hours=1)).timestamp() * 1000),
    }
    return json.dumps({nest: inner} if nest else inner)


@pytest.fixture
def oauthy(monkeypatch):
    """A catalog of one server whose grant was issued today."""
    monkeypatch.setenv("MCP_OAUTHY_KEYS", KEYS_JSON)
    monkeypatch.setenv("MCP_OAUTHY_TOKEN", _token_json(0))
    _catalog(monkeypatch, OAUTHY)


def _google(monkeypatch, handler):
    """Answer the token endpoint with `handler`, via httpx's own MockTransport.

    No new test dependency: httpx ships this, and `requirements-dev.txt` has
    neither respx nor an async plugin - hence `asyncio.run` below, as in
    tests/test_concurrency.py.
    """
    real = httpx.AsyncClient
    monkeypatch.setattr(
        mcp_catalog.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )


def _answer(status, body=None):
    return lambda _request: httpx.Response(status, json=body or {})


def _probe_all():
    asyncio.run(mcp_catalog.refresh_health())
    return mcp_catalog.status()


def test_the_countdown_is_read_from_the_environment_not_from_the_file(
    monkeypatch, tmp_path
):
    """The central decision, and the one that fails silently if it is undone.

    The server owns its credential file once it is written and puts REFRESHED
    tokens back into it, so that file's `expiry_date` moves forward every hour.
    Deriving the grant date from it would report "6 days, 23 hours left" forever
    - a countdown that never counts, which is worse than none because it looks
    like it is working.
    """
    monkeypatch.setenv("MCP_OAUTHY_KEYS", KEYS_JSON)
    monkeypatch.setenv("MCP_OAUTHY_TOKEN", _token_json(6))
    _catalog(monkeypatch, OAUTHY)

    mcp_catalog.resolved()  # materialises the file from the variable
    written = tmp_path / "state" / "oauthy" / "token.json"
    written.write_text(_token_json(0), encoding="utf-8")  # the server refreshes

    entry = mcp_catalog.status()["oauthy"]

    assert entry["days_left"] == 1, "the countdown followed the file, not the secret"
    assert entry["state"] == "expiring"


@pytest.mark.parametrize(
    ("path", "nest"),
    [(("normal",), "normal"), (("tokens",), "tokens")],
)
def test_both_real_token_nestings_resolve(monkeypatch, path, nest):
    """The two packages disagree about where the token sits, and both are shipped:
    the calendar server keys by account name, the gmail server nests under
    `tokens`. Read out of their sources rather than guessed."""
    check = dataclasses.replace(OAUTHY_CHECK, token_path=path)
    monkeypatch.setenv("MCP_OAUTHY_TOKEN", _token_json(1, nest=nest))

    token = mcp_catalog._token_object(check)

    assert token is not None
    assert token["refresh_token"] == REFRESH_TOKEN


def test_a_flat_token_file_still_resolves(monkeypatch):
    """The calendar server migrates an older flat file by wrapping it in an
    account key, so both shapes are in the wild. A credential that works must not
    be reported as unreadable."""
    monkeypatch.setenv("MCP_OAUTHY_TOKEN", _token_json(1, nest=None))

    token = mcp_catalog._token_object(OAUTHY_CHECK)

    assert token is not None
    assert token["refresh_token"] == REFRESH_TOKEN


@pytest.mark.parametrize(
    "value",
    ["not json at all", "[]", '{"tokens": {}}', '{"tokens": {"refresh_token": 7}}'],
)
def test_an_unreadable_token_is_ready_without_a_countdown_and_never_raises(
    monkeypatch, value
):
    """A prediction that cannot be made must not be reported as a prediction of
    zero, and must not take /healthz down with it."""
    monkeypatch.setenv("MCP_OAUTHY_KEYS", KEYS_JSON)
    monkeypatch.setenv("MCP_OAUTHY_TOKEN", value)
    _catalog(monkeypatch, OAUTHY)

    entry = mcp_catalog.status()["oauthy"]

    assert entry["state"] == "ready"
    assert "days_left" not in entry


def test_a_token_with_no_expiry_date_is_ready_without_a_countdown(monkeypatch):
    monkeypatch.setenv("MCP_OAUTHY_KEYS", KEYS_JSON)
    monkeypatch.setenv(
        "MCP_OAUTHY_TOKEN", json.dumps({"tokens": {"refresh_token": REFRESH_TOKEN}})
    )
    _catalog(monkeypatch, OAUTHY)

    entry = mcp_catalog.status()["oauthy"]

    assert entry["state"] == "ready"
    assert "days_left" not in entry


def test_grant_ttl_days_of_none_switches_the_countdown_off(monkeypatch):
    """What moving to a Workspace domain with user type Internal looks like: no
    clock, so no prediction - while the probe carries on unchanged."""
    server = dataclasses.replace(
        OAUTHY, oauth=dataclasses.replace(OAUTHY_CHECK, grant_ttl_days=None)
    )
    monkeypatch.setenv("MCP_OAUTHY_KEYS", KEYS_JSON)
    monkeypatch.setenv("MCP_OAUTHY_TOKEN", _token_json(90))
    _catalog(monkeypatch, server)
    _google(monkeypatch, _answer(200, {"access_token": "fresh"}))

    entry = _probe_all()["oauthy"]

    assert "days_left" not in entry
    assert entry["state"] == "ready"
    assert entry["refresh"] == "valid"


def test_the_grant_date_backs_the_access_token_hour_off_the_stored_expiry(monkeypatch):
    """The stored `expiry_date` is when the ACCESS token dies, an hour after the
    grant was issued, and the seven-day clock runs from the grant.

    Asserted as an absolute date rather than as `days_left`, and chosen to sit
    just after midnight, because that is the only way the hour is observable at
    all: the prediction is date-granular, so shifting it by an hour changes the
    answer only across a midnight. A mutation dropping the correction survived
    every other test here, which is what this one is for.
    """
    just_after_midnight = datetime(2026, 8, 20, 0, 30, tzinfo=UTC)
    monkeypatch.setenv(
        "MCP_OAUTHY_TOKEN",
        json.dumps(
            {
                "tokens": {
                    "refresh_token": REFRESH_TOKEN,
                    "expiry_date": int(just_after_midnight.timestamp() * 1000),
                }
            }
        ),
    )

    # Granted 23:30 on the 19th, so seven days later is still the 26th - not the
    # 27th, which is where the un-backed-off access expiry would land it.
    assert mcp_catalog._grant_expiry(OAUTHY_CHECK) == date(2026, 8, 26)


def test_a_grant_inside_two_days_reports_expiring(monkeypatch):
    monkeypatch.setenv("MCP_OAUTHY_KEYS", KEYS_JSON)
    monkeypatch.setenv("MCP_OAUTHY_TOKEN", _token_json(6))
    _catalog(monkeypatch, OAUTHY)

    entry = mcp_catalog.status()["oauthy"]

    assert entry["state"] == "expiring"
    assert entry["days_left"] == 1


def test_google_confirming_the_grant_reports_valid(oauthy, monkeypatch):
    _google(monkeypatch, _answer(200, {"access_token": "fresh"}))

    entry = _probe_all()["oauthy"]

    assert entry["state"] == "ready"
    assert entry["refresh"] == "valid"


def test_invalid_grant_reports_expired(oauthy, monkeypatch):
    """The seven-day clock running out, as Google reports it."""
    _google(monkeypatch, _answer(400, {"error": "invalid_grant"}))

    entry = _probe_all()["oauthy"]

    assert entry["state"] == "expired"
    assert entry["refresh"] == "invalid"


@pytest.mark.parametrize("error", ["invalid_client", "unauthorized_client"])
def test_a_disabled_oauth_client_reports_expired(oauthy, monkeypatch, error):
    """This is not the seven-day clock and no countdown can see it coming.
    Publishing a consent screen with restricted scopes disables the client, and
    every tool then fails while the variables stay exactly as set."""
    _google(monkeypatch, _answer(401, {"error": error}))

    assert _probe_all()["oauthy"]["state"] == "expired"


@pytest.mark.parametrize(
    "handler",
    [
        _answer(500, {"error": "backendError"}),
        lambda _r: httpx.Response(503, text="<html>not json</html>"),
        lambda _r: (_ for _ in ()).throw(httpx.ConnectTimeout("no route")),
    ],
    ids=["a 500", "an unparseable body", "a timeout"],
)
def test_a_fault_at_google_is_unknown_and_never_expired(oauthy, monkeypatch, handler):
    """A timeout is not an expiry. Reporting one as the other sends someone
    through a browser consent flow because a network blipped."""
    _google(monkeypatch, handler)

    entry = _probe_all()["oauthy"]

    assert entry["refresh"] == "unknown"
    assert entry["state"] == "ready"


def test_a_probe_saying_valid_still_reports_expiring_once_the_clock_is_past(
    oauthy, monkeypatch
):
    """Where the two signals disagree, each is trusted about what it can see. The
    probe is authoritative about death, so `valid` prevents `expired` - but a
    grant we believe should already be gone is still worth acting on before it
    goes."""
    monkeypatch.setenv("MCP_OAUTHY_TOKEN", _token_json(30))
    _google(monkeypatch, _answer(200, {"access_token": "fresh"}))

    entry = _probe_all()["oauthy"]

    assert entry["refresh"] == "valid"
    assert entry["state"] == "expiring"
    assert entry["days_left"] < 0


def test_an_unprobed_server_says_so_rather_than_claiming_confirmation(oauthy):
    """The whole point of the bead: "ready, and Google confirmed it" must be
    distinguishable from "ready, and nobody has asked yet"."""
    assert mcp_catalog.status()["oauthy"]["refresh"] == "unchecked"


def test_a_dead_credential_never_drops_the_server(oauthy, monkeypatch):
    """`_live()` stays presence-only and network-free. If an outage at Google
    could strip the agent's tools we would have rebuilt the "the tools are gone
    versus the tools never existed" confusion this module exists to prevent - and
    a tool that errors loudly beats a tool that silently is not there."""
    _google(monkeypatch, _answer(400, {"error": "invalid_grant"}))

    assert _probe_all()["oauthy"]["state"] == "expired"
    assert "oauthy" in mcp_catalog.resolved()
    assert "mcp__oauthy__look" in mcp_catalog.auto_approved_tools()


def test_a_missing_variable_is_never_probed(monkeypatch):
    """No point asking Google about a credential we do not have.

    Asserted on the absence of a recorded VERDICT rather than on the absence of a
    request: `_probe` gives up before reaching the network when there is no token
    to send, so a handler that fails on contact proves nothing here. It is kept
    below anyway, for the day that early return moves.
    """
    monkeypatch.setenv("MCP_OAUTHY_KEYS", KEYS_JSON)
    monkeypatch.delenv("MCP_OAUTHY_TOKEN", raising=False)
    _catalog(monkeypatch, OAUTHY)
    _google(monkeypatch, lambda _r: pytest.fail("probed a missing credential"))

    entry = _probe_all()["oauthy"]

    assert "oauthy" not in mcp_catalog._refresh_state, "recorded a verdict anyway"
    assert entry == {"state": "missing", "missing": ["MCP_OAUTHY_TOKEN"]}


def test_a_credential_going_missing_clears_the_verdict_it_used_to_have(
    oauthy, monkeypatch
):
    """Otherwise `refresh` would go on reporting what Google said about a token
    that is no longer there - a stale claim, which is this bead's whole subject."""
    _google(monkeypatch, _answer(200, {"access_token": "fresh"}))
    assert _probe_all()["oauthy"]["refresh"] == "valid"

    monkeypatch.delenv("MCP_OAUTHY_TOKEN", raising=False)

    assert _probe_all()["oauthy"] == {
        "state": "missing",
        "missing": ["MCP_OAUTHY_TOKEN"],
    }
    assert "oauthy" not in mcp_catalog._refresh_state


def test_status_never_leaks_a_token_or_a_client_secret(oauthy, monkeypatch):
    """The invariant the whole module rests on, now that this code handles the
    values themselves rather than only their names."""
    _google(monkeypatch, _answer(400, {"error": "invalid_grant"}))

    rendered = json.dumps(_probe_all())

    assert REFRESH_TOKEN not in rendered
    assert CLIENT_SECRET not in rendered
    assert "at-whatever" not in rendered


def test_healthz_schedules_a_probe_rather_than_awaiting_google(oauthy, monkeypatch):
    """Stale-while-revalidate. /healthz is unauthenticated and is what decides
    whether the host may suspend, so its latency is not Google's to set - and the
    TTL is what stops a fast pinger becoming load on Google."""
    calls = []
    _google(monkeypatch, lambda r: calls.append(r) or httpx.Response(200, json={}))

    async def go():
        mcp_catalog.schedule_health_refresh()
        assert calls == [], "schedule_health_refresh awaited the network"
        assert mcp_catalog.status()["oauthy"]["refresh"] == "unchecked"
        scheduled = mcp_catalog._refresh_task
        assert scheduled is not None, "no probe was scheduled at all"
        await scheduled
        assert len(calls) == 1
        # Still fresh, so a second call must not schedule another round. Asserted
        # on the task IDENTITY rather than on the call count, which would also be
        # 1 if a new task had been created and simply not run yet.
        mcp_catalog.schedule_health_refresh()
        assert mcp_catalog._refresh_task is scheduled, "re-probed inside the TTL"
        assert len(calls) == 1

    asyncio.run(go())


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
