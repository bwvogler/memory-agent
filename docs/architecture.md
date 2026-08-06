# Architecture

This document explains how the pieces fit and, more usefully, the four things
that will bite you if you get them wrong. The *why* behind the major choices
lives in `decisions/`; this is the operational view.

## The shape

```
   Browser (small trusted group)
        │  HTTPS, Google Workspace SSO
        ▼
 ┌─────────────────────────────────────┐
 │ Cloudflare (free)                   │
 │  • Pages  → static chat UI          │
 │  • Access → Zero Trust policy       │
 │  • Tunnel → no inbound ports        │
 └───────────────┬─────────────────────┘
                 │ Cf-Access-Jwt-Assertion
                 ▼
 ┌─────────────────────────────────────┐
 │ Fly.io Machine (same region as DB)  │
 │  cloudflared ── FastAPI             │
 │                  │                  │
 │                  ├─ verify JWT      │
 │                  ├─ turn registry   │
 │                  └─ Claude Agent SDK│
 │                       │  spawns     │
 │                    claude CLI       │
 │                       │             │
 │  /work/<user>/  ◄─────┤ scratch cwd │
 │  /mnt/kb/       ◄─────┘ TigerFS     │
 │       │ FUSE                        │
 └───────┼─────────────────────────────┘
         │ postgres://
         ▼
   Postgres (free tier)
     • TigerFS tables (the knowledge base)
     • agent_sessions (transcripts)
```

One Postgres serves both the knowledge base and the transcript store, so a
deployment needs exactly one database.

## The subprocess model, and what it implies

The Agent SDK does not wrap an API — it spawns a `claude` CLI subprocess and
talks to it over stdio. That subprocess owns a shell, a working directory, and
JSONL transcripts on local disk. One session is one subprocess with its own
process tree.

Practical consequences: budget ~1 GB RAM per concurrent agent as a *floor*, not a
ceiling, and measure peak RSS on a representative session before trusting any
concurrency number. Session length and tool activity both grow memory. There is
no built-in session timeout, so bound work with `max_turns`.

Three kinds of state live on local disk by default and none survive a restart:
transcripts (`~/.claude/projects/`), memory files, and working-directory
artifacts. This project sends transcripts to Postgres via a `SessionStore` and
puts memory in the knowledge base (ADR 0004). Scratch artifacts are deliberately
disposable.

## Four things that will bite you

### 1. Co-locate the container and the database, aggressively

TigerFS turns every `ls`, `cat`, `stat` and `grep` into SQL. An agent exploring a
knowledge base is *extremely* chatty at the filesystem layer — one `grep -r` can
be hundreds of round trips. Same region and it is fine. Different continents and
the agent will feel broken, and you will blame the model.

`scripts/spike-latency.sh` is a go/no-go gate. Run it from inside the deployed
container against the real database, before writing anything on top.

### 2. Keep the agent's scratch space off the knowledge base

Each session gets a working directory on local disk, passed explicitly as `cwd`
on every query. By default all subprocesses inherit the *application's* working
directory — and if that directory is the mount, every temp file, half-finished
draft and stray artifact becomes a versioned row in your knowledge base.

The KB is an *additional* accessible directory the agent reads and writes
deliberately, not the place it lives. `kb.assert_scratch_outside_kb()` fails the
process at startup if this is misconfigured, and `entrypoint.sh` checks it again
before mounting, because it is silent and irreversible-feeling when wrong.

### 3. Assert the mount before accepting traffic

This is the nastiest failure mode in the whole design, because nothing errors.
A missing `CLAUDE.md` is silently skipped by the SDK. An absent knowledge base is
not an exception — it is just an empty directory. The agent answers cheerfully
and knows nothing, and you spend two hours suspecting the model.

So: `entrypoint.sh` waits for the control surface (`.log/`, `.savepoint/`) to
appear rather than sleeping a fixed interval, `/healthz` returns 503 when the
mount is not live, and the UI shows a warning dot. Wire `/healthz` into whatever
you use for alerting.

### 4. Do not let the host suspend mid-turn

Fly's autostop keys on proxy traffic. An agent spending four minutes thinking
with no bytes flowing looks idle.

Two mitigations, and be clear-eyed about the gap between them. While a browser is
attached, the SSE heartbeat is real proxy traffic and holds the machine up — that
part is handled. When *no* client is attached (laptop closed, tab gone) there is
no traffic, and a self-ping to localhost does not help because Fly counts
requests at the proxy, not inside the container. `/healthz` exposes `busy: true`
while any turn is running, so an external uptime pinger can keep the machine warm;
alternatively set `auto_stop_machines = false` and pay the ~$11/mo for always-on.
Pick one deliberately rather than discovering it from a truncated turn.

## The turn protocol

Never make one HTTP request span a whole agent turn.

```
POST /api/turns              → 202 {turn_id}, agent runs detached
GET  /api/turns/{id}/events  → SSE, replays from Last-Event-ID
GET  /api/turns/{id}         → polling fallback
POST /api/turns/{id}/revert  → roll the KB back to this turn's savepoint
```

