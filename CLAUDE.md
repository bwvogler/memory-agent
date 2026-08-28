# memory-agent

A personal LLM wiki agent backed by TigerFS (FUSE → PostgreSQL). Users chat
with the agent; it writes and maintains a structured markdown knowledge base in
the TigerFS workspace.

## Architecture

- `app/main.py` — FastAPI app, routes, startup lifespan
- `app/agent.py` — agent construction: system prompt, skill loading, session options
- `app/kb.py` — TigerFS helpers: mount health, SQL queries, scratch dirs, savepoints,
  and the beads task ledger
- `app/kbview.py` — a directory's `VIEW.md` spec: normalising it, and building
  the sorted, grouped entries its index renders (`docs/decisions/0018`)
- `app/signals.py` — records which skills each turn used and files a bead when
  a turn is reverted, errors, or is denied a tool
- `app/guards.py` — SDK hooks enforcing two rules the prompt could not: no
  shell command may corrupt a KB file, and no turn may defer work without
  filing it
- `app/evolve.py` — bounded self-evolution: what a reflection turn may change
  about its own skills, enforced as a hook, plus the evolution log
- `app/interact.py` — the round-trips to the human (a question tool, a
  permission callback) and the hooks that report tool results and subagents
- `app/conversations.py` — the durable, household-shared event log a
  household conversation streams from; `turns.Turn` still exists per turn but
  no longer owns the stream once it belongs to one (`docs/decisions/0017`)
- `app/mcp_server.py` — the four capabilities as MCP tools, for a machine caller
- `app/mcp_catalog.py` — the opposite direction: outbound MCP servers the agent
  may connect to, defined in the image and credentialed from the environment
- `app/config.py` — all config read from environment variables
- `skills/kb-curator/SKILL.md` — universal wiki-maintenance skill, offered to every agent session by `agent._read_skills`
- `bootstrap/` — skill files seeded into the KB on first startup (ingest, lint, reflect); editable in the KB
- `static/` — web UI (chat at `/`, wiki view at `/kb`); `view.js` is the
  directory-index renderer, kept pure so a browser check can call it directly

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

**A directory decides how it is displayed, and the spec is its own file.** A
directory may hold a `VIEW.md` whose *frontmatter is* a display spec: it drives
the generated index the centre pane shows when you click that directory, and
the labelled chips above each page at that level. `app/kbview.py` normalises it
and builds the entries; `static/view.js` paints them.

Its own file for one reason, and it is a data-loss reason rather than a
tidiness one: the store's `headers` JSONB column is **full-replace on every
write**, so a key omitted is a key deleted. A `view:` block inside `GUIDE.md`
would be destroyed, silently, by the first turn that rewrote that file's prose
without repeating it — the same shape as
`test_appending_to_a_kb_file_still_destroys_it`. Because the whole of `VIEW.md`
is the spec, rewriting it always rewrites the spec. Keys nest under `view:` and
`page:` because `title`/`author`/`encoding` are routed to dedicated columns and
would never reach `headers` at all.

That column is also why **there is no YAML parser and no new dependency here**.
TigerFS parses frontmatter on the way in, so `holder: [brian, laura]` is read
back as a JSON list. The listing query never looked at it; now it does.

The spec is **declarative on purpose, and does not flip ADR 0016** — a layout
from a fixed set plus field names, rendered by fixed JS building DOM nodes with
`textContent`. `textContent` is not the whole defence: a frontmatter *key* can
be `__proto__`, so untrusted lookups go through a `Map`; no class or id is
derived from a group key; one `fmt()` bounds every value at 200 chars and strips
bidi overrides; and **no field ever becomes an href** — the only link an index
emits is built from the entry's own path.

**There is no `filter`, and adding one would be a mistake.** A view may reorder
and group; it may never drop an entry. An index that hides files that exist
makes a liar of the artifact whose whole value is "the wiki says what is there",
and it is a hiding primitive an agent could write after ingesting a poisoned
page. Sorting and grouping run server-side because all three pytest tiers are
Python and none executes the served JavaScript.

The same rule is why `/api/kb/dir` returns `dirs` even though the tree already
has them. Omitting them was the plan until a browser check showed `wiki/`
rendering "Nothing here yet." over a `recipes/` visible in the tree beside it —
the identical lie, reached by omission instead of by design.

