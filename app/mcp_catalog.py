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

--- Present is not the same as alive ---

`missing()` answers a question about OUR config: is the variable set. For a long
time `status()` reported `ready` on the strength of that alone, which quietly
claimed something it had never checked - that Google still honours the token
inside. It does not always. A refresh token granted by a consent screen in
Testing dies after seven days; a client can be disabled; a grant can be revoked.
Every one of those leaves the variable exactly as set as before.

So `OAuthCheck` adds two signals, and they are separate because neither one
covers the other. The *predicted* expiry needs no network: an access token lives
an hour, so `expiry_date - 1h` is when the grant was issued and the seven-day
clock started. The *probe* is one HTTPS call that asks Google to refresh, and it
is the only thing that catches revocation or a disabled client - failures with no
date attached.

Two rules hold this in place. A dead credential does NOT drop the server:
`_live()` stays presence-only and network-free, because if an outage at Google
could strip the agent's tools we would have rebuilt the "the tools are gone
versus the tools never existed" confusion this module exists to prevent, arriving
through a new door. And `/healthz` never awaits Google - it reads a cache and
schedules a refresh, because it is unauthenticated, it drives the host's suspend
decision, and its latency is not Google's to set.

CATALOG's two entries are Google Calendar and Gmail, both pointing at one
household account. See docs/decisions/0015 for why the credential is shared, and
`Server` below for what each field controls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .config import config

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date

    from claude_agent_sdk.types import McpServerConfig

log = logging.getLogger(__name__)

# Where a refresh is attempted, and how long an access token lives. The second
# is a constant of Google's rather than ours, and it is load-bearing: it is what
# turns the access token's `expiry_date` back into the moment the GRANT was
# issued, which is the instant the seven-day Testing clock starts from.
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL
_ACCESS_TOKEN_LIFETIME = timedelta(hours=1)

# How stale a probe result may be before /healthz schedules another, and how
# close to the predicted expiry counts as worth shouting about. The TTL is what
# keeps an unauthenticated endpoint from turning a pinger into load on Google:
# one call per interval, whatever the request rate.
_HEALTH_TTL_SECONDS = 900
_EXPIRING_WITHIN_DAYS = 2


