"""HTTP surface: submit a turn, stream it back, revert it.

Auth is handled by Cloudflare Access in front of this process, and verified
again here (see app/auth.py for why both).
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import agent, kb, signals
from .auth import Identity, current_identity
from .config import config
from .session_store import PostgresSessionStore
from .turns import TurnState, registry, spawn

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

    global store  # noqa: PLW0603 - the store outlives every request
    if config.session_database_url:
        store = await _start_session_store()
    signals.attach_store(store)

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
        },
        status_code=200 if mounted else 503,
    )


@app.get("/api/me")
async def me(identity: CurrentUser) -> dict[str, str]:
    return {"email": identity.email}


@app.post("/api/turns")
async def create_turn(request: Request, identity: CurrentUser) -> JSONResponse:
    """Submit a message. Returns immediately with a turn id.

    The agent runs detached so that no single HTTP request has to survive for
    the whole turn.
    """
    body = await request.json()
    prompt = (body.get("message") or "").strip()
    images = body.get("images") or []  # list of {"media_type": str, "data": str}
    if not prompt and not images:
        raise HTTPException(400, "message is required")
    resume = body.get("session_id") or None

    turn = registry.create(user_email=identity.email, session_id=resume)
    spawn(
        agent.run_turn(
            turn,
            prompt=prompt,
            user_slug=identity.slug,
            resume=resume,
            images=images or None,
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


@app.post("/api/reflect")
async def reflect(identity: CurrentUser) -> JSONResponse:
    """Run a reflection turn now, instead of waiting for a signal to trigger one.

    This exists because signal-gated reflection alone is untestable at this
    scale. When Stage 3 was built the ledger held 6 turns and zero reverts, so
    a loop that only fires on a signal would have been dead code shipped
    unexercised - the worst way to deploy self-modification. See ADR 0008.
    """
    if registry.any_running():
        raise HTTPException(409, "a turn is already running; try again when idle")
    turn = registry.create(user_email=identity.email)
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


def _sse_escape(text: str) -> str:
    """SSE data lines cannot contain raw newlines."""
    return text.replace("\r\n", "\n").replace("\n", "\\n")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/kb", dependencies=AUTHENTICATED)
@app.get("/kb/{path:path}", dependencies=AUTHENTICATED)
async def kb_ui() -> FileResponse:
    return FileResponse(STATIC_DIR / "kb.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