Children come from a **`parent_id` join, never a `LIKE` prefix** — no wildcard
escaping to get wrong, one scan instead of two, and it works at the workspace
root, where a prefix pattern degrades to `'/%'` and matches nothing. Two
measured facts to keep: `filetype` for a directory row is `'directory'`, *not*
the `'dir'` the TigerFS reference documents (the documented value matches
nothing and renders as an empty workspace), and the store speaks YAML 1.2 — `no`
stays `"no"`, `1:30` stays `"1:30"`.

`GET /api/kb/dir` costs no extra queries: `GUIDE.md` and `VIEW.md` are children
like any other file. `GET /api/kb/spec` is the one-row subset so a *file* open
does not pay for its directory's children. `/api/kb/file` gained `fields` — a
wider select on a row it already fetched, and load-bearing: without it a file
opened by deep link cannot tell "empty" from "not told", and printed the spec's
`empty_labels` over data it never had. Only a browser caught that.
See `docs/decisions/0018`.

**A skill is reached because the system prompt names it, and nothing else.**
This is the load-bearing fact about skills here, and it was wrong in this file
for a long time. `ClaudeAgentOptions.skills` is the SDK's switch, but it enables
*discovered* skills and nothing in this deployment is discoverable: `add_dirs`
grants access to a path without making it a skill source, and
`setting_sources=[]` rules out the CLI's own scan. Measured rather than
reasoned — three live turns recorded **zero** skills used, and patching
`skills="all"` in changed nothing.

`agent._read_skills` is the fix and the whole mechanism: it walks both tiers
(`skills/` in the image, `memory/skills/` in the KB), reads each `SKILL.md`'s
`description` via `evolve.description_of`, and appends a listing of name,
absolute path and description to the system prompt. That is deliberately just
level one of progressive disclosure — the metadata, plus where to read the
rest — so a skill's body costs nothing on turns that do not use it, exactly as
a real skill would. It is read fresh every turn because a reflection turn's
*entire* permitted change is a description, so a cached listing would make
self-improvement land somewhere nothing re-reads.

Two rules fall out. An image skill wins over a same-named KB directory, because
`memory/skills/kb-curator/` holds only a `LEARNED.md` overlay and letting it win
would drop the real skill. And a skill whose description will not parse is left
out with a warning rather than listed blank — silently unreachable is the defect
this whole mechanism exists to end, so `tests/test_skill_listing.py` asserts
every shipped skill actually offers one.

**Bootstrap skills.** `bootstrap/skills/` contains example skills (ingest, lint,
reflect, views) seeded into `memory/skills/` at startup. They live in the KB so
the human can edit and improve them over time. `app/mcp_server.py` and the
`kb-lint` subagent also name them by path directly, which is the same mechanism
applied to one caller rather than to the prompt.

Seeding tracks a hash of what it last shipped (`.bootstrap-state.json`) so an
improved skill actually reaches existing deployments: an unmodified file is
replaced, a human-edited one is left alone with a warning. Files predating the
state file are never touched, since we cannot tell whether they were edited.

Two constraints shape what a skill here may look like, and `bootstrap/skills/GUIDE.md`
states both because neither is visible in the file that violates it. **Exactly
two frontmatter keys, `name` and `description`, and nothing else** —
`evolve.bounded_skill_edit` refuses any key added, removed or reordered, so a
third key locks the skill out of the one change reflection is allowed to make
to it, and the refusal surfaces months later inside a reflection turn rather
than at seed time. `test_every_bootstrap_skill_can_still_be_evolved` pins it
for every skill, not just today's.

The *set*, not the order — and that was measured rather than assumed, by a
container test written to assert the wrong thing. **The store sorts frontmatter
keys**, so a skill shipped `name` then `description` is stored `description`
then `name`; the guard's reorder check is satisfied because both sides of its
comparison come from the store and are normalised identically. This is also why
`shipped_source` in `tests/test_seed_bootstrap.py` writes them description-first,
a detail that reads as arbitrary until you know. The fast tier cannot see any
of it: its double simulates the folded-block collapse and not the sort.