@dataclass(frozen=True)
class OAuthCheck:
    """How to ask Google whether a server's stored grant is still good.

    Declarative and per-entry, so the OAuth knowledge stays beside the server it
    describes instead of spreading into main.py, and so a third Google entry
    costs one more literal rather than another branch.

    `token_path` is where the token object sits inside the token JSON, which the
    two packages disagree about: the calendar server keys by account name
    (`{"normal": {...}}`) and the gmail server nests under `{"tokens": {...}}`.
    Read out of their sources rather than guessed. The calendar server also
    migrates an older flat file by wrapping it, so a lookup that misses falls
    back to the root - see `_token_object`.

    `grant_ttl_days` is the seven-day clock a consent screen in Testing puts on
    its refresh tokens. It is nullable because it is a property of the ACCOUNT
    rather than of the API: on a Workspace domain with the consent screen set to
    user type Internal there is no clock, and switching this to None is then the
    whole change. It never affects the probe, which is about a different failure.
    """

    keys_var: str
    token_var: str
    token_path: tuple[str, ...]
    grant_ttl_days: int | None = 7


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

    `oauth`, when set, is how `status()` finds out whether the credential is
    still good rather than merely present. It is optional because a server
    holding a plain bearer token has nothing to check.
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
    oauth: OAuthCheck | None = None

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
        # `manage-accounts add` returns an OAuth consent URL as TEXT, which the
        # agent would put in the chat for a person to click. That is a capability
        # grant solicited through conversation - a whole additional Google
        # account, at full calendar scope - and docs/decisions/0015 reserves
        # capability grants for a reviewed deploy precisely because chat leaves
        # no diff. `remove` is the same argument pointed the other way.
        #
        # Two things already stop it, and neither is a reason to allow it. The
        # callback is localhost:3000 on THIS container, so a laptop browser
        # would redirect to its own machine and the token would never arrive;
        # and if it did arrive it would land in the file `_materialise` rewrites
        # from the secret at the next boot. A grant that works until the next
        # deploy and appears in no `fly secrets` is worse than one that fails.
        deny=("manage-accounts",),
        # This server keys its token file by account mode; "normal" is the
        # default and the only one this deployment uses, since `manage-accounts`
        # is denied and no second account can be added.
        oauth=OAuthCheck(
            keys_var="MCP_GOOGLE_OAUTH_KEYS",
            # The suppression below is for S106, which reads this as a
            # hard-coded credential - the opposite of what it is. The value is a
            # variable NAME, which is the invariant this whole module is built to
            # keep, and the structural test in tests/test_mcp_catalog.py is what
            # actually enforces it.
            token_var="MCP_GCAL_TOKEN",  # noqa: S106
            token_path=("normal",),
        ),
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
        # Same OAuth client as calendar, a different token file, and a different
        # shape inside it: this server writes `{"tokens": {...}, "scopes": [...]}`.
        oauth=OAuthCheck(
            keys_var="MCP_GOOGLE_OAUTH_KEYS",
            token_var="MCP_GMAIL_TOKEN",  # noqa: S106 - a variable name; see calendar
            token_path=("tokens",),
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


def _json_var(our_var: str) -> dict[str, Any] | None:
    """The JSON object in an environment variable, or None if it is unusable.

    Never raises and never logs a value. A credential we cannot parse is worth
    one line naming the variable, because the alternative is a server reported as
    healthy on the strength of a string nobody could read.
    """
    raw = os.environ.get(our_var, "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        log.warning("mcp catalog: %s is set but does not contain valid JSON", our_var)
        return None
    return parsed if isinstance(parsed, dict) else None


def _token_object(check: OAuthCheck) -> dict[str, Any] | None:
    """The stored token, read from the ENVIRONMENT and never from the file.

    This is the load-bearing decision in the whole health check. `_materialise`
    writes the token to disk once per process, and then the server owns that file
    and writes REFRESHED tokens back to it - so its `expiry_date` marches forward
    every hour, and a grant date derived from it would read "six days and
    twenty-three hours left" forever. The environment variable is fixed for the
    life of the process and still holds the value as of the last
    `fly secrets set`, which is the moment the grant was actually issued.

    It also sidesteps reading a file another process is in the middle of
    replacing - the gmail server writes to a temporary name and renames over it.
    """
    parsed = _json_var(check.token_var)
    if parsed is None:
        return None

    node: Any = parsed
    for key in check.token_path:
        if not isinstance(node, dict):
            break
        node = node.get(key)
    if not (isinstance(node, dict) and isinstance(node.get("refresh_token"), str)):
        # The calendar server migrates an older flat token file by wrapping it in
        # an account key. Accept the pre-migration shape too: a credential that
        # works is not one to report as unreadable.
        node = parsed
    if isinstance(node, dict) and isinstance(node.get("refresh_token"), str):
        return node
    log.warning(
        "mcp catalog: %s holds JSON with no refresh_token at %s or at its root",
        check.token_var,
        "/".join(check.token_path),
    )
    return None


def _grant_expiry(check: OAuthCheck) -> date | None:
    """The day this grant is predicted to die, or None if that cannot be said.

    Needs no network. An access token lives an hour, so backing that off the
    stored `expiry_date` gives the moment the grant was issued, and a consent
    screen in Testing kills the refresh token `grant_ttl_days` after that.

    None covers three different cases on purpose - no clock on this account, no
    parseable token, no usable `expiry_date` - because the caller does the same
    thing with all three: report readiness without a countdown. A prediction that
    cannot be made must not be reported as a prediction of zero.
    """
    if check.grant_ttl_days is None:
        return None
    token = _token_object(check)
    if token is None:
        return None
    expiry_ms = token.get("expiry_date")
    if not isinstance(expiry_ms, int | float) or expiry_ms <= 0:
        return None
    issued = datetime.fromtimestamp(expiry_ms / 1000, tz=UTC) - _ACCESS_TOKEN_LIFETIME
    return (issued + timedelta(days=check.grant_ttl_days)).date()


# The last probe verdict per server, and when the set was refreshed. `0.0` means
# never, which is what makes the first /healthz call schedule a probe.
_refresh_state: dict[str, str] = {}
_refresh_checked_at: float = 0.0
_refresh_task: asyncio.Task[None] | None = None

# Google's answers that mean a human has to go and re-authorise, as opposed to
# "ask again later". `invalid_client` and `unauthorized_client` are in here
# because that is what a DISABLED OAuth client returns - the exact failure that
# publishing a consent screen with restricted scopes produced, and the one the
# seven-day countdown cannot see coming.
_DEAD_GRANT_ERRORS = frozenset(
    {"invalid_grant", "invalid_client", "unauthorized_client"}
)


async def _probe(check: OAuthCheck) -> str:
    """Ask Google to refresh this grant: `valid`, `invalid`, or `unknown`.

    Idempotent and cheap. A refresh does not rotate the refresh token and does
    not disturb the access token the running server holds, so this can be
    repeated without touching anything the servers depend on.

    `unknown` rather than `invalid` for every fault that is not Google saying the
    grant is dead. A timeout, a 500 or an unparseable body means we do not know,
    and reporting that as expiry would send someone to re-run a consent flow
    because a network blipped.
    """
    token = _token_object(check)
    keys = _json_var(check.keys_var)
    if token is None or keys is None:
        return "unknown"
    # A downloaded Desktop-app client nests under `installed`; the calendar
    # server also accepts client_id/client_secret at the root, so we do too.
    client = keys.get("installed") if isinstance(keys.get("installed"), dict) else keys
    if not isinstance(client, dict) or "client_id" not in client:
        log.warning("mcp catalog: %s has no OAuth client_id", check.keys_var)
        return "unknown"

    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": token["refresh_token"],
                    "client_id": client["client_id"],
                    "client_secret": client.get("client_secret", ""),
                },
            )
    except httpx.HTTPError as exc:
        # The exception type, not its message: a client secret can appear in a
        # request-echoing error and this line goes to a log we do not control.
        log.warning(
            "mcp catalog: could not reach Google to check %s (%s)",
            check.token_var,
            type(exc).__name__,
        )
        return "unknown"

    if resp.status_code == httpx.codes.OK:
        return "valid"

    error = ""
    with contextlib.suppress(ValueError):
        body = resp.json()
        if isinstance(body, dict):
            error = str(body.get("error", ""))
    if error in _DEAD_GRANT_ERRORS:
        log.warning(
            "mcp catalog: Google rejected the grant in %s (%s). Re-run "
            "scripts/google-auth.sh and set the secrets it prints; /healthz "
            "reports this under mcp_catalog.",
            check.token_var,
            error,
        )
        return "invalid"
    log.warning(
        "mcp catalog: checking %s returned HTTP %s (%s)",
        check.token_var,
        resp.status_code,
        error or "no error code",
    )
    return "unknown"


