# 0013 — The human is a tool the agent can call

**Status:** accepted

## Context

`app/agent.py` has carried this comment since the first commit:

```python
# Headless: nobody is present to answer a permission prompt.
permission_mode="acceptEdits",
```

It was true of the process and false of the product. The process is a subprocess
with no TTY. The product is a chat window with a person in it, holding an SSE
stream open, watching the turn go past. The premise conflated the two, and three
things followed from it.

**A tool the agent needed was refused by nobody.** `allowed_tools` grants
`Bash(bd:*)` and nothing else, so a tool outside it — `WebFetch`, `WebSearch`, or
a shell command the CLI did not approve itself — produced a permission request
with no answerer. The CLI refused it,
recorded it in `ResultMessage.permission_denials`, and `signals.record_turn`
filed a **P1** bead reading "This is almost always a deployment defect — check
`allowed_tools` in `_options`". Sometimes that was right. Often the honest answer
was "ask the person who is sitting right there", and the ledger instead
accumulated P1 defect reports about a system behaving as designed.

**The agent's own way of asking is broken here.** Claude Code ships an
`AskUserQuestion` tool. Under this SDK, with no TTY, it has nowhere to draw its
prompt, so it **resolves immediately with empty answers** — see
[anthropics/claude-code#50728](https://github.com/anthropics/claude-code/issues/50728).
The agent believes it consulted someone. Nobody was consulted. That is worse than
having no such tool, because the failure is indistinguishable from success.

**Delegation was unreachable, and would have corrupted the transcript.** `Task`
was never enabled. Had it simply been switched on, `_render` would have broken:
it forwarded every `text_delta` to the reply regardless of `parent_tool_use_id`,
so a subagent's tokens would have been spliced into the middle of a sentence the
user was reading. No exception, no log line — the same class of silent failure
this codebase keeps having to design against.

**And a whole layer of the turn was invisible.** Tool *results* arrive on a
`UserMessage`, which `_render` rejected wholesale, so a `Write` that failed and a
`Write` that succeeded rendered identically: `→ write`. No path, no outcome. In a
system whose documented failure mode is silence, the UI was silent about the one
thing a person could have caught.

## Decision

Treat the human as a tool the agent can call, and the browser as the place the
call is answered.

**Asking is an in-process MCP tool, not the built-in.** `app/interact.py` builds
a per-turn SDK MCP server exposing `mcp__ask__ask_user`, which appends an `ask`
event to the turn's event buffer and blocks on an `asyncio.Future`. A new route,
`POST /api/turns/{id}/answer`, resolves it. `AskUserQuestion` goes in
`disallowed_tools`, and the system prompt says why, because an agent that knows
the tool is broken will not reach for it.

**Permission is the same mechanism.** `can_use_tool` turns an unapproved tool
into an Allow/Deny prompt in the UI, resolved through
`POST /api/turns/{id}/permission`. `permission_mode` stays `acceptEdits` — wiki
edits must never prompt, that is the product — so the callback only sees what
`acceptEdits` did not already cover.

**It sees less than that, and the difference was measured rather than reasoned
about.** The CLI approves some read-only shell commands on its own, ahead of the
callback: a live test that asked for `echo` completed with the command run and no
prompt raised at all. So the reachable set is not "everything outside
`allowed_tools`" — a `Bash` call may or may not arrive here depending on what the
command is, and that boundary belongs to the CLI, not to this code. `WebFetch`,
absent from `allowed_tools` entirely, does reach it; that is what the live tier
now asserts on. Anything relying on a *particular* Bash command prompting would
be relying on an implementation detail upstream.

This requires **streaming-mode input**: the SDK raises `ValueError` if
`can_use_tool` is set and the prompt is a plain string. `_image_prompt` already
yielded a single message generator for image turns, so it became
`_stream_prompt` and now wraps every turn. `query()` is otherwise unchanged; no
`ClaudeSDKClient`.

**The two timeouts resolve in opposite directions**, and this is the load-bearing
detail. An unanswered *question* returns text telling the agent to proceed on its
own judgement and to say in its reply that it did. An unanswered *permission
request* is **denied**. The failure to design against differs in each case: for a
question it is a turn that stalls forever on someone who closed the tab; for a
permission it is a turn spending its whole budget waiting for an approval that
was never coming. Neither is an error, and neither kills the turn.

**Interactivity is a property of the caller, not the deployment.**
`Turn.interactive` is False for a turn a machine started (ADR 0014). Such a turn
gets no permission callback — restoring the previous deny-what-is-not-preapproved
behaviour exactly — and its `ask_user` answers immediately that nobody is there.
The tool is kept rather than removed on purpose: an agent told it is alone records
the assumption it made, where an agent whose tool vanished guesses silently.

**Reflection gets none of this.** A reflection turn is triggered by a signal and
runs under the synthetic owner `reflection@{slug}`. There is nobody to answer it,
so a prompt could only spend its timeout. It keeps the observer hooks, which ask
nothing.

**A human refusal is evidence, not a defect.** `turn.human_denials` is
subtracted from `permission_denials` alongside `guard_denials`, and a refusal
files its own `deferred`, priority-3 signal bead instead. A person declining a
tool is the same species of signal as a Revert — an explicit human judgement
bound to an exact turn — and belongs in the ledger the same way. Deliberately
**not** deduped, for the reason reverts are not: each refusal is a distinct
judgement and the count is the data.

**Subagent output is tagged, and the UI nests it.** `_render` routes anything
carrying `parent_tool_use_id` to `agent_text` and never to `text_delta`. Two
named agents are declared (`kb-query`, `kb-lint`) alongside the generic one, and
each of their prompts *points at* the skill in the knowledge base rather than
restating it — those skills are edited by the human, and a copy in the image
would drift invisibly.

**Structured events carry JSON.** `_sse_escape` rewrites newlines, and
`json.dumps` never emits a raw one, so the new event kinds pass through it
untouched while `text_delta` and friends keep their existing raw-string shape.
Tool inputs, tool results, thinking, todo lists and subagent lifecycle all reach
the UI. Failures are styled; successes stay quiet.

**`strict_mcp_config=True`.** Adding `mcp_servers` made this necessary rather
than tidy: `cwd` is the agent's own *writable* scratch directory, so without it
the agent could write a `.mcp.json` there and grant itself servers.
`setting_sources=[]` governs settings files, not that one.

## Consequences

A turn can now block on a person, which means a turn can now be **slow for a
reason that is not the model**. `registry.any_running()` keeps the host awake
while it waits, and `POST /api/reflect` answers 409 for the duration. Both are
bounded by the timeouts, which is the main thing those timeouts are for.

A pending question dies with the process. The turn registry is in-process
(`app/turns.py`) and always was; a restart mid-question loses the future along
with the turn. `finish()` cancels every pending future so an errored turn does not
strand a coroutine on something nobody will resolve — but the durable fix is the
one `app/turns.py` already names, and it is not this ADR's.

`TodoWrite` is allowed and rendered, and it is **not** a task ledger. It is
in-turn progress, never persisted, and the `Stop` guard still refuses to end a
turn that deferred work without filing a bead. ADR 0006 is unchanged: beads is
the only place work survives.

The two named subagents compete with `kb-curator` in the router, which ADR 0011
warned about in the context of skills. `kb-curator`'s description was narrowed to
cede lookups to `kb-query` rather than leaving the collision to chance.

Nothing here is eval-gated. As with ADR 0008, the gate is a person reading the
turn and clicking Revert — which now also reverts a turn they answered a question
inside.

## What was rejected

**Leaving `AskUserQuestion` enabled as well.** It looks like the obvious primary
route and silently returns nothing. Two tools for one job, one of which fails
invisibly, is worse than one tool.

**A `ClaudeSDKClient` and bidirectional messaging.** `query()` with a streaming
prompt satisfies `can_use_tool` and keeps the one-message-per-turn model the whole
app is built around. Interrupts and follow-ups are a different feature.

**Making `reflect` a subagent.** Its remit is enforced by
`evolve.write_guard_for`, installed only in `_reflection_options`, plus four
things an `AgentDefinition` cannot express: a cold start with no `resume`, the
`reflect-` savepoint namespace, the process-wide reflection lock, and the
evolution log. It is technically possible to scope the guard by `agent_type`,
which `PreToolUseHookInput` carries — it just re-implements five protections to
save one. `reflect` stays a top-level turn, reachable headlessly through ADR 0014.

**Making `ingest` a subagent.** Its own skill tells the agent to check its
emphasis with the person before writing anything, and a subagent cannot. Its
per-page detail would also collapse into a summary the main agent relays, which
is exactly the detail a reviewer wants.

**A global `INTERACTIVE` config flag.** Interactivity varies per caller within
one deployment — a browser turn and an MCP turn run in the same process — so it
belongs on the `Turn`.
