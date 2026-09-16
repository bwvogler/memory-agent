"""Hybrid search over the wiki: lexical always on, semantic gated on a key.

See docs/decisions/0020 for the two invariants this file is built to hold:

1. Search RANKS; it never LISTS. Glob and Grep stay the only way to answer
   "what is there" (ADR 0018's argument against a `filter`). The tool
   description, the system-prompt text and `render_results` all say so.
2. Staleness is self-detecting, by construction. `bootstrap/skills/views/
   SKILL.md` argues against a persisted derived index because "there is
   nothing to keep it honest." The fusion query answers that with a
   read-time liveness join back to `tigerfs.memory` on `modified_at`: a
   chunk whose file has been written, moved or deleted since indexing does
   not match, so it is simply not returned. Under-inclusive in that window,
   which is the safe direction; never wrong.

This file WRITES (to `kb_chunks`, a table of its own), unlike app/kb.py,
whose whole SQL section rests on issuing zero writes. Kept separate so both
invariants stay literally true. It never touches `tigerfs.memory` itself.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import embed, kb

if TYPE_CHECKING:
    from claude_agent_sdk.types import McpSdkServerConfig

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Needs nothing beyond ordinary CREATE TABLE privilege. Split from
# SCHEMA_DENSE below because CREATE EXTENSION needs a privilege this half
# does not, and a missing one must not abort this half too - see start().
SCHEMA_LEXICAL = """
CREATE TABLE IF NOT EXISTS kb_chunks (
    file_id            uuid        NOT NULL,
    chunk_index        int         NOT NULL,
    body_sha           text        NOT NULL,
    source_modified_at timestamptz NOT NULL,
    path               text        NOT NULL,
    heading            text        NOT NULL DEFAULT '',
    content            text        NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    indexed_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (file_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS kb_chunks_tsv_idx ON kb_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS kb_chunks_live_idx
    ON kb_chunks (file_id, source_modified_at);
"""
# The generated column MUST use the two-argument to_tsvector('english', ...).
# The one-argument form is STABLE, not IMMUTABLE, and Postgres refuses it in
# a generated column with an error that does not say why.

SCHEMA_DENSE = f"""
ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS embedding vector({embed.DIMENSIONS});
CREATE INDEX IF NOT EXISTS kb_chunks_hnsw_idx
    ON kb_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
"""
# m/ef_construction are pgvector's own defaults, and also what the reference
# repo (scalefreegan/hybrid-rag) ships - keeping them means the one thing that
# changes if this is ever tuned is a number, not an argument. vector_cosine_ops
# matches `<=>`; Voyage returns L2-normalised vectors, so cosine and inner
# product agree, and cosine is the one that stays correct if that ever stops
# being true.

# Rewritten by kb.export_backlog after EVERY turn - indexing it would mean
# every turn re-embeds the whole ledger projection for zero retrieval value.
UNINDEXED = frozenset({"backlog.md"})

MAX_CHUNK_CHARS = 2000
MIN_CHUNK_CHARS = 200
MAX_FRONTMATTER_CHARS = 200

RRF_K = 60  # Cormack, Clarke & Buttcher 2009 - the published default.
OVERFETCH = 3  # candidates fetched per side before the liveness filter

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)

# --------------------------------------------------------------------------
# Module state - synchronous and cache-only, like app/mcp_catalog.py's
# equivalents, so status() is safe to call from /healthz at any time.
# --------------------------------------------------------------------------

_state: str = "unavailable"  # "unavailable" | "lexical" | "hybrid"
_stats: dict[str, Any] = {"chunks": 0, "embedded": 0, "last_pass": None}
_reindex_lock = asyncio.Lock()


async def start() -> None:
    """Create the search schema. Never raises; degrades in two steps.

    Not folded into session_store.SCHEMA, for three independent reasons: (1)
    that runs against SESSION_DATABASE_URL, and kb_chunks must live in
    KB_DATABASE_URL because the fusion query joins tigerfs.memory in the same
    statement, and Postgres has no cross-database join; (2) search needs
    partial success - table yes, extension no - as a first-class outcome,
    which one conn.execute(SCHEMA) cannot express; (3) CREATE EXTENSION
    vector needs a privilege nothing else in session_store.SCHEMA needs, and
    folded in, a missing privilege would abort that whole statement and take
    agent_sessions down with a search feature.
    """
    global _state  # noqa: PLW0603 - one search state per process, like _kb_pool
    try:
        conn_pool = await kb.pool()
        async with conn_pool.acquire() as conn:
            await conn.execute(SCHEMA_LEXICAL)
    except Exception:
        log.exception(
            "search schema unavailable; search is off. "
            "/healthz reports this under search.state=unavailable"
        )
        _state = "unavailable"
        return
    _state = "lexical"
    if not embed.enabled():
        log.info(
            "VOYAGE_API_KEY unset: search is lexical-only and no wiki text "
            "leaves this process"
        )
        return
    try:
        conn_pool = await kb.pool()
        async with conn_pool.acquire() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            await conn.execute(SCHEMA_DENSE)
    except Exception:
        log.exception("pgvector unavailable; search stays lexical-only")
        return
    _state = "hybrid"


def status() -> dict[str, Any]:
    """Per-process search readiness for /healthz. Synchronous, cache-only.

    `state` is the one field a monitor should read - `hybrid`, `lexical` or
    `unavailable` - set to the worst thing currently known, same convention
    as app/mcp_catalog.py's status(). Never folded into the top-level `ok`:
    an unset key is a correct, supported deployment and a restart would not
    change it, but it must be VISIBLE, or "lexical-only" and "broken" become
    indistinguishable from outside.
    """
    return {"state": _state, **_stats}


# ---------------------------------------------------------------------------
# Chunking - pure, so it is fully covered by the fast test tier
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    index: int
    heading: str
    content: str  # what gets embedded AND fed to to_tsvector


def _frontmatter_line(headers: dict) -> str:
    """One line of `key: value | key: value`, from already-parsed frontmatter.

    TigerFS parses frontmatter on the way in, so this is a dict walk and not
    a YAML parse - the same fact ADR 0018 relies on. Deliberately a small
    local formatter rather than reaching into kbview's private `_scalar_text`
    across a module boundary.
    """
    parts = []
    for key, value in headers.items():
        text = _scalar_text(value)
        if text:
            parts.append(f"{key}: {text}")
    line = " | ".join(parts)
    if len(line) > MAX_FRONTMATTER_CHARS:
        line = line[: MAX_FRONTMATTER_CHARS - 1].rstrip() + "…"
    return line


def _scalar_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(t for t in (_scalar_text(v) for v in value) if t)
    return ""


def _split_oversized(text: str, limit: int) -> list[str]:
    """Split one section over the char cap on paragraph boundaries."""
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return [text[:limit]] if text.strip() else []
    pieces: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > limit and current:
            pieces.append(current)
            current = para
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def chunk_document(
    path: str, title: str | None, headers: dict, body: str
) -> list[Chunk]:
    """Split one file's body into chunks carrying its real hierarchy.

    Unlike the reference repo (scalefreegan/hybrid-rag), this does not
    synthesise a disclosure hierarchy - this wiki already has one (the
    directory tree, VIEW.md, GUIDE.md, index pages), and a generated second
    one would be exactly the drifting derived artifact
    bootstrap/skills/views/SKILL.md argues against.

    Every chunk's content carries a three-line prefix: the live path and
    title, the frontmatter (proper nouns for the lexical half), and the
    heading trail (topical frame for the dense half). That prefix is why
    hybrid earns itself here - `ragu`, `italian`, a person's name - are
    exactly the query class where lexical beats dense on a household wiki.
    """
    if not body or not body.strip() or path in UNINDEXED:
        return []

    fm_line = _frontmatter_line(headers) if headers else ""

    headings = list(_HEADING_RE.finditer(body))
    sections: list[tuple[str, str]] = []  # (heading trail, section text)
    if not headings:
        sections.append(("", body.strip()))
    else:
        trail: list[tuple[int, str]] = []  # (level, text) stack
        first = headings[0]
        if first.start() > 0:
            lead = body[: first.start()].strip()
            if lead:
                sections.append(("", lead))
        for i, m in enumerate(headings):
            level = len(m.group(1))
            heading_text = m.group(2).strip()
            trail = [*(t for t in trail if t[0] < level), (level, heading_text)]
            # The section's text INCLUDES its own heading line (start = the
            # heading match's start, not its end) - otherwise merging two
            # small sections into one chunk would drop the second section's
            # heading text entirely from both the embedded and the tsvector'd
            # content, which is the topical frame the dense half needs.
            start = m.start()
            end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
            section_text = body[start:end].strip()
            trail_str = " > ".join(t for _, t in trail)
            sections.append((trail_str, section_text))

    # Greedily merge sections up to MAX_CHUNK_CHARS; split an oversized
    # section on paragraph boundaries; merge a too-small fragment forward.
    merged: list[tuple[str, str]] = []
    pending_heading = ""
    pending_text = ""
    for heading_trail, text in sections:
        if not text:
            continue
        candidate_len = len(pending_text) + len(text) + 2
        if pending_text and candidate_len > MAX_CHUNK_CHARS:
            merged.append((pending_heading, pending_text))
            pending_heading, pending_text = heading_trail, text
        elif pending_text:
            pending_text = f"{pending_text}\n\n{text}"
            # Always advance to the MOST RECENT section's trail, not just the
            # first: for the common case of nested headers with no body text
            # of their own ("# A" then immediately "## B" then "### C"), this
            # is what makes the reported heading the deepest, most specific
            # one active - the one a search result should actually show.
            pending_heading = heading_trail
        else:
            pending_heading, pending_text = heading_trail, text
        if len(pending_text) < MIN_CHUNK_CHARS:
            continue
        if len(pending_text) > MAX_CHUNK_CHARS:
            pieces = _split_oversized(pending_text, MAX_CHUNK_CHARS)
            merged.extend((pending_heading, piece) for piece in pieces[:-1])
            pending_text = pieces[-1] if pieces else ""
    if pending_text:
        merged.append((pending_heading, pending_text))

    prefix_lines = [f"{path} — {title}" if title else path]
    if fm_line:
        prefix_lines.append(fm_line)

    chunks = []
    for idx, (heading_trail, text) in enumerate(merged):
        lines = list(prefix_lines)
        if heading_trail:
            lines.append(heading_trail)
        content = "\n".join(lines) + "\n\n" + text
        chunks.append(Chunk(index=idx, heading=heading_trail, content=content))
    return chunks


# ---------------------------------------------------------------------------
# Reindex - the diff that makes staleness self-detecting (invariant 2)
# ---------------------------------------------------------------------------

# One query, no separate progress list anywhere. `sha256` is built into
# Postgres 11+ (no pgcrypto). The hash covers title and headers as well as
# body, since a frontmatter-only edit changes the chunk prefix and nothing
# else. NOT EXISTS against a matching (file_id, modified_at) pair is the same
# liveness idea the fusion query uses on the read side - a file already
# indexed at its current modified_at needs no work.
_DIFF_SQL = (
    kb.PATHS_CTE  # noqa: S608 - module constant, no caller input
    + """
SELECT p.path, m.id AS file_id, m.title, m.headers, m.body, m.modified_at,
       encode(sha256(convert_to(
           coalesce(m.body,'') || coalesce(m.title,'') || coalesce(m.headers::text,''),
           'UTF8')), 'hex') AS body_sha
FROM   paths p
JOIN   tigerfs.memory m ON m.id = p.id
WHERE  p.filetype = 'file' AND p.path LIKE '%.md'
  AND  NOT EXISTS (
           SELECT 1 FROM kb_chunks c
           WHERE  c.file_id = m.id AND c.source_modified_at = m.modified_at
       )
"""
)

_ORPHAN_SWEEP_SQL = """
DELETE FROM kb_chunks c
WHERE NOT EXISTS (SELECT 1 FROM tigerfs.memory m WHERE m.id = c.file_id)
"""

_TOUCH_SQL = """
UPDATE kb_chunks SET source_modified_at = $2 WHERE file_id = $1
"""

_DELETE_CHUNKS_SQL = "DELETE FROM kb_chunks WHERE file_id = $1"

# ON CONFLICT DO UPDATE, not a bare INSERT: this deployment runs one process
# (see CLAUDE.md's "One machine owns the ledger"), but the reindex trigger is
# fired from several independent call sites - a turn finishing, reflection,
# a revert, the boot backfill - each a detached spawn(), so two passes CAN
# overlap in practice. Measured, not theoretical: driving a standalone
# reindex against a freshly-booted stack raced the boot backfill's own pass
# for the same file and hit `UniqueViolationError` on this exact primary key
# before this guard existed. The DELETE immediately above already clears
# stale rows for a file mid-pass, so a conflict here means a genuinely
# concurrent writer, not a stale leftover - upsert is the correct response
# either way, and cheaper than a distributed lock for a race this narrow.
_INSERT_LEXICAL_SQL = """
INSERT INTO kb_chunks (file_id, chunk_index, body_sha, source_modified_at,
                        path, heading, content)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (file_id, chunk_index) DO UPDATE SET
    body_sha = EXCLUDED.body_sha,
    source_modified_at = EXCLUDED.source_modified_at,
    path = EXCLUDED.path,
    heading = EXCLUDED.heading,
    content = EXCLUDED.content,
    indexed_at = now()
"""
# `embedding` is deliberately absent from this SET clause, not set to NULL:
# this path is taken whenever THIS call's embed attempt came back empty
# (no key, or a transient failure - see embed.py), which says nothing about
# whether a CONCURRENT hybrid writer already put a real vector on this exact
# row. Leaving the column out of the UPDATE means a lexical-path conflict can
# never destroy a dense value it does not know about.

_INSERT_HYBRID_SQL = """
INSERT INTO kb_chunks (file_id, chunk_index, body_sha, source_modified_at,
                        path, heading, content, embedding)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector)
ON CONFLICT (file_id, chunk_index) DO UPDATE SET
    body_sha = EXCLUDED.body_sha,
    source_modified_at = EXCLUDED.source_modified_at,
    path = EXCLUDED.path,
    heading = EXCLUDED.heading,
    content = EXCLUDED.content,
    embedding = EXCLUDED.embedding,
    indexed_at = now()
"""


async def reindex() -> int:
    """Diff the live store against kb_chunks and bring it up to date.

    Never raises - logs and returns 0 on any failure, matching app/kb.py's
    posture toward beads: a broken index must not break a turn. A dropped
    pass costs nothing, because this diff is idempotent by content hash; the
    lock only prevents two passes writing at once, which the detached
    `spawn()` call sites make possible.
    """
    if _reindex_lock.locked():
        return 0
    async with _reindex_lock:
        try:
            return await _reindex()
        except Exception:
            log.exception("search reindex pass failed; index left as-is")
            return 0


async def _reindex() -> int:
    if _state == "unavailable":
        return 0
    conn_pool = await kb.pool()
    async with conn_pool.acquire() as conn:
        await conn.execute(_ORPHAN_SWEEP_SQL)
        rows = await conn.fetch(_DIFF_SQL)

    indexed = 0
    dense = _state == "hybrid"
    for row in rows:
        path = row["path"]
        if path in UNINDEXED:
            continue
        file_id = row["file_id"]
        body_sha = row["body_sha"]

        existing = await _existing_sha(file_id)
        if existing == body_sha:
            # A no-op rewrite - agents do this constantly. No Voyage call.
            async with conn_pool.acquire() as conn:
                await conn.execute(_TOUCH_SQL, file_id, row["modified_at"])
            indexed += 1
            continue

        chunks = chunk_document(
            path, row["title"], row["headers"] or {}, row["body"] or ""
        )
        async with conn_pool.acquire() as conn:
            await conn.execute(_DELETE_CHUNKS_SQL, file_id)
        if not chunks:
            indexed += 1
            continue

        vectors: list[list[float]] | None = None
        if dense:
            vectors = await embed.embed_documents([c.content for c in chunks])

        async with conn_pool.acquire() as conn, conn.transaction():
            for i, chunk in enumerate(chunks):
                if vectors is not None:
                    await conn.execute(
                        _INSERT_HYBRID_SQL,
                        file_id,
                        chunk.index,
                        body_sha,
                        row["modified_at"],
                        path,
                        chunk.heading,
                        chunk.content,
                        embed.to_pgvector(vectors[i]),
                    )
                else:
                    await conn.execute(
                        _INSERT_LEXICAL_SQL,
                        file_id,
                        chunk.index,
                        body_sha,
                        row["modified_at"],
                        path,
                        chunk.heading,
                        chunk.content,
                    )
        indexed += 1

    _stats["last_pass"] = _now_iso()
    _stats["last_pass_indexed"] = indexed
    await _refresh_counts()
    return indexed


_EXISTING_SHA_SQL = "SELECT body_sha FROM kb_chunks WHERE file_id = $1 LIMIT 1"


async def _existing_sha(file_id: Any) -> str | None:
    conn_pool = await kb.pool()
    row = await conn_pool.fetchrow(_EXISTING_SHA_SQL, file_id)
    return row["body_sha"] if row else None


async def _refresh_counts() -> None:
    conn_pool = await kb.pool()
    total = await conn_pool.fetchval("SELECT count(*) FROM kb_chunks")
    embedded = (
        await conn_pool.fetchval(
            "SELECT count(*) FROM kb_chunks WHERE embedding IS NOT NULL"
        )
        if _state == "hybrid"
        else 0
    )
    _stats["chunks"] = total or 0
    _stats["embedded"] = embedded or 0


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


# ---------------------------------------------------------------------------
# Fusion query - Reciprocal Rank Fusion over dense + lexical candidates
# ---------------------------------------------------------------------------

_LIVE_CTE = """
live AS (
    SELECT c.file_id, c.chunk_index, c.heading, c.content, p.path
    FROM   kb_chunks c
    JOIN   tigerfs.memory m ON m.id = c.file_id
                           AND m.modified_at = c.source_modified_at
    JOIN   paths p ON p.id = c.file_id
)
"""

_FUSION_HYBRID_SQL = (
    kb.PATHS_CTE  # noqa: S608 - module constant, no caller input
    + """,
q AS (SELECT websearch_to_tsquery('english', $2) AS tsq),
dense_raw AS (
    SELECT file_id, chunk_index, embedding <=> $1::vector AS distance
    FROM   kb_chunks
    WHERE  embedding IS NOT NULL
    ORDER  BY embedding <=> $1::vector
    LIMIT  $3
),
lex_raw AS (
    SELECT c.file_id, c.chunk_index, ts_rank_cd(c.tsv, q.tsq) AS score
    FROM   kb_chunks c, q
    WHERE  c.tsv @@ q.tsq
    ORDER  BY score DESC
    LIMIT  $3
),
"""
    + _LIVE_CTE
    + """,
dense AS (
    SELECT d.file_id, d.chunk_index,
           row_number() OVER (ORDER BY d.distance) AS rank
    FROM   dense_raw d JOIN live USING (file_id, chunk_index)
),
lexical AS (
    SELECT l.file_id, l.chunk_index,
           row_number() OVER (ORDER BY l.score DESC) AS rank
    FROM   lex_raw l JOIN live USING (file_id, chunk_index)
),
fused AS (
    SELECT COALESCE(d.file_id, x.file_id) AS file_id,
           COALESCE(d.chunk_index, x.chunk_index) AS chunk_index,
           -- ::float8, not a bare 1.0: a bare decimal literal is Postgres's
           -- `numeric` type, which asyncpg decodes as a Decimal, and Hit.score
           -- is typed float - Decimal silently survives the dataclass (no
           -- runtime enforcement) all the way to json.dumps(), which cannot
           -- serialise it and stringifies it instead, breaking `score > 0`
           -- comparisons downstream. Cast at the source instead of coercing
           -- every reader of Hit.score.
           COALESCE(1.0::float8 / ($4 + d.rank), 0.0)
               + COALESCE(1.0::float8 / ($4 + x.rank), 0.0)
               AS score,
           d.rank AS dense_rank, x.rank AS lexical_rank
    FROM   dense d
    FULL OUTER JOIN lexical x
           ON d.file_id = x.file_id AND d.chunk_index = x.chunk_index
)
SELECT live.path, live.heading, f.score, f.dense_rank, f.lexical_rank,
       ts_headline('english', live.content, q.tsq,
           'MaxFragments=2, MinWords=8, MaxWords=26, '
           'StartSel=**, StopSel=**, FragmentDelimiter= … ') AS snippet
FROM   fused f
JOIN   live USING (file_id, chunk_index), q
ORDER  BY f.score DESC
LIMIT  $5
"""
)

_FUSION_LEXICAL_SQL = (
    kb.PATHS_CTE  # noqa: S608 - module constant, no caller input
    + """,
q AS (SELECT websearch_to_tsquery('english', $1) AS tsq),
lex_raw AS (
    SELECT c.file_id, c.chunk_index, ts_rank_cd(c.tsv, q.tsq) AS score
    FROM   kb_chunks c, q
    WHERE  c.tsv @@ q.tsq
    ORDER  BY score DESC
    LIMIT  $2
),
"""
    + _LIVE_CTE
    + """,
lexical AS (
    SELECT l.file_id, l.chunk_index,
           row_number() OVER (ORDER BY l.score DESC) AS rank
    FROM   lex_raw l JOIN live USING (file_id, chunk_index)
)
SELECT live.path, live.heading,
       1.0::float8 / ($3 + x.rank) AS score,
       NULL::bigint AS dense_rank, x.rank AS lexical_rank,
       ts_headline('english', live.content, q.tsq,
           'MaxFragments=2, MinWords=8, MaxWords=26, '
           'StartSel=**, StopSel=**, FragmentDelimiter= … ') AS snippet
FROM   lexical x
JOIN   live USING (file_id, chunk_index), q
ORDER  BY score DESC
LIMIT  $4
"""
)


@dataclass(frozen=True)
class Hit:
    path: str
    heading: str
    snippet: str
    score: float
    dense_rank: int | None
    lexical_rank: int | None


async def search(query_text: str, limit: int = 8) -> list[Hit]:
    """Run the fusion query. Empty list on any failure - never raises."""
    if _state == "unavailable":
        return []
    limit = max(1, min(limit, 20))
    over = limit * OVERFETCH
    try:
        conn_pool = await kb.pool()
        vector = await embed.embed_query(query_text) if _state == "hybrid" else None
        async with conn_pool.acquire() as conn:
            if vector is not None:
                rows = await conn.fetch(
                    _FUSION_HYBRID_SQL,
                    embed.to_pgvector(vector),
                    query_text,
                    over,
                    RRF_K,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    _FUSION_LEXICAL_SQL, query_text, over, RRF_K, limit
                )
    except Exception:
        log.exception("search query failed")
        return []
    return [
        Hit(
            path=r["path"],
            heading=r["heading"],
            snippet=r["snippet"] or "",
            # float(), not r["score"] bare: the SQL casts every score
            # expression to float8 so asyncpg hands back a native float, not
            # a Decimal - but Hit.score's `float` annotation is not enforced
            # at runtime, and an unserialisable Decimal reaching main.py's
            # JSON response silently stringifies instead of erroring. Belt
            # and suspenders at the one place every row passes through.
            score=float(r["score"]),
            dense_rank=r["dense_rank"],
            lexical_rank=r["lexical_rank"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Rendering - pure, so it is fully covered by the fast test tier
# ---------------------------------------------------------------------------

_UNAVAILABLE_TEXT = (
    "Search is unavailable right now. Use Glob and Grep, which see the whole "
    "wiki and see it as of this instant."
)

_EMPTY_TEXT = (
    'Nothing matched "{query}". That does not mean the wiki has nothing on '
    "it - this ranks what it has indexed, and a page written earlier in this "
    "turn may not be indexed yet. Try Grep for a specific word, or Glob the "
    "directory you expect it in."
)

_LEXICAL_ONLY_NOTE = (
    "\n(Wording match only; semantic ranking is off in this deployment.)"
)

_HEADER = (
    "{n} passage{s}, ranked by relevance. This RANKS; it does not list. Use "
    "Glob/Grep when you need to know everything that is there.\n"
)


def _provenance(hit: Hit) -> str:
    if hit.dense_rank is not None and hit.lexical_rank is not None:
        return f"[both: dense #{hit.dense_rank}, lexical #{hit.lexical_rank}]"
    if hit.dense_rank is not None:
        return f"[dense #{hit.dense_rank}]"
    if hit.lexical_rank is not None:
        return f"[lexical #{hit.lexical_rank}]"
    return ""


def render_results(hits: list[Hit], query_text: str, state: str) -> str:
    """The text block the MCP tool returns. Pure - see tests/test_search_render.py."""
    if state == "unavailable":
        return _UNAVAILABLE_TEXT
    if not hits:
        return _EMPTY_TEXT.format(query=query_text)

    lines = [_HEADER.format(n=len(hits), s="" if len(hits) == 1 else "s")]
    for i, hit in enumerate(hits, start=1):
        title = f" · {hit.heading}" if hit.heading else ""
        lines.append(f"{i}. {hit.path}{title}")
        prov = _provenance(hit)
        if prov:
            lines.append(f"   {prov}")
        snippet = hit.snippet.replace("\n", " ").strip()
        if snippet:
            lines.append(f"   ...{snippet}...")
        lines.append("")
    text = "\n".join(lines).rstrip()
    if state == "lexical":
        text += _LEXICAL_ONLY_NOTE
    return text


# ---------------------------------------------------------------------------
# The agent-facing MCP tool
# ---------------------------------------------------------------------------

_SEARCH_TOOL_DESCRIPTION = (
    "Rank passages from anywhere in the wiki by meaning and by wording at "
    "once. Use it BEFORE you write: the most expensive failure here is a "
    "second page about something the wiki already covers, and Grep only "
    "finds words you guessed right. It returns a RANKING, not a listing - "
    "anything it did not return still exists. When you need to know what is "
    "actually there, use Glob and Grep, which see everything."
)

_search_server: McpSdkServerConfig | None = None


def search_server() -> McpSdkServerConfig:
    """Build (once) the in-process MCP server carrying the `search` tool.

    Unlike app/interact.py's `ask_server_for`, this closes over nothing turn-
    specific, so it is built once and memoised rather than once per turn.
    Server name `wiki`, tool name `search` -> `mcp__wiki__search`. Not `kb`,
    which would read as this app's own outbound /mcp surface
    (`mcp__kb-prod__query`).
    """
    global _search_server  # noqa: PLW0603 - memoised singleton, like the pattern above
    if _search_server is not None:
        return _search_server

    @tool(
        "search",
        _SEARCH_TOOL_DESCRIPTION,
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What you are looking for, as a phrase or a question. "
                        "Real words, not a regex."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "How many passages to return, 1-20. Default 8.",
                },
            },
            "required": ["query"],
        },
    )
    async def search_tool(args: dict[str, Any]) -> dict[str, Any]:
        query_text = str(args.get("query") or "")
        limit = int(args.get("limit") or 8)
        hits = await search(query_text, limit)
        text = render_results(hits, query_text, _state)
        return {"content": [{"type": "text", "text": text}]}

    _search_server = create_sdk_mcp_server("wiki", tools=[search_tool])
    return _search_server
