# memory-agent

A personal LLM wiki agent backed by TigerFS (FUSE → PostgreSQL). Users chat
with the agent; it writes and maintains a structured markdown knowledge base in
the TigerFS workspace.

## Architecture

- `app/main.py` — FastAPI app, routes, startup lifespan
- `app/agent.py` — agent construction: system prompt, skill loading, session options
- `app/kb.py` — TigerFS helpers: mount health, SQL queries, scratch dirs, savepoints
- `app/config.py` — all config read from environment variables
- `skills/kb-curator/SKILL.md` — universal wiki-maintenance skill loaded into every agent session
- `bootstrap/` — skill files seeded into the KB on first startup (ingest, lint); editable in the KB
- `static/` — web UI (chat at `/`, wiki view at `/kb`)

## Key design decisions

**Scratch vs. KB.** The agent's cwd is `$WORK_DIR/{user_slug}/` (local disk),
NOT the KB mount. This keeps temp files out of the versioned wiki. KB writes
must use absolute paths to `$KB_MOUNT/memory/`.

**Two-layer system prompt.** The universal layer in `agent.py` covers TigerFS
mechanics and the workspace path. The per-instance layer is loaded at runtime
from `$KB_MOUNT/memory/AGENT_GUIDE.md` — a plain markdown file the human edits
(by asking the agent) to shape behavior for their specific wiki without a
redeploy. Both are injected as appended system prompt text; neither relies on
the SDK's CLAUDE.md discovery (which is disabled via `setting_sources=[]` and
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` for multi-tenant isolation).

**Hierarchical conventions.** Directory-specific format requirements live in a
`GUIDE.md` inside each subdirectory (e.g. `memory/recipes/GUIDE.md`). The
agent reads these before writing. The human creates them by asking the agent.

**Bootstrap skills.** `bootstrap/skills/` contains example skills (ingest,
lint) seeded into `memory/skills/` at startup if absent. They live in the KB
so the human can edit and improve them over time. They are NOT auto-loaded into
every session — the user invokes them explicitly.

**Memory.** `memory/CLAUDE.md` is short-lived accumulated notes — high-signal
facts the agent wrote down to survive across conversations. The agent prunes it
when it touches it. It is separate from `AGENT_GUIDE.md`, which is the stable
operator-written schema document.

## Local dev

```sh
cp .env.example .env   # fill in ANTHROPIC_API_KEY, KB_DATABASE_URL
docker compose up
```

Chat: http://localhost:8000  
Wiki: http://localhost:8000/kb

## Environment variables

See `app/config.py` for the full list. Required: `ANTHROPIC_API_KEY`,
`KB_DATABASE_URL`. Notable optional: `KB_MOUNT` (default `/mnt/kb`),
`WORK_DIR` (default `/work`), `AGENT_MODEL` (default `claude-sonnet-4-6`).

## Deploying

```sh
fly deploy
```

Bootstrap seeding (`AGENT_GUIDE.md`, `memory/skills/`) runs automatically at
startup when the KB mount is live. It is idempotent — existing files are never
overwritten.
