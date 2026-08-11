"""HTTP surface: submit a turn, stream it back, revert it.

Auth is handled by Cloudflare Access in front of this process, and verified
again here (see app/auth.py for why both).
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import agent, kb
from .auth import Identity, current_identity
from .config import config
from .session_store import PostgresSessionStore
from .turns import TurnState, registry

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("memory-agent")

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")

HEARTBEAT_SECONDS = 15  # Cloudflare's 524 is a time-to-next-byte timeout (~125s).

store: PostgresSessionStore | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
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

    global store
    if config.session_database_url:
        store = PostgresSessionStore(config.session_database_url)
        try:
            await store.start()
        except Exception:
            log.exception("session store unavailable; transcripts will not be durable")
            store = None

    yield

    if store:
        await store.close()


app = FastAPI(title="memory-agent", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    """Unauthenticated liveness probe.

    Exposes `busy` so an external pinger can avoid suspending the host while a
    detached turn is still running. See the "Do not suspend mid-turn" section
    of docs/architecture.md - this endpoint is the hook, not the solution.
    """
    mounted = kb.is_mounted()
    return JSONResponse(
        {
            "ok": mounted,
            "kb_mounted": mounted,
            "control_surface": kb.probe_control_surface(),
            "busy": registry.any_running(),
        },
        status_code=200 if mounted else 503,
    )


@app.get("/api/me")
async def me(identity: Identity = Depends(current_identity)):
    return {"email": identity.email}


@app.post("/api/turns")
async def create_turn(request: Request, identity: Identity = Depends(current_identity)):
    """Submit a message. Returns immediately with a turn id.

    The agent runs detached so that no single HTTP request has to survive for
    the whole turn.
    """
    body = await request.json()
    prompt = (body.get("message") or "").strip()
    if not prompt:
        raise HTTPException(400, "message is required")
    resume = body.get("session_id") or None

    turn = registry.create(user_email=identity.email, session_id=resume)
    asyncio.create_task(
        agent.run_turn(turn, prompt=prompt, user_slug=identity.slug, resume=resume)
    )
    return JSONResponse({"turn_id": turn.id}, status_code=202)


@app.get("/api/turns/{turn_id}")
async def get_turn(turn_id: str, identity: Identity = Depends(current_identity)):
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
    turn_id: str, request: Request, identity: Identity = Depends(current_identity)
):
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
                yield f"id: {event.seq}\nevent: {event.kind}\ndata: {_sse_escape(event.data)}\n\n"

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
async def revert_turn(turn_id: str, identity: Identity = Depends(current_identity)):
    """Roll the knowledge base back to this turn's savepoint.

    Safe to expose in the UI: TigerFS undo is itself reversible.
    """
    turn = registry.get(turn_id)
    if not turn or turn.user_email != identity.email:
        raise HTTPException(404, "no such turn")
    if not turn.savepoint:
        raise HTTPException(409, "this turn has no savepoint to revert to")

    ok = await kb.undo_to_savepoint(turn.savepoint)
    if not ok:
        raise HTTPException(500, "undo failed; check the server log")
    return {"reverted_to": turn.savepoint}


@app.get("/api/sessions")
async def list_sessions(identity: Identity = Depends(current_identity)):
    if not store:
        return {"sessions": []}
    return {"sessions": await store.sessions_for(identity.email)}


@app.get("/api/kb/log")
async def kb_log(identity: Identity = Depends(current_identity)):
    """Recent knowledge-base operations, with per-user attribution."""
    return {"entries": await kb.recent_log()}


@app.get("/api/kb/files")
async def kb_files(identity: Identity = Depends(current_identity)):
    """List markdown files in the KB workspace via git index (no SQL round trips)."""
    # Committed files from the local git index (zero SQL).
    _, committed, _ = await kb._run(*kb._git_args(), "ls-files", "*.md")
    # Untracked files not yet in a savepoint (one TigerFS directory scan).
    _, untracked, _ = await kb._run(
        *kb._git_args(), "ls-files", "--others", "--exclude-standard", "*.md"
    )
    seen: set[str] = set()
    files: list[str] = []
    for line in (committed + "\n" + untracked).splitlines():
        f = line.strip()
        if f.endswith(".md") and f not in seen:
            seen.add(f)
            files.append(f)
    return {"files": sorted(files)}


@app.get("/api/kb/file")
async def kb_file(path: str, identity: Identity = Depends(current_identity)):
    """Return raw markdown for a KB file. Path is relative to the workspace."""
    def _read():
        root = kb.workspace_root()
        target = (root / path).resolve()
        if not str(target).startswith(str(root.resolve())):
            return None, 403
        if not target.exists() or not target.is_file():
            return None, 404
        return target.read_text(encoding="utf-8"), 200

    content, status = await asyncio.to_thread(_read)
    if status == 403:
        raise HTTPException(403, "path outside workspace")
    if status == 404:
        raise HTTPException(404, "not found")
    return {"path": path, "content": content}


def _sse_escape(text: str) -> str:
    """SSE data lines cannot contain raw newlines."""
    return text.replace("\r\n", "\n").replace("\n", "\\n")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/kb")
@app.get("/kb/{path:path}")
async def kb_ui(identity: Identity = Depends(current_identity)):
    return FileResponse(os.path.join(STATIC_DIR, "kb.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
