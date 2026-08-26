# 0017 — The conversation is the unit, not the turn

**Status:** accepted

## Context

Every piece of chat state in this app lived on the `Turn`: `turns.Turn.events`
is a plain list, `Registry` is a bounded in-process dict, and `sessionId` on
the client is a bare JS variable set from the SSE `session` event. None of
that survives a reload — a fresh `EventSource` carries no history, only
whatever turn happened to be running gets replayed, and `sessionId` resets to
`null`, so the *next* turn is a cold SDK session, not just a cold UI. ADR 0016
already named this: "a reload during a turn cannot reconstruct the user's own
message text... not a general transcript-durability fix." `kb-nb4` asked for
the fix directly: history that survives a reload, with session continuity.

Three more cracks showed up once a household — not a single user — was taken
seriously (ADR 0012).

**Ownership meant "the only viewer."** Every turn route compared
`turn.user_email` against the caller's verified email and 404'd on a
mismatch. That is not a security boundary — `auth.verify()`'s allowlist
already is one — it is a second, narrower rule left over from a single-user
design, and it meant a second household member could not so much as watch a
turn in progress, let alone answer a question on it.

**One turn at a time was global, and a second person's message just bounced
off it.** `turns.Registry.begin` refuses correctly (savepoints are
workspace-wide — ADR 0009), but the refusal had no relationship to *who* was
running or *which conversation* they were in. Two people talking about
different things collided exactly as hard as two tabs racing the same turn.

**There was no way to stop a turn.** `img-r7o` records a real 16-minute prod
hang from a botched attempt to add `ClaudeSDKClient.interrupt()` — reverted
the next day. A stuck turn is never evicted (only finished turns are,
`Registry._evict`) and blocks every later turn on the instance indefinitely.

## Decision

**Move the event stream from the turn to a new, durable, household-shared
`Conversation`, and make everything else fall out of that.**

`app/conversations.py` adds `Conversation` (an append-only, seq-numbered
event log with an in-memory tail plus a ~100ms-batched Postgres flush) and
`ConversationRegistry`, a process-wide cache backed by three new tables —
`conversations`, `conversation_events`, `conversation_turns` — in
`session_store.SCHEMA`. `conversation_events.data` is `TEXT`, not `JSONB`, on
purpose: an event's data is sometimes a JSON payload and sometimes a raw
string (`text_delta`, `session`), and round-tripping the latter through
`jsonb` would corrupt it.

`turns.Turn` gains an optional `conversation_id`. When set, `Turn.append`
forwards into the conversation instead of the turn's own local list;
`conversation_id is None` (reflection, `/mcp`) keeps the exact old behavior
unchanged, which is what let every existing caller of `Turn` — signals,
guards, interact, `mcp_server._text` — stay untouched. Only the browser path
(`app/main.py`) ever sets it.

`app/main.py` replaces `POST /api/turns`, `GET /api/turns/{id}`, and
`GET /api/turns/{id}/events` outright with `GET`/`POST /api/conversations`,
`PATCH /api/conversations/{id}`, `GET /api/conversations/{id}/events`
(replays from `Last-Event-ID`, where `0` now means "the whole conversation,"
falling back to Postgres for anything older than the in-memory tail), and
`POST /api/conversations/{id}/messages`. Removed rather than kept as compat
shims: this shipped as one pass, not a staged rollout, so there was no
in-flight client to keep working against the old routes.

**Turn-taking by injection, not a second lock.** `_stream_prompt` becomes
`agent._input_stream`: for a conversation turn it stays open after its first
message, reading from a new `Turn.inbox` (an `asyncio.Queue`, created only
when `conversation_id` is set) until a `ResultMessage` arrives with nothing
queued, at which point `_run_turn` pushes `None` to close it. `POST
/api/conversations/{id}/messages` checks `registry.running()`: a turn already
running for *this* conversation gets the new message pushed onto its inbox
instead of a 409; a turn running for a *different* conversation still
refuses, unchanged, and the refusal now names who is busy
(`display_name_for(running.actor_email)`) instead of just repeating `BUSY`.
`Registry.begin` itself is untouched — one turn at a time, instance-wide, is
still the rule, only the caller's decision about what to do with a busy
instance changed.

**`POST /api/turns/{id}/stop`** cancels the task `spawn()` returns (now kept
on `Turn.task`). `_run_turn` catches `CancelledError`, sets
`terminal_reason = signals.OUTCOME_STOPPED`, finishes as `DONE` (the person
meant to end it — it is not a failure), and re-raises. `signals._outcome`
checks `OUTCOME_STOPPED` first so a stop files no "turn failed" bead, the same
reasoning `human_denials` already uses for a permission refusal. Every turn is
additionally wrapped in `asyncio.timeout(config.turn_timeout_seconds)` — the
backstop `img-r7o` asks for before anyone touches this loop again, independent
of `interrupt()`.

**Ownership checks dropped, not narrowed.** `revert_turn`, `_pending_turn`
(backing `/answer` and `/permission`) lost their `turn.user_email !=
identity.email` comparison; any allowlisted household member can now watch,
answer, or revert any turn. `GET /api/uploads/...` is unaffected — its
ownership-by-path property (ADR 0016) is stronger than the compare ever was
and had nothing to do with this rule.

