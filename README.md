# memory-agent

A cheap, secure, self-hosted endpoint that puts a coding agent in front of a
versioned knowledge base — with atomic undo on every turn.

You send it a message. A Claude Code agent picks it up, reads and writes a
knowledge base that lives in Postgres but looks like a filesystem, and streams
its answer back. Every turn is checkpointed, so any change the agent makes to
your knowledge is one click from being reverted.

Infrastructure runs around **$1–3/month**. The front end and auth are free.

```
Browser ──► Cloudflare (Access at the edge, free — no tunnel)
                  │  verified Google identity, as a signed JWT
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

**Auth is Cloudflare Access, so there is almost no auth code.** Google SSO,
policy as "allow emails ending in `@yourdomain.com`", free for 50 users. The
origin *does* keep a public `.fly.dev` hostname — see "Why there is no tunnel
here" — so `app/auth.py` verifies the JWT signature rather than trusting the
header Cloudflare injects.

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

If your users aren't on a domain you control — personal addresses, a handful
of named collaborators — set `ALLOWED_EMAILS` instead (or as well; the two are
OR'd):

```bash
fly secrets set ALLOWED_EMAILS=alice@example.com,bob@example.com
```

Allowlisting a domain you don't own (`gmail.com`, `icloud.com`) admits everyone
who has ever used that provider, not the people you meant. See
`docs/decisions/0005-explicit-email-allowlist.md`.

**3. A hostname, and a certificate for it.** Access applications only exist on a
hostname in a zone on your Cloudflare account, so you need a domain there before
anything below works. The order matters, because Fly validates ownership over
DNS and cannot do that through Cloudflare's proxy:

```bash
# a. In Cloudflare DNS: CNAME app -> <your-app>.fly.dev, proxy DISABLED (grey cloud)
fly certs add app.yourdomain.com
fly certs check app.yourdomain.com      # wait for Issued
# b. Now enable the proxy (orange cloud), and set SSL/TLS to Full (strict)
```

Proxy the record before the certificate exists and you get Cloudflare 526s;
leave it grey afterwards and every request bypasses Access, because Access runs
at the edge and an unproxied record never reaches it.

**4. Cloudflare Access.** First an identity provider, then the application —
in that order, because the application form does not survive navigating away
from it. Zero Trust → Integrations → Identity providers → Add new. Since June
2026 new accounts get the **Cloudflare** identity provider by default and
One-time PIN is no longer added automatically; add OTP explicitly if your users
are on addresses you don't control, since the Cloudflare provider only admits
members of your Cloudflare account. Google works for any Google account but
needs a GCP OAuth client whose authorized origin is
`https://<team>.cloudflareaccess.com` and whose redirect URI is that plus
`/cdn-cgi/access/callback` — your application hostname appears nowhere in it.

Then Access → Applications → Add → self-hosted, with the *public hostname*
destination (not the private-hostname or private-IP options, which are for
tunnels). Path empty, so `/api/*` and `/mcp` are covered too. Give it a policy
per kind of caller:

* **Allow**, Include → Emails listing exact addresses, for people.
* **Service Auth**, Include → Service Token, for `/mcp`. It must be its own
  policy: Access evaluates Service Auth before the identity policies, and a
  token in an Allow policy is sent toward a login it cannot complete.

Copy the **AUD tag** from the application and your team domain:

```bash
fly secrets set \
  CF_ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com \
  CF_ACCESS_AUD=<aud-tag>
```

**Why there is no tunnel here.** Cloudflare's own advice is to enforce Access at
a tunnel ingress as well as at the application. That advice does not survive
this `fly.toml`: the tunnel would run *inside* the machine,
`auto_stop_machines = "suspend"` with `min_machines_running = 0` stops it along
with everything else, and the only thing that wakes the machine is the Fly proxy
receiving a request on the route the tunnel was meant to replace. Suspended, the
tunnel is down and nothing can bring it back.

So there is no tunnel: `cloudflared` is not in the image and `entrypoint.sh`
does not start one. A leftover `TUNNEL_TOKEN` secret is warned about at startup
rather than ignored, because a token sitting in `fly secrets list` looks exactly
like a tunnel that is running. If you want one anyway, set
`min_machines_running = 1`, accept the bill, and put `cloudflared` back — but
the app-layer JWT check in `app/auth.py` is what actually protects this, which
is why it verifies signatures rather than trusting a header.

