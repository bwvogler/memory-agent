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

--- A worked example, for whoever adds the first one (kb-b82) ---

    Server(
        name="calendar",
        summary="Google Calendar for the household account.",
        command="npx",
        args=("-y", "@some/google-calendar-mcp@1.2.3"),
        secrets={
            # what the server expects  : what we read from the environment
            "GOOGLE_OAUTH_CLIENT_ID": "MCP_GOOGLE_CLIENT_ID",
            "GOOGLE_OAUTH_CLIENT_SECRET": "MCP_GOOGLE_CLIENT_SECRET",
            "GOOGLE_OAUTH_REFRESH_TOKEN": "MCP_GOOGLE_REFRESH_TOKEN",
        },
        auto_approve=("list_events", "search_events"),
    )

Pin the package version in `args` rather than tracking a tag, for the reason the
Dockerfile pins bd and no longer ships cloudflared. Note what is NOT in
`auto_approve`: creating and deleting events are left out on purpose, so they
fall through to `can_use_tool` and a person clicks Allow. See `auto_approve`
below - that split is the whole safety story for a write-capable server.

CATALOG ships empty, so none of this changes anything until someone adds an
entry AND sets its secrets.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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

    `auto_approve` lists UNQUALIFIED tool names that may run without a
    permission prompt; `_options` qualifies them to `mcp__{name}__{tool}` before
    they reach `allowed_tools`. Everything the server exposes that is NOT listed
    here still works - it falls through to `can_use_tool`, which is an
    Allow/Deny in the UI on a turn a person is watching, and a silent denial on
    a machine turn. Put read-shaped tools here and leave writes out: that is how
    "read my calendar freely, ask before creating an event" is expressed, and it
    costs no new machinery.
    """

    name: str
    summary: str
    command: str
    args: tuple[str, ...] = ()
    secrets: Mapping[str, str] = field(default_factory=dict)
    auto_approve: tuple[str, ...] = ()

    def missing(self) -> list[str]:
        """Names of the environment variables this server needs and lacks."""
        return sorted(
            our_var
            for our_var in self.secrets.values()
            if not os.environ.get(our_var, "").strip()
        )


# Empty on purpose. Adding an entry here is a reviewed, deployed change, which
# is the entire point of the module docstring above.
CATALOG: tuple[Server, ...] = ()


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


def resolved() -> dict[str, McpServerConfig]:
    """The `mcp_servers=` entries for every server that can actually start."""
    return {
        server.name: {
            "type": "stdio",
            "command": server.command,
            "args": list(server.args),
            "env": {
                theirs: os.environ[ours] for theirs, ours in server.secrets.items()
            },
        }
        for server in _live()
    }


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
        "deliberate - call the tool and let them decide.\n\n"
        "Their configuration and credentials are not yours to change and are "
        "not in the knowledge base. If a service is missing, broken, or you want "
        "another one, file a bead saying so."
    )
