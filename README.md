# memory-agent

A cheap, secure, self-hosted endpoint that puts a coding agent in front of a
versioned knowledge base — with atomic undo on every turn.

You send it a message. A Claude Code agent picks it up, reads and writes a
knowledge base that lives in Postgres but looks like a filesystem, and streams
its answer back. Every turn is checkpointed, so any change the agent makes to
your knowledge is one click from being reverted.

Infrastructure runs around **$1–3/month**. The front end and auth are free.

```
Browser ──► Cloudflare (Pages + Access + Tunnel, free)
                  │  verified Google Workspace identity
                  ▼
            Fly.io Machine  ($1-3/mo, suspended when idle)
              ├─ FastAPI: turn queue, SSE stream
              ├─ Claude Agent SDK ──► claude CLI subprocess
              └─ /mnt/kb ──FUSE──► TigerFS
                                      │
                                      ▼
                              Postgres (free tier)
```

## Why this shape

Three decisions do most of the work, and each is written up in `docs/decisions/`:

**The knowledge base is a filesystem, not a vector store.** TigerFS mounts
Postgres as a versioned filesystem, so the agent uses the file tools it is
already good at — `ls`, `cat`, `grep`, Read, Write — instead of a retrieval API
it has to be taught. Every write is a row, every row has history.

**Every turn gets a savepoint.** The agent is checkpointed before it touches
anything, so a bad write is one atomic undo away — and TigerFS undo is itself
reversible. This is what makes "let an agent write to my knowledge base" a
reviewable idea rather than a reckless one. It is the single best reason to
choose TigerFS over a RAG pipeline.

**Auth is Cloudflare Access, so there is no auth code.** Google Workspace SSO,
policy as "allow emails ending in `@yourdomain.com`", free for 50 users, and the
origin has no public IP so it cannot be bypassed.

## Status: working scaffold, two unverified spots

Honest labelling, because you are going to deploy this.

**Verified:** every design claim in `docs/` is cited to primary sources. All
Python compiles, `fly.toml` and `docker-compose.yml` parse, shell scripts pass
`bash -n`. Smoke-tested by executing the modules that have no SDK dependency:
mount detection, the control-surface probe, the guard that refuses to start if
scratch space is inside the knowledge base, event replay from a cursor, busy
tracking, SSE escaping, and identity slugging. The auth flow follows
Cloudflare's documented JWT verification requirements rather than trusting a
header.

**Not verified — the author had no live TigerFS mount or Fly account:**

1. **The savepoint/undo write syntax** in `app/kb.py`. The dot-directories are
   documented; the exact gesture for *creating* a savepoint is inferred. All of
   it is confined to one file, and `scripts/spike-fuse.sh` prints the real
   control surface so you can confirm and fix in one place.
2. **Exact SDK option names** in `app/agent.py` (`add_dirs`, `permission_mode`,
   the `system_prompt` preset shape). These drift between SDK releases; they are
   all set in one function.

Run the two Phase 0 spikes before anything else. They exist precisely to fail
fast:

```bash
scripts/spike-fuse.sh "$KB_DATABASE_URL"     # can this host mount at all?
scripts/spike-latency.sh /mnt/kb             # is the database close enough?
```

That second one is the number that decides whether this whole design is pleasant
or miserable. TigerFS turns every `ls` and `grep` into SQL, and an agent
exploring a knowledge base is *extremely* chatty at the filesystem layer. If the
container and the database are in different regions, the agent will feel broken
and you will blame the model.

## Quickstart, local

```bash
cp .env.example .env          # set ANTHROPIC_API_KEY
export ANTHROPIC_API_KEY=sk-ant-...
docker compose up --build
open http://localhost:8080
```

`docker-compose.yml` grants `SYS_ADMIN` and `/dev/fuse`. Without those the mount
fails and nothing works — the same requirement that rules out several managed
hosts in production. `DEV_BYPASS_AUTH=1` is set for local use only; it disables
authentication completely and the app logs an error at startup saying so.

## Deploy

**1. Postgres.** Any provider, free tier is fine. Note its region.

**2. Fly.** Same region as the database.

```bash
fly launch --no-deploy            # edit primary_region in fly.toml to match
fly volumes create memory_agent_work --size 5
fly secrets set \
  ANTHROPIC_API_KEY=sk-ant-... \
  KB_DATABASE_URL=postgres://... \
  SESSION_DATABASE_URL=postgres://... \
  ALLOWED_EMAIL_DOMAINS=yourdomain.com
fly deploy
```

**3. Cloudflare Access.** In Zero Trust → Access → Applications, add a
self-hosted app for your hostname. Add Google Workspace as an identity provider,
then a policy allowing your email domain. Copy the **AUD tag** and your team
domain:

```bash
fly secrets set \
  CF_ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com \
  CF_ACCESS_AUD=<aud-tag>
```

**4. Tunnel.** Create a tunnel, route your hostname to `http://localhost:8080`,
and set `fly secrets set TUNNEL_TOKEN=...`. The entrypoint starts `cloudflared`
automatically when that token is present. Also enable Access enforcement at the
tunnel ingress (`access: {required: true, teamName, audTag}`) so unsigned
requests never reach the app — belt *and* braces, because if the origin is ever
reachable directly, header-trust alone is a full auth bypass.

**5. Seed the knowledge base.** Create `memory/CLAUDE.md` in the mount. The
agent reads it at the start of every session.

## What to check after deploying

**Streaming actually streams.** Cloudflare and `cloudflared` will happily buffer
`text/event-stream` and deliver a whole turn in one lump at the end. The app
sets the three headers that prevent it, but this has regressed more than once —
make it a post-deploy smoke test, not a one-time check.

**`/healthz` reports `kb_mounted: true`.** A knowledge base that is not mounted
produces no errors anywhere. The agent just quietly knows nothing.

## Layout

```
app/
  auth.py            Cloudflare Access JWT verification (signature, aud, iss)
  agent.py           Agent SDK wrapper; explicit memory loading; savepoint/turn
  kb.py              Mount health, savepoints, undo, operation log
  turns.py           Detached turns with replayable event buffers
  session_store.py   Postgres transcript persistence
  main.py            HTTP surface
static/index.html    Minimal chat UI, SSE, per-turn revert button
skills/kb-curator/   Teaches the agent how to tend the knowledge base
scripts/             Phase 0 de-risking spikes — run these first
docs/                Architecture, hosting comparison, ADRs
```

## Adapting it

**Slack instead of a web UI.** Genuinely worth considering: Slack is already a
secure, authenticated, mobile front end, and it deletes the entire Pages +
Access + SSE + buffering third of this system. The compute layer is unchanged.

**A different host.** `docs/hosting-comparison.md` has the full table with
citations. Short version: Modal VM Sandboxes for a true $0 (recurring free
credits, but `vm_runtime` is experimental and disk does not persist), Cloudflare
Containers for a flat $5 (but ephemeral disk, so rehydrate every wake). Cloud
Run, Railway and Render cannot do this at all.

**No FUSE at all.** If the latency spike comes back ugly, write a thin MCP
server over the same Postgres schema. You lose `ls`/`grep` ergonomics and the
undo semantics, but you can host anywhere — including Anthropic's Managed
Agents — and each tool call becomes one round trip instead of hundreds.

## Licence

MIT. See `LICENSE`.
