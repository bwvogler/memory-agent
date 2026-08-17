"""The MCP servers this deployment may connect *out* to.

Not to be confused with app/mcp_server.py, which is this app acting as a server
for a machine caller. This module is the other direction: this app as an MCP
*client*, giving the agent tools that reach outside the knowledge base.

--- Where this can live, and where it cannot ---

`.claude/settings.json` and a `.mcp.json` beside the agent's cwd are both dead
ends here: `_options` sets `setting_sources=[]` and `strict_mcp_config=True`, so
neither file is ever read. The only live path is the `mcp_servers=` dict, which
is what this module fills.

The knowledge base is disqualified as a home, and that is forced rather than
chosen. `$KB_MOUNT/memory/` is agent-writable under `acceptEdits`, it is served
to any authenticated browser by `/api/kb/file`, and it is captured in savepoints
and in kb.git. A server list there would be a credential printed on a web page
and a capability the agent could grant itself.

So: **definitions here, in the image; secrets in the environment.** A `Server`
NAMES the variable it needs and never holds the value, which is what keeps this
file reviewable in git and testable in CI while the credential stays in
`fly secrets` and appears in no repository.

--- Credentials that are files, not values ---

`secrets` is the right shape for a bearer token and useless for Google, which is
what both catalog entries turned out to need. Every credible Google MCP server
authenticates from a `gcp-oauth.keys.json` plus a saved token JSON on disk,
produced by a browser consent flow that cannot happen in this container - there
is no browser, and the machine suspends.

`files` closes that gap without weakening anything: it maps a FILENAME to the
environment variable holding that file's contents, and `_materialise` writes
them into a private directory at launch. The invariant is untouched - an entry
still names a variable and never holds a value - and the JSON reaches the disk
of this container only, never the volume, never the KB, never git.

--- Why the agent may read this and never write it ---

Three reasons, in increasing order of force. The weakest is mechanical: `app/`
is in neither `cwd` nor `add_dirs`, the container filesystem is not the volume,
and this module is imported once at startup. That is only "we put it somewhere
awkward".

The second is that a catalog entry is a `command` this container executes with
nothing in front of it. Bash is narrowed to `Bash(bd:*)` and the KB write guard
hangs off `matcher="Bash"` - all of it tool-call machinery. Launching a stdio
server is the SDK spawning a subprocess before the model does anything, so no
PreToolUse hook fires and `can_use_tool` is never consulted. A writable catalog
would be a hole straight through the Bash allowlist, reached by writing a file
rather than by asking.

The third is the sharp one. An agent-authored entry could not invent a secret,
and would not need to: it could point an EXISTING secret at a command of its
choosing, and `resolved()` would faithfully inject the value into that process.
Splitting definitions from secrets protects the value at rest and says nothing
about where it gets aimed. Immutability is what makes the split mean anything.

Underneath all three: savepoints cover `$KB_MOUNT/memory` and do not cover your
calendar. Wiki pages, skill descriptions and the bead graph are content, inside
the blast radius of one Revert click. A capability grant is different in kind -
by the time anyone notices, the tool has already run and reached outside this
system. Content is revertable, so the agent writes it; capability is not, so a
human does. The agent's channel for wanting a server is a bead.

--- Household-shared, and say so ---

One credential per server, shared by everyone. This deployment is one household
rather than many tenants (docs/decisions/0012), and `MCP_IDENTITY_EMAIL` already
makes the same call for the inbound direction. The consequence is not hidden:
**enabling a server enables it for every user**, and behind one token the app
cannot tell household members apart. For a Google Calendar that means pointing
the token at a dedicated household account each person shares their calendar
with - consumer Gmail has no domain-wide delegation, so it is per-calendar
sharing either way.

--- Three tiers, and why the third exists ---

`auto_approve` runs a tool silently. Anything unlisted falls through to
`can_use_tool`, where a person clicks Allow. `deny` is the third: the tool never
runs at all, because its qualified name goes into `disallowed_tools`.

`deny` was added for Gmail, and the reason is worth keeping. Google's
`gmail.compose` scope grants sending as well as drafting - they are one scope,
not two - so "the agent may draft but never send" cannot be expressed at the
token layer. It has to be expressed here. Say the cost out loud: `deny` is OUR
config, enforced by the CLI, while the Google token still permits the send. That
is one layer weaker than not holding the scope at all, and it is the deliberate
price of `draft_email`. The way back is a re-auth with narrower scopes, not a
redeploy.

CATALOG's two entries are Google Calendar and Gmail, both pointing at one
household account. See docs/decisions/0015 for why the credential is shared, and
`Server` below for what each field controls.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .config import config

if TYPE_CHECKING:
    from collections.abc import Mapping

    from claude_agent_sdk.types import McpServerConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Server:
    """One outbound MCP server: a command to run, and the secrets it needs.

    `secrets` maps the variable name the server process expects to the variable
    name we read from our own environment. The two are separate because the
    server's expected names are its business and ours are namespaced `MCP_*`,
    and because a mapping of name-to-name can be read in review without anyone
    wondering whether a value slipped in.

    `files` is the same idea for a credential the server insists on reading from
    disk: it maps a FILENAME to the variable holding that file's contents.
    `_materialise` writes them into a private per-server directory, and `env`
    below is how the server is told where to look.

    `env` carries literal, NON-secret values - paths and flags a reviewer can
    read at a glance. `{dir}` in a value is replaced with the materialised
    directory, which is the only reason this field exists: the path depends on
    the server's own name and would otherwise have to be repeated by hand.

    `auto_approve` lists UNQUALIFIED tool names that may run without a
    permission prompt; `_options` qualifies them to `mcp__{name}__{tool}` before
    they reach `allowed_tools`. Everything the server exposes that is NOT listed
    here still works - it falls through to `can_use_tool`, which is an
    Allow/Deny in the UI on a turn a person is watching, and a silent denial on
    a machine turn. Put read-shaped tools here and leave writes out: that is how
    "read my calendar freely, ask before creating an event" is expressed, and it
    costs no new machinery.

    `deny` is the tier below that: those tools never run at all. Reach for it
    only when a tool should not be reachable even with a human clicking Allow -
    today, the five Gmail tools that put mail in front of another person.
    """

    name: str
    summary: str
    command: str
    args: tuple[str, ...] = ()
    secrets: Mapping[str, str] = field(default_factory=dict)
    files: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    auto_approve: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """A tool cannot be both pre-approved and forbidden.

        Measured, so as not to overstate it: the CLI resolves this SAFELY -
        `disallowed_tools` wins and the tool does not run, even when it also
        appears in `allowed_tools`. So this raise is not preventing a leak.

        It is still an error, because the two fields would then disagree about
        what the author meant, and the reader of a future diff has no way to
        tell which line is the stale one. Fail at import, where a contradiction
        is cheap, rather than leaving it to be discovered by someone wondering
        why a pre-approved tool keeps refusing.
        """
        both = sorted(set(self.auto_approve) & set(self.deny))
        if both:
            msg = (
                f"mcp server {self.name!r}: {', '.join(both)} is in both "
                f"auto_approve and deny"
            )
            raise ValueError(msg)

    def needs(self) -> list[str]:
        """Every environment variable this server reads, secrets and files."""
        return [*self.secrets.values(), *self.files.values()]

    def missing(self) -> list[str]:
        """Names of the environment variables this server needs and lacks."""
        return sorted(
            our_var
            for our_var in self.needs()
            if not os.environ.get(our_var, "").strip()
        )

    def state_dir(self) -> Path:
        return Path(config.mcp_state_dir) / self.name


# Both servers are baked into the image at pinned versions (see the Dockerfile)
# rather than npx-ed at runtime, for the reason bd is pinned: an unpinned tool
# can ship different behaviour on a rebuild nobody reviewed. Here that would
# mean new TOOLS, which neither `auto_approve` nor `deny` would name - so a
# version bump is a security review, not a chore. See kb-b82.
#
# One OAuth client serves both, so MCP_GOOGLE_OAUTH_KEYS is shared and the two
# token files differ. Both point at the same household Google account.
CATALOG: tuple[Server, ...] = (
    Server(
        name="calendar",
        summary=(
            "Google Calendar for the household account: every calendar shared "
            "with it, at whatever level each person granted."
        ),
        command="google-calendar-mcp",
        args=("start",),
        files={
            "gcp-oauth.keys.json": "MCP_GOOGLE_OAUTH_KEYS",
            "tokens.json": "MCP_GCAL_TOKEN",
        },
        env={
            "GOOGLE_OAUTH_CREDENTIALS": "{dir}/gcp-oauth.keys.json",
            "GOOGLE_CALENDAR_MCP_TOKEN_PATH": "{dir}/tokens.json",
        },
        # Reading a calendar is what this is for; every write asks. `list-colors`
        # and `get-current-time` touch no data at all. `respond-to-event` is
        # deliberately left to fall through rather than denied: an RSVP is a
        # reasonable thing to want, and it is visible to the organiser, so it
        # wants a person - not a refusal.
        auto_approve=(
            "list-calendars",
            "list-events",
            "search-events",
            "get-event",
            "get-freebusy",
            "list-colors",
            "get-current-time",
        ),
        # Adding, removing or re-authorising a Google account is capability
        # management, which docs/decisions/0015 reserves for a human by way of a
        # deploy. It is also the one tool here that could break the integration
        # in a way no Revert reaches.
        deny=("manage-accounts",),
    ),
    Server(
        name="gmail",
        summary=(
            "Gmail for the household account's OWN inbox - mail addressed to "
            "the household, not anyone's personal mail."
        ),
        command="gmail-mcp",
        files={
            "gcp-oauth.keys.json": "MCP_GOOGLE_OAUTH_KEYS",
            "credentials.json": "MCP_GMAIL_TOKEN",
        },
        env={
            "GMAIL_OAUTH_PATH": "{dir}/gcp-oauth.keys.json",
            "GMAIL_CREDENTIALS_PATH": "{dir}/credentials.json",
        },
        # The token carries gmail.readonly + gmail.compose, and this server
        # registers tools by granted scope - so the 18 label, filter, delete and
        # spam tools are not merely unlisted here, they do not exist. What is
        # left is 17: these 8, the 4 that fall through to a person, and `deny`.
        auto_approve=(
            "search_emails",
            "read_email",
            "get_thread",
            "list_inbox_threads",
            "get_inbox_with_threads",
            "list_email_labels",
            "list_drafts",
            "get_draft",
        ),
        # Everything that puts mail in front of another human. `draft_email` is
        # deliberately NOT here - a draft sits in the mailbox for a person to
        # read, which is the whole reason gmail.compose was worth its cost.
        deny=(
            "send_email",
            "send_draft",
            "reply_all",
            "reply_to_email",
            "forward_email",
        ),
    ),
)


# Which (server, missing-variables) pairs have already been logged. _live() runs
# three times per turn - once each for the servers, the allowlist and the prompt
# - so warning every call would put three identical lines in the log per turn
# and teach whoever reads them to skip the line. Keyed on the missing set rather
# than the name alone, so a server that breaks in a NEW way says so again.
_warned: set[tuple[str, tuple[str, ...]]] = set()


def _live() -> list[Server]:
    """Catalog entries whose every secret is actually present.

    A server with a missing secret is DROPPED with a logged reason rather than
    launched to fail on first use. Same call app/kb.py makes about beads, and
    the same reason /healthz reports `transcripts` separately from `ok`: the
    failure mode of this system is silence, so an absent capability has to say
    why it is absent somewhere a person can find it. The log is the transient
    half of that; `status()` is the half a person can go and look at.
    """
    live = []
    for server in CATALOG:
        missing = server.missing()
        if missing:
            key = (server.name, tuple(missing))
            if key not in _warned:
                _warned.add(key)
                log.warning(
                    "mcp server %r is configured but disabled: %s not set. "
                    "Its tools are absent from every turn until it is set; "
                    "/healthz reports this under mcp_catalog.",
                    server.name,
                    ", ".join(missing),
                )
            continue
        live.append(server)
    return live


# Servers whose credential files this process has already written. Once per
# process rather than once per turn, and that is not just an optimisation: these
# servers write REFRESHED tokens back to the file, so rewriting every turn would
# discard the refresh and force another one immediately. A rotated secret takes
# effect on restart, which `fly secrets set` performs anyway.
_materialised: set[str] = set()


def _materialise(server: Server) -> Path:
    """Write this server's credential files to a private directory, once.

    The contents come from the environment and land on this container's own
    filesystem: not the volume, not the KB, and outside `add_dirs`, so no tool
    the agent holds can read them back. Modes are tight for the ordinary reason,
    but the containment that matters is the location, not the bits.
    """
    directory = server.state_dir()
    if server.name in _materialised:
        return directory

    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    for filename, our_var in server.files.items():
        path = directory / filename
        path.write_text(os.environ[our_var], encoding="utf-8")
        path.chmod(0o600)
    _materialised.add(server.name)
    return directory


def resolved() -> dict[str, McpServerConfig]:
    """The `mcp_servers=` entries for every server that can actually start."""
    servers: dict[str, McpServerConfig] = {}
    for server in _live():
        directory = _materialise(server)
        servers[server.name] = {
            "type": "stdio",
            "command": server.command,
            "args": list(server.args),
            "env": {
                **{
                    theirs: value.replace("{dir}", str(directory))
                    for theirs, value in server.env.items()
                },
                **{theirs: os.environ[ours] for theirs, ours in server.secrets.items()},
            },
        }
    return servers


def auto_approved_tools() -> list[str]:
    """Fully qualified tool names that may run without a permission prompt.

    Explicit names rather than an `mcp__x__*` wildcard: whether the CLI honours
    a wildcard there is its business, and a wildcard would silently pre-approve
    every tool a server gains in a version bump - including whatever it learns
    to delete.
    """
    return [
        f"mcp__{server.name}__{tool}"
        for server in _live()
        for tool in server.auto_approve
    ]


def denied_tools() -> list[str]:
    """Fully qualified tool names that must never run, for `disallowed_tools`.

    Built from CATALOG rather than `_live()`, unlike the two functions around
    it. A denial costs nothing when its server is absent, and deriving it from
    liveness would mean a missing secret quietly shortened the deny list - the
    wrong way round for the one list whose entire job is to stay long.
    """
    return [f"mcp__{server.name}__{tool}" for server in CATALOG for tool in server.deny]


def status() -> dict[str, str]:
    """Per-server readiness, for /healthz. `ready`, or which variable is unset.

    Deliberately never includes a secret's value, only its variable name.
    """
    return {
        server.name: "ready"
        if not server.missing()
        else "missing " + ", ".join(server.missing())
        for server in CATALOG
    }


def summaries() -> str:
    """The system-prompt fragment, or "" when no server is live.

    Empty rather than "you have no servers": a paragraph explaining an absence
    is prompt weight spent on nothing, and this file already lost that argument
    once - see the note beside the Write-verification text in agent.py.
    """
    live = _live()
    if not live:
        return ""
    lines = "\n".join(f"* `{s.name}` - {s.summary}" for s in live)
    return (
        "--- Connected services ---\n\n"
        "You have tools from these MCP servers:\n\n"
        f"{lines}\n\n"
        "They belong to the household, not to the person you are talking to: "
        "one shared account per service, so treat what you read through them as "
        "shared and say whose data you are looking at when it matters. Some of "
        "their tools will ask the human for permission before running, which is "
        "deliberate - call the tool and let them decide. A few are refused "
        "outright; do not look for another route to the same effect.\n\n"
        "What you read through a service is NOT wiki material by default. Use "
        "it to answer the question in front of you, and write it into the "
        "knowledge base only when asked for that specific thing. A mail or an "
        "invitation is someone's correspondence, and the wiki is a shared page "
        "everyone in the household can read.\n\n"
        "Their configuration and credentials are not yours to change and are "
        "not in the knowledge base. If a service is missing, broken, or you want "
        "another one, file a bead saying so."
    )
