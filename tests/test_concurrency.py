"""One turn at a time, because a savepoint covers the whole workspace.

`kb.create_savepoint` is a `git add -A` and a commit over the single shared
workspace at $KB_MOUNT/memory. The savepoint *name* is per turn; the content is
global. So two turns in flight do not merely compete for CPU - one turn's
half-written files are swept into the other's savepoint, and reverting either
rolls back both. Revert is what makes writing to the wiki reviewable (ADR 0003)
and bounded self-modification defensible (ADR 0008), so this is a correctness
rule, not a throttle. See img-lsp.

Three of the four entry points guarded this themselves, three different ways.
The fourth was `POST /api/turns` - the browser path, carrying nearly all of the
traffic - which had no guard at all, so two tabs was enough to hit it. These
tests hold the rule where it now lives (`Registry.begin`) and hold each entry
point to it, including the two that answer something other than an exception.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import pathlib
import types

import pytest
from fastapi import HTTPException

from app import agent, auth, main, mcp_server
from app.turns import Registry, TurnInProgressError, TurnState, registry


@pytest.fixture
def clean_registry(monkeypatch):
    """A registry of our own, so a test never sees another test's turns."""
    fresh = Registry()
    for module in (main, agent, mcp_server):
        monkeypatch.setattr(module, "registry", fresh)
    return fresh


# --- the rule itself --------------------------------------------------------


def test_a_second_turn_is_refused_while_one_is_in_flight():
    reg = Registry()
    first = reg.begin(user_email="a@e.com")

    with pytest.raises(TurnInProgressError) as caught:
        reg.begin(user_email="b@e.com")

    # The refusal carries the turn that caused it. Without that, diagnosing a
    # wedged instance means guessing which turn never finished.
    assert caught.value.running is first


def test_the_second_turn_is_never_created():
    """A refused turn must leave no trace: an unfinished turn blocks the next."""
    reg = Registry()
    reg.begin(user_email="a@e.com")
    with pytest.raises(TurnInProgressError):
        reg.begin(user_email="b@e.com")

    assert len([t for t in reg._turns.values() if not t.finished]) == 1


@pytest.mark.parametrize("state", [TurnState.DONE, TurnState.ERROR])
def test_a_finished_turn_releases_the_gate(state):
    """Including a turn that ERRORed - a failure must not wedge the instance."""
    reg = Registry()
    first = reg.begin(user_email="a@e.com")
    first.finish(state)

    assert reg.begin(user_email="b@e.com") is not first


def test_the_gate_is_workspace_wide_not_per_user():
    """Savepoints are global, so one household member blocks the other."""
    reg = Registry()
    reg.begin(user_email="brian@e.com")

    with pytest.raises(TurnInProgressError):
        reg.begin(user_email="laura@e.com")


def test_admission_is_atomic():
    """No await between the check and the insert, so no interleaving.

    The bug this replaces was not that callers forgot to check - three of them
    did check. It was that `if any_running(): refuse` followed by `create()` is
    atomic only by accident on an event loop, and stops being so the moment
    anyone adds an await between the two lines. Here two coroutines race
    deliberately; exactly one may win.
    """
    reg = Registry()
    outcomes = []

    async def attempt() -> None:
        await asyncio.sleep(0)
        try:
            outcomes.append(reg.begin(user_email="a@e.com").id)
        except TurnInProgressError:
            outcomes.append(None)

    async def race() -> None:
        await asyncio.gather(*[attempt() for _ in range(5)])

    asyncio.run(race())
    assert len([o for o in outcomes if o is not None]) == 1


# --- each entry point -------------------------------------------------------


def _request(body: dict):
    return types.SimpleNamespace(json=_coro(body))


def _coro(value):
    async def _call():
        return value

    return _call


def test_the_browser_path_answers_409(clean_registry):
    """The regression this whole change exists for: it used to answer 202."""
    clean_registry.begin(user_email="someone@e.com")  # a turn in another conversation
    identity = auth.Identity(email="brian@e.com", subject="s")
    body = {"message": "hello"}

    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.post_message("conv-elsewhere", _request(body), identity))

    assert caught.value.status_code == 409
    assert "savepoints" in caught.value.detail


