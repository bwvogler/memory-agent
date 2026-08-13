# memory-agent

A personal LLM wiki agent backed by TigerFS (FUSE → PostgreSQL). Users chat
with the agent; it writes and maintains a structured markdown knowledge base in
the TigerFS workspace.

## Architecture

- `app/main.py` — FastAPI app, routes, startup lifespan
- `app/agent.py` — agent construction: system prompt, skill loading, session options
- `app/kb.py` — TigerFS helpers: mount health, SQL queries, scratch dirs, savepoints,
  and the beads task ledger
- `app/signals.py` — records which skills each turn used and files a bead when
  a turn is reverted, errors, or is denied a tool
- `app/guards.py` — SDK hooks enforcing two rules the prompt could not: no
  shell command may corrupt a KB file, and no turn may defer work without
  filing it
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
lint) seeded into `memory/skills/` at startup. They live in the KB so the human
can edit and improve them over time. They are NOT auto-loaded into every
session — the user invokes them explicitly.

Seeding tracks a hash of what it last shipped (`.bootstrap-state.json`) so an
improved skill actually reaches existing deployments: an unmodified file is
replaced, a human-edited one is left alone with a warning. Files predating the
state file are never touched, since we cannot tell whether they were edited.

**Task state lives in beads.** `bd` (pinned in the Dockerfile) keeps a
dependency-aware issue graph per user at `$WORK_DIR/{user_slug}/.beads`, on the
volume rather than in the KB — it is an embedded Dolt database and does not
belong on FUSE. Skills file discovered work as beads instead of reporting it
into chat, and `memory/backlog.md` is regenerated from the graph after every
turn as a human-readable projection.

Agent instructions for `bd` come from `bd prime`, injected into the appended
system prompt at turn start. bd would normally install a `SessionStart` hook to
do this, but `setting_sources=[]` means it can never fire, so `run_turn` does it
explicitly. `_BEADS_OVERRIDES` in `agent.py` overrides the two rules where bd
disagrees with this deployment: memory stays in `memory/CLAUDE.md`, and the
agent never runs git. See `docs/decisions/0006`.

**Two rules are enforced by hooks, not by instructions.** `app/guards.py`
passes `PreToolUse` and `Stop` callbacks to the SDK. Because they are Python
objects handed to `ClaudeAgentOptions` rather than `.claude/settings.json`
entries, `setting_sources=[]` does not suppress them and the agent cannot
author them in its writable cwd.

Both exist because a prompt said the right thing and the agent did something
else anyway. `PreToolUse` refuses shell appends into the KB — the mount has no
read-modify-write, so an append zeroes what came before and truncates what came
after, which is how a user's `memory/CLAUDE.md` was once destroyed. `Stop`
refuses to end a turn whose reply defers work ("a follow-up for a later
session") without a `bd` command to match; without it the ledger silently
loses exactly the work it exists to hold (`kb-3cl`).

Every refusal states the safe alternative. A bare denial is what drove the
agent into inventing the shell workaround in the first place, and both guards
fail open — a guard that raised would take down the turn it was protecting.

**Signals are captured, not acted on.** `app/signals.py` observes every turn:
which skills it read, whether it errored, exhausted `max_turns`, or was denied
a tool. A Revert click is the valuable one — an explicit, human-labelled "that
was wrong" bound to an exact turn — and files a bead quoting the prompt, the
skills involved, and the diff that was rolled back.

Nothing consumes these yet, on purpose. Bead `kb-3sv` gates that decision on
looking at real signals first, and blocks every Stage 3 bead. `GET /api/signals`
reports per-skill outcome rates so the gate can be answered with data.

Two rules the code depends on: signal beads are `deferred` so evidence never
reaches `bd ready`, and *every* turn is recorded, not just failing ones —
`kb-curator` loads on every turn, so without the denominator it appears in
every revert by construction. See the amendment in `docs/decisions/0006`.

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

## Tests

```sh
uv venv .venv
uv pip install -r requirements-dev.txt --python .venv/bin/python

.venv/bin/python -m pytest                 # fast units only (~1s)
.venv/bin/python -m pytest --container     # + real Docker stack (~1 min, no API key)
.venv/bin/python -m pytest --live          # + one real agent turn (spends tokens)
```

Both slow tiers are opt-in, so a bare `pytest` needs no Docker, database or API
key. `--live` implies `--container`.

The container tier builds the real image and runs the real stack under its own
compose project name, so its teardown (`down -v`) can never touch a dev stack's
volumes. It exists because this system's failure mode is silence: `kb.py`
deliberately logs-and-continues when beads is unreachable (a turn that cannot
reach its ledger should still answer the user), which means a completely broken
`bd` looks identical to a working one at runtime. Only running it tells the two
apart.

The live tier is the only thing that catches permission regressions —
`permission_mode="acceptEdits"` does not cover Bash, so without the
`allowed_tools` allowlist the agent writes to the wiki normally and is silently
blocked from ever running `bd`. It asserts on mechanism (a bd command ran, the
ledger changed), never on what the model chose to say.

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
