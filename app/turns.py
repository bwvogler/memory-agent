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


class TurnState(str, enum.Enum):
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class Event:
    seq: int
    kind: str
    data: str


@dataclass
class Turn:
    id: str
    user_email: str
    session_id: str | None = None
    savepoint: str | None = None
    state: TurnState = TurnState.RUNNING
    error: str | None = None
    events: list[Event] = field(default_factory=list)

    # Signal capture (app/signals.py). The prompt is kept because the revert
    # handler needs it long after run_turn has returned, and skills because
    # attributing a revert to anything requires knowing what the turn read.
    prompt: str = ""
    skills: set[str] = field(default_factory=set)
    terminal_reason: str | None = None
    permission_denials: list[str] = field(default_factory=list)

    _waiters: list[asyncio.Event] = field(default_factory=list, repr=False)

    def append(self, kind: str, data: str) -> Event:
        event = Event(seq=len(self.events) + 1, kind=kind, data=data)
        self.events.append(event)
        self._wake()
        return event

    def finish(self, state: TurnState, error: str | None = None) -> None:
        self.state = state
        self.error = error
        self._wake()

    def _wake(self) -> None:
        for waiter in self._waiters:
            waiter.set()
        self._waiters.clear()

    async def wait_for_change(self, timeout: float) -> None:
        """Block until a new event arrives, the turn ends, or timeout."""
        waiter = asyncio.Event()
        self._waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout)
        except asyncio.TimeoutError:
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
        }


class Registry:
    """Bounded store of recent turns, oldest evicted first."""

    def __init__(self, max_turns: int = 200) -> None:
        self._turns: dict[str, Turn] = {}
        self._max = max_turns

    def create(self, user_email: str, session_id: str | None = None) -> Turn:
        turn = Turn(id=uuid.uuid4().hex, user_email=user_email, session_id=session_id)
        self._turns[turn.id] = turn
        self._evict()
        return turn

    def get(self, turn_id: str) -> Turn | None:
        return self._turns.get(turn_id)

    def any_running(self) -> bool:
        """Used by the keepalive loop to stop the host suspending mid-turn."""
        return any(not t.finished for t in self._turns.values())

    def _evict(self) -> None:
        if len(self._turns) <= self._max:
            return
        finished = [t for t in self._turns.values() if t.finished]
        for turn in finished[: len(self._turns) - self._max]:
            self._turns.pop(turn.id, None)


registry = Registry()
