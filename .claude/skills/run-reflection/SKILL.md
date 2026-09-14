---
name: run-reflection
description: >
  Trigger the app's four MCP capabilities — ingest, query, lint, reflect —
  directly from Claude Code, against either the local dev stack or
  production, without opening the chat UI in a browser. Use when asked to
  run reflection, ingest something into the KB, query it, or lint it, "from
  here."
---

# Run the App's MCP Tools From Claude Code

`app/mcp_server.py` mounts four tools at `/mcp`: `ingest`, `query`, `lint`,
`reflect`. Claude Code is itself an MCP client, so the right way to call
these is connecting Claude Code to that endpoint as a named server (`claude
mcp add`), then calling the tool directly — never curl, never the browser
chat UI.

Two server entries exist, one per target:

| Name | Target | Auth |
|---|---|---|
| `kb-dev` | local dev stack (`http://localhost:8080/mcp`) | none — `DEV_BYPASS_AUTH=1` |
| `kb-prod` | deployed app, via the **Cloudflare-proxied custom domain** `https://app.memory-agent.net/mcp` — never the raw `.fly.dev` hostname, which bypasses Cloudflare's edge entirely and only gets our own app's 403 | Cloudflare Access service token, sent as `CF-Access-Client-Id` / `CF-Access-Client-Secret` headers |

Once connected, call `mcp__kb-dev__reflect` / `mcp__kb-prod__reflect` (or
`ingest`/`query`/`lint`) like any other tool. **A newly added or reconnected
MCP server's tools only appear in a fresh Claude Code session** — the tool
list is fixed at session start, so add/fix a server, then use it from your
*next* session, not the one that just ran `claude mcp add`.

