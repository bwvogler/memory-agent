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
- `app/mcp_catalog.py` — the opposite direction: outbound MCP servers the agent
  may connect to, defined in the image and credentialed from the environment
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
`git add -A` over one workspace, so concurrent turns interfere. See
`docs/decisions/0009`.

**One turn at a time, and every caller goes through the same door.**
`Registry.begin` is that door: it refuses with `TurnInProgressError` if a turn
is already in flight, and its check and insert sit in one method with no
`await` between them, which is what makes admission atomic on an event loop.
There were four entry points and three different guards — `/mcp` held an
`asyncio.Lock`, `POST /api/reflect` and `maybe_reflect` each rolled their own
check, and `POST /api/turns` had none at all, so two browser tabs were enough
to sweep one turn's half-written files into the other's savepoint and make
reverting either roll back both. Each caller keeps its own *answer* — 409 for
a browser, a `Busy` payload for a machine, a quiet skip for reflection — but
none keeps its own rule.

This is a refusal, not a queue: a queued turn hands the browser a turn id that
streams nothing until the turn ahead finishes, which is the "it looked hung"
failure this UI has already been fixed for once. The UI restores the composer
on a 409 so a refusal never eats a typed message.

It also makes `run_turn` reaching a terminal state load-bearing. An unfinished
turn is never evicted, so a turn that raised before `finish()` would wedge
every later turn — which is why the savepoint and beads priming now sit inside
its `try` rather than above it. Scoping savepoints per user is the real fix and
is still open as `img-lsp`.

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

Ids are the join key and are preserved across the pull, so a `kb-` bead in this
repo **also exists on the volume** — the prefix is not saying "about the
knowledge base". That is a rule about finishing, not about provenance: closing a
`kb-` bead here closes one of its two copies, and the other needs a manifest
line. An `img-` bead has no second copy and needs none. Wiki beads never travel;
nothing about the KB's content is committed to this repo.

The loop closes by deploying: append a line to `docs/shipped-beads.jsonl`, and
`kb.reconcile_shipped_all()` closes that bead in every ledger on the volume at
startup, noting the commit and image ref. No ssh, no human step.
See `docs/decisions/0010`, and **The work ledger** below for the commands.

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
queueing: savepoints are workspace-wide, so two turns interfere. That rule now
lives in `Registry.begin` for every caller rather than in a lock this surface
kept for itself; what stays here is the *answer* — a `Busy` payload rather than
an exception, because a machine caller wants the diagnosis.

Every MCP turn acts as one identity, so `MCP_IDENTITY_EMAIL` must be a real
household member's address — a synthetic one puts every bead `lint` files into a
graph nobody's `bd ready` shows, which is ADR 0012's defect through a third door.
See `docs/decisions/0014`.

**And this app can call other MCP servers, which is the opposite direction and
easy to confuse.** `/mcp` is this app *as a server*; `app/mcp_catalog.py` is
this app *as a client*. Nothing had been able to fill the second role at all:
`setting_sources=[]` and `strict_mcp_config=True` between them close every
documented way of configuring an MCP server, leaving only the `mcp_servers=`
dict, which held one hard-coded entry. That is why `kb-068` blocked `kb-b82`
rather than following it.

Definitions now live in `CATALOG` in the image and secrets in the environment,
where an entry *names* the variable it needs and never holds the value. It ships
empty, so adding one is a reviewed, deployed change.

That immutability is the decision, not an implementation detail. A catalog entry
is a `command` this container executes, and launching a stdio server is the SDK
spawning a subprocess *before* the model acts — so no `PreToolUse` hook fires and
`can_use_tool` is never consulted. A writable catalog would be a hole straight
through the `Bash(bd:*)` allowlist, reached by writing a file rather than by
asking. Worse, it would not need to invent a secret to abuse one: it could aim an
existing secret at a command of its choosing, and the resolver would inject the
value. Splitting definitions from secrets protects the value at rest and says
nothing about where it gets pointed. Underneath both: savepoints cover
`$KB_MOUNT/memory` and do not cover your calendar — content is revertable so the
agent writes it, capability is not so a human does. The agent's channel for
wanting a server is a bead.

