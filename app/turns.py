"""In-memory turn registry with replayable event buffers.

Why turns are detached from the HTTP request:

An agent turn can take minutes. If one HTTP request spans the whole turn, then
a dropped connection, a closed laptop, an expiring Cloudflare Access session,
or a proxy hiccup all destroy work that was nearly finished. So:

    POST /turns              -> 202 + {turn_id}, agent runs detached
    GET  /turns/{id}/events  -> SSE, replays from Last-Event-ID
    GET  /turns/{id}         -> plain polling fallback

Because turn state lives server-side, a reconnect costs nothing: the client
replays from where it left off.

This implementation is deliberately in-process, which means turns are lost if
the container restarts mid-turn. That is an accepted tradeoff for a
single-container deployment - see the "Scaling past one container" section of
docs/architecture.md for what to change first (move this registry into Redis or
Postgres and pin sessions by consistent hash).
"""

from __future__ import annotations

import asyncio
import enum
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .conversations import conversations

if TYPE_CHECKING:
    from collections.abc import Coroutine

# The event loop keeps only a WEAK reference to a running task. A turn detached
# with a bare asyncio.create_task() and no other reference can therefore be
# garbage-collected mid-flight: the turn stops, no exception surfaces, and the
# SSE stream simply never reaches a terminal event. That is precisely the silent
# failure this codebase keeps having to design against, so every detached task
# goes through spawn() and is held until it finishes.
_background: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    """Run a coroutine detached from its caller, keeping it alive until done."""
    task = asyncio.create_task(coro, name=name)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


class TurnState(enum.StrEnum):
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


BUSY = (
    "a turn is already running, and only one runs at a time because savepoints "
    "cover the whole workspace. Try again in a moment."
)


class TurnInProgressError(RuntimeError):
    """Raised by Registry.begin when a turn is already in flight.

    This is a correctness constraint wearing the costume of a throttle.
    `kb.create_savepoint` is a `git add -A` and a commit over the single shared
    workspace at $KB_MOUNT/memory: the savepoint NAME is per turn, the content
    is global. So two turns at once do not merely compete for CPU - one turn's
    half-written files are swept into the other's savepoint, and reverting
    either rolls back both. Revert is what makes writing to the wiki reviewable
    (ADR 0003) and bounded self-modification defensible (ADR 0008); overlapping
    turns leave the button in place and quietly hollow it out.

    Refusing is the interim answer, not the final one. Scoping savepoints per
    user would let concurrent turns actually run - see img-lsp and ADR 0009,
    which names this as the ceiling that has to go before a second machine
    could ever help.
    """

    def __init__(self, running: Turn) -> None:
        self.running = running
        super().__init__(BUSY)


@dataclass
class Event:
    seq: int
    kind: str
    data: str