Because turn state lives server-side, a dropped connection, an expired Access
session, a closed laptop or a proxy hiccup costs nothing — the client reconnects
and replays.

**On Cloudflare specifically.** The "100-second limit" is a half-myth: error 524
is a *time-to-next-byte* timeout, currently ~125s by default, not a total
duration cap. A ten-minute turn is fine as long as bytes keep flowing, hence the
15-second heartbeat comment frame.

The real bug is **buffering**. Cloudflare and `cloudflared` will buffer
`text/event-stream` and deliver the entire turn in one lump at the end. The app
sets `X-Accel-Buffering: no`, `Cache-Control: no-cache, no-transform` and the
right content type, and flushes per chunk. This has regressed more than once,
including in 2026 — make it a post-deploy smoke test.

SSE rather than WebSocket, deliberately: it survives Access cleanly with cookie
auth, reconnects with replay for free, and browsers cannot set headers on `new
WebSocket()`, which would force non-browser clients onto Access service tokens.

## Auth

Cloudflare Access authenticates against Google Workspace and injects a signed JWT
as `Cf-Access-Jwt-Assertion`. **Presence of that header is not authentication** —
Cloudflare's docs are blunt that "validation of the header alone is not
sufficient… the JWT and signature must be confirmed to avoid identity spoofing."

`app/auth.py` verifies the RS256 signature against the team's JWKS (with cache
and rotation handling), checks `aud` against the application's AUD tag, checks
`iss` and `exp`, and then applies an email-domain allowlist. Enforce Access at
the `cloudflared` ingress too. If the origin is ever directly reachable,
header-trust alone is a full auth bypass — hence tunnel-only, no public IP.

Access sessions expire (24h by default), so a reconnect mid-stream can return a
redirect to the login page. Handle that rather than rendering login HTML into the
chat pane.

## Multi-tenant isolation

In a shared container, default SDK behaviour reads settings and memory from the
filesystem and can leak one user's context into another's session. Four settings
together prevent it, all applied in `agent._options()`:

`setting_sources=[]` so nothing loads from the filesystem. `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`
because auto memory loads into the system prompt regardless of `setting_sources`.
`CLAUDE_CONFIG_DIR` per user so nobody shares `~/.claude.json`. And an explicit
per-user `cwd` on every call.

## Scaling past one container

The turn registry is in-process, so turns are lost if the container restarts
mid-turn, and a second container would not see the first one's turns. In order of
what to change first:

Move the registry into Redis or Postgres so turn state outlives the process. Then
run a pool behind a load balancer and pin each session to a container by
consistent hash on `session_id`, so a pinned session keeps reaching the same
running subprocess. Bound concurrency per host as
`(host RAM - overhead) / per-session RAM ceiling`.

Large parallel subagent fan-outs from one session can hit API rate limits; batch
rather than issuing one wide dispatch.

## Cost

| Component | Monthly |
|---|---|
| Cloudflare Pages + Access + Tunnel (≤50 users) | $0 |
| Fly Machine, 2 GB, mostly suspended (~5 h active) | ~$0.50–1.50 |
| Fly volume, 5 GB | ~$0.75 |
| Postgres, free tier | $0 |
| **Infrastructure** | **~$1–3** |
| Anthropic tokens | dominates the above by an order of magnitude |

Anthropic's hosting guidance makes the point directly: a minimally provisioned
container runs about $0.05/hour while a single long agent session can spend
dollars in tokens. Optimise the skills and the prompt, not the server.

## Observability

The SDK inherits OpenTelemetry configuration from the environment, so setting the
OTEL variables at the container level exports spans, metrics and logs for every
query with no code change. Prompt text and tool inputs are excluded by default.

```
CLAUDE_CODE_ENABLE_TELEMETRY=1
CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1     # traces only
OTEL_TRACES_EXPORTER=otlp
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_ENDPOINT=http://collector.example.com:4318
```

Also alert on `{"type": "system", "subtype": "mirror_error"}` messages, which
mean a transcript batch was dropped after retries.

## Open questions a deployment has to answer

Whether the knowledge base is one shared corpus or partitioned per person —
shared is much simpler and is what this implementation assumes. Whether everyone
can *write* or only some people can, which sets how paranoid the savepoint layer
needs to be. And whether you need real audit: if so, thread the Access `email`
claim through to the mount as the acting user, so `.log/` attribution is a person
rather than always "the agent".

## References

- <https://code.claude.com/docs/en/agent-sdk/hosting>
- <https://code.claude.com/docs/en/agent-sdk/session-storage>
- <https://code.claude.com/docs/en/agent-sdk/observability>
- <https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/application-token/>
- <https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/>
- <https://community.cloudflare.com/t/using-server-sent-events-sse-with-cloudflare-proxy/656279>
- <https://tigerfs.io/> · <https://github.com/timescale/tigerfs>
