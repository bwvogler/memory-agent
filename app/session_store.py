"""Postgres-backed SessionStore for agent transcripts.

By default the agent SDK writes session transcripts to local disk, where they
are lost on restart, scale-down, or a move to another node. A SessionStore
mirrors them to durable storage.

Three things worth knowing about how SessionStore behaves:

  * Transcripts only. It does NOT mirror CLAUDE.md memory files or other
    working-directory artifacts. Those need their own strategy - in this
    project, they live in the TigerFS mount (docs/decisions/0004).
  * Mirror, not replacement. The subprocess writes to local disk first and the
    store receives a copy of each batch; local writes stay authoritative.
  * Rejected batches are retried up to three times, then dropped with a
    `{"type": "system", "subtype": "mirror_error"}` message. Alert on those if
    durability matters to you.

Reference: https://code.claude.com/docs/en/agent-sdk/session-storage

This adapter reuses the same database that backs the knowledge base, so a
deployment needs exactly one Postgres.
"""

from __future__ import annotations

import logging

import asyncpg

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id TEXT PRIMARY KEY,
    user_email TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_session_lines (
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
    seq BIGSERIAL,
    line JSONB NOT NULL,
    written_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, seq)
);

CREATE INDEX IF NOT EXISTS agent_session_lines_session_idx
    ON agent_session_lines (session_id, seq);

