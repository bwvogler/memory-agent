"""The four capabilities, reachable by a machine instead of a browser.

`ingest`, `query`, `lint` and `reflect` already existed here - three as skills
under `bootstrap/skills/`, one as `run_reflection` - but only as things a chat
turn might stumble into via the router. This module makes them addressable, so
another Claude Code session can call them the way it calls any other tool.

--- Every tool is a door onto a skill, never a copy of one ---

A tool here builds a short prompt that says "read this skill and follow it", and
nothing more. The skills live in the knowledge base, seeded from `bootstrap/` and
then edited by the human, so restating their contents in this file would create a
second copy that drifts from the one being maintained - and the drift would be
invisible, because both would look authoritative.

--- Why these run as real turns ---

Each tool goes through `registry.begin` and `agent.run_turn`, which is what
keeps the savepoint, the guards, the signal ledger and the backlog projection.
An MCP call is therefore revertable from the web UI like anything else, and shows
up in the same evidence reflection reads. `reflect` goes through
`agent.maybe_reflect` for the same reason in reverse: reflection's protections
live in that function and in `_reflection_options`, not in a prompt, so the
only safe way to run it headlessly is to run the real thing.

--- Why they are non-interactive ---

`Turn.interactive=False`, because nobody is watching an MCP call. The agent keeps
its question tool but is told plainly that it is alone, which is better than
handing it a tool that silently returns nothing. See app/interact.py.

--- Why a refusal and not a queue ---

One turn at a time, and a tool refuses outright if anything else is running. Two
concurrent agents do not fit under the machine's memory ceiling, and savepoints
are a workspace-wide operation, so two turns would interfere. ADR 0009 explains
why the ceiling is shared state rather than hardware.

This surface used to enforce that with an `asyncio.Lock` of its own. It no
longer does: the rule lives in `turns.Registry.begin` and applies to every
caller, because three entry points spelled it three ways and the fourth - the
browser - did not spell it at all. What is still local to this file is the
*answer*: a `Busy` payload rather than a raised error, for the same reason a
failed turn returns its diagnosis instead of raising.

--- Auth ---

A mounted ASGI app does not run FastAPI's dependencies, so `Depends` would look
correct here and never fire. The middleware below runs `auth.verify` itself and
puts the caller in a ContextVar the tools read. Machine callers arrive on a
Cloudflare Access service token; see `auth.verify` and ADR 0014.

Header only, never the cookie - see `_authenticate`. That is what makes this
endpoint unusable by a page in the household's browser.

--- Two things a mount breaks, both silently ---

Mounting is not free, and both of these look like working code:

* A mounted sub-app's **lifespan never runs**, so the streamable-HTTP session
  manager is never started and every request fails with "Task group is not
  initialized". `session_manager()` exists for `app/main.py` to enter.
* FastMCP's `streamable_http_path` defaults to `/mcp`, which under a `/mcp` mount
  serves at `/mcp/mcp` and answers 404 at the URL you published.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request

from . import agent, auth, kb
from .auth import Identity
from .config import config
from .turns import BUSY, TurnInProgressError, TurnState, registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.types import Receive, Scope, Send

    from .turns import Turn

log = logging.getLogger(__name__)

# The caller of the MCP request currently being served. A ContextVar rather than
# an argument because FastMCP owns the call signature of a tool.
_caller: contextvars.ContextVar[Identity | None] = contextvars.ContextVar(
    "mcp_caller", default=None
)

# One agent at a time - now enforced by Registry.begin, for every caller rather
# than for this surface alone. The asyncio.Lock that used to live here was one
# of four different spellings of the same rule, and the browser path had no
# spelling of it at all. See turns.TurnInProgressError.
_BUSY = f"Busy: {BUSY}"

# streamable_http_path defaults to "/mcp", which would land the endpoint at
# /mcp/mcp once this app is mounted at /mcp. Serving it at the mount root is what
# makes the advertised URL the one that works.
#
# The transport_security override is not a relaxation, it is a correction.
# FastMCP auto-enables DNS-rebinding protection whenever its `host` setting looks
# like localhost - which it does here, because we never call `run()` and the
# setting is a default we do not use - and allows only localhost Host headers. In
# production the Host header is this app's public name, so every single call
# would answer 421. The threat that check exists for is a browser reaching an
# endpoint it should not with a credential it did not have to earn, and
# `_authenticate` below addresses that directly by accepting no cookie at all.
mcp = FastMCP(
    "memory-agent",
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _identity() -> Identity:
    caller = _caller.get()
    if caller is None:  # pragma: no cover - the middleware always sets it
        msg = "no authenticated caller in context"
        raise RuntimeError(msg)
    return caller


def _text(turn: Turn) -> str:
    """The reply the agent produced, reassembled from the event buffer.

    Prefers streamed deltas and falls back to whole-message text, mirroring how
    the browser decides. Subagent output is excluded by construction: it is on
    the stream as `agent_text`, not `text`.
    """
    deltas = "".join(e.data for e in turn.events if e.kind == "text_delta")
    if deltas.strip():
        return deltas
    return "\n\n".join(e.data for e in turn.events if e.kind == "text")


async def _run(prompt: str) -> dict[str, Any]:
    """Run one non-interactive turn to completion and report what it did.

    Returns rather than raises on a failed turn: the caller wants the diagnosis,
    and an MCP error would discard the events that explain it.
    """
    identity = _identity()
    try:
        turn = registry.begin(user_email=identity.email, interactive=False)
    except TurnInProgressError:
        return {"ok": False, "error": _BUSY}

    await agent.run_turn(turn, prompt=prompt, user_slug=identity.slug)
    return {
        "ok": turn.state is not TurnState.ERROR,
        "turn_id": turn.id,
        "reply": _text(turn),
        "error": turn.error,
        # The savepoint is the useful half of the answer: it is what a human
        # needs to undo this call from the web UI.
        "savepoint": turn.savepoint,
        "skills": sorted(turn.skills),
        "subagents": turn.subagents,
    }


def _skill_prompt(skill: str, body: str) -> str:
    return (
        f"Read {kb.workspace_root()}/skills/{skill}/SKILL.md and follow it.\n\n"
        "That skill is the specification and this prompt does not restate it.\n\n"
        f"{body}"
    )


@mcp.tool()
async def ingest(material: str, note: str = "") -> dict[str, Any]:
    """Write something into the wiki: an article, a note, a document's contents.

    Follows the `ingest` skill, so it lands on the right pages, updates the
    index, and files follow-up work as beads.
    """
    body = f"Ingest the following material:\n\n{material}"
    if note.strip():
        body += f"\n\nThe caller added this context: {note.strip()}"
    body += (
        "\n\nNobody is watching this run, so do not wait to check your emphasis "
        "with anyone. Write the pages, and say at the end which judgement calls "
        "you made that a person might want to revisit."
    )
    return await _run(_skill_prompt("ingest", body))


@mcp.tool()
async def query(question: str) -> dict[str, Any]:
    """Answer a question from the wiki, without changing anything.

    Read-only. Says so plainly when the wiki does not answer the question rather
    than filling the gap from the model's own knowledge.
    """
    return await _run(
        f"Answer this question from the knowledge base: {question}\n\n"
        "Delegate the reading to the kb-query subagent, then answer from what it "
        "reports, naming the pages the answer came from.\n\n"
        "Change nothing: no pages, no memory, no beads. If the wiki does not "
        "answer the question, say exactly that and stop - do not answer it from "
        "your own knowledge, because the caller cannot tell the two apart."
    )


@mcp.tool()
async def lint(scope: str = "") -> dict[str, Any]:
    """Audit the wiki for staleness, gaps and contradictions, filing beads.

    Follows the `lint` skill: findings become beads, deduped against the ledger,
    rather than prose nobody reads twice.
    """
    where = scope.strip() or "the whole knowledge base"
    return await _run(
        _skill_prompt("lint", f"Audit {where}.\n\nReport a summary of what you filed.")
    )


@mcp.tool()
async def reflect() -> dict[str, Any]:
    """Let the agent improve one of its own skills from recorded signals.

    Runs the real reflection turn rather than an imitation of it: the remit is
    enforced by a hook in app/evolve.py and by options a prompt cannot set, so
    this only starts it and reports the turn id. Refuses while anything else is
    running - reflection is never urgent.
    """
    identity = _identity()
    turn_id = await agent.maybe_reflect(identity.slug, trigger="mcp")
    if turn_id is None:
        return {
            "ok": False,
            "error": "Not started: a turn is already running, or a reflection is. "
            "Reflection always yields, and the signals it reads are not going "
            "anywhere. Try again when the instance is idle.",
        }
    return {
        "ok": True,
        "turn_id": turn_id,
        "note": "Reflection started. It runs detached; read what it changed in "
        "memory/evolution.md, or revert it from the web UI.",
    }


async def _authenticate(request: Request) -> Identity:
    """Verify a machine caller. **Header only - never the cookie.**

    `auth.current_identity` accepts either the `Cf-Access-Jwt-Assertion` header or
    the `CF_Authorization` cookie, which is right for the browser UI and wrong
    here. A cookie is an *ambient* credential - the browser attaches it to a
    request the page did not have to prove anything to make - and this endpoint
    drives the agent, so an unattended POST to it writes to the wiki.

    Whether a cross-site POST would actually carry that cookie depends on the
    `SameSite` attribute Cloudflare sets, which is not verified here and is not
    ours to control. That is the point: a machine caller sends the header anyway,
    so refusing the cookie costs a real caller nothing and removes the question.
    Defence in depth against an ambient credential, not a patched exploit.
    """
    if config.dev_bypass_auth:
        return Identity(email=config.dev_fake_email, subject="dev")
    token = request.headers.get("Cf-Access-Jwt-Assertion")
    if not token:
        raise HTTPException(
            403,
            "this endpoint requires the Cf-Access-Jwt-Assertion header; a "
            "browser cookie is deliberately not accepted here",
        )
    identity = await auth.verify(token)

    # A second, real-per-person OAuth path onto this same surface (ADR 0014
    # amendment): Cloudflare Access's "Managed OAuth" toggle on THIS SAME Access
    # Application lets Claude/ChatGPT drive a real OAuth 2.1 + PKCE login for a
    # household member, and the token that reaches us is - measured directly,
    # not assumed - the identical Cf-Access-Jwt-Assertion header and `aud` the
    # browser already uses, so auth.verify() needed no changes at all. What it
    # does NOT do is collapse: a real per-person email comes back as itself, and
    # deliberately is NOT folded onto MCP_IDENTITY_EMAIL the way a service
    # token's common_name is - the household chose their own real identity for
    # this path over the shared placeholder one. MCP_OAUTH_EMAILS is therefore
    # its own allowlist, independent of ADR 0005's browser list, for the same
    # reason MCP_CLIENT_IDS already is one: being allowed to open the browser UI
    # is a materially smaller grant than being allowed to drive an unattended,
    # non-interactive turn (Turn.interactive=False) from a phone with nobody
    # watching. A service-token caller already collapsed onto MCP_IDENTITY_EMAIL
    # by auth.verify() skips this check; anyone else needs their real email on
    # this list. Empty by default, so an untouched deployment refuses every real
    # identity here exactly as before.
    if identity.email != config.mcp_identity_email.lower() and (
        identity.email not in config.mcp_oauth_emails
    ):
        raise HTTPException(
            403, f"{identity.email!r} is not enabled for the /mcp OAuth path"
        )
    return identity


@contextlib.asynccontextmanager
async def session_manager() -> AsyncIterator[None]:
    """Start the streamable-HTTP session manager. **Required, not optional.**

    `streamable_http_app()` returns a Starlette app whose own lifespan starts
    this. A MOUNTED sub-app's lifespan is never run by the parent, so without
    this every single MCP request fails with "Task group is not initialized" -
    a 500 on the surface's happy path, from a component that imported and
    mounted perfectly. `app/main.py`'s lifespan enters it.

    Enterable **once per process**: the underlying manager raises if run twice,
    and `mcp` is a module-level singleton. One process, one lifespan, so that is
    not a production constraint - but it does mean only one test can enter it.
    """
    async with mcp.session_manager.run():
        yield


def asgi_app():
    """The MCP app, wrapped in the authentication its mount point will not run."""
    inner = mcp.streamable_http_app()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return
        # Built from the raw scope rather than injected, because this runs
        # outside the router that would normally have produced a Request.
        try:
            identity = await _authenticate(Request(scope, receive))
        except Exception as exc:  # noqa: BLE001 - any failure is a refusal
            status = getattr(exc, "status_code", 403)
            detail = getattr(exc, "detail", "not authorised")
            log.info("rejected an MCP request: %s", detail)
            await _refuse(send, status, str(detail))
            return

        token = _caller.set(identity)
        try:
            await inner(scope, receive, send)
        finally:
            _caller.reset(token)

    return app


async def _refuse(send: Send, status: int, detail: str) -> None:
    body = detail.encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def enabled() -> bool:
    """True when a machine caller could actually authenticate.

    Mounting the surface is harmless either way - every request is verified -
    but reporting this in /healthz is the difference between "MCP is off" and
    "MCP is on and every call is being refused", which are hard to tell apart
    from the outside.
    """
    return bool(
        config.dev_bypass_auth
        or (config.mcp_client_ids and config.mcp_identity_email)
        or config.mcp_oauth_emails
    )