`Server.auto_approve` is the other half. Listed tools are allowlisted by name
(never a wildcard, which would pre-approve whatever the next version learns to
delete with); everything else still works but falls through to `can_use_tool`,
so a person clicks Allow. That is how "read the calendar freely, ask before
writing" is expressed. Credentials are household-shared and this is stated rather
than buried: enabling a server enables it for everyone, and behind one token the
app cannot tell household members apart. Reflection gets no catalog at all.

**The catalog now holds Google Calendar and Gmail, and filling it in contradicted
three things ADR 0015 assumed.** All three are recorded as an amendment there
rather than quietly fixed.

`secrets` names a *value*, and Google credentials are *files* — a
`gcp-oauth.keys.json` and a saved token, from a browser flow that cannot happen
in this container. `Server.files` names the variable holding a file's contents
and `_materialise` writes it to `MCP_STATE_DIR` once per process — once, because
these servers write refreshed tokens back and rewriting per turn would discard
the refresh. Container-local is the containment that matters, not the 0600: not
the volume, not the KB, outside `add_dirs`. `scripts/google-auth.sh` runs the
consent flow on a laptop and prints the `fly secrets set` line; its loudest
warning is that a consent screen left in **Testing** expires refresh tokens after
seven days, so the integration works and then dies a week later.

Gmail cannot use the household-hub model Calendar uses. A Gmail token reaches
only its own mailbox; consumer delegation exists but is web-UI only, and the API
answers `403 Delegation denied`. So the entry reads the household account's *own*
inbox, and per-person forwarding filters are what make that useful.

`Server.deny` is a third tier, and it exists because Google's `gmail.compose`
scope grants **sending** — one scope, not two, measured by booting the server and
listing its tools at each scope. The five sending tools go into `disallowed_tools`.
Say the cost: that is our config enforced by the CLI, weaker than not holding the
scope, and the deliberate price of `draft_email`. It is a real control though, and
that was verified rather than assumed — a live test drives the real CLI with a
stub server whose handler records whether it was entered, and deny wins even over
an explicit `allowed_tools` entry. Which also demotes `__post_init__`'s overlap
check: it refuses a contradiction because two fields disagreeing about intent is
unreadable later, not because it would leak.

Both servers are pinned in the Dockerfile, and so is **Node itself** — a
checksummed tarball, because the Gmail server declares `node >=22.23.1`, Debian
ships v20, and npm reports that as a warning and installs anyway. The image got
*smaller* (1.57 GB → 1.53 GB): the servers cost 148 MB after `*.d.ts`/`*.map` are
stripped — 443 MB before, since `googleapis` carries typed clients for all 323
Google APIs, twice — and the pinned Node replaced more than that. Do not try to
delete the unused API directories; `apis/index.js` requires all 323 eagerly.

A server whose secret is unset is dropped rather than launched to fail later,
warned about once per distinct fault, and reported by `/healthz` under
`mcp_catalog` — because "the tools are gone" must not look like "the tools never
existed". See `docs/decisions/0015`.

**Present is not alive, and `/healthz` says which.** `missing()` answers a
question about our own config; it says nothing about whether Google still honours
the token, and those come apart — a seven-day Testing clock, a revocation, a
client disabled by publishing a consent screen with restricted scopes. Each entry
now reports a `state` (`missing`, `ready`, `expiring`, `expired`) plus a separate
`refresh` verdict, so "ready, and Google confirmed it" and "ready, and nobody has
asked yet" stop being the same string.

Two signals, because neither covers the other: the countdown is arithmetic on the
stored `expiry_date` and catches the clock, while a `grant_type=refresh_token`
POST catches the failures that have no date attached. A timeout is `unknown`, not
`expired`.

The bit to preserve is where the token is read from. **The environment variable,
never the materialised file** — both servers write refreshed tokens back to those
files, so a grant date derived from one reads "6 days, 23 hours left" forever, a
countdown that never counts. A test pins this because a mutation pointing the
lookup at the file passes every other test in the suite.

Two rules go with it. A dead credential does **not** drop the server: `_live()`
stays presence-only and network-free, or an outage at Google would silently strip
the agent's tools — this section's own confusion through a new door. And
`/healthz` never awaits Google; it reads a cache and schedules a refresh at most
every 15 minutes, because it is unauthenticated and drives the host's suspend
decision. Neither is folded into `ok`: no restart fixes an expired token. See the
`img-xak` amendment in `docs/decisions/0015`.

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
versions appear in `.github/workflows/ci.yml` — change both together. ruff can
add rules to `ALL` in a patch release and ty is pre-1.0; either drifting turns an
unrelated PR red, which is how a team learns to ignore CI. The commit hook is not
a third place, because it reads the pins out of `requirements-dev.txt`.

