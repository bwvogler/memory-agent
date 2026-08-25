"""HTTP surface: submit a turn, stream it back, revert it.

Auth is handled by Cloudflare Access in front of this process, and verified
again here (see app/auth.py for why both).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import agent, interact, kb, mcp_catalog, mcp_server, signals
from .auth import Identity, current_identity
from .config import config
from .session_store import PostgresSessionStore
from .turns import Turn, TurnInProgressError, TurnState, registry, spawn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("memory-agent")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

HEARTBEAT_SECONDS = 15  # Cloudflare's 524 is a time-to-next-byte timeout (~125s).

store: PostgresSessionStore | None = None

# Every route below is authenticated. Handlers that need the caller take
# `identity: CurrentUser`; handlers that only need the *check* declare it as a
# route dependency instead, so there is no unused parameter pretending to be
# used. Both run the same verification.
CurrentUser = Annotated[Identity, Depends(current_identity)]
AUTHENTICATED = [Depends(current_identity)]


# Compose gates the app on `pg_isready`, which answers for the postmaster and
# not for the database this DSN names, so the first connection can lose a race
# the healthcheck already declared won. One attempt at boot made that permanent
# for the life of the process: no transcripts, no skill ledger, and a stack
# that reported itself completely healthy. Retry briefly instead.
SESSION_STORE_ATTEMPTS = 5
SESSION_STORE_BACKOFF_S = 2.0


async def _start_session_store() -> PostgresSessionStore | None:
    """Connect the durable store, or give up and say so loudly."""
    candidate = PostgresSessionStore(config.session_database_url)
    for attempt in range(1, SESSION_STORE_ATTEMPTS + 1):
        try:
            await candidate.start()
        except Exception:
            if attempt == SESSION_STORE_ATTEMPTS:
                log.exception(
                    "session store unavailable after %d attempts; transcripts "
                    "will not be durable and the skill ledger will be empty. "
                    "/healthz reports transcripts=unavailable.",
                    SESSION_STORE_ATTEMPTS,
                )
                return None
            log.warning(
                "session store not ready (attempt %d/%d); retrying in %.0fs",
                attempt,
                SESSION_STORE_ATTEMPTS,
                SESSION_STORE_BACKOFF_S,
            )
            await asyncio.sleep(SESSION_STORE_BACKOFF_S)
        else:
            return candidate
    return None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    problems = config.validate()
    for problem in problems:
        log.error("CONFIG: %s", problem)

    kb.assert_scratch_outside_kb()

    # Assert the mount before accepting traffic. A knowledge base that is not
    # mounted produces no errors anywhere - just an agent that mysteriously
    # knows nothing - so this check is the difference between a five-minute
    # diagnosis and a two-hour one.
    if not kb.is_mounted():
        log.error(
            "KB mount at %s does not look live. Control surface probe: %s. "
            "The agent will start but will have NO knowledge base.",
            config.kb_mount,
            kb.probe_control_surface(),
        )
    else:
        log.info("KB mount healthy: %s", kb.probe_control_surface())
        agent.seed_guide()
        agent.seed_bootstrap()

    # Independent of the mount: the ledgers live on the volume, and a deploy
    # that fixed something should say so even if the KB is unreachable. This is
    # the return path for ideas the agent filed about the image it runs on -
    # see docs/decisions/0010.
    if closed := await kb.reconcile_shipped_all():
        log.info("closed shipped beads: %s", closed)

    global store  # noqa: PLW0603 - the store outlives every request
    if config.session_database_url:
        store = await _start_session_store()
    signals.attach_store(store)

    # The MCP surface is a MOUNTED sub-app, and a mount's own lifespan never
    # runs. Its session manager therefore has to be started from here or every
    # /mcp request fails on the happy path with "Task group is not initialized".
    async with mcp_server.session_manager():
        yield

    if store:
        await store.close()
    await kb.close_pool()


app = FastAPI(title="memory-agent", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Unauthenticated liveness probe.

    Exposes `busy` so an external pinger can avoid suspending the host while a
    detached turn is still running. See the "Do not suspend mid-turn" section
    of docs/architecture.md - this endpoint is the hook, not the solution.

    `transcripts` reports the durable store as one of `ready`, `unconfigured`
    or `unavailable`. It is deliberately NOT part of `ok`: a turn that cannot
    reach its ledger should still answer the user, which is the same call kb.py
    makes about beads. But it has to be *visible*, because the alternative is
    what already happened - a stack whose store failed to start reported itself
    perfectly healthy, and the only symptom was two tests failing much later
    for reasons that looked unrelated to each other and to the cause.
    """
    mounted = kb.is_mounted()
    # Kicks off an OAuth probe when the cached verdicts are stale, and returns
    # immediately - this endpoint never waits on Google. See mcp_catalog.
    mcp_catalog.schedule_health_refresh()
    if store is not None:
        transcripts = "ready"
    elif not config.session_database_url:
        transcripts = "unconfigured"
    else:
        transcripts = "unavailable"
    return JSONResponse(
        {
            "ok": mounted,
            "kb_mounted": mounted,
            "transcripts": transcripts,
            "control_surface": kb.probe_control_surface(),
            "busy": registry.any_running(),
            # Not part of `ok` either. The surface is always mounted and always
            # verified; this says whether a machine caller could get past that
            # verification, which is the difference between "MCP is off" and
            # "MCP is on and refusing every call".
            "mcp": mcp_server.enabled(),
            # The other direction: outbound servers this app connects to as a
            # client. Each reports a `state` - `missing`, `expired`, `expiring`
            # or `ready` - never a secret's value. A server whose credential is
            # missing is dropped silently from the agent's toolset, and this is
            # the one place that says so; without it "the calendar tools are
            # gone" and "the calendar tools never existed" look identical from
            # outside.
            #
            # Not folded into `ok`, for the same reason as `transcripts`: an
            # expired Google token is not a reason to fail a liveness probe and
            # have the host restarted, since no restart would fix it. It needs a
            # person with a browser, so it needs to be VISIBLE, not fatal.
            "mcp_catalog": mcp_catalog.status(),
        },
        status_code=200 if mounted else 503,
    )