def test_the_browser_path_says_why_it_refused(clean_registry):
    """The UI renders `detail` verbatim, so the reason has to be in it."""
    clean_registry.begin(user_email="someone@e.com")  # a turn in another conversation
    identity = auth.Identity(email="brian@e.com", subject="s")
    body = {"message": "hello"}

    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.post_message("conv-elsewhere", _request(body), identity))

    assert "already running" in caught.value.detail
    assert "Try again" in caught.value.detail


def test_a_message_for_the_same_conversation_is_injected_not_refused(clean_registry):
    """Turn-taking by injection: the whole point of docs/decisions/0017.

    A second household member's message for the conversation ALREADY running
    joins that turn instead of hitting the global one-turn-at-a-time refusal -
    that refusal still applies to every OTHER conversation, unchanged.
    """
    running = clean_registry.begin(
        user_email="someone@e.com", conversation_id="conv1", actor_email="someone@e.com"
    )
    identity = auth.Identity(email="brian@e.com", subject="s")

    body = {"message": "hello"}

    async def go():
        return await main.post_message("conv1", _request(body), identity)

    response = asyncio.run(go())
    body = jsonlib.loads(bytes(response.body))

    assert body == {"turn_id": running.id, "injected": True, "seq": 1}
    assert running.inbox.qsize() == 1


def test_the_reflect_route_answers_409(clean_registry):
    clean_registry.begin(user_email="someone@e.com")
    identity = auth.Identity(email="brian@e.com", subject="s")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.reflect(identity))

    assert caught.value.status_code == 409


def test_the_mcp_surface_returns_busy_rather_than_raising(clean_registry):
    """A machine caller wants the diagnosis, not a transport error."""
    clean_registry.begin(user_email="someone@e.com")
    token = mcp_server._caller.set(auth.Identity(email="p@e.com", subject="s"))
    try:
        result = asyncio.run(mcp_server.query("anything"))
    finally:
        mcp_server._caller.reset(token)

    assert result["ok"] is False
    assert "Busy" in result["error"]


def test_reflection_skips_quietly_rather_than_erroring(clean_registry, monkeypatch):
    """A signal-triggered reflection is never urgent; the user's turn wins.

    It has to *skip*, not raise: maybe_reflect is called from the tail of
    run_turn, and an exception there would rewrite a turn that already
    succeeded as a failure.
    """
    clean_registry.begin(user_email="someone@e.com")
    monkeypatch.setattr(agent, "_reflecting", asyncio.Lock())

    async def go():
        return await agent.maybe_reflect("brian_e_com", trigger="test")

    assert asyncio.run(go()) is None


# --- what the gate now depends on -------------------------------------------


def test_a_turn_that_fails_before_the_agent_loop_still_finishes(monkeypatch):
    """Otherwise the first stumble wedges every later turn, permanently.

    An unfinished turn is never evicted (only finished ones are) and now blocks
    admission, so `run_turn` reaching a terminal state on every path is what
    stops one bad savepoint from making a turn the last one this process runs.
    The three awaits that used to sit above the try - create_savepoint,
    ensure_beads, bd_prime - are exactly the ones this covers.
    """
    turn = registry._create(user_email="a@e.com")

    async def boom(_name):
        msg = "the mount went away"
        raise OSError(msg)

    monkeypatch.setattr(agent.kb, "create_savepoint", boom)

    async def go():
        await agent.run_turn(turn, prompt="hi", user_slug="a_e_com")

    asyncio.run(go())

    assert turn.finished
    assert turn.state is TurnState.ERROR
    assert "the mount went away" in (turn.error or "")


def test_no_entry_point_bypasses_begin():
    """Structural, because the defect was four spellings of one rule.

    `_create` is the unguarded constructor. Anything outside turns.py reaching
    for it is a fifth door, and the last time there was an extra door nobody
    noticed until two browser tabs corrupted a savepoint.
    """
    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = [
        path.name
        for path in sorted(app_dir.glob("*.py"))
        if path.name != "turns.py" and "_create(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
