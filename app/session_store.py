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

    async def register(self, session_id: str, user_email: str) -> None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
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
        assert self._pool is not None
        if not lines:
            return
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(
                    "INSERT INTO agent_session_lines (session_id, line) VALUES ($1, $2::jsonb)",
                    [(session_id, line) for line in lines],
                )
                await conn.execute(
                    "UPDATE agent_sessions SET updated_at = now() WHERE session_id = $1",
                    session_id,
                )

    async def read(self, session_id: str) -> list[str]:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT line::text FROM agent_session_lines "
                "WHERE session_id = $1 ORDER BY seq ASC",
                session_id,
            )
        return [r[0] for r in rows]

    async def sessions_for(self, user_email: str, limit: int = 20) -> list[dict]:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT session_id, created_at, updated_at FROM agent_sessions "
                "WHERE user_email = $1 ORDER BY updated_at DESC LIMIT $2",
                user_email,
                limit,
            )
        return [dict(r) for r in rows]
