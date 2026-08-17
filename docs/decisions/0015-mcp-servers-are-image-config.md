# 0015 — MCP servers are image config, because a capability is not content

**Status:** accepted

## Context

`kb-b82` asks for Google Calendar, and specifies the last step as "add the MCP
server config to `.claude/settings.json`". That route does not exist in this
deployment, and has not since ADR 0013:

```python
setting_sources=[],        # no settings file is ever read
strict_mcp_config=True,    # no .mcp.json beside the agent's cwd either
mcp_servers={"ask": interact.ask_server_for(turn)} if turn else {},
```

Both suppressions were deliberate — the first for multi-tenant isolation, the
second because `cwd` is the agent's own *writable* scratch, so without it the
agent could drop a `.mcp.json` there and grant itself servers. Between them they
close every documented way of configuring an MCP server except the
`mcp_servers=` dict, which was hard-coded to hold exactly one in-process server.

So **no MCP server could be added at all**, by anyone, by any route. That is why
this bead blocks `kb-b82` rather than following it, and why the dependency edge
between them was reversed. The question is not "where would it be convenient to
put a server list" but "who is allowed to grant this agent a new capability, and
through what".

## Decision

**Definitions live in the image; secrets live in the environment; neither is
writable at runtime.** `app/mcp_catalog.py` holds a `CATALOG` of frozen `Server`
records, each naming the environment variables it needs rather than holding
their values. `_options` merges `mcp_catalog.resolved()` into `mcp_servers=`
beside the `ask` server. `CATALOG` ships empty, so nothing changes until someone
adds an entry *and* sets its secrets.

### The knowledge base is disqualified, and this is forced rather than preferred

`$KB_MOUNT/memory/` is the obvious home — it is where `AGENT_GUIDE.md` and the
editable skills live, and the human already maintains it by asking the agent. It
fails on three counts at once. It is agent-writable under `acceptEdits`. It is
served to any authenticated browser by `/api/kb/file`, so a credential in it is
a credential on a web page. And it is captured in savepoints and `kb.git`, so a
credential in it is also a credential in git history, on an unreplicated volume,
recoverable by anyone who can read either.

### Why the agent may read the catalog and never write it

Three reasons, in increasing order of force. Only the third is decisive, and the
first is worth stating mainly so nobody mistakes it for the argument.

**1. Mechanical, and weak.** `app/` is in neither `cwd` nor `add_dirs`, the
container filesystem is not the volume, and the module is imported once at
startup. This is "we put it somewhere awkward", and awkwardness is not a
security property.

**2. A catalog entry is a `command` this container executes, with nothing in
front of it.** Look at what `_options` actually guards: `Bash` is narrowed to
`Bash(bd:*)`, everything else falls through to `can_use_tool`, and ADR 0007's
`kb_write_guard` hangs off `matcher="Bash"`. All of that is *tool-call*
machinery. Launching a stdio MCP server is the SDK spawning a subprocess before
the model does anything at all — no `PreToolUse` hook fires, and `can_use_tool`
is never consulted. A writable catalog would therefore be a hole straight
through the Bash allowlist, wide enough to run anything, reached by writing a
file rather than by asking.

**3. A writable catalog plus an existing secret is a credential-exfiltration
primitive.** Splitting definitions from secrets means an agent-authored entry
cannot invent `MCP_GOOGLE_REFRESH_TOKEN`. It does not need to. It can point an
*existing* secret at a command of its choosing, and `resolved()` will faithfully
inject the value into that process's environment. The split protects the value
at rest and says nothing whatever about where it gets aimed. **Immutability is
what makes the split mean anything at all**; without it the two halves of this
decision would cancel out.

Underneath all three is the property that makes this whole system defensible:
**savepoints cover `$KB_MOUNT/memory`, and they do not cover your calendar.**
Wiki pages, skill descriptions under the ADR 0008 remit, and the bead graph are
all *content*, inside the blast radius of one Revert click — which is precisely
the argument for letting an agent write them. A capability grant is different in
kind. By the time anyone notices, the tool has already run and reached outside
this system, and there is nothing to roll back. Content is revertable, so the
agent writes it; capability is not, so a human does.

The agent's channel for wanting a server is a bead, which is the same "propose,
a human decides" route ADR 0006 already established and needs no new machinery.
It *may* read the catalog: it sees the tools regardless, so hiding names would
be theatre. It never sees a secret's value.

### stdio is supported, and the costs are real

