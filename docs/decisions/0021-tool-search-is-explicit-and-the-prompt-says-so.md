# 0021 — Tool search is on by default, so the prompt must not say it is off

**Status:** accepted

## Context

`app/mcp_catalog.py` told the agent, in every turn that had a connected
service, that "there is no tool-search or deferred-loading step in this
environment, and no such tool exists here to call." That sentence was added in
`cb20583` after a resumed chat claimed Google Calendar tools were unreachable
through a "ToolSearch" registry and then invented tool names that the calendar
server does not expose. The reasoning was that `betas` is never set in
`app/agent.py`, so nothing here could enable deferral.

The reasoning was wrong, and it is the kind of wrong that reads as careful. The
bundled CLI defers MCP tool schemas behind a `ToolSearch` tool unless told
otherwise; no SDK option turns it on because it is already on. The sentence
therefore told the agent something false about its own environment, on exactly
the turns where it was about to decide whether to call a tool it could not yet
see. A model that has been told "search does not exist" and is then shown a
`ToolSearch` tool has two options, and one of them is to guess a name.

It also became a blocker for the GitHub work. `github-mcp-server` exposes dozens
of tools, and a deployment that loads every schema upfront pays for all of them
on every turn whether or not a turn uses them.

## Decision

**Deferred tool loading is the explicit, configured behaviour, and the prompt
describes it accurately.**

- `config.tool_search` (`AGENT_TOOL_SEARCH`, default `"true"`) is passed to the
  CLI as `ENABLE_TOOL_SEARCH` on ordinary turns. `"true"`, not `"auto"`: under
  `auto` the CLI defers only once tool definitions pass a share of the context
  window, so the agent's world would change whenever a server was added or a
  schema grew, which is the same inconsistency that produced the invented names.
- Reflection turns pass `"false"`. They hold no catalog and a small fixed tool
  set, so there is nothing worth deferring and a search step is one more thing
  to go wrong in a turn whose remit is already narrow (ADR 0008).
- `mcp_catalog.summaries()` now says the opposite of what it said: a service's
  tools load on demand, run `ToolSearch` before the first call to a service,
  call only names a search returned, and never guess one.
- `kb-query` and `kb-lint` gain `ToolSearch` in their explicit `tools` lists,
  because they name `mcp__wiki__search` and a subagent with an explicit list
  sees only what it names.
- `ToolSearch` stays out of `disallowed_tools`. Excluding it turns the feature
  off silently, which would make `AGENT_TOOL_SEARCH` a setting that does
  nothing.

## Consequences

**`wiki` and `ask` are deferred like everything else, and that is a real
cost.** The CLI has a per-server `alwaysLoad` and a per-tool
`_meta["anthropic/alwaysLoad"]`, but neither is reachable from here: the Python
server config types carry no such field in the pinned SDK (0.2.136) or in the
newest at the time of writing (0.2.166, bundling CLI 2.1.299), and
`create_sdk_mcp_server` sets `_meta` only for `maxResultSizeChars`. The route
that would work is handing the SDK a hand-built `mcp.server.Server` whose tool
list carries the marker, which means rewriting `app/search.py` and
`app/interact.py` off the `@tool` helper. Not taken: the price is one extra
search the first time a turn wants either tool, and the system prompt already
names `mcp__wiki__search` and `mcp__ask__ask_user` exactly, so a `select:`
search for them is cheap. The case to watch is `ask_user`, since a question the
agent cannot find is a turn that never asks it. That is a reason to look at
real turns before rebuilding two servers, not after.

**The subagent `ToolSearch` grant is unverified.** The documentation says
subagents with an explicit `tools` list see only what they name, and is silent
on whether a foreground subagent can reach `ToolSearch` at all. It was added on
the strength of that silence being harmless, and the live subagent test passes
with it, but that test asserts transcript routing and not that the subagent
searched. Nobody has watched `kb-query` use it.

**Denial survives deferral, which is the property the deny tier rests on.**
`Server.deny` (ADR 0015) is the only control between the agent and Gmail's
send tools, so whether `disallowed_tools` still binds a tool found through
`ToolSearch` was worth proving rather than assuming.
`test_a_denied_mcp_tool_stays_denied_when_tool_search_defers_it` runs the real
CLI with `ENABLE_TOOL_SEARCH=true` and a stub server, and asserts on
*execution*: it requires that a `ToolSearch` call happened, that the allowed
tool ran, and that the denied one did not. The denied tool is deliberately in
`disallowed_tools` only, which is the shape production has. What it does not
establish is whether search *returns* a denied tool; only that calling it
fails.

**An unrelated cascade in the live tier turned out to be this ADR's neighbour,
and is recorded here because it looked like a regression from this change.** A turn that files a signal
bead starts an automatic reflection after the client has been told `done`, and
that reflection is a real model call holding the whole instance
(`agent.maybe_reflect`, ADR 0009's one-turn rule). A live test's next POST
lands on a 409 it did nothing to deserve, which test depends on timing, and the
bare `.json()["turn_id"]` turned each one into a `KeyError` with no body.
Reproduced on clean `main` before any change here. `tests/conftest.post_when_free`
now retries a 409 up to a deadline, and `test_container.py` still pins the
refusal itself. The residual risk is a reflection that outlasts the helper's
300 seconds. The slowest live test in a full run took 310 seconds, most of it
spent waiting out one, so the margin is not generous.

**A version bump of the CLI is now a behavioural change to re-measure.** The
default being "on" is a CLI default, and CLI defaults move. Setting it
explicitly means a bump that changed it cannot silently reopen the false-prompt
defect, but a bump that changed what *deferred* means would still be ours to
find.

## Note on verification

Measured, with a throwaway in-process server on CLI 2.1.228 (SDK 0.2.136): the
init message's tool list contained `ToolSearch` with `ENABLE_TOOL_SEARCH` unset
(28 tools) and `true` (27), and did not with `false` (26). That establishes the
search tool exists, not that a given tool was deferred; the live test is the
observation that a stub tool was reached through a search rather than called
directly. The test was mutation-checked: removing the `disallowed_tools` entry
made the denied tool execute and the test fail.

Measured on the SDK side by reading the downloaded 0.2.166 wheel's sources:
no `alwaysLoad` field on any server config type, and `_build_meta` emitting only
`anthropic/maxResultSizeChars`.

Read from documentation and not observed: the `auto` threshold and its default
share of the context window, that `alwaysLoad` works on every server type, and
that tool search is unsupported on models before Sonnet 4 and Haiku 4.5. All
three are claims about the CLI that this deployment currently never exercises.

Not measured at all: any of this against the production machine. The dev stack
was used for the live tier, which builds the same image, and the CLI is bundled
in the SDK wheel, so the version should match; nobody has checked prod's
`ENABLE_TOOL_SEARCH` from the inside.