-- The skill-usage ledger (app/signals.py). Deliberately here rather than in a
-- store of its own: this class already owns the durable-Postgres role and the
-- pool, and one more small table is cheaper than a second connection path.
--
-- Every turn is recorded, not just failing ones. A skill that loads on every
-- turn appears in every failure by construction, so a table of failures alone
-- cannot distinguish a bad skill from a common one.
CREATE TABLE IF NOT EXISTS turn_outcomes (
    turn_id TEXT PRIMARY KEY,
    user_email TEXT,
    outcome TEXT NOT NULL,
    terminal_reason TEXT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS turn_skill_uses (
    turn_id TEXT NOT NULL REFERENCES turn_outcomes(turn_id) ON DELETE CASCADE,
    skill TEXT NOT NULL,
    PRIMARY KEY (turn_id, skill)
);

CREATE INDEX IF NOT EXISTS turn_skill_uses_skill_idx ON turn_skill_uses (skill);

-- Household-shared, durable chat conversations (app/conversations.py). A
-- conversation is the unit a reload rejoins and multiple people watch live;
-- see docs/decisions/0017. `data` is TEXT, not JSONB: Event.data is sometimes
-- a JSON payload and sometimes a raw string (text_delta, session), and
-- app/main.py's _sse_escape depends on that distinction - round-tripping
-- through jsonb would reorder keys and break byte-identical replay.
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    session_id TEXT,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS conversation_events (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq BIGINT NOT NULL,
    turn_id TEXT,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    actor TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (conversation_id, seq)
);

CREATE TABLE IF NOT EXISTS conversation_turns (
    turn_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    actor_email TEXT NOT NULL,
    savepoint TEXT,
    state TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS conversation_turns_conv_idx
    ON conversation_turns (conversation_id);
"""


class PostgresSessionStore:
    """Minimal SessionStore: append transcript batches, read them back.

    The SDK's SessionStore protocol is small but versioned; if your SDK release
    expects different method names, adapt here - nothing else in the project
    touches transcript persistence.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA)
        log.info("session store ready")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    def _ready(self) -> asyncpg.Pool:
        """The pool, or a message saying what actually went wrong.

        Every method below used to open with `assert self._pool is not None`.
        python -O strips asserts, and then the same methods fail with
        "NoneType has no attribute acquire" - which says nothing about the
        store never having been started. Startup already tolerates an
        unreachable database (see _start_session_store in main), so being
        called before start() is a reachable state, not an impossible one.
        """
        if self._pool is None:
            raise RuntimeError(
                "session store used before start() succeeded; "
                "check /healthz for transcripts=unavailable"
            )
        return self._pool

    async def register(self, session_id: str, user_email: str) -> None:
        async with self._ready().acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_sessions (session_id, user_email)
                VALUES ($1, $2)
                ON CONFLICT (session_id)
                DO UPDATE SET updated_at = now()
                """,
                session_id,
                user_email,
            )

    async def append(self, session_id: str, lines: list[str]) -> None:
        """Append a batch of JSONL transcript lines."""
        if not lines:
            return
        async with self._ready().acquire() as conn, conn.transaction():
            await conn.executemany(
                "INSERT INTO agent_session_lines (session_id, line) "
                "VALUES ($1, $2::jsonb)",
                [(session_id, line) for line in lines],
            )
            await conn.execute(
                "UPDATE agent_sessions SET updated_at = now() WHERE session_id = $1",
                session_id,
            )

    async def read(self, session_id: str) -> list[str]:
        async with self._ready().acquire() as conn:
            rows = await conn.fetch(
                "SELECT line::text FROM agent_session_lines "
                "WHERE session_id = $1 ORDER BY seq ASC",
                session_id,
            )
        return [r[0] for r in rows]

    # -- skill-usage ledger -------------------------------------------------

    async def record_turn_outcome(
        self,
        turn_id: str,
        user_email: str,
        outcome: str,
        terminal_reason: str | None,
        skills: list[str],
    ) -> None:
        """Record one finished turn and the skills it used."""
        async with self._ready().acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO turn_outcomes
                    (turn_id, user_email, outcome, terminal_reason)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (turn_id) DO UPDATE
                    SET outcome = EXCLUDED.outcome,
                        terminal_reason = EXCLUDED.terminal_reason
                """,
                turn_id,
                user_email,
                outcome,
                terminal_reason,
            )
            if skills:
                await conn.executemany(
                    "INSERT INTO turn_skill_uses (turn_id, skill) "
                    "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    [(turn_id, skill) for skill in skills],
                )

    async def mark_turn_outcome(self, turn_id: str, outcome: str) -> None:
        """Overwrite a finished turn's outcome, e.g. when it is reverted."""
        async with self._ready().acquire() as conn:
            await conn.execute(
                "UPDATE turn_outcomes SET outcome = $2 WHERE turn_id = $1",
                turn_id,
                outcome,
            )

    async def skill_signal_summary(self) -> list[dict]:
        """Per-skill turn counts by outcome - the denominator kb-3sv needs.

        `reverted` alone says nothing: a skill loaded on every turn is present
        in every revert. `turns` is what makes the rate meaningful.
        """
        async with self._ready().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT u.skill,
                       count(*)                                        AS turns,
                       count(*) FILTER (WHERE o.outcome = 'reverted')  AS reverted,
                       count(*) FILTER (WHERE o.outcome = 'error')     AS errored,
                       count(*) FILTER (WHERE o.outcome = 'max_turns') AS max_turns
                FROM   turn_skill_uses u
                JOIN   turn_outcomes o ON o.turn_id = u.turn_id
                GROUP  BY u.skill
                ORDER  BY reverted DESC, turns DESC
                """
            )
        return [dict(r) for r in rows]

    async def turn_totals(self) -> dict:
        """Overall turn counts by outcome, for context around the per-skill rates."""
        async with self._ready().acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT count(*)                                      AS turns,
                       count(*) FILTER (WHERE outcome = 'reverted')  AS reverted,
                       count(*) FILTER (WHERE outcome = 'error')     AS errored,
                       count(*) FILTER (WHERE outcome = 'max_turns') AS max_turns
                FROM   turn_outcomes
                """
            )
        return dict(row) if row else {}

    async def sessions_for(self, user_email: str, limit: int = 20) -> list[dict]:
        async with self._ready().acquire() as conn:
            rows = await conn.fetch(
                "SELECT session_id, created_at, updated_at FROM agent_sessions "
                "WHERE user_email = $1 ORDER BY updated_at DESC LIMIT $2",
                user_email,
                limit,
            )
        return [dict(r) for r in rows]

    # -- conversations --------------------------------------------------
    #
    # A conversation is household-shared (docs/decisions/0012), so unlike
    # everything above it these methods take no user_email filter - every
    # allowlisted member sees every conversation. See docs/decisions/0017.

    async def create_conversation(
        self, conversation_id: str, created_by: str, title: str | None = None
    ) -> None:
        async with self._ready().acquire() as conn:
            await conn.execute(
                "INSERT INTO conversations (id, created_by, title) "
                "VALUES ($1, $2, $3) ON CONFLICT (id) DO NOTHING",
                conversation_id,
                created_by,
                title,
            )

    async def list_conversations(self, *, limit: int = 100) -> list[dict]:
        """Newest first. `last_actor` is whoever started the most recent turn."""
        async with self._ready().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.id, c.title, c.created_by, c.created_at, c.updated_at,
                       c.archived,
                       (SELECT t.actor_email FROM conversation_turns t
                        WHERE t.conversation_id = c.id
                        ORDER BY t.started_at DESC LIMIT 1) AS last_actor
                FROM conversations c
                WHERE NOT c.archived
                ORDER BY c.updated_at DESC
                LIMIT $1
                """,
                limit,
            )
        return [dict(r) for r in rows]

    async def get_conversation(self, conversation_id: str) -> dict | None:
        async with self._ready().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, title, session_id, created_by, created_at, "
                "updated_at, archived FROM conversations WHERE id = $1",
                conversation_id,
            )
        return dict(row) if row else None

    async def set_conversation_session(
        self, conversation_id: str, session_id: str
    ) -> None:
        async with self._ready().acquire() as conn:
            await conn.execute(
                "UPDATE conversations SET session_id = $2, updated_at = now() "
                "WHERE id = $1",
                conversation_id,
                session_id,
            )

    async def set_conversation_title(self, conversation_id: str, title: str) -> None:
        async with self._ready().acquire() as conn:
            await conn.execute(
                "UPDATE conversations SET title = $2 WHERE id = $1",
                conversation_id,
                title,
            )

    async def set_conversation_title_if_unset(
        self, conversation_id: str, title: str
    ) -> bool:
        """Auto-titling's write: never overwrites a title a person set (by
        hand, via set_conversation_title) or one an earlier turn already
        generated. Returns whether this call was the one that set it - the
        caller uses that to decide whether to push a live update, since two
        titling attempts racing (two turns finishing close together) must
        not both announce a change."""
        async with self._ready().acquire() as conn:
            result = await conn.execute(
                "UPDATE conversations SET title = $2 "
                "WHERE id = $1 AND (title IS NULL OR title = '')",
                conversation_id,
                title,
            )
        return result == "UPDATE 1"

    async def set_conversation_archived(
        self, conversation_id: str, *, archived: bool
    ) -> None:
        async with self._ready().acquire() as conn:
            await conn.execute(
                "UPDATE conversations SET archived = $2 WHERE id = $1",
                conversation_id,
                archived,
            )

    async def max_conversation_seq(self, conversation_id: str) -> int:
        """The highest seq already durable, or 0. Seeds a Conversation's
        in-process counter after a restart so seq assignment stays monotonic."""
        async with self._ready().acquire() as conn:
            value = await conn.fetchval(
                "SELECT max(seq) FROM conversation_events WHERE conversation_id = $1",
                conversation_id,
            )
        return int(value) if value is not None else 0

    async def append_conversation_events(
        self, conversation_id: str, events: list[dict]
    ) -> None:
        """Batch-insert a flush of events. Idempotent on (conversation_id, seq),
        because a flush that fails partway is retried whole by the caller."""
        if not events:
            return
        async with self._ready().acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO conversation_events
                    (conversation_id, seq, turn_id, kind, data, actor)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (conversation_id, seq) DO NOTHING
                """,
                [
                    (
                        conversation_id,
                        e["seq"],
                        e.get("turn_id"),
                        e["kind"],
                        e["data"],
                        e.get("actor"),
                    )
                    for e in events
                ],
            )
            await conn.execute(
                "UPDATE conversations SET updated_at = now() WHERE id = $1",
                conversation_id,
            )

    async def read_conversation_events(
        self, conversation_id: str, after_seq: int = 0
    ) -> list[dict]:
        async with self._ready().acquire() as conn:
            rows = await conn.fetch(
                "SELECT seq, turn_id, kind, data, actor FROM conversation_events "
                "WHERE conversation_id = $1 AND seq > $2 ORDER BY seq ASC",
                conversation_id,
                after_seq,
            )
        return [dict(r) for r in rows]

    async def create_conversation_turn(
        self, turn_id: str, conversation_id: str, actor_email: str
    ) -> None:
        async with self._ready().acquire() as conn:
            await conn.execute(
                "INSERT INTO conversation_turns "
                "(turn_id, conversation_id, actor_email, state) "
                "VALUES ($1, $2, $3, 'running') "
                "ON CONFLICT (turn_id) DO NOTHING",
                turn_id,
                conversation_id,
                actor_email,
            )

    async def set_conversation_turn_savepoint(
        self, turn_id: str, savepoint: str
    ) -> None:
        async with self._ready().acquire() as conn:
            await conn.execute(
                "UPDATE conversation_turns SET savepoint = $2 WHERE turn_id = $1",
                turn_id,
                savepoint,
            )

    async def finish_conversation_turn(self, turn_id: str, state: str) -> None:
        async with self._ready().acquire() as conn:
            await conn.execute(
                "UPDATE conversation_turns SET state = $2, ended_at = now() "
                "WHERE turn_id = $1",
                turn_id,
                state,
            )