async def refresh_health() -> None:
    """Re-probe every catalog entry that has a credential to check.

    Sequential rather than gathered: there are two entries sharing one OAuth
    client, and nothing here is worth hitting Google in parallel for.
    """
    global _refresh_checked_at  # noqa: PLW0603 - process-wide probe cache
    # Stamped before the work, not after, so a slow round of probes cannot let
    # concurrent /healthz calls decide the cache is still stale and pile on.
    _refresh_checked_at = time.time()
    for server in CATALOG:
        if server.oauth is None or server.missing():
            _refresh_state.pop(server.name, None)
            continue
        _refresh_state[server.name] = await _probe(server.oauth)


def schedule_health_refresh() -> None:
    """Start a probe if the cached verdicts are stale, without awaiting one.

    /healthz calls this and then reads the cache. It must never await Google:
    the endpoint is unauthenticated, it is what an external pinger uses to decide
    whether the host may suspend, and its latency is not a third party's to set.
    The TTL is also what keeps a fast pinger from becoming load on Google - one
    call per interval regardless of the request rate.
    """
    global _refresh_task  # noqa: PLW0603 - process-wide probe cache
    if _refresh_task is not None and not _refresh_task.done():
        return
    if (time.time() - _refresh_checked_at) < _HEALTH_TTL_SECONDS:
        return
    # The reference is kept because a bare create_task can be garbage-collected
    # mid-flight (ruff RUF006), which would make this silently do nothing.
    _refresh_task = asyncio.create_task(refresh_health())


def status() -> dict[str, dict[str, Any]]:
    """Per-server readiness for /healthz: present, alive, and how long left.

    Synchronous and cache-only, so it is safe to call from anywhere. Deliberately
    never includes a secret's value - only variable NAMES, Google's own error
    vocabulary, and a date.

    `state` is the single field a monitor should read, set to the worst thing
    currently known. `refresh` is carried separately rather than folded into it,
    because "ready, and Google confirmed it" and "ready, and nobody has asked
    yet" are exactly the two things this bead exists to stop conflating.

    Where the two signals disagree, each is trusted about what it can see: a
    probe is authoritative about death, so `valid` prevents `expired`, while a
    countdown already at zero still reports `expiring` - Google may still be
    honouring a grant we believe should be gone, and that is worth acting on
    before it stops.
    """
    report: dict[str, dict[str, Any]] = {}
    for server in CATALOG:
        missing = server.missing()
        if missing:
            report[server.name] = {"state": "missing", "missing": missing}
            continue

        entry: dict[str, Any] = {"state": "ready"}
        report[server.name] = entry
        if server.oauth is None:
            continue

        refresh = _refresh_state.get(server.name, "unchecked")
        entry["refresh"] = refresh
        expires = _grant_expiry(server.oauth)
        days_left: int | None = None
        if expires is not None:
            days_left = (expires - datetime.now(tz=UTC).date()).days
            entry["expires"] = expires.isoformat()
            entry["days_left"] = days_left

        overdue = refresh != "valid" and days_left is not None and days_left < 0
        if refresh == "invalid" or overdue:
            entry["state"] = "expired"
        elif days_left is not None and days_left <= _EXPIRING_WITHIN_DAYS:
            entry["state"] = "expiring"
    return report


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
