# 0014 — The machine is a caller, and it needs an identity to be one

**Status:** accepted

## Context

Four things this system does are worth calling from outside it: write something
into the wiki, ask the wiki a question, audit the wiki, and let the agent improve
its own skills. Three already existed as skills under `bootstrap/skills/`
(`ingest`, `lint`, `reflect`) and one did not exist at all (`query`, its trigger
absorbed into the always-loaded `kb-curator`). None of them was *addressable*.
The only way to reach any of them was to type a sentence into the chat box and
hope the router picked the right skill.

The agent noticed this itself. `kb-068` — "there is nowhere to configure an MCP"
— has been open and blocked since ADR 0010.

It was blocked on something more basic than MCP transport. **No machine could
authenticate to this app at all.** `app/auth.py` verifies a Cloudflare Access JWT
and then requires a non-empty `email` claim:

```python
email = (claims.get("email") or "").lower()
if not email:
    raise HTTPException(403, "Access token has no email claim")
```

A Cloudflare Access **service token** — the mechanism Access provides for exactly
this, and the one `app/main.py` already names in its SSE-versus-WebSocket comment
— carries `common_name`, not `email`. So a correctly configured service token,
which Access itself had verified and admitted, hit a 403 from the application one
layer later. The comment in `main.py` said non-browser clients "would be forced
onto Access service tokens" as though that were the answer, and the code refused
them.

## Decision

**Map an allowlisted service token onto a real identity, and change nothing else.**

In `verify()`, when there is no `email` claim, look for `common_name`; if it
appears in `MCP_CLIENT_IDS`, adopt `MCP_IDENTITY_EMAIL` as the caller's email.
Three properties matter more than the mechanism:

* **Closed by default.** `MCP_CLIENT_IDS` is empty unless set, so an untouched
  deployment refuses service tokens byte-for-byte as it did before. This is not a
  door that opens on upgrade.
* **Access is still the gate.** The app does not authenticate anyone; it decides
  whether to keep rejecting someone Access already vouched for.
* **ADR 0005's second layer still applies.** The mapped email goes through the
  same `ALLOWED_EMAIL_DOMAINS` / `ALLOWED_EMAILS` check as a human's. Arriving via
  a token is not a way around the allowlist.

`Identity.subject` becomes the token's `common_name` when `sub` is empty. The
email is shared by every machine caller; the subject is the only record of which
one acted.

**`app/mcp_server.py` exposes the four capabilities over streamable HTTP**,
mounted at `/mcp`. Each tool builds a short prompt that says *read this skill and
follow it* — never a copy of the skill's contents, because those skills are seeded
from `bootstrap/` and then edited by the human, and a copy in the image would
drift from the maintained one while looking equally authoritative.

**Every tool runs a real turn.** `registry.create` plus `agent.run_turn`, which
keeps the savepoint, both guards, the signal ledger and the backlog projection. An
MCP call is therefore revertable from the web UI like any other turn, and lands in
the same evidence reflection reads. The response carries the savepoint name for
exactly that reason. `reflect` goes through `agent.maybe_reflect` rather than
imitating it, because reflection's protections live in that function and in
`_reflection_options`, not in a prompt (ADR 0008, and the rejection at the end of
ADR 0013).

**MCP turns are non-interactive.** `Turn.interactive=False`, so no permission
callback is installed and `ask_user` answers immediately that nobody is watching.
See ADR 0013 for why the tool is kept rather than removed.

**Authentication is bespoke here, and has to be.** A mounted ASGI app does not run
the parent app's dependencies, so the `dependencies=AUTHENTICATED` used on every
other route would look correct and never fire — the worst possible failure for an
auth check. `asgi_app()` wraps the MCP app in middleware that authenticates the
request itself and puts the result in a `ContextVar` the tools read.

**And it accepts the header only, never the cookie.** `current_identity` takes
either `Cf-Access-Jwt-Assertion` or the `CF_Authorization` cookie, which is
correct for the browser UI and wrong for this. A cookie is an *ambient*
credential — the browser attaches it to a request the page never had to prove
anything to make — and this endpoint drives the agent, so an unattended POST to it
writes to the wiki.