The consequence to know: `<your-app>.fly.dev` stays publicly routable and Access
cannot cover it. That is safe — it returns 403 without a valid token — but it
means `DEV_BYPASS_AUTH` is the only thing standing between a fresh deploy and an
open knowledge base. Never set it in production. `fly secrets list` is worth
reading after every deploy for exactly that reason.

**5. Seed the knowledge base.** Create `memory/CLAUDE.md` in the mount. The
agent reads it at the start of every session.

## What to check after deploying

**Streaming actually streams.** Cloudflare and `cloudflared` will happily buffer
`text/event-stream` and deliver a whole turn in one lump at the end. The app
sets the three headers that prevent it, but this has regressed more than once —
make it a post-deploy smoke test, not a one-time check.

**`/healthz` reports `kb_mounted: true`.** A knowledge base that is not mounted
produces no errors anywhere. The agent just quietly knows nothing.

**`/healthz` reports every configured MCP server as `ready`.** `mcp_catalog`
lists outbound servers (`app/mcp_catalog.py`) and says `missing <VAR>` for any
whose credential is unset. Such a server is dropped from the agent's toolset
entirely, which from the outside is indistinguishable from never having
configured it — this field is the difference. See `docs/decisions/0015`.

Today that is `calendar` and `gmail`, both pointing at one household Google
account. They report `missing MCP_…` until you run `scripts/google-auth.sh` and
set the three secrets it prints, and an unconfigured server changes nothing about
a turn. The script's own warning is the one to heed: an OAuth consent screen left
in **Testing** expires refresh tokens after seven days, so the integration works
and then fails a week later for no visible reason.

Gmail reads the household account's **own inbox** and cannot read anyone else's —
a Gmail token reaches only its own mailbox, and delegation is a web-UI feature the
API refuses. Forwarding filters are how you decide what it sees. The agent can
draft mail; the five tools that would *send* it are refused outright.

## Working against the deployed machine

State is split across two tiers, and only one of them is reachable from a
laptop by default.

**The knowledge base is in Postgres**, so it needs no special access — mount it
locally and browse it with ordinary tools:

```bash
bash scripts/mount-kb.sh --dev    # the throwaway Postgres from docker compose
bash scripts/mount-kb.sh --prod   # whatever .env points at
```

`.env` usually points at the *same* database the deployed machine writes to. If
so, everything under the mountpoint is production: an editor save lands there
with no review and no deploy in between. That is why `--prod` has to be said out
loud, and why the script prints which host it mounted every time.

**Everything else is on the Fly volume** — the bead ledger, `kb.git`, per-user
scratch, the SDK transcripts — attached to exactly one machine
(`docs/decisions/0009`). Reach it with:

```bash
scripts/fly.sh doctor            # slug, volume usage, bd versions
scripts/fly.sh bd ready --json   # the deployed ledger
scripts/fly.sh run ls -la /work
scripts/fly.sh shell
```

It is read-only by default: `bd close` and friends need `--write`, because that
graph sits on an unreplicated volume and no savepoint covers it. Two things it
handles that a hand-written `fly ssh` does not — the machine is suspended
(`min_machines_running = 0`, and SSH is not proxy traffic, so it must be woken
over HTTP first), and `flyctl ssh console -C` runs no shell, strips quote
characters and splits on whitespace, so any argument containing a space arrives
mangled unless it is encoded.

Keep local `bd` on the version the `Dockerfile` pins. bd refuses to open a
database written by a newer schema, and this repo now has a `.beads` of its own
— a newer local bd would upgrade it in place. `scripts/fly.sh doctor` prints
both versions side by side.

## Layout

```
app/
  auth.py            Cloudflare Access JWT verification (signature, aud, iss)
  agent.py           Agent SDK wrapper; explicit memory loading; savepoint/turn
  kb.py              Mount health, savepoints, undo, operation log
  turns.py           Detached turns with replayable event buffers
  mcp_catalog.py     Outbound MCP servers: defined in the image, keyed from env
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