And **a skill here cannot ship a script**: `allowed_tools` is
`Bash(bd:*)` and `acceptEdits` does not cover Bash, so anything else falls
through to `can_use_tool` — a prompt on an interactive turn, a denial on a
reflection or `/mcp` one. That inverts the usual "prefer a script for
deterministic checks" advice; a skill's feedback loop has to be expressible in
`Read`/`Glob`/`Grep`.

`views` is the one whose work does not fit in a turn, and it is the first skill
here with a `references/` directory — split by *phase*, so a turn resuming a
backfill loads the backfill file and not the layout examples. It is a seed
rather than an image skill because the procedure is a household's to rewrite;
the *vocabulary* it works with stays in the image at
`skills/kb-curator/references/directory-views.md`, where it cannot drift. That
two-level nesting under `skills/` was an unexercised seeding path until now
(`_seed_tree`'s `mkdir(parents=True)` is the whole of it), and it is covered in
both the fast and container tiers, because a reference that silently failed to
seed reads as the agent ignoring an instruction.

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

That refusal is instance-wide, not conversation-wide, and stays that way —
what changed with conversations (`docs/decisions/0017`) is what a caller does
*before* reaching it. `POST /api/conversations/{id}/messages` checks whether
the turn already running belongs to the SAME conversation; if so the message
is injected into it (`turns.Turn.inbox`, `agent._input_stream`) instead of
calling `begin()` a second time. A turn running for a *different*
conversation still gets the refusal above, now naming who is busy instead of
repeating the bare `BUSY` text. Injection is not a second exemption from "one
turn at a time" — it is still exactly one turn, one savepoint — it is a
second person's message joining the turn already in flight rather than
starting a competing one.

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

**One page, three panes: tree, renderer, chat.** `/`, `/kb` and
`/kb/{path:path}` now serve the same `static/index.html`; the old
`static/kb.html` navigated away from the chat entirely, which lost the
session, so a turn that said "I updated your recipes index" gave you no way
to look without leaving. `/` is authenticated now that it *is* the wiki. The
centre pane is driven by the tree, and separately pushed to by chat: a
successful KB write auto-opens (`interact.describe_tool_target` adds a
`target` to the `tool_use` payload, correlated against `tool_result`'s `ok`
by tool id so a *failed* write never moves the pane), while an upload only
opens on click — you already know what you attached, the agent's write is
the thing that's news. `GET /api/uploads/{turn_id}/{name}` serves upload
bytes back, ownership proved by path (the slug comes from `Identity`, never
the URL) rather than by the in-process, evictable `Registry`. The renderer
gained a sanitizer at the parser (`marked`'s `html` token is escaped, and any
`href`/`src` outside `http(s):`/`mailto:` is dropped) because the centre pane
now renders uploaded documents too, not just agent-written wiki pages — see
docs/decisions/0016, including the reload-resume bug that first cut of this
shipped with (`onerror` clearing the same localStorage marker a page reload
needs intact) and the fix (clear only on an authoritative `done`/`failed`).

**The three panes now collapse, swipe, and land in true chronological
order — see the amendments in `docs/decisions/0016`.** The flex-row layout had
never been tried below its own 772px floor: no width-based media query existed
anywhere in `app.css`, and `body { height: 100vh }` doesn't shrink for an
on-screen keyboard. Two header buttons (`#toggle-tree`/`#toggle-chat`) collapse
a sidebar on desktop, persisted in `localStorage` beside the existing width
keys. Below 820px the layout becomes a CSS scroll-snap carousel — one pane per
screen, a tab strip in the header, no touch-gesture code — landing on chat at
boot; the auto-navigate-on-write behavior above gets a dot on the Article tab
when its target pane is off-screen, since silently not-navigating would be the
same lie by omission ADR 0018 names for `dirs`. `visualViewport` (not `dvh`
alone, which tracks browser chrome but not a keyboard) drives an `--app-h`
custom property so the composer never ends up hidden behind the keyboard.
Enter sends only where `matchMedia('(pointer: fine)')` matches, checked live
at each keydown rather than cached at boot.

Separately, the chat transcript is chronological now, which it never actually
was: the client had always rendered every stretch of a turn's prose into one
fixed `div.body`, with every tool call, thought and subagent box appended as a
*later sibling* of it — so a turn's final answer sat at the top, above
whatever tool activity followed it, regardless of when anything actually
happened. (The server side was already correct: one monotonic seq counter in
`Conversation.append`, and `_render_stream` emits a `tool_use` at
`content_block_start` specifically so it interleaves with the surrounding text
deltas.) `app.js` now tracks which block — a prose `div.body` or a collapsed
`details.activity` run — is currently open, and opening one closes the other,
so blocks land in the DOM in the order they actually happened, with
consecutive tool/thinking/subagent/todo activity folded into one
click-to-expand run rather than left as bare siblings. `ask`/`permission`
still bypass the run entirely (a question buried behind a disclosure could
strand a turn nobody notices needs an answer), and a failed step forces its
whole run open rather than requiring anyone to expand it to find out why the
turn stalled.

**The conversation is the unit, not the turn.** `app/conversations.py`'s
`Conversation` is a durable, household-shared, seq-numbered event log —
`conversation_events`/`conversation_turns`/`conversations` in
`session_store.SCHEMA` — that a `turns.Turn` streams into when it belongs to
one (`Turn.conversation_id`, set only by the browser path; reflection and
`/mcp` turns keep their old private per-turn buffer unchanged). `GET
/api/conversations/{id}/events` replays the WHOLE conversation from seq 0 on
a fresh `EventSource`, not just whatever turn happened to be running, which
is what `docs/decisions/0016` flagged as "not a general transcript-durability
fix." `POST /api/turns` and `GET /api/turns/{id}/events` are gone, replaced
by `GET`/`POST /api/conversations` and `POST /api/conversations/{id}/messages`.
`GET /api/turns/{id}` is kept, deliberately: it is a polling fallback the
`--live` test tier actually depends on (it polls a turn in a loop rather than
holding an SSE connection open through a multi-minute model call), which
`pytest --container` caught missing after the first cut removed it too. For a
conversation turn its `events` are filtered out of the conversation's own
buffer by `turn_id`, since `turn.events` itself stays empty once a turn has a
`conversation_id`.

Ownership checks on `/revert`, `/answer` and `/permission` are dropped, not
narrowed: any allowlisted household member can watch, answer, or revert any
turn (`auth.verify()`'s allowlist is the real boundary, and always was). A
message sent while a turn is already running for the SAME conversation is
injected into it (`Turn.inbox`, `agent._input_stream`) rather than refused —
see the amendment above and `docs/decisions/0017`. `POST
/api/turns/{id}/stop` cancels the running task; `_run_turn` treats
`CancelledError` as a clean stop, not a failure
(`signals.OUTCOME_STOPPED`), and every turn is now also wrapped in
`asyncio.timeout(config.turn_timeout_seconds)` — a backstop `img-r7o` asked
for, independent of ever revisiting `ClaudeSDKClient.interrupt()`, which stays
avoided for the reason that bead documents.

Attribution matters once more than one person can speak in one conversation:
`auth.display_name_for` (backed by a `HOUSEHOLD_NAMES` config map) is
prefixed onto the message TEXT sent to the model — not a separate content
block, so it survives into the CLI's own transcript across a `resume=` — and
a system-prompt note tells the agent "you" is not guaranteed to mean the same
person twice.

Not done in this pass, each for a stated reason in `docs/decisions/0017`:
presence/typing indicators, the `SessionStore` SDK-protocol rewrite that
would close `img-2jj`, and the Phase-5 backlog (`@agent` addressing,
auto-titling, search, KB provenance links, revert-to-a-message, a context
budget warning).

## Local dev

See the `dev-checks` skill for the setup commands, the chat/wiki URLs, and the
`static/` vs `app/` bind-mount nuance.

**`docker compose down -v` destroys the local ledger.** The dev stack's `/work`
is a named volume, so the bead graph, `kb.git` and every savepoint go with it.
That is how five beads cited in this file and in the ADRs came to point at
nothing. The repo's own ledger is in git and survives; the local stack's does
not, so treat anything it holds as scratch.

## Tests

See the `dev-checks` skill for setup and the exact tier commands. Both slow
tiers (`--container`, `--live`) are opt-in — a bare `pytest` needs no Docker,
database, or API key — and `--live` implies `--container`.

**Only `--live` gets a real key, and `--container` is now forced onto a
placeholder even when you have one.** That overwrite is deliberate. The smoke
tier asserts on a turn that fails fast, so a real key makes it call the model,
blow `wait_until_idle`'s deadline and bill you for a tier documented as free.
`conftest.py` used to load `.env` unconditionally, which meant the tier passed on
CI and failed on any developer machine with a key - two tests, for reasons
nothing to do with the change under test.

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

See the `dev-checks` skill for the commands and where each pin lives.

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
`--no-verify` until it may as well not be installed. `dev-checks` has the
`scripts/install-hooks.sh` and blame-ignore setup commands.

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

`bd ready` is the source of truth for what is open. This file deliberately keeps
no snapshot of the backlog — a list here would be wrong within a week. The
`prod-ops` skill has the commands for reaching the deployed ledger, and `ship`
has the ritual for closing a bead after its code lands.

**With one exception, and it is upstream's: a blocking edge between the two
prefixes does not block.** `bd dep add kb-x img-y` is accepted, stored, and
rendered by `bd show` and `bd dep tree` — which will say `[BLOCKED]` — while
`bd ready` offers the blocked bead anyway and `--explain` calls the open blocker
"resolved" in the same breath as listing it as ready. Reproduced from scratch on
the pinned bd, and the variable is the *prefix*, not the pull: an imported bead
carrying a native prefix blocks correctly, so `beads-pull.sh` is not implicated
(`img-523`). Upstream has it root-caused and open as
[#4647](https://github.com/gastownhall/beads/issues/4647) —
`isCrossPrefixDep` files the edge as `DepTargetExternal`, the same bucket as a
GitHub URL, which `bd ready`'s materialised `is_blocked` column never consults.
It is not a pin problem: it reproduces on the tested 1.1 line too, so no upgrade
available today fixes it (see `img-4r2` before touching that pin at all).

So a cross-prefix edge is **documentation only**. State the ordering in the
description as well, the way `img-n7g` does, because the edge you can see in
`dep tree` is not enforcing anything.

**Every ledger is on bd 1.2.2 at schema v53, and getting there cost a data
migration.** This is settled history, kept because the next pin bump will want
it. bd 1.2.1 was retracted upstream as an accidental untested release, but not
before it migrated every database here from v53 to v65. 1.2.2 is the tested
1.1.2 code re-released, so it speaks v53 and **refuses a v65 database
outright** — which made the fix a per-database Dolt cursor rollback
(upstream's `docs/RECOVERY-1.2.1.md`, not ours; we have no such file), not a
version change. Both ledgers have had it, and upstream's optional follow-up to
re-track the audit `events` table was applied to both as well.

The lasting lesson is about direction, and it will apply verbatim next time.
A binary refuses a database *ahead* of it, and a newer bd may migrate on first
run without asking — so an unpinned bump is not reversible by reinstalling the
old binary. `kb.py` logs-and-continues when bd is unreachable, so the symptom is
never an error; it is a ledger that quietly stops recording. Assume any bump
needs a migration plan.

Two things worth keeping from the measurements. The rollback is **lossless** —
on this repo's ledger and again on prod's, the full export compared
record-for-record identical across the change, dependency edges and `bd ready`
included. And `BD_IGNORE_SCHEMA_SKEW=1`, the escape hatch that bridged the
deploy, is not read-only: under it 1.2.2 creates, notes, closes, exports and
computes `ready` against a v65 database. It is out of `fly.toml` now, and
`fly.toml` says why it must not come back.

`bd sql` is not the tool for any of this: it answers `not yet supported in
embedded mode`, and embedded is what this deployment runs. The recovery used a
checksum-matched `dolt` binary in the container's `/tmp`, which a redeploy
discards. Do not put `dolt` in the image for it.

Moving the pin also cost one flag, and the loss was silent. `bd create --status`
is 1.2.x-only; on 1.2.2 it is `unknown flag: --status` and **nothing is created**.
Every signal bead is created `deferred` precisely so evidence stays out of
`bd ready`, so the whole signal-capture path went quiet - and quietly, because
`create_bead` logs-and-returns-`None` and its callers are all written to survive
an unreachable ledger. `kb.create_bead` now sets status in a second
`bd update` call, which works on both lines. Anything else reaching for a bd flag
should assume the 1.1 surface until the container tier says otherwise.

The application code was only half of it, and the other half sat broken for
longer. `skills/kb-curator/SKILL.md` documented the same dead flag in the
`bd create ... --labels image --status deferred` command it tells the agent to
run when it notices something about the *app* rather than the wiki — so every
image bead the agent tried to file that way printed `unknown flag` and created
nothing. Found while writing the `views` skill, which was about to copy the
command; reproduced from scratch on a throwaway ledger before changing it. It
is now the same two-step `create` then `update --status` that `kb.create_bead`
uses. Worth taking as a general lesson rather than a fixed bug: a bd invocation
living in *prose* is not covered by any tier, so the flags in a skill file are
only as current as the last person who ran them.

The ordering hazard is asymmetric, and it is the part to remember if a rollback
is ever needed again. Upstream warns that a leftover 1.2.1 silently re-migrates
a recovered database. *This* repo was protected by accident: `.beads/config.yaml`
sets `sync.remote`, so 1.2.1 hit the remote-migrate gate and refused the write
loudly. The ledger on the volume has no remote, so nothing there would have
stopped it. **Never roll back a volume ledger while an image pinning the older
binary can still take a turn** — the next turn undoes it and says nothing. Deploy
first, always.

**Getting what prod has filed.** Only `image`-labelled beads travel: beads
about the KB's *content* stay on the volume, where the agent that filed them
can also work them, and signal beads stay there as evidence. The `prod-ops`
skill has the pull command and its upsert/stale-skip mechanics.

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

**Looking without pulling** is read-only and touches no local state — it is
also how you spot a bead that *should* have travelled and did not, since the
`image` label is the only filter `beads-pull.sh` applies. **Mutating the
volume needs `--write`, and past the flag there is no confirmation and no
undo** — that graph is on an unreplicated volume with no savepoint covering
it. The guard is a list of verbs, which makes it exactly as good as that list
is complete: a verb nobody added is not a weaker guard, it is *no* guard, and
silently (`bd sql` proved this once, with no flag needed at all). The
`prod-ops` skill has the full verb list, the read-vs-write recipes, and the
recovery commands for an accidental mutation.

**Closing a bead after shipping is not the same as shipping the code.**
Because ids survive the pull, a `kb-` bead exists in two ledgers and `bd
close` here closes only one — the volume keeps showing it open until a line
in `docs/shipped-beads.jsonl` reaches `reconcile_shipped` at the next
startup. An `img-` bead has no second copy and needs no manifest line. The
`ship` skill has the full ritual, including the one failure mode that has
already bitten this project once: a close refused for an open blocker gets
recorded as applied and never retried, silently, unless the blocker ships
first — never work around this with `--force` or by reordering the manifest.

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

**Something here pushes to GitHub, and it is not you.** `bd backup status`
reports `enabled=true (auto: git remote detected)`: bd turned its own off-machine
backup on because `.beads/config.yaml` carries a `sync.remote`, and it publishes
by pushing the branch. `git reflog show origin/<branch>` says `update by push`
against commits nobody pushed by hand.

The trigger is not the commit, which is what makes it hard to reason about. A
backup runs when the **bead database has changed** since the last one and the
`interval` (15m) has elapsed — and the push then carries every commit that
happens to exist at that moment. So a commit touching only source code can sit
local indefinitely, and then be published by an unrelated `bd close` twenty
minutes later. Both halves were observed: five commits show up as push targets
and each follows a bead write, while two source-only commits stayed local with
the interval long expired.

Documented rather than disabled. The backup is worth having — this project has
already lost a ledger to `docker compose down -v` — and a feature branch reaching
GitHub is not the failure. Being wrong about it is: two sessions of summaries
here described these commits as local and unpushed, confidently, which is not a
claim a reader can check by reading the code. So the rule is: **check `git
ls-remote`, do not infer.** If a commit must stay local, `git-push: false` under
`backup:` in `.beads/config.yaml` keeps the local backup and drops the push.

Related and easy to conflate: `.beads/backup/` is 1.6 MB of Dolt backup data and
is gitignored by bd's own `.beads/.gitignore`, so none of this bloats the repo.
`.beads/interactions.jsonl` — a per-machine field-change log bd writes beside the
ledger — is ignored from the root `.gitignore` instead, because bd generates
`.beads/.gitignore` and may rewrite it.