CI runs four jobs on every push: ruff, ty, the fast pytest tier, and
`pytest --container`. The commit hook deliberately runs only the static checks —
a commit hook that stands up Docker and Postgres gets bypassed with
`--no-verify` until it may as well not be installed.

```sh
scripts/install-hooks.sh
git config blame.ignoreRevsFile .git-blame-ignore-revs   # skip the format commit
```

**Why that is a script and not `pre-commit install`.** `bd` sets
`core.hooksPath` to `.beads/hooks` for its own hooks, and `core.hooksPath`
*replaces* `.git/hooks` rather than adding to it — so everything pre-commit
writes there is dead. pre-commit knows and refuses (`Cowardly refusing to
install hooks with core.hooksPath set`), with no flag to write elsewhere, and
unsetting the config would disable beads' hooks to enable ours. So the two share
the file: `git rev-parse --git-path hooks` reports whichever directory is live,
and `bd hooks install` rewrites only the region between its own `BEGIN/END BEADS`
markers. Both of those were measured, not assumed (`img-bl4`).

The hook itself is two lines calling `scripts/pre-commit-checks.sh`. `.beads/hooks`
*is* committed here, so the block usually arrives with a checkout — but it is
generated by the installer, so changing it would mean every checkout re-running
that, while the script it calls is source, and gets linted, typed over and tested
like anything else. What a clone does not inherit is `core.hooksPath` itself,
which is local config; the installer targets whatever `git rev-parse --git-path
hooks` reports, so it is correct before and after `bd` redirects it. The gate skips a commit that stages no Python, refuses if `.venv` is absent rather
than letting ty pass by resolving nothing, and never `--fix`es — an auto-fixing
hook commits something other than what was staged. It reads the working tree
rather than the index and says so when they differ.

## Environment variables

See `app/config.py` for the full list. Required: `ANTHROPIC_API_KEY`,
`KB_DATABASE_URL`. Notable optional: `KB_MOUNT` (default `/mnt/kb`),
`WORK_DIR` (default `/work`), `AGENT_MODEL` (default `claude-sonnet-4-6`),
`MAX_UPLOAD_BYTES` / `MAX_UPLOAD_TOTAL_BYTES` (10 MB per attachment, 25 MB per
request — the UI mirrors the first of these, and the server is the authority),
`ASK_TIMEOUT_SECONDS` / `PERMISSION_TIMEOUT_SECONDS` (600 / 300 — how long a turn
waits for a person, and they resolve in opposite directions).

Outbound MCP servers (`app/mcp_catalog.py`) add their own variables, one set per
entry in `CATALOG`, named by the entry rather than listed in `config.py`. Today
that is three, because one OAuth client serves both Google servers:
`MCP_GOOGLE_OAUTH_KEYS`, `MCP_GCAL_TOKEN` and `MCP_GMAIL_TOKEN`, all produced by
`scripts/google-auth.sh`. `/healthz` reports each entry's `state` under
`mcp_catalog`; unset is the shipped state and leaves every turn exactly as it was.
`MCP_STATE_DIR` (default `/tmp/mcp-catalog`) is where the credential files are
written and should stay container-local.

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

## The work ledger

```sh
bd ready                     # what is workable now
bd list --all                # everything, including closed
scripts/beads-pull.sh        # collect new image beads from prod
scripts/fly.sh bd list --all # look at prod without pulling
```

`bd ready` is the source of truth for what is open. This file deliberately keeps
no snapshot of the backlog — a list here would be wrong within a week.

**Getting what prod has filed.** `scripts/beads-pull.sh [user_slug]` needs
`flyctl` (logged in), `jq` and `bd` on PATH, and wakes the suspended machine
itself, so a slow first run is not a hang. Only `image`-labelled beads travel:
beads about the KB's *content* stay on the volume, where the agent that filed
them can also work them, and signal beads stay there as evidence.

