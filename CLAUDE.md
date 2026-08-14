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
- `app/evolve.py` — bounded self-evolution: what a reflection turn may change
  about its own skills, enforced as a hook, plus the evolution log
- `app/interact.py` — the round-trips to the human (a question tool, a
  permission callback) and the hooks that report tool results and subagents
- `app/mcp_server.py` — the four capabilities as MCP tools, for a machine caller
- `app/config.py` — all config read from environment variables
- `skills/kb-curator/SKILL.md` — universal wiki-maintenance skill loaded into every agent session
- `bootstrap/` — skill files seeded into the KB on first startup (ingest, lint, reflect); editable in the KB
- `static/` — web UI (chat at `/`, wiki view at `/kb`)

## Key design decisions

**Scratch vs. KB.** The agent's cwd is `$WORK_DIR/{user_slug}/` (local disk),
NOT the KB mount. This keeps temp files out of the versioned wiki. KB writes
must use absolute paths to `$KB_MOUNT/memory/`.

**Attachments are files on disk, not content blocks.** An image pasted into the
chat becomes a base64 block in the message, which is right for a screenshot. A
document does not: `POST /api/turns` takes `files: [{name, data}]`, writes each
one to `$WORK_DIR/{user_slug}/uploads/{turn_id}/`, and the prompt names the
paths. A 5 MB CSV then costs nothing until the agent decides to `Read` it, and
`turn.prompt` — which a revert bead quotes — records what the turn was given.

The filename is attacker-controlled and is treated that way: `kb.safe_upload_name`
reduces it to a single path component and `kb.resolve_upload_path` asserts
containment, because `../../.beads/issues.jsonl` would otherwise overwrite the
ledger. Size is the other unbounded input — base64 in a JSON body has no natural
limit, so `MAX_UPLOAD_BYTES` and `MAX_UPLOAD_TOTAL_BYTES` are checked *before*
`registry.create`, so a rejected upload leaves no orphan turn streaming forever.

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

**Bootstrap skills.** `bootstrap/skills/` contains example skills (ingest, lint,
reflect) seeded into `memory/skills/` at startup. They live in the KB so the
human can edit and improve them over time. They are NOT auto-loaded into every
session — the user invokes them explicitly, or `app/mcp_server.py` names them.

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

**One machine, and the reason is the volume.** The ledger, `kb.git` and
per-user scratch all live on `/work`, which attaches to exactly one machine,
while the KB is shared through Postgres. A second machine would diverge on all
three — most dangerously on savepoints, where a revert routed to the wrong
machine finds nothing to roll back for writes that are plainly there. The
ceiling that matters is not hardware anyway: savepoints are a global
`git add -A` over one workspace, so concurrent turns already interfere. See
`docs/decisions/0009`.

That split decides how you reach each tier from a laptop. The KB mounts locally
(`scripts/mount-kb.sh`, which now names the database and makes production
opt-in). The volume needs `scripts/fly.sh` — read-only by default, and the only
place that knows how to wake a suspended machine and get a quoted command past
`flyctl ssh -C`, which runs no shell and strips quotes. `scripts/beads-pull.sh`
goes through it rather than repeating either trick.

**Ideas about the image live in a second ledger, in git.** The agent is the
only user who sees this product from the inside, and most of what it files is
about the app rather than the wiki — which it cannot touch: no repo, no git, a
read-only image. Those beads are labelled `image` and created `deferred`, out of
`bd ready` by the same mechanism signal beads use, then pulled here with
`scripts/beads-pull.sh` into this repo's ledger (`.beads/`, prefix `img`,
`issues.jsonl` committed).

Ids are the join key and are preserved across the pull, so `kb-` in a git diff
means **originated on prod**, not "about the knowledge base" — `img-` means
discovered here. Wiki beads never travel; nothing about the KB's content is
committed to this repo.

The loop closes by deploying: append a line to `docs/shipped-beads.jsonl`, and
`kb.reconcile_shipped_all()` closes that bead in every ledger on the volume at
startup, noting the commit and image ref. No ssh, no human step. Run
`bd export -o .beads/issues.jsonl` before committing ledger changes — the Dolt
database beside it is gitignored, and the JSONL is what git actually tracks.
See `docs/decisions/0010`.

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
See `docs/decisions/0007`.