@dataclass
class Turn:
    id: str
    user_email: str
    # Set only for a turn started from the browser (app/main.py). When set,
    # this turn's events live on the shared app/conversations.Conversation
    # instead of the local `events` list below, so a reload and a second
    # household member both see them. Reflection and /mcp turns leave this
    # None and keep using `events` exactly as before - see the module
    # docstring in app/conversations.py for why that split is deliberate.
    conversation_id: str | None = None
    # Who to attribute this turn's `user_message` event to. None for
    # reflection/mcp turns, which are not a person speaking in a household
    # conversation. See docs/decisions/0017.
    actor_email: str | None = None
    session_id: str | None = None
    savepoint: str | None = None
    state: TurnState = TurnState.RUNNING
    error: str | None = None
    events: list[Event] = field(default_factory=list)
    # Held so POST /api/turns/{id}/stop can cancel it. None until spawn() sets
    # it, and always None for a turn with no stop route (reflection).
    task: asyncio.Task | None = field(default=None, repr=False)
    # Messages sent while this turn is already running, for the same
    # conversation - injected rather than starting a second turn. None for a
    # turn that does not support injection (conversation_id is None).
    inbox: asyncio.Queue | None = field(default=None, repr=False)

    # Whether a human is watching this turn and can answer it. True for a turn
    # started from the browser, False for one started by a machine caller over
    # /mcp - which decides whether the agent gets a permission callback at all,
    # since a prompt nobody can answer only wastes the timeout. Interactivity is
    # a property of the CALLER, not of the deployment.
    interactive: bool = True

    # Signal capture (app/signals.py). The prompt is kept because the revert
    # handler needs it long after run_turn has returned, and skills because
    # attributing a revert to anything requires knowing what the turn read.
    prompt: str = ""
    skills: set[str] = field(default_factory=set)
    terminal_reason: str | None = None
    permission_denials: list[str] = field(default_factory=list)
    # Tools the HUMAN refused, as opposed to ones a guard or the permission
    # system refused. Same reason guard_denials exists below: signals files a P1
    # "check allowed_tools" bead for an unexplained denial, and a person
    # clicking Deny is not a deployment defect.
    human_denials: list[str] = field(default_factory=list)
    # Where each denied call was aimed, keyed by tool name - a list because a
    # bead is filed once per tool name even if it was denied on several
    # distinct targets in one turn. A dict rather than folding this into
    # human_denials/permission_denials themselves: those are compared as SETS
    # OF NAMES to compute signals.py's `unexpected`, and giving their elements
    # a richer shape would silently break that arithmetic.
    denial_details: dict[str, list[str]] = field(default_factory=dict)

    # Observability captured from hooks rather than from the message stream:
    # which subagents ran, and which tool calls failed. Both are things the UI
    # showed no trace of before.
    subagents: list[dict] = field(default_factory=list)
    tool_failures: list[str] = field(default_factory=list)

    # Self-evolution (app/evolve.py). `evolved` holds the bounded skill edits
    # this turn was allowed to make - empty for every ordinary turn, and the
    # thing a later Revert consults to mark a proposal rejected.
    reflection: bool = False
    evolved: list = field(default_factory=list)
    # Tools OUR OWN hooks refused, as opposed to ones the permission system
    # refused. Without the distinction a guard doing its job files a P1 bead
    # reporting itself as a deployment defect.
    guard_denials: list[str] = field(default_factory=list)

    # Questions and permission requests this turn is blocked on, keyed by
    # request id. `resolved` keeps the answers so a client replaying the event
    # stream from Last-Event-ID can tell an answered question from a live one
    # and does not draw a second form for it.
    pending: dict[str, asyncio.Future] = field(default_factory=dict, repr=False)
    resolved: dict[str, dict] = field(default_factory=dict)

    _waiters: list[asyncio.Event] = field(default_factory=list, repr=False)

    def append(self, kind: str, data: str, *, actor: str | None = None) -> Event:
        """Record one event.

        Routed to the shared Conversation when this turn belongs to one -
        that is what makes it visible to a reload and to a second household
        member. `actor` is only meaningful there (it becomes
        `conversation_events.actor`); a reflection/mcp turn's local buffer has
        no such column and ignores it.
        """
        if self.conversation_id is not None:
            conv = conversations.get(self.conversation_id)
            if conv is not None:
                conv_event = conv.append(
                    kind, data, turn_id=self.id, actor=actor or self.actor_email
                )
                return Event(
                    seq=conv_event.seq, kind=conv_event.kind, data=conv_event.data
                )
        event = Event(seq=len(self.events) + 1, kind=kind, data=data)
        self.events.append(event)
        self._wake()
        return event

    def open_request(self, request_id: str) -> asyncio.Future:
        """Register a question or permission request and return its future."""
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        return future

    def resolve(self, request_id: str, answer: dict) -> bool:
        """Answer a pending request. False if it is unknown or already answered.

        Returning a bool rather than raising is what lets the HTTP route reply
        409 for a double submit - two clicks on the same form, or a form
        submitted from a tab that reconnected - instead of turning it into a 500.
        """
        future = self.pending.pop(request_id, None)
        if future is None or future.done():
            return False
        self.resolved[request_id] = answer
        future.set_result(answer)
        return True

    def finish(self, state: TurnState, error: str | None = None) -> None:
        # Cancel before waking: a turn that errored while something was blocked
        # on a human leaves that coroutine awaiting a future nobody will ever
        # resolve, and the browser form waits on a turn that is already gone.
        for future in self.pending.values():
            if not future.done():
                future.cancel()
        self.pending.clear()
        self.state = state
        self.error = error
        self._wake()

    def _wake(self) -> None:
        for waiter in self._waiters:
            waiter.set()
        self._waiters.clear()

    # ASYNC109 wants the caller to own the timeout via asyncio.timeout. Here
    # the timeout IS the feature - it is the SSE heartbeat interval, and the
    # method returning on it is how a keepalive frame gets sent.
    async def wait_for_change(self, timeout: float) -> None:  # noqa: ASYNC109
        """Block until a new event arrives, the turn ends, or timeout."""
        waiter = asyncio.Event()
        self._waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout)
        except TimeoutError:
            pass
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def since(self, seq: int) -> list[Event]:
        return [e for e in self.events if e.seq > seq]

    @property
    def finished(self) -> bool:
        return self.state in (TurnState.DONE, TurnState.ERROR)

    def summary(self) -> dict:
        return {
            "turn_id": self.id,
            "state": self.state.value,
            "error": self.error,
            "session_id": self.session_id,
            "savepoint": self.savepoint,
            "event_count": len(self.events),
            "skills": sorted(self.skills),
            "reflection": self.reflection,
            "evolved": [c.summary() for c in self.evolved],
            "interactive": self.interactive,
            "subagents": self.subagents,
            "pending": sorted(self.pending),
        }