It is an upsert and safe to re-run. A bead arriving for the first time flips
`deferred` → `open`; one already tracked has `status` dropped from the payload,
so prod can go on saying `deferred` without reopening work closed here. It ends
by running `bd export`, so a real pull shows up in `git status` — and that, not
the output, is the thing to read. `bd import` reports `Imported N issues` for
rows it re-applied unchanged, so a pull that brought nothing still announces a
number. A clean `git diff .beads/issues.jsonl` means nothing arrived.

**The two ledgers are expected to disagree, and the disagreement is safe.** A
bead worked here drifts from its prod twin immediately — rescoped, retitled,
repointed — while prod keeps whatever the agent filed on the day it filed it.
That looks like a sync problem and is not one, for two reasons. Both copies are
`deferred` on prod, so nothing there ever claims them; and `bd import` compares
`updated_at` and **skips a row older than the one it would overwrite**, saying
so as `N stale skipped`. A locally edited bead is by definition newer, so a pull
cannot revert deliberate local work — which is measured, not assumed: prod's
`kb-068` still carries the pre-split title, description, priority *and the
reversed dependency edge*, and replaying a pull against a copy of this ledger
left all four untouched.

What that leaves is one narrow case worth watching. `stale skipped` is a
guarantee about *ages*, not about intent, so a prod-side edit made after a local
one wins — which is exactly how `kb-nb4` correctly picked up the design doc the
prod agent wrote. Read the changed-field list the pull prints, and never reach
for `--allow-stale`.

So: do not `--write` prod to reconcile a bead you are actively working. The
divergence costs nothing and closes itself, because `docs/shipped-beads.jsonl`
closes the prod copy by id and a closed bead's description no longer matters.

**Looking without pulling.** `scripts/fly.sh` reaches the deployed ledger and is
read-only by default. Use it when you only want to see what prod has; it touches
no local state. It is also how you spot a bead that *should* have travelled and
did not — the `image` label is the only filter, so an idea about the image filed
without it stays stranded on the volume.

Mutating verbs need `--write`, because that graph is on an unreplicated volume
with no savepoint covering it, and `--write` is the *whole* of that protection:
past the flag there is no confirmation and no undo. `scripts/fly.sh --write bd
close <id>` closes a live bead immediately, which is easy to do while meaning to
test that the guard refuses. Recovering is two commands rather than one, because
`bd reopen` restores a bead to `open` and not to the status it had:

```sh
scripts/fly.sh --write bd reopen <id>
scripts/fly.sh --write bd update <id> --status deferred   # image beads only
```

Miss the second and the bead starts showing up in the prod agent's `bd ready`,
which `deferred` exists to prevent. Rescuing a stranded bead is the one routine
reason to reach for `--write` at all:
`scripts/fly.sh --write bd label <id> image`, then pull.

**Closing a bead after shipping.** Because ids survive the pull, a `kb-` bead
exists in two ledgers and `bd close` here closes one of them. The volume goes on
showing it open until a line in `docs/shipped-beads.jsonl` — `id`, `summary`,
`commit` — reaches `reconcile_shipped` at the next startup. Shipping the code is
not finishing the bead; appending that line is. An `img-` bead was created here,
has no second copy, and needs no manifest line.

Check the id against `bd list` before appending, because a wrong one fails
silently and permanently. A bead the ledger never had is the common case rather
than a fault — every user's ledger sees the same manifest — so `reconcile_shipped`
records the id as applied and never retries it (`app/kb.py`). The only trace is
one `log.info`.

**Before committing ledger changes**, run `bd export -o .beads/issues.jsonl`. The
Dolt database beside it is gitignored and the JSONL is what git tracks.
`beads-pull.sh` does this for you; a bead you create or close by hand does not.

That step survives beads owning a `pre-commit` hook, which is the obvious reason
to think it is stale, so here is why it is not. bd does have an auto-export, and
`bd hooks run` refuses to perform it: `auto-export: skipping — running as git
hook`. The refusal is unconditional — it holds with `export.auto: true` in
`.beads/config.yaml`, which otherwise works and writes the same file — so a
commit made after `bd close` carries a `issues.jsonl` that predates it, silently
and with the hook reporting success. That was measured in a throwaway repo, since
"the hook probably handles it" is the kind of guess this paragraph exists to
settle (`img-uc6`).

We leave `export.auto` off rather than turning it on and deleting the paragraph,
for two reasons that both survive it: the export still needs `git add`, so a
manual step remains either way, and every write would then print bd's
`no Dolt remote configured` warning, which teaches a reader to skip bd's output.