**The human is a tool the agent can call.** "Headless: nobody is present to
answer a permission prompt" was true of the subprocess and false of the product —
there is a person holding an SSE stream open. `app/interact.py` gives the agent
`mcp__ask__ask_user`, an in-process SDK MCP tool that appends an `ask` event and
blocks on a future which `POST /api/turns/{id}/answer` resolves; `can_use_tool`
does the same for an unapproved tool, answered at
`POST /api/turns/{id}/permission`. `permission_mode` stays `acceptEdits`, so wiki
edits still never prompt.

The callback sees less than "everything outside `allowed_tools`", and this was
measured rather than assumed: the CLI approves some read-only shell commands
itself, ahead of the callback — a live test asking for `echo` ran it and raised no
prompt at all. `WebFetch` does reach the callback, which is what the live tier
asserts on. Do not build anything on a *particular* Bash command prompting; where
that line falls belongs to the CLI.

The built-in `AskUserQuestion` is in `disallowed_tools`, and that is not
tidiness: with no TTY it **resolves instantly with empty answers**
(anthropics/claude-code#50728), so the agent believes it consulted someone who
was never asked.

The two timeouts resolve in **opposite** directions, which is the part to
preserve. An unanswered question returns text telling the agent to proceed and
say that it did; an unanswered permission request is denied. A stalled turn is
the worse failure for the first, and a wasted turn budget for the second.

`can_use_tool` is only honoured in streaming mode — the SDK raises `ValueError`
on a string prompt — so `_stream_prompt` now wraps every turn, not just image
ones. Interactivity lives on the `Turn`, not in config: a browser turn is
interactive, an `/mcp` turn and a reflection turn are not, and a non-interactive
turn gets no callback at all while `ask_user` tells it plainly that it is alone.

A human clicking **Deny** files a `deferred` P3 signal bead, and is subtracted
from `permission_denials` alongside guard denials — without that, a person saying
no filed a P1 bead telling a future reflection to "check `allowed_tools`".
`strict_mcp_config=True` became necessary rather than tidy once `mcp_servers`
existed: `cwd` is the agent's own *writable* scratch, so it could otherwise write
a `.mcp.json` there and grant itself servers. See `docs/decisions/0013`.

**Delegation, and the bug that enabling it would have caused.** `Task` is
allowed, so the generic subagent works, and two named ones are declared:
`kb-query` (read-only) and `kb-lint`. Their prompts *point at* the KB skill file
rather than restating it, because those skills are human-edited and a copy in the
image would drift invisibly. `ingest` and `reflect` are deliberately not
subagents — `ingest` is written to check in with a person mid-task, and
`reflect`'s remit lives in `evolve.write_guard_for` plus four things an
`AgentDefinition` cannot express. Both are reachable headlessly instead.

Switching `Task` on alone would have corrupted the transcript: `_render`
forwarded every `text_delta` regardless of `parent_tool_use_id`, so a subagent's
tokens would have spliced into the middle of a sentence the user was reading.
Subagent output now goes out as `agent_text` and the UI nests it.

**A tool call is announced before it runs, not after.** `content_block_start`
carries the tool's name and id before its arguments and before it executes, so
`_render` emits `tool_use` there and again from the `AssistantMessage` with the
arguments filled in — same `id`, so the UI updates the line rather than drawing
two. Two events instead of a server-side accumulator keeps `_render` pure and
keeps `describe_tool_input` in one language.

This exists because a turn looked hung: the agent said "Reading the CSV now.",
then read a large file. Text streams first, the message carrying the tool call
does not arrive until the model's response completes, and a *successful* tool
result is deliberately quiet — so nothing moved for the length of the read. The
other half of the fix is in the UI: the working indicator now lives for the whole
turn with an elapsed clock, rather than being removed on the first token.

**The event stream carries structure now.** New event kinds (`tool_use`,
`tool_result`, `thinking`, `todo`, `agent_start`/`agent_stop`, `ask`,
`permission`) carry a `json.dumps` payload, which passes through `_sse_escape`
untouched because it never contains a raw newline; `text_delta` and friends keep
their raw-string shape. Tool *results* come from `PostToolUse` /
`PostToolUseFailure` hooks rather than the message stream, because results arrive
on a `UserMessage` that `_render` drops — which is why a failed `Write` and a
successful one used to render identically. `TodoWrite` is allowed and rendered as
in-turn progress only: it is never persisted and never substitutes for a bead,
and the `Stop` guard still enforces that.

**A machine can call this app, which first required giving it an identity.**
`verify()` required a non-empty `email` claim, and a Cloudflare Access *service
token* carries `common_name` — so a token Access had already admitted got a 403
one layer later, which is what blocked `kb-068`. An allowlisted `common_name` now
maps to `MCP_IDENTITY_EMAIL`; empty `MCP_CLIENT_IDS` (the default) keeps the old
refusal exactly, and the mapped email still faces ADR 0005's allowlist.

`app/mcp_server.py` mounts `ingest`, `query`, `lint` and `reflect` at `/mcp` over
streamable HTTP. Each runs a **real turn** — savepoint, guards, signals, backlog
— so an MCP call is revertable from the web UI, and the response carries the
savepoint name for that. `reflect` goes through `maybe_reflect` rather than
imitating reflection. Auth there is hand-rolled because a *mounted* ASGI app does
not run FastAPI's dependencies, so `dependencies=AUTHENTICATED` would look right
and never fire. One turn at a time, and a busy instance **refuses** rather than
queueing: savepoints are workspace-wide, so two turns interfere.

Every MCP turn acts as one identity, so `MCP_IDENTITY_EMAIL` must be a real
household member's address — a synthetic one puts every bead `lint` files into a
graph nobody's `bd ready` shows, which is ADR 0012's defect through a third door.
See `docs/decisions/0014`.

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

The ledger write and the bead filing are separate `try` blocks, and the order
matters: losing the denominator is survivable, losing the evidence that a turn
went wrong is not. They shared one block once, with the ledger first, so a
session store that lost its boot race silently filed no signals at all. The
store now retries at startup and `/healthz` reports `transcripts` as `ready`,
`unconfigured` or `unavailable` — deliberately not folded into `ok`, since a
turn that cannot reach its ledger should still answer the user, but never again
invisible. `_wait_for_health` in the test suite gates on it.

**The agent rewrites its own skills, within a remit it cannot exceed.** A
reflection turn may rewrite one skill's `description` and append under a
`## Learned` heading. That is the entire vocabulary of self-modification, and
`app/evolve.py` enforces it by comparing the file on disk against the content
about to be written — image skills, `AGENT_GUIDE.md`, new skills and `Edit`
itself are all out of reach.

A skill shipped in the image is still unwritable, but its *lessons* are not: it
keeps an overlay at `memory/skills/<skill>/LEARNED.md`, seeded from
`bootstrap/` like any other file, which its body tells the agent to read. The
overlay carries no frontmatter and no description, so it is data rather than a
second skill competing for the router's attention. Reflection appends to it
under the same append-only bound; ordinary turns rewrite it freely, which is
what makes it prunable and is where human-stated curation preferences land
instead of `memory/CLAUDE.md`. Because an image skill's body cannot absorb
anything, its `consolidate` bead asks for a prune plus a proposed repo change
for whatever has outgrown the overlay — the only route from a production lesson
into a shipped skill, and it runs through a human.

Reflection is an ordinary Turn, so it savepoints
and reverts like any other; a Revert also files a `REJECTED self-edit` bead,
without which the loop oscillates forever on the same evidence. Every accepted
change lands in `memory/evolution.md`, written by the application rather than
the agent, because the failure mode to design against is not a bad edit but an
unnoticed one.

It fires on a Stage-2 signal, and can be triggered directly with
`POST /api/reflect`. The manual path is not a convenience: bead `kb-3sv` gated
Stage 3 on real signal data, the ledger held 6 turns and 0 reverts, and the
user chose to proceed anyway — so a signal-only trigger would have shipped
unexercised self-modification. See `docs/decisions/0008`, which records the
override honestly.

**Memory.** `memory/CLAUDE.md` is short-lived accumulated notes — high-signal
facts the agent wrote down to survive across conversations. The agent prunes it
when it touches it. It is separate from `AGENT_GUIDE.md`, which is the stable
operator-written schema document.

**Proposed, not built: the Fair Play deck as the wiki's spine.** ADR 0011
argues that a Fair Play card is a capability contract, because the system's own
Conception / Planning / Execution split is already an agent-capability split —
Planning is beads and works, Execution has skills but no integrations, and
Conception has nothing at all, since there is no scheduler in `app/`. Cards
would carry YAML frontmatter (a documented exception to the wiki's prose
convention, because code reads them across 105 files), skills would be earned
rather than generated one-per-card, and the deck reaches every turn through one
line in `memory/skills/kb-curator/LEARNED.md` rather than a new skill.

ADR 0012 is its blocker, and it is a live defect rather than a design
preference. `bd` discovers its graph by walking up from cwd, so each user's
scratch holds a private ledger, while `kb.export_backlog` writes the *shared*
`memory/backlog.md` from whichever user just took a turn. A second login makes
the two overwrite each other — the exact failure ADR 0009 documented for two
machines, arriving through a different door. The fix is to initialise one graph
at `$WORK_DIR`, on the discovery path of every user, which makes this deployment
explicitly one household rather than many tenants.

Neither is implemented. Both are `**Status:** proposed`.

## Local dev

```sh
cp .env.example .env   # fill in ANTHROPIC_API_KEY, KB_DATABASE_URL
docker compose up
```

Chat: http://localhost:8080  
Wiki: http://localhost:8080/kb

`docker-compose.override.yml` bind-mounts `static/` over the copy in the image,
so a CSS or JS change needs only a reload. Compose loads that file automatically
for a bare `docker compose up` and **not** when a file list is passed with `-f`,
which is how the container test tier invokes it (`tests/conftest.py`) — so the
tier keeps exercising what the image actually contains. `app/` is not mounted, so
a Python change still needs `docker compose up -d --build app`.

**`docker compose down -v` destroys the local ledger.** The dev stack's `/work`
is a named volume, so the bead graph, `kb.git` and every savepoint go with it.
That is how five beads cited in this file and in the ADRs came to point at
nothing. The repo's own ledger is in git and survives; the local stack's does
not, so treat anything it holds as scratch.

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

It is also the only tier that can prove `can_use_tool` is wired at all, for the
same reason: the callback is only honoured in streaming mode, so a regression
that put a plain string back into `query(prompt=...)` raises `ValueError` on
every turn and no unit test would see it. The live tests answer a real
permission prompt through the real route and then assert the tool ran, and
separately assert that a real subagent's output never reached the reply.

## Linting and types

```sh
ruff check app tests          # lint
ruff format app tests         # format
ty check app tests            # types
```

Config is in `pyproject.toml`, which exists for these two tools and nothing
else — there is no `[project]` table, because the image pip-installs
`requirements.txt` and copies `app/` rather than installing a package.

ruff selects `ALL` and then opts out, with the reason written next to each
ignore. The reason is the part that goes stale silently, so it lives beside the
rule rather than in a commit message. Test-only idioms (bare `assert`, reaching
into private state, unused fixture arguments) are handled by `per-file-ignores`,
not by weakening the rule set for `app/`.

Both tools are **pinned exactly** in `requirements-dev.txt`, and the same
versions appear in `.github/workflows/ci.yml` and `.pre-commit-config.yaml` —
change all three together. ruff can add rules to `ALL` in a patch release and
ty is pre-1.0; either drifting turns an unrelated PR red, which is how a team
learns to ignore CI.

CI runs four jobs on every push: ruff, ty, the fast pytest tier, and
`pytest --container`. Pre-commit deliberately runs only the static checks — a
commit hook that stands up Docker and Postgres gets bypassed with `--no-verify`
until it may as well not be installed.

```sh
pip install pre-commit && pre-commit install
git config blame.ignoreRevsFile .git-blame-ignore-revs   # skip the format commit
```

## Environment variables

See `app/config.py` for the full list. Required: `ANTHROPIC_API_KEY`,
`KB_DATABASE_URL`. Notable optional: `KB_MOUNT` (default `/mnt/kb`),
`WORK_DIR` (default `/work`), `AGENT_MODEL` (default `claude-sonnet-4-6`),
`MAX_UPLOAD_BYTES` / `MAX_UPLOAD_TOTAL_BYTES` (10 MB per attachment, 25 MB per
request — the UI mirrors the first of these, and the server is the authority),
`ASK_TIMEOUT_SECONDS` / `PERMISSION_TIMEOUT_SECONDS` (600 / 300 — how long a turn
waits for a person, and they resolve in opposite directions).

`MCP_CLIENT_IDS` and `MCP_IDENTITY_EMAIL` together enable the `/mcp` surface, and
must be set together: the `common_name` values of the Cloudflare Access service
tokens allowed in, and the household member every machine call acts as. Leaving
`MCP_CLIENT_IDS` empty (the default) refuses every machine caller exactly as
before. `/healthz` reports `mcp` so "off" is distinguishable from "on and
refusing everything".

## Deploying

```sh
fly deploy
```

Bootstrap seeding (`AGENT_GUIDE.md`, `memory/skills/`) runs automatically at
startup when the KB mount is live. It is idempotent — existing files are never
overwritten.