class Registry:
    """Bounded store of recent turns, oldest evicted first."""

    def __init__(self, max_turns: int = 200) -> None:
        self._turns: dict[str, Turn] = {}
        self._max = max_turns

    def begin(
        self,
        user_email: str,
        session_id: str | None = None,
        *,
        interactive: bool = True,
        conversation_id: str | None = None,
        actor_email: str | None = None,
    ) -> Turn:
        """Admit one turn, or raise TurnInProgressError. The only way to start one.

        The check and the insert live in one method with no `await` between
        them, which is what makes admission atomic on a single-threaded event
        loop. A caller writing `if any_running(): refuse` and then `create()`
        is atomic only by accident, and stops being so the first time someone
        adds an await between the two lines.

        That accident had already happened four different ways: /mcp held its
        own asyncio.Lock, POST /api/reflect and maybe_reflect each rolled their
        own check, and POST /api/turns - the browser path, which carries very
        nearly all of the traffic - had no check at all. Two tabs was enough.

        Still exactly one turn at a time, instance-wide (docs/decisions/0009):
        this does not change with conversations. What changes is at the
        caller - app/main.py checks whether the turn already running belongs
        to the SAME conversation before calling this, and injects into it
        instead of calling begin() a second time. See docs/decisions/0017.
        """
        in_flight = self.running()
        if in_flight is not None:
            raise TurnInProgressError(in_flight)
        return self._create(
            user_email,
            session_id=session_id,
            interactive=interactive,
            conversation_id=conversation_id,
            actor_email=actor_email,
        )

    def _create(
        self,
        user_email: str,
        session_id: str | None = None,
        *,
        interactive: bool = True,
        conversation_id: str | None = None,
        actor_email: str | None = None,
    ) -> Turn:
        turn = Turn(
            id=uuid.uuid4().hex,
            user_email=user_email,
            session_id=session_id,
            interactive=interactive,
            conversation_id=conversation_id,
            actor_email=actor_email,
            # Only a conversation turn can be steered mid-flight - see
            # agent._input_stream, which is the only reader.
            inbox=asyncio.Queue() if conversation_id is not None else None,
        )
        self._turns[turn.id] = turn
        self._evict()
        return turn

    def get(self, turn_id: str) -> Turn | None:
        return self._turns.get(turn_id)

    def running(self) -> Turn | None:
        """The turn currently in flight, if there is one."""
        return next((t for t in self._turns.values() if not t.finished), None)

    def any_running(self) -> bool:
        """Used by the keepalive loop to stop the host suspending mid-turn."""
        return self.running() is not None

    def _evict(self) -> None:
        if len(self._turns) <= self._max:
            return
        finished = [t for t in self._turns.values() if t.finished]
        for turn in finished[: len(self._turns) - self._max]:
            self._turns.pop(turn.id, None)


registry = Registry()
