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
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import agent, interact, kb, kbview, mcp_catalog, mcp_server, signals
from .auth import Identity, current_identity, display_name_for
from .config import config
from .conversations import conversations
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
    conversations.attach_store(store)
    conversations.start_flusher()

    # The MCP surface is a MOUNTED sub-app, and a mount's own lifespan never
    # runs. Its session manager therefore has to be started from here or every
    # /mcp request fails on the happy path with "Task group is not initialized".
    async with mcp_server.session_manager():
        yield

    # Drains anything still queued before the store closes underneath it.
    await conversations.stop_flusher()
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


def _sse_frame(seq: int, kind: str, data: str) -> str:
    return f"id: {seq}\nevent: {kind}\ndata: {_sse_escape(data)}\n\n"


_SSE_HEADERS = {
    # Cloudflare and cloudflared will happily buffer text/event-stream and
    # deliver the whole turn in one lump at the end. These three headers are
    # what stop that. Re-test after every deploy: this has regressed more
    # than once.
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@app.get("/api/conversations")
async def list_conversations(identity: CurrentUser) -> dict[str, Any]:  # noqa: ARG001
    """Every household conversation, newest first. See docs/decisions/0012:
    there is no per-user filter - conversations are household-shared."""
    if not store:
        return {"conversations": []}
    return {"conversations": await store.list_conversations()}


@app.post("/api/conversations")
async def create_conversation(identity: CurrentUser) -> JSONResponse:
    """Start a new, empty conversation. The explicit "New chat" kb-nb4 asked for."""
    conversation_id = uuid.uuid4().hex
    if store:
        await store.create_conversation(conversation_id, identity.email)
    await conversations.get_or_load(conversation_id)
    return JSONResponse({"conversation_id": conversation_id}, status_code=201)


@app.patch("/api/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: str,
    request: Request,
    identity: CurrentUser,  # noqa: ARG001
) -> dict[str, Any]:
    if not store:
        raise HTTPException(409, "no durable store configured")
    body = await request.json()
    if "title" in body:
        await store.set_conversation_title(conversation_id, str(body["title"] or ""))
    if "archived" in body:
        await store.set_conversation_archived(
            conversation_id, archived=bool(body["archived"])
        )
    return {"ok": True}


@app.get("/api/conversations/{conversation_id}/events")
async def stream_conversation(
    conversation_id: str,
    request: Request,
    identity: CurrentUser,  # noqa: ARG001
) -> StreamingResponse:
    """SSE stream for a whole conversation, replayable via Last-Event-ID.

    `0` (a fresh EventSource, no header at all) means "from the beginning" -
    the change from the old per-turn stream, which only ever replayed one
    turn. Household-shared: unlike the old route, there is no ownership check
    here at all - any allowlisted member watches the same stream. See
    docs/decisions/0017.

    SSE rather than WebSocket, unchanged from the old route: it survives
    Access cleanly with cookie auth, reconnects with replay for free, and
    browsers cannot set headers on `new WebSocket()`.
    """
    conv = await conversations.get_or_load(conversation_id)
    try:
        last_seq = int(request.headers.get("Last-Event-ID", "0"))
    except ValueError:
        last_seq = 0

    async def generate():
        cursor = last_seq
        # Anything older than the in-memory tail lives only in Postgres - a
        # fresh Conversation object after a restart, or a reconnect from
        # further back than EVENT_BUFFER_MAX events ago.
        if store is not None and cursor < conv.earliest_buffered_seq:
            history = await store.read_conversation_events(
                conversation_id, after_seq=cursor
            )
            for row in history:
                cursor = row["seq"]
                yield _sse_frame(row["seq"], row["kind"], row["data"])

        while True:
            if await request.is_disconnected():
                return
            for event in conv.since_buffered(cursor):
                cursor = event.seq
                yield _sse_frame(event.seq, event.kind, event.data)
            # A comment frame counts as bytes, which keeps Cloudflare's
            # time-to-next-byte timer from firing during long silent thinking.
            yield ": keepalive\n\n"
            await conv.wait_for_change(timeout=HEARTBEAT_SECONDS)

    return StreamingResponse(
        generate(), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@app.post("/api/conversations/{conversation_id}/messages")
async def post_message(
    conversation_id: str, request: Request, identity: CurrentUser
) -> JSONResponse:
    """Submit a message into a conversation.

    If a turn is already running IN THIS conversation, the message is
    injected into it rather than refused - turn-taking by injection, see
    `agent._input_stream` and docs/decisions/0017. A turn running in a
    DIFFERENT conversation still refuses: exactly one turn runs at a time,
    instance-wide (savepoints are workspace-wide - ADR 0009), unchanged.
    """
    body = await request.json()
    prompt = (body.get("message") or "").strip()
    images = body.get("images") or []
    files = body.get("files") or []
    if not prompt and not images and not files:
        raise HTTPException(400, "message is required")
    decoded = _decode_attachments(files)

    conv = await conversations.get_or_load(conversation_id)
    running = registry.running()

    if running is not None and running.conversation_id == conversation_id:
        if decoded:
            # Attaching a file mid-turn would need a turn id to stage it
            # under, and this message has none yet - simpler to ask the
            # sender to wait than to invent a second staging path.
            raise HTTPException(
                409,
                "attachments cannot be added to a running turn; wait for it to finish",
            )
        if running.inbox is None:  # pragma: no cover - every conversation turn has one
            raise HTTPException(409, str(TurnInProgressError(running)))
        # turn_id/actor are duplicated into the JSON payload as well as the
        # column: the SSE frame only forwards (seq, kind, data), and the
        # client needs both to detect a turn boundary and attribute the
        # bubble without a second round trip. See docs/decisions/0017.
        event = conv.append(
            "user_message",
            interact.json_event(
                text=prompt,
                images=bool(images),
                turn_id=running.id,
                actor=identity.email,
            ),
            turn_id=running.id,
            actor=identity.email,
        )
        running.inbox.put_nowait((prompt, images or None, identity.email))
        return JSONResponse(
            {"turn_id": running.id, "injected": True, "seq": event.seq}, status_code=202
        )

    if running is not None:
        # Named, not the bare BUSY text - the point of a shared log is that a
        # refusal can say WHY instead of just that. See docs/decisions/0017.
        who = (
            display_name_for(running.actor_email) if running.actor_email else "Someone"
        )
        detail = f"{who} has a turn running elsewhere. {TurnInProgressError(running)}"
        raise HTTPException(409, detail)

    try:
        turn = registry.begin(
            user_email=identity.email,
            interactive=True,
            conversation_id=conversation_id,
            actor_email=identity.email,
        )
    except TurnInProgressError as exc:  # a race with the check above
        raise HTTPException(409, str(exc)) from exc

    try:
        staged = _stage_attachments(identity.slug, turn.id, decoded)
    except OSError as exc:
        log.exception("could not stage attachments for turn %s", turn.id)
        turn.finish(TurnState.ERROR, error=f"could not save attachments: {exc}")
        raise HTTPException(500, "could not save attachments") from exc

    conv.append(
        "user_message",
        interact.json_event(
            text=prompt, images=bool(images), turn_id=turn.id, actor=identity.email
        ),
        turn_id=turn.id,
        actor=identity.email,
    )
    for path in staged:
        turn.append(
            "attachment",
            interact.json_event(
                name=path.name, url=f"/api/uploads/{turn.id}/{quote(path.name)}"
            ),
        )

    resume = None
    if store:
        conv_row = await store.get_conversation(conversation_id)
        resume = (conv_row or {}).get("session_id")

    turn.task = spawn(
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
    return JSONResponse({"turn_id": turn.id, "injected": False}, status_code=202)


@app.get("/api/turns/{turn_id}")
async def get_turn(turn_id: str, identity: CurrentUser) -> dict[str, Any]:  # noqa: ARG001
    """Polling fallback, for a caller that would rather not hold an SSE
    connection open - the `--live` test tier in particular, which polls this
    in a loop instead of consuming a stream. No ownership check, matching
    every other turn route now - see docs/decisions/0017.

    For a conversation turn, `events` is filtered out of the conversation's
    own buffer by `turn_id`; `turn.events` itself stays empty in that case
    (see turns.Turn.append), so reading it directly here would silently
    under-report.
    """
    turn = registry.get(turn_id)
    if not turn:
        raise HTTPException(404, "no such turn")
    if turn.conversation_id is not None:
        conv = conversations.get(turn.conversation_id)
        events = (
            [
                {"seq": e.seq, "kind": e.kind, "data": e.data}
                for e in conv.events
                if e.turn_id == turn_id
            ]
            if conv is not None
            else []
        )
    else:
        events = [{"seq": e.seq, "kind": e.kind, "data": e.data} for e in turn.events]
    # summary()'s event_count reads turn.events directly, which is empty for
    # a conversation turn (see turns.Turn.append) - overridden here so it
    # does not silently under-report against the `events` list above.
    return {**turn.summary(), "event_count": len(events), "events": events}


@app.post("/api/turns/{turn_id}/stop")
async def stop_turn(turn_id: str, identity: CurrentUser) -> dict[str, Any]:  # noqa: ARG001
    """Cancel a running turn. See agent._run_turn's CancelledError handling.

    Household-shared: whoever asked and whoever stops it may be different
    people, which is fine - the trace up to this point is already durable
    (app/conversations.py), and the SDK's own transcript holds the partial
    work for a later resume.
    """
    turn = registry.get(turn_id)
    if not turn:
        raise HTTPException(404, "no such turn")
    if turn.finished:
        raise HTTPException(409, "this turn has already finished")
    if turn.task is None:
        raise HTTPException(409, "this turn cannot be stopped")
    turn.task.cancel()
    return {"stopping": turn_id}


@app.post("/api/turns/{turn_id}/revert")
async def revert_turn(turn_id: str, identity: CurrentUser) -> dict[str, Any]:
    """Roll the knowledge base back to this turn's savepoint.

    Safe to expose in the UI: TigerFS undo is itself reversible. No ownership
    check: any allowlisted household member may revert any turn - see
    docs/decisions/0017's household-thread consequence.
    """
    turn = registry.get(turn_id)
    if not turn:
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
    bead_id = await signals.on_revert(
        turn, identity.slug, diff_stat, reverted_by=identity.email
    )
    # If this was a reflection turn, the revert is also a rejection of the
    # self-edit it made. Recorded so the loop cannot re-propose it forever.
    await signals.note_rejected_proposals(turn, identity.slug)
    return {"reverted_to": turn.savepoint, "signal_bead": bead_id}


def _pending_turn(turn_id: str, identity: CurrentUser) -> Turn:  # noqa: ARG001
    """The turn a human is answering, or the right HTTP error.

    No ownership check: any allowlisted household member may answer a
    question or a permission prompt on any running turn, since the turn is
    now visible to all of them - see docs/decisions/0017.
    """
    turn = registry.get(turn_id)
    if not turn:
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
    answer = {"answers": answers, "notes": notes, "actor": identity.email}
    if not turn.resolve(request_id, answer):
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
    answer = {
        "decision": decision,
        "note": str(body.get("note") or ""),
        "actor": identity.email,
    }
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
async def kb_file(path: str) -> dict[str, Any]:
    """Return raw markdown for a KB file, plus its frontmatter (one query).

    `fields` is additive and comes off the row the body was already read from.
    It is what lets a file opened by deep link draw the header its directory
    asked for - without it the renderer knows only that it has no value, which
    is indistinguishable from the value being empty, and it would print the
    spec's "nobody filled this in" label over data it was never given.
    """
    doc = await kb.sql_read_document(path)
    if doc is None:
        raise HTTPException(404, "not found")
    return {"path": path, "content": doc["body"] or "", "fields": doc["fields"]}


def _spec_payload(
    raw: object, source: str | None
) -> tuple[kbview.View, dict[str, Any]]:
    """Normalise a spec into the shape both view routes return.

    `view` and `page` are always usable objects, never null. A directory with
    no `VIEW.md` gets the defaults rather than a null the client would have to
    branch on - the *absence* is reported by `view_source_path`, which is the
    only thing that actually differs, and is what lets the UI say "this
    directory has no view yet" without pretending it cannot render one.
    """
    view, page, warnings = kbview.normalise(raw)
    return view, {
        "view": asdict(view),
        "page": asdict(page),
        "view_source_path": source,
        "view_error": None,
        "warnings": warnings,
    }


@app.get("/api/kb/dir", dependencies=AUTHENTICATED)
async def kb_dir(path: str = "") -> dict[str, Any]:
    """A directory rendered as an index: its guide, its spec, its entries.

    One query. `GUIDE.md` and `VIEW.md` are children of this directory like
    any other file, so both arrive in the same result set that carries the
    entries - there is nothing extra to fetch.

    A bad spec never costs the reader the page: `kbview.normalise` degrades to
    the default view and reports what it could not use, so the failure mode of
    an agent writing nonsense is a warning above a plain list.
    """
    dir_path = path.strip("/")
    children = await kb.sql_list_children(dir_path)
    if not children and dir_path and not await kb.sql_dir_exists(dir_path):
        raise HTTPException(404, "not found")

    files = [c for c in children if c.get("filetype") == "file"]
    by_name = {c["filename"]: c for c in files}

    spec_row = by_name.get(kbview.SPEC_FILE)
    view, payload = _spec_payload(
        spec_row["headers"] if spec_row else None,
        spec_row["path"] if spec_row else None,
    )

    guide_row = by_name.get(kbview.GUIDE_FILE)
    entries = kbview.build_entries(
        [c for c in files if kbview.is_entry(c["filename"])], view
    )

    # Subdirectories are listed even though the tree already has them, and the
    # duplication is the lesser problem. Without them a directory whose only
    # children are folders renders as "Nothing here yet." over a `recipes/`
    # that plainly exists - which is the same lie as a filter, arrived at by
    # omission rather than by design.
    dirs = [
        {"path": c["path"], "name": c["filename"]}
        for c in children
        if c.get("filetype") == kb.DIRECTORY_FILETYPE
        and not c["filename"].startswith(".")
    ]

    return {
        "path": dir_path,
        "guide": (
            {"path": guide_row["path"], "content": guide_row["body"] or ""}
            if guide_row
            else None
        ),
        **payload,
        "dirs": sorted(dirs, key=lambda d: d["name"]),
        "groups": [asdict(g) for g in kbview.build_groups(entries, view)],
    }


@app.get("/api/kb/spec", dependencies=AUTHENTICATED)
async def kb_spec(path: str = "") -> dict[str, Any]:
    """Just one directory's spec, for rendering a *file* at that level.

    Deliberately not a `page` key bolted onto `/api/kb/file`: that would put a
    second lookup on every tree click, which is the per-click cost ADR 0016
    split `_PATHS_CTE` to remove. This is one row by path, and the client
    caches it per directory.
    """
    dir_path = path.strip("/")
    source = f"{dir_path}/{kbview.SPEC_FILE}" if dir_path else kbview.SPEC_FILE
    raw = await kb.sql_read_headers(source)
    return _spec_payload(raw, source if raw is not None else None)[1]


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
@app.get("/c/{conversation_id}", dependencies=AUTHENTICATED)
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
    means a copied URL is the URL you land on. `/c/{conversation_id}` is the
    same trick for the conversation the chat pane opens - see
    docs/decisions/0017.
    """
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# The MCP surface, for a machine caller rather than a browser. Mounted rather
# than routed, because it is a whole ASGI app - which is also why it carries its
# own authentication: a mount does not run this app's dependencies, so the
# `dependencies=AUTHENTICATED` used everywhere above would silently never fire
# here. See app/mcp_server.py and docs/decisions/0014-the-machine-is-a-caller.md.
app.mount("/mcp", mcp_server.asgi_app(), name="mcp")
