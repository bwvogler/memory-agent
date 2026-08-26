"""Household-shared, durable chat conversations.

The unit of state this module adds is the conversation, not the turn (see
docs/decisions/0017). A `turns.Turn` still exists and still savepoints, but
its event stream now lives here when it belongs to a conversation, so that:

* A reload replays the WHOLE conversation, not just the in-flight turn.
* Two household members watch the same live stream (app/main.py drops the
  per-turn ownership check that used to make this impossible).
* A message sent while a turn is running can be injected into it instead of
  refused, because the running turn and the new message share one seq space.

Durability and liveness are split deliberately. `Conversation.append` assigns
a seq and wakes subscribers immediately - a client tailing the stream must not
wait on a database round trip for every token of `text_delta`. A background
flusher batches writes to Postgres at ~100ms, matching the SDK's own
transcript-mirror cadence. `Conversation.flush` can be awaited directly by a
caller that needs a guarantee before it does something the client will trust
(the terminal SSE frame), which is the one place "batched" is not good enough.

Not every turn goes through here. Reflection and `/mcp` turns were never part
of a household conversation - they keep `turns.Turn`'s own private event
buffer unchanged, which is also what keeps every existing caller of that class
working without modification. Only a turn created with a `conversation_id`
(the browser path, `app/main.py`) uses this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session_store import PostgresSessionStore

log = logging.getLogger(__name__)

FLUSH_INTERVAL_SECONDS = 0.1

# Generous in-memory tail. A reconnect within this many events of the head
# never touches Postgres; a cold process (restart, or a conversation nobody
# has touched this process's lifetime) reads its replay from the durable
# store instead - see Conversation.since's docstring.
EVENT_BUFFER_MAX = 5000


@dataclass
class ConvEvent:
    seq: int
    kind: str
    data: str
    turn_id: str | None = None
    actor: str | None = None


@dataclass
class Conversation:
    id: str
    next_seq: int = 0
    events: deque[ConvEvent] = field(
        default_factory=lambda: deque(maxlen=EVENT_BUFFER_MAX)
    )
    _pending: list[ConvEvent] = field(default_factory=list, repr=False)
    _flush_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _waiters: list[asyncio.Event] = field(default_factory=list, repr=False)

    def append(
        self,
        kind: str,
        data: str,
        *,
        turn_id: str | None = None,
        actor: str | None = None,
    ) -> ConvEvent:
        self.next_seq += 1
        event = ConvEvent(
            seq=self.next_seq, kind=kind, data=data, turn_id=turn_id, actor=actor
        )
        self.events.append(event)
        self._pending.append(event)
        self._wake()
        return event

    def _wake(self) -> None:
        for waiter in self._waiters:
            waiter.set()
        self._waiters.clear()

    async def wait_for_change(self, timeout: float) -> None:  # noqa: ASYNC109 - see turns.Turn
        waiter = asyncio.Event()
        self._waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout)
        except TimeoutError:
            pass
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def since_buffered(self, seq: int) -> list[ConvEvent]:
        """Events after `seq` that are still in the in-memory tail.

        Not the whole answer for a caller replaying from further back than
        `EVENT_BUFFER_MAX` events ago - that caller must also read
        `store.read_conversation_events(id, after_seq=seq)` for anything
        older than `earliest_buffered_seq`. See app/main.py's SSE route.
        """
        return [e for e in self.events if e.seq > seq]

    @property
    def earliest_buffered_seq(self) -> int:
        """The seq before the oldest event still held in memory.

        Equals `next_seq` (i.e. "nothing buffered, ask Postgres for
        everything") when the buffer is empty - a fresh Conversation object
        after a restart, before anything has been appended in this process.
        """
        return self.events[0].seq - 1 if self.events else self.next_seq

    async def flush(self, store: PostgresSessionStore | None) -> None:
        """Drain pending events to Postgres.

        Await this directly when a caller needs durability guaranteed before
        it does something the client will trust - the terminal SSE frame, in
        particular, must never claim `done` over events that are not yet
        safely stored (see turns.Turn.finish and app/main.py).
        """
        async with self._flush_lock:
            if not self._pending or store is None:
                return
            batch, self._pending = self._pending, []
            try:
                await store.append_conversation_events(
                    self.id,
                    [
                        {
                            "seq": e.seq,
                            "turn_id": e.turn_id,
                            "kind": e.kind,
                            "data": e.data,
                            "actor": e.actor,
                        }
                        for e in batch
                    ],
                )
            except Exception:
                log.exception(
                    "failed to flush %d event(s) for conversation %s; will retry",
                    len(batch),
                    self.id,
                )
                # Put back at the FRONT: a later flush must not reorder these
                # ahead of events appended since, which would write seq out
                # of order (harmless to the unique key, but confusing to read).
                self._pending = batch + self._pending


class ConversationRegistry:
    """In-process cache of live Conversations, backed by Postgres.

    Deliberately unbounded (unlike turns.Registry, which evicts): a household
    has few enough conversations that holding them all costs nothing, and
    unlike a Turn a Conversation is never "finished" - there is no eviction
    rule that would not eventually forget one still in the sidebar.
    """

    def __init__(self) -> None:
        self._conversations: dict[str, Conversation] = {}
        self._store: PostgresSessionStore | None = None
        self._flusher_task: asyncio.Task[None] | None = None

    def attach_store(self, store: PostgresSessionStore | None) -> None:
        self._store = store

    async def get_or_load(self, conversation_id: str) -> Conversation:
        """The live Conversation, creating it and seeding `next_seq` on first
        touch this process (e.g. after a restart, or the first message since
        boot). Idempotent: a second call for an already-loaded id is free."""
        conv = self._conversations.get(conversation_id)
        if conv is not None:
            return conv
        conv = Conversation(id=conversation_id)
        if self._store is not None:
            try:
                conv.next_seq = await self._store.max_conversation_seq(conversation_id)
            except Exception:
                log.exception(
                    "could not seed seq counter for conversation %s from the store; "
                    "starting at 0, which is only safe for a conversation that has "
                    "never been flushed",
                    conversation_id,
                )
        self._conversations[conversation_id] = conv
        return conv

    def get(self, conversation_id: str) -> Conversation | None:
        return self._conversations.get(conversation_id)

    async def flush_all(self) -> None:
        for conv in list(self._conversations.values()):
            await conv.flush(self._store)

    async def flush_one(self, conversation_id: str) -> None:
        """Await durability for one conversation - see turns.Turn.finish's
        caller in app/agent.py, which needs this before a client is told the
        turn is `done`."""
        conv = self._conversations.get(conversation_id)
        if conv is not None:
            await conv.flush(self._store)

    # -- thin delegation to the store, so agent.py need not import it directly --
    #
    # Every method here logs-and-continues on failure rather than raising: a
    # turn that cannot reach the durable store should still run, the same
    # call app/kb.py makes about beads. What matters is that the turn's
    # events themselves are still flushed (they retry - see Conversation.flush)
    # even if this bookkeeping row is not.

    async def record_turn_start(
        self, turn_id: str, conversation_id: str, actor_email: str
    ) -> None:
        if self._store is None:
            return
        try:
            await self._store.create_conversation_turn(
                turn_id, conversation_id, actor_email
            )
        except Exception:
            log.exception("could not record the start of turn %s", turn_id)

    async def record_turn_savepoint(self, turn_id: str, savepoint: str) -> None:
        if self._store is None:
            return
        try:
            await self._store.set_conversation_turn_savepoint(turn_id, savepoint)
        except Exception:
            log.exception("could not record the savepoint for turn %s", turn_id)

    async def record_turn_end(self, turn_id: str, state: str) -> None:
        if self._store is None:
            return
        try:
            await self._store.finish_conversation_turn(turn_id, state)
        except Exception:
            log.exception("could not record the end of turn %s", turn_id)

    async def record_session(self, conversation_id: str, session_id: str) -> None:
        """Remember the SDK session to `resume=` next time - see run_turn."""
        if self._store is None:
            return
        try:
            await self._store.set_conversation_session(conversation_id, session_id)
        except Exception:
            log.exception(
                "could not record session %s for conversation %s",
                session_id,
                conversation_id,
            )

    def start_flusher(self) -> None:
        if self._flusher_task is not None:
            return
        self._flusher_task = asyncio.create_task(
            self._flush_loop(), name="conversation-flusher"
        )

    async def stop_flusher(self) -> None:
        if self._flusher_task is None:
            return
        self._flusher_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._flusher_task
        self._flusher_task = None
        # A final flush so nothing appended just before shutdown is lost.
        await self.flush_all()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            try:
                await self.flush_all()
            except Exception:
                log.exception("conversation flush loop failed; will retry")


conversations = ConversationRegistry()