@app.get("/api/me")
async def me(identity: CurrentUser) -> dict[str, str]:
    return {"email": identity.email}


def _decode_attachments(files: list[dict[str, Any]]) -> list[tuple[str, bytes]]:
    """Validate and decode the `files` payload, or raise the right HTTP error.

    Separate from the route and from any filesystem work so the limits can be
    tested as arithmetic. Everything here is checked BEFORE a turn is created:
    a rejected upload should leave no turn behind for the UI to stream.
    """
    decoded: list[tuple[str, bytes]] = []
    total = 0
    for entry in files:
        name = kb.safe_upload_name(str(entry.get("name") or ""))
        if name is None:
            raise HTTPException(400, "attachment is missing a usable filename")
        try:
            blob = base64.b64decode(str(entry.get("data") or ""), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(400, f"attachment {name} is not valid base64") from exc
        if not blob:
            raise HTTPException(400, f"attachment {name} is empty")
        if len(blob) > config.max_upload_bytes:
            raise HTTPException(
                413, f"attachment {name} exceeds {config.max_upload_bytes} bytes"
            )
        total += len(blob)
        if total > config.max_upload_total_bytes:
            raise HTTPException(
                413,
                f"attachments exceed {config.max_upload_total_bytes} bytes in total",
            )
        decoded.append((name, blob))
    return decoded


def _stage_attachments(
    user_slug: str, turn_id: str, decoded: list[tuple[str, bytes]]
) -> list[Path]:
    """Write decoded attachments into the turn's upload directory."""
    staged: list[Path] = []
    for name, blob in decoded:
        path = kb.resolve_upload_path(user_slug, turn_id, name)
        if path is None:
            raise HTTPException(400, f"attachment {name} has an unusable filename")
        path.write_bytes(blob)
        staged.append(path)
    return staged


# uuid4().hex, which is what turns.Registry stamps every turn id with. Checked
# before anything touches the filesystem: an id that fails this can never be a
# real turn, so there is no reason to let it reach a path lookup at all.
_TURN_ID = re.compile(r"\A[0-9a-f]{32}\Z")

# A pure allowlist, never mimetypes.guess_type: this route serves attacker-
# controlled bytes back over an authenticated origin that can also submit a
# turn, so the one thing that must never happen is an upload coming back as
# text/html or image/svg+xml (both are scripting contexts on this origin).
# Everything not listed is an opaque download, not a render.
_UPLOAD_MEDIA_TYPES = {
    ".md": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/plain; charset=utf-8",
    ".json": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _upload_media_type(name: str) -> tuple[str, bool]:
    """(content-type, inline?) for an uploaded file's name. Pure, so it is
    testable as a lookup table rather than through a route."""
    media_type = _UPLOAD_MEDIA_TYPES.get(Path(name).suffix.lower())
    if media_type is None:
        return "application/octet-stream", False
    return media_type, True


@app.post("/api/turns")
async def create_turn(request: Request, identity: CurrentUser) -> JSONResponse:
    """Submit a message. Returns immediately with a turn id.

    The agent runs detached so that no single HTTP request has to survive for
    the whole turn.

    Attachments take a different route from images and deliberately so. An
    image becomes a base64 content block in the message, which is right for a
    screenshot; a document is written to the agent's scratch directory and only
    its path is mentioned, so a 5 MB CSV costs nothing until the agent decides
    to read it. See `agent._attachment_note`.
    """
    body = await request.json()
    prompt = (body.get("message") or "").strip()
    images = body.get("images") or []  # list of {"media_type": str, "data": str}
    files = body.get("files") or []  # list of {"name": str, "data": str}
    if not prompt and not images and not files:
        raise HTTPException(400, "message is required")
    resume = body.get("session_id") or None

    # Before registry.create, so a 400 or 413 leaves no orphan turn behind.
    decoded = _decode_attachments(files)

    # 409 rather than a queue, and the message says why. Queueing would hand the
    # browser a turn id that streams nothing for however long the turn in front
    # of it takes, which is the "it looked hung" failure this UI has already had
    # to be fixed for once. Refusing is at least legible.
    try:
        turn = registry.begin(user_email=identity.email, session_id=resume)
    except TurnInProgressError as exc:
        raise HTTPException(409, str(exc)) from exc

    try:
        staged = _stage_attachments(identity.slug, turn.id, decoded)
    except OSError as exc:
        # The turn exists but will never run, and an unfinished turn streams
        # forever. Terminate it here rather than leaving the UI waiting.
        log.exception("could not stage attachments for turn %s", turn.id)
        turn.finish(TurnState.ERROR, error=f"could not save attachments: {exc}")
        raise HTTPException(500, "could not save attachments") from exc

    # Named on the event stream, not on the Turn object: the Registry is
    # in-process and evicts, but a reload reconnects with Last-Event-ID and
    # replays events, so this - not a `turn.files` field - is what makes an
    # uploaded file's chip survive a refresh mid-turn.
    for path in staged:
        turn.append(
            "attachment",
            interact.json_event(
                name=path.name, url=f"/api/uploads/{turn.id}/{quote(path.name)}"
            ),
        )

    spawn(
        agent.run_turn(
            turn,
            prompt=prompt,
            user_slug=identity.slug,
            resume=resume,
            images=images or None,
            files=staged or None,
        ),
        name=f"turn-{turn.id}",
    )
    return JSONResponse({"turn_id": turn.id}, status_code=202)


@app.get("/api/turns/{turn_id}")
async def get_turn(turn_id: str, identity: CurrentUser) -> dict[str, Any]:
    """Polling fallback, for when streaming misbehaves."""
    turn = registry.get(turn_id)
    if not turn or turn.user_email != identity.email:
        raise HTTPException(404, "no such turn")
    return {
        **turn.summary(),
        "events": [{"seq": e.seq, "kind": e.kind, "data": e.data} for e in turn.events],
    }


@app.get("/api/turns/{turn_id}/events")
async def stream_turn(
    turn_id: str, request: Request, identity: CurrentUser
) -> StreamingResponse:
    """SSE stream, replayable via Last-Event-ID.

    SSE rather than WebSocket on purpose: it survives Access cleanly with cookie
    auth, reconnects with replay for free, and browsers cannot set headers on
    `new WebSocket()` - which would force any non-browser client onto Access
    service tokens.
    """
    turn = registry.get(turn_id)
    if not turn or turn.user_email != identity.email:
        raise HTTPException(404, "no such turn")

    try:
        last_seq = int(request.headers.get("Last-Event-ID", "0"))
    except ValueError:
        last_seq = 0

    async def generate():
        cursor = last_seq
        while True:
            if await request.is_disconnected():
                return

            for event in turn.since(cursor):
                cursor = event.seq
                yield (
                    f"id: {event.seq}\n"
                    f"event: {event.kind}\n"
                    f"data: {_sse_escape(event.data)}\n\n"
                )

            if turn.finished and cursor >= len(turn.events):
                terminal = "done" if turn.state is TurnState.DONE else "failed"
                yield f"event: {terminal}\ndata: {_sse_escape(turn.error or '')}\n\n"
                return

            # A comment frame counts as bytes, which keeps Cloudflare's
            # time-to-next-byte timer from firing during long silent thinking.
            yield ": keepalive\n\n"
            await turn.wait_for_change(timeout=HEARTBEAT_SECONDS)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            # Cloudflare and cloudflared will happily buffer text/event-stream
            # and deliver the whole turn in one lump at the end. These three
            # headers are what stop that. Re-test after every deploy: this has
            # regressed more than once.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/turns/{turn_id}/revert")
async def revert_turn(turn_id: str, identity: CurrentUser) -> dict[str, Any]:
    """Roll the knowledge base back to this turn's savepoint.

    Safe to expose in the UI: TigerFS undo is itself reversible.
    """
    turn = registry.get(turn_id)
    if not turn or turn.user_email != identity.email:
        raise HTTPException(404, "no such turn")
    if not turn.savepoint:
        raise HTTPException(409, "this turn has no savepoint to revert to")

    # Capture what is about to be rolled back before rolling it back: once the
    # reset lands, the working tree matches the savepoint and the diff is empty.
    diff_stat = await kb.diff_since_savepoint(turn.savepoint)

    ok = await kb.undo_to_savepoint(turn.savepoint)
    if not ok:
        raise HTTPException(500, "undo failed; check the server log")

    # A revert is the strongest signal this system gets: a human saying "that
    # was wrong" about one exact turn. Recorded, not acted on - see
    # app/signals.py and bead kb-3sv.
    bead_id = await signals.on_revert(turn, identity.slug, diff_stat)
    # If this was a reflection turn, the revert is also a rejection of the
    # self-edit it made. Recorded so the loop cannot re-propose it forever.
    await signals.note_rejected_proposals(turn, identity.slug)
    return {"reverted_to": turn.savepoint, "signal_bead": bead_id}


def _pending_turn(turn_id: str, identity: CurrentUser) -> Turn:
    """The turn a human is answering, or the right HTTP error.

    Ownership is the same raw comparison the rest of this file uses. It matters
    more here than on a read: these two routes let a caller unblock a *running*
    agent, so answering someone else's question would be putting words in their
    turn.
    """
    turn = registry.get(turn_id)
    if not turn or turn.user_email != identity.email:
        raise HTTPException(404, "no such turn")
    return turn


@app.post("/api/turns/{turn_id}/answer")
async def answer_turn(
    turn_id: str, request: Request, identity: CurrentUser
) -> dict[str, Any]:
    """Answer a question the agent asked mid-turn.

    409 rather than 500 on a request id that is unknown or already answered,
    because both are ordinary: two clicks on one form, or a tab that reconnected
    and replayed the question after another tab had already answered it.
    """
    turn = _pending_turn(turn_id, identity)
    body = await request.json()
    request_id = str(body.get("request_id") or "")
    answers = [str(a) for a in (body.get("answers") or [])]
    notes = str(body.get("notes") or "")
    if not request_id:
        raise HTTPException(400, "request_id is required")
    if not turn.resolve(request_id, {"answers": answers, "notes": notes}):
        raise HTTPException(409, "that question is not waiting for an answer")
    return {"answered": request_id}


@app.post("/api/turns/{turn_id}/permission")
async def decide_permission(
    turn_id: str, request: Request, identity: CurrentUser
) -> dict[str, Any]:
    """Allow or deny a tool the agent asked to use."""
    turn = _pending_turn(turn_id, identity)
    body = await request.json()
    request_id = str(body.get("request_id") or "")
    decision = str(body.get("decision") or "")
    if not request_id:
        raise HTTPException(400, "request_id is required")
    if decision not in ("allow", "deny"):
        raise HTTPException(400, "decision must be 'allow' or 'deny'")
    answer = {"decision": decision, "note": str(body.get("note") or "")}
    if not turn.resolve(request_id, answer):
        raise HTTPException(409, "that request is not waiting for a decision")
    return {"decided": request_id, "decision": decision}


@app.post("/api/reflect")
async def reflect(identity: CurrentUser) -> JSONResponse:
    """Run a reflection turn now, instead of waiting for a signal to trigger one.

    This exists because signal-gated reflection alone is untestable at this
    scale. When Stage 3 was built the ledger held 6 turns and zero reverts, so
    a loop that only fires on a signal would have been dead code shipped
    unexercised - the worst way to deploy self-modification. See ADR 0008.
    """
    # Non-interactive even though a person pressed the button: reflection runs
    # under _reflection_options, which installs no question tool and no
    # permission callback, and the flag has to say the same thing the options do.
    try:
        turn = registry.begin(user_email=identity.email, interactive=False)
    except TurnInProgressError as exc:
        raise HTTPException(409, str(exc)) from exc
    spawn(
        agent.run_reflection(turn, identity.slug, trigger="manual"),
        name=f"reflection-{turn.id}",
    )
    return JSONResponse({"turn_id": turn.id}, status_code=202)


@app.get("/api/sessions")
async def list_sessions(identity: CurrentUser) -> dict[str, Any]:
    if not store:
        return {"sessions": []}
    return {"sessions": await store.sessions_for(identity.email)}


@app.get("/api/signals", dependencies=AUTHENTICATED)
async def signal_summary() -> dict[str, Any]:
    """What the Stage 2 ledger has actually captured so far.

    This exists so bead kb-3sv - "do the captured signals justify building
    Stage 3?" - can be answered by looking, rather than by arguing. Read the
    rates next to `totals`: a skill loaded on every turn is present in every
    revert whether or not it had anything to do with it.
    """
    if not store:
        return {"totals": {}, "skills": [], "note": "no session store configured"}
    return {
        "totals": await store.turn_totals(),
        "skills": await store.skill_signal_summary(),
    }


@app.get("/api/kb/log", dependencies=AUTHENTICATED)
async def kb_log() -> dict[str, Any]:
    """Recent knowledge-base operations, with per-user attribution."""
    return {"entries": await kb.recent_log()}


@app.get("/api/kb/files", dependencies=AUTHENTICATED)
async def kb_files() -> dict[str, Any]:
    """List markdown files in the KB workspace (single SQL query)."""
    return {"files": await kb.sql_list_files()}


@app.get("/api/kb/file", dependencies=AUTHENTICATED)
async def kb_file(path: str) -> dict[str, str]:
    """Return raw markdown for a KB file (single SQL query)."""
    content = await kb.sql_read_file(path)
    if content is None:
        raise HTTPException(404, "not found")
    return {"path": path, "content": content}


@app.get("/api/uploads/{turn_id}/{name}")
async def uploaded_file(turn_id: str, name: str, identity: CurrentUser) -> FileResponse:
    """Serve back an attachment's bytes, for the centre pane.

    Ownership is proven by the path, not by looking up the Turn: the slug
    comes from the verified Identity and never appears in the URL, so there is
    no input by which a caller can name another user's directory. A
    `turn.user_email` check would prove the same fact more weakly, and would
    404 a perfectly valid file once the in-process Registry evicts it (bounded
    to 200 turns, oldest-finished first).

    `turn_id` is checked against the shape `Registry` actually stamps BEFORE
    any path is built from it - `resolve_upload_path`'s containment check
    passes for any `turn_id`, since the escape would have already happened one
    directory up, in `uploads_dir_for`. Only `uploads/` is servable, never
    arbitrary scratch: scratch also holds the bead ledger and everything the
    agent fetched from the web, on a volume with no savepoint.
    """
    if not _TURN_ID.fullmatch(turn_id):
        raise HTTPException(404, "no such attachment")
    path = kb.upload_path_for_read(identity.slug, turn_id, name)
    if path is None or not path.is_file():
        raise HTTPException(404, "no such attachment")

    media_type, inline = _upload_media_type(path.name)
    disposition = "inline" if inline else "attachment"
    # safe_upload_name only strips non-printable characters, not quotes or
    # backslashes - both are escaped here so a crafted filename cannot break
    # out of the quoted-string and inject a header.
    safe_name = path.name.replace("\\", "\\\\").replace('"', '\\"')
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'{disposition}; filename="{safe_name}"',
    }
    return FileResponse(path, media_type=media_type, headers=headers)


def _sse_escape(text: str) -> str:
    """SSE data lines cannot contain raw newlines."""
    return text.replace("\r\n", "\n").replace("\n", "\\n")


@app.get("/", dependencies=AUTHENTICATED)
@app.get("/kb", dependencies=AUTHENTICATED)
@app.get("/kb/{path:path}", dependencies=AUTHENTICATED)
async def index() -> FileResponse:
    """The merged tree/renderer/chat page, at every URL that used to be two.

    `/` was unauthenticated when it was an empty shell whose every fetch was
    authenticated anyway - that stopped being harmless once `/` became the
    wiki itself, since an open shell that 403s its own tree fetch is a worse
    experience than the Access login redirect it now gets instead. `fly.toml`
    probes `/healthz`, not this route.

    `/kb/{path:path}` still takes no `path` argument: the server has nothing
    to do with it, and the client reads `location.pathname` to pick the
    initial centre pane. Serving the same document rather than redirecting
    means a copied URL is the URL you land on.
    """
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# The MCP surface, for a machine caller rather than a browser. Mounted rather
# than routed, because it is a whole ASGI app - which is also why it carries its
# own authentication: a mount does not run this app's dependencies, so the
# `dependencies=AUTHENTICATED` used everywhere above would silently never fire
# here. See app/mcp_server.py and docs/decisions/0014-the-machine-is-a-caller.md.
app.mount("/mcp", mcp_server.asgi_app(), name="mcp")