A remote HTTP server would be lighter — a URL and a bearer token, nothing new
running in the container. But there is no first-party hosted MCP for Google
Calendar, so remote-only would mean routing household calendar data through a
third-party broker. `Server` therefore carries `command`/`args`/`secrets`, and
the first integration will bake a **pinned** npm package into the Dockerfile —
pinned for the reason bd is pinned and the reason `cloudflared` was removed in
`img-753`, which tracked `releases/latest` and could ship a different binary on
every rebuild. The costs to hold against that: image weight against the 2 GB
suspend ceiling in `fly.toml`, a Node process spawned per turn, and owning the
OAuth client.

### Two-tier approval

`Server.auto_approve` lists *unqualified* tool names that reach `allowed_tools`
as `mcp__{name}__{tool}`. Everything else the server exposes still works — it
falls through to `can_use_tool`, which ADR 0013 made into an Allow/Deny in the
UI on a turn a person is watching, and a silent denial on a machine turn. Put
read-shaped tools in `auto_approve` and leave writes out, and you get "read my
calendar freely, ask me before creating an event" for nothing.

Names, never a wildcard. `mcp__calendar__*` would silently pre-approve every
tool the server gains at its next version bump, including whichever one it
learns to delete with.

### Household-shared, said out loud

One credential per server, shared by every user. This deployment is one
household rather than many tenants (ADR 0012), and `MCP_IDENTITY_EMAIL` already
makes the same call for the inbound direction (ADR 0014). The consequence is
stated rather than buried: **enabling a server enables it for everyone**, and
behind one token the app cannot tell household members apart.

For a calendar that means a dedicated household Google account each person
shares their calendar with. A refresh token belongs to one Google account and
reaches every calendar shared *with* that account, at whatever level each person
granted — free/busy, read, or write. Consumer Gmail has no domain-wide
delegation, so it is per-calendar sharing either way; the choice is only whether
the hub is a shared account or one person's.

### A missing secret disables its server loudly

An entry whose variables are unset is dropped from `mcp_servers=` and from
`allowed_tools`, with one warning naming the variable, and `/healthz` reports
`mcp_catalog: {"calendar": "missing MCP_..."}`. Launching it instead would fail
at first use, which is later and much quieter. This is the same call `kb.py`
makes about beads and the same reason `transcripts` is reported separately from
`ok`: the failure mode of this system is silence, and "the calendar tools are
gone" must not look identical to "the calendar tools never existed".

The warning is logged once per distinct fault rather than per lookup, because
`_live()` runs three times per turn — servers, allowlist, prompt — and three
identical lines per turn is how a log teaches people to skip a line.

### Reflection gets no connected service

`_reflection_options` builds no `mcp_servers` at all, alongside its other
narrowings. Reflection judges evidence already in the ledger and rewrites one
skill within the ADR 0008 remit; reaching outside is not part of that. It is
also the worst turn to hand an outbound capability to — nobody is watching, so
`can_use_tool` is absent by design.

## Consequences

`kb-b82` becomes a small diff: one `CATALOG` entry, one pinned line in the
Dockerfile, and `fly secrets set`. `img-n7g` keeps everything genuinely
downstream of a first integration — the UI, and per-user OAuth, which needs a
token store and a callback route that do not exist.

Adding a server requires a deploy. For a household with one integration that is
honest rather than burdensome, and it is the direct cost of the decision above:
the review *is* the security control.

## Alternatives rejected

**A JSON file in the KB.** Editable by asking the agent, visible in the wiki
view, revertable. Every one of those is a defect here: see above.

**`MCP_SERVERS_JSON`, the whole catalog as one secret.** Adding a server needs
no deploy, only `fly secrets set` and a restart — genuinely attractive. But the
catalog stops being reviewable in git and testable in CI, a typo becomes a
runtime failure rather than a test failure, and the reviewability is the control
being traded away.

**A file on the volume at `/work`.** Survives deploys, suits rotating tokens,
edited with `scripts/fly.sh --write`. But the volume is unreplicated with no
savepoint covering it, and it is one permission-callback Allow away from being
agent-reachable — a weaker version of the KB objection rather than an escape
from it.

**Remote HTTP only.** Lighter, and no Node in the image. Rejected because for
this first integration it means a broker sees the household's calendar.

**Letting the agent toggle an already-reviewed server off.** No command chosen,
no secret moved, so genuinely safer than a writable catalog. Left out because
nothing wants it yet and it is additive later.

**Per-user credentials now.** Correct, and most of `img-n7g`. Deferred so the
config-location question could be answered without also building a secret store
and an OAuth callback — but note that "household-shared" is a *decision* here,
not an oversight, and reversing it later means reversing this paragraph.