Whether a cross-site POST would in fact carry that cookie depends on the
`SameSite` attribute Cloudflare sets, which this decision does **not** verify and
does not control. That is the reasoning, not a gap in it: a machine caller sends
the header anyway, so refusing the cookie costs a real caller nothing and removes
the need to know the answer. Defence in depth against an ambient credential — not
a patch for a demonstrated exploit, and it should not be cited as one.

That is also why the MCP SDK's DNS-rebinding protection is switched off here
rather than configured. FastMCP auto-enables it whenever its `host` setting looks
like localhost — which it does, because we never call `run()` and that setting is
an unused default — and then permits only localhost `Host` headers, so in
production **every call would answer 421**. That was observed locally as a 421 and
inferred for production by reading the SDK's default allowlist; it was not tested
against a real deployed hostname. The check guards the same ambient-credential
shape that accepting no cookie already forecloses, so leaving it on with a
hostname allowlist would mean carrying the app's public name in config for a
defence that is now redundant.

**One turn at a time, and a refusal rather than a queue.** Two concurrent agents
do not fit under the 2 GB suspend ceiling ADR 0008 cites, and savepoints are a
`git add -A` over one shared workspace, so two turns interfere by construction
(ADR 0009). A tool refuses while anything else runs, naming the reason, which is
the same answer `POST /api/reflect` has always given.

**`/healthz` reports `mcp`.** Not folded into `ok`, for the reason `transcripts`
is not: the surface is always mounted and always verified, and what an operator
needs to distinguish is "MCP is off" from "MCP is on and refusing every call".
Half-configuring it — client ids without an identity email — is a fatal
misconfiguration in `config.validate()`, because it otherwise fails closed
*silently* while the operator believes they enabled it.

## Consequences

**Every machine caller acts as one person.** `Identity.slug` derives scratch, the
bead ledger and `CLAUDE_CONFIG_DIR`, so `MCP_IDENTITY_EMAIL` decides where MCP
work lands. Per ADR 0012 this deployment is one household with no private surface,
so sharing an identity is correct rather than a compromise — **but the email must
be a real household member's**. Point it at a synthetic address and every bead
`lint` files goes into a private graph nobody's `bd ready` will ever show. That is
the same defect ADR 0012 documents, reached through a third door, and it is not
fixed here: ADR 0012's household ledger at `$WORK_DIR` remains the fix, and it
remains proposed.

**A long call can outlive an MCP client's patience.** `ingest` and `lint` are
multi-minute turns. The tool returns the turn id, so a client that gives up can
still find the turn in the web UI, but there is no resumption or progress
reporting. If that becomes the common case, the answer is the same one
`app/turns.py` already prescribes for the registry.

**`mcp` is now a direct dependency.** It was already present in the dev venv as
nothing's requirement, which would have made this work locally and fail in the
image. Declared explicitly in `requirements.txt`.

**Importing FastMCP emits a `pydantic-settings` warning** on every test run, about
a FastAPI field whose annotation is a forward reference. Nothing here can fix it,
so it is filtered in `pytest.ini` — scoped to that one warning class, because a
warning nobody can act on is how a team learns to stop reading test output.

## What was rejected

**A shared bearer token or API key in the app.** A second secret to rotate, a
second thing to leak, and it bypasses the edge that is already doing this job
properly. Access service tokens exist; the app's only defect was refusing them.

**Trusting `common_name` as an identity in its own right.** Then the bead ledger,
scratch and config dir would fork per token, and ADR 0012's collision would
multiply rather than stay at one. Mapping to a configured human keeps exactly one
household ledger.

**A local stdio bridge instead of a remote route.** It needs no new auth path and
no new production surface, which is genuinely attractive — but it only works from
a laptop with a live Access session, and the point of these four tools is that
something else can call them.

**Queueing concurrent calls.** A queue turns "the instance is busy" into "your
call is mysteriously slow", and the savepoint interference it would be hiding is
real. Refusing says the true thing.