**`reflect` is the one tool of the four that is fire-and-forget — always
follow it with polling the turn, or you'll never see it think.**
`ingest`/`query`/`lint` all `await` their turn fully inside the MCP call and
hand back the finished result. `reflect()` (`app/mcp_server.py`) instead calls
`agent.maybe_reflect`, which spawns the turn and returns a `turn_id`
**immediately** — functionally the same `202`-style handle as `POST
/api/reflect`. The actual reasoning (reading signal beads, deciding on a
skill edit, applying it under `write_guard_for`'s bound) happens as a
separate agent turn running inside the app's own container via its own
`claude_agent_sdk.query()` call — **none of that thinking ever happens in the
calling Claude Code session**, and there is no MCP progress/streaming wired
up to change that. This is not a gap to route around: the savepoint, the
`Stop`/`PreToolUse` guards, and the bounded self-edit all live *inside that
turn*, which is the entire point of ADR 0008. The way to actually watch it
think is the two-step version below, not trying to make Claude Code itself
be the reflecting agent.

After calling `reflect` and getting back a `turn_id`, poll the turn using the
*same auth* as the MCP call (`GET /api/turns/{turn_id}` takes the identical
`Cf-Access-Jwt-Assertion`-derived auth as `/mcp` — for `kb-prod` that means
the same `CF-Access-Client-Id`/`CF-Access-Client-Secret` headers) until
`state` is a terminal value — `done`, `failed`, `stopped`, or **`error`**
(observed live: a turn whose model call fails outright, e.g. "Credit balance
is too low," lands in `error`, not `failed` — poll for all four, not just the
first three):

```sh
# kb-dev
curl -s http://localhost:8080/api/turns/<turn_id> | python3 -m json.tool

# kb-prod
curl -s https://app.memory-agent.net/api/turns/<turn_id> \
  -H "CF-Access-Client-Id: <client-id>" \
  -H "CF-Access-Client-Secret: <client-secret>" | python3 -m json.tool
```

The response's `events` array is the actual thinking trace —
`thinking_delta`/`text_delta`/`tool_use`/`tool_result` in order — and
`evolved` shows what, if anything, changed. Report that trace back, not just
the terminal `state`; "done" with no detail is exactly the black-box
experience this step exists to avoid.

## Local (`kb-dev`)

1. Bring the dev stack up with auth bypassed, per the `dev-checks` skill:
   ```sh
   docker compose up -d          # add --build app if app/ changed
   ```
   `.env` needs `DEV_BYPASS_AUTH=1` — `_authenticate` in `app/mcp_server.py`
   honors it exactly like the browser route, so no Cloudflare Access token is
   needed locally.
2. `claude mcp get kb-dev` should show `✔ Connected`. If it's missing:
   `claude mcp add --transport http kb-dev http://localhost:8080/mcp -s user`.
3. Call the tool: `mcp__kb-dev__reflect`, etc.

Low stakes: this is a throwaway local KB and ledger (`docker compose down -v`
wipes it anyway, per CLAUDE.md's "Local dev" note) — fine to run without
asking first.

## Production (`kb-prod`)

This deliberately uses a **service token** (`MCP_CLIENT_IDS` /
`MCP_IDENTITY_EMAIL`), not the personal OAuth path (`MCP_OAUTH_EMAILS`) also
available on this app. The distinction matters (ADR 0014): OAuth is a
*person* logging in as themselves; a service token is a *machine* acting
with its own identity — the right shape for Claude Code acting unattended,
rather than impersonating a household member's own login.

Setup, done once:
1. Find the real Cloudflare-proxied hostname first: `fly certs list` (or
   `fly ips list`) — the raw `<app>.fly.dev` name is *not* behind Cloudflare's
   edge, so a service token sent there is never even seen by Access; it just
   hits our own app's `Cf-Access-Jwt-Assertion`-only check and 403s.
2. In the Cloudflare Zero Trust dashboard: Access → Service Auth → Service
   Tokens → Create Service Token, bound to the same Access Application that
   already gates `/mcp`. This yields a Client ID + Client Secret (shown once
   — save them).
3. **Add a policy rule admitting that token** on the Application itself
   (Access → Applications → the app → Policies → an Include rule of type
   *Service Token* naming the one just created). Creating the token alone
   grants it nothing — without a matching policy rule, Cloudflare's edge
   answers a `401` with `WWW-Authenticate: Bearer ... invalid_token` even
   though the Client ID/Secret are valid. Verify directly before wiring up
   Claude Code:
   ```sh
   curl -i -X POST https://app.memory-agent.net/mcp/ \
     -H "CF-Access-Client-Id: <client-id>" \
     -H "CF-Access-Client-Secret: <client-secret>" \
     -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0.0.1"}}}'
   ```
   A `401` from Cloudflare (`server: cloudflare` in the headers) means the
   policy step above isn't done yet; a JSON-RPC response means it's working.
4. `fly secrets set MCP_CLIENT_IDS=<client-id> MCP_IDENTITY_EMAIL=<household-email>`
   — the email must be a real household member's address already on the
   allowlist, never a synthetic one (ADR 0012's collision, reached through a
   third door otherwise).
5. `claude mcp add --transport http kb-prod https://app.memory-agent.net/mcp -s user -H "CF-Access-Client-Id: <client-id>" -H "CF-Access-Client-Secret: <client-secret>"`
   (remove any existing `kb-prod` entry first: `claude mcp remove kb-prod -s user`).
6. `claude mcp get kb-prod` should show `✔ Connected` with no login prompt.

**The client id and secret are HTTP headers, and `claude mcp add -H` is the
only place they go.** They live in `~/.claude.json` under
`mcpServers.kb-prod.headers`, written by step 5; nothing reads them from the
environment. In particular **they must never be pasted into `.env`**, where
their own names make them illegal: `CF-Access-Client-Id` is not a valid shell
identifier, `scripts/mount-kb.sh` does `set -a; source .env` under `set -e`,
and the result is that *every* mount — `--dev` included — dies with
`CF-Access-Client-Id=<your secret>: command not found`, printing the
credential to the terminal and naming nothing that is actually wrong. That
happened; it is bead `img-z3x`.

If you want them in a shell for the `curl` recipes above, give them legal
names (`CF_ACCESS_CLIENT_ID`, underscores) — the *header* on the wire keeps its
hyphens either way — or read them straight out of `~/.claude.json` so there is
one copy to rotate.

Once connected, call `mcp__kb-prod__reflect`, etc.

**This is a real production turn** — real Anthropic API spend against the
deployed key, and (for `reflect`) a real, though bounded, write to a skill's
`description` or `LEARNED.md` overlay (`app/evolve.py`'s `write_guard_for`
enforces the bound; nothing else in the KB is reachable this way). Mirror
`prod-ops`'s posture: confirm with the user before calling any `kb-prod`
tool, the same caution that skill already applies to every other
prod-touching action — this is not something to run just because it's
convenient to.

## Notes

- `reflect` takes no signal-data minimum to run — bead `kb-3sv`'s gate on
  Stage 3 was deliberately bypassed for this manual trigger (ADR 0008). With a
  thin ledger (a fresh local stack, or a quiet production one), "no signal
  beads found, no skill change is warranted" is the expected, correct
  outcome — not a failure.
- `ingest`/`query`/`lint` follow the identical connect-then-call shape; see
  `app/mcp_server.py` for each tool's arguments.
- Stack start/health and prod-touching caution rules live in `dev-checks` and
  `prod-ops` respectively — this skill only covers reaching the four MCP
  tools from Claude Code, not the mechanics of running or protecting the app.