**Attribution, so a shared conversation is legible rather than merely
visible.** `auth.display_name_for(email)` resolves a `HOUSEHOLD_NAMES`
config map (same shape as `MCP_CLIENT_IDS`/`MCP_IDENTITY_EMAIL`, ADR 0014),
falling back to the email's local part — never blank. The speaker's name is
prefixed onto the message *text* sent to the model (`agent._speaker_prefix`),
not a separate content block, specifically so it survives into the CLI's own
transcript and is still there after a `resume=`. A new system-prompt block,
`_SHARED_CONVERSATION`, is appended only when `turn.conversation_id` is set,
telling the model more than one person may be speaking and that "you" is not
guaranteed to mean the same person twice. `answered`/`permission_resolved`
events and the text handed back to the agent now name who actually answered,
when it differs from who was asked (`interact._format_answer`,
`_request_permission`) — otherwise a second person's answer reads to the
model as the original asker changing their mind.

**Turn boundaries, detected without a protocol rewrite.** The client used to
own one `EventSource` per turn; it now opens exactly one per conversation and
never closes it except to switch conversations. The only new information the
client needs that the old wire format could not carry is *which turn* a
`user_message` belongs to and *who sent it* — every other event kind (tool
use, thinking, ask, todo...) already only ever belongs to whichever turn is
currently live, since at most one turn runs at a time. So only
`user_message`'s JSON payload grew `turn_id`/`actor` fields; nothing else
needed a systemic envelope change. `static/app.js`'s `turnUI` object is
recreated whenever an incoming `user_message`'s `turn_id` differs from the
current one (a fresh turn) and left alone when it matches (an injected
follow-up) — which is also what makes replay and live rendering the exact
same code path. The user's own bubble is no longer built client-side at
submit time at all; it is rendered purely from the `user_message` echo, for
every sender alike.

## Consequences

A savepoint now potentially covers work prompted by two different people (an
injected follow-up shares the first message's turn and savepoint), so
Revert rolls back both. That is the honest cost of leaving per-user savepoint
scoping (`img-0pv`) unaddressed — strictly better than before, where the
second person's message was refused outright, but not free.

**Deferred, not attempted, each for a stated reason:**

- *Presence / typing indicators.* `Conversation`'s subscriber-wake mechanism
  (the same `asyncio.Event` waiter list `turns.Turn` already used) would
  support a `presence` event trivially; none was wired up. Time, not
  difficulty.
- *The `SessionStore` SDK-protocol rewrite* (`session_store=`, which would
  fix `img-2jj` — `PostgresSessionStore.append` does not match SDK 0.2.136's
  `SessionKey` shape, so `agent_sessions` stays empty). Not a functional gap:
  `conversations.session_id` is stored from the `session` event and passed as
  `resume=`, and the CLI's own on-disk transcript under
  `CLAUDE_CONFIG_DIR=$WORK_DIR/.claude-{slug}` already survives a restart
  because it is on the volume. `img-2jj` stays open; this is a durability
  upgrade against losing the volume, not against losing a reload.
- *Real `ClaudeSDKClient.interrupt()`.* Deliberately not reattempted.
  `img-r7o` is a live 16-minute prod hang from exactly this migration off the
  one-shot `query()` function; injection was chosen specifically because it
  reaches the same user-visible goal (steer a running turn) without needing
  it.
- *Attribution for reflection/`/mcp` turns pretending to be a household
  member.* Turned out to be moot rather than merely deferred: both keep
  `conversation_id = None` and their own private per-turn buffer exactly as
  before, so neither structurally ever joins a shared conversation stream.
  Simpler than the "render as system / via MCP" handling this ADR originally
  meant to add, and there is nothing to fix as long as that stays true.
- *Everything filed as a backlog rather than a commitment in the design that
  led here* — `@agent` addressing so two people can talk without spending a
  turn, auto-titling, full-text search over `conversation_events`,
  conversation-to-KB provenance, revert-to-a-specific-message, a context
  budget warning. None built.

## Note on verification

Static: `ruff check app tests` and `ty check app tests` both clean; all 334
fast `pytest` tests pass (four updated for the removed routes and the new
injection path, one added specifically for injection — a second household
member's message into an already-running turn for the same conversation
returns `injected: true` against the *running* turn's id, not a new one).

Live: driven against the real `docker compose` stack
(`DEV_BYPASS_AUTH=1`) with Playwright, not reasoned about from source. A real
turn (against a since-expired local test key, which exercised the *failure*
path rather than success) confirmed: the `user_message` event renders the
sender's own bubble — nothing is drawn before the server echoes it back; the
working indicator and Stop button appear while the turn runs and clear on
`turn_failed`; the error text and Revert button render correctly; a full page
reload replays the entire conversation, not just the last turn; "New chat"
produces a genuinely empty conversation; and switching back via the picker
restores the original history intact — all with zero browser console errors.
One real bug surfaced by this and fixed before it shipped: a freshly created
conversation had no matching `<option>` in the picker yet, so
`<select>.value = id` was silently ignored by the browser and the picker
showed blank. `main.py`'s "one turn at a time, instance-wide" 409 was also
observed firing correctly and legibly across two unrelated test runs that
raced each other by accident, which is closer to a live demonstration of the
rule than a synthetic test would have been.
