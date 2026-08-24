"""The interaction surface, and the one bug enabling Task would have introduced.

Three things here are load-bearing and would all fail silently:

* Subagent text must never reach the main reply. `_render` used to forward every
  delta regardless of `parent_tool_use_id`, so turning on `Task` would have
  spliced a subagent's tokens into the middle of a sentence the user was reading.
  No exception, no log line - just a corrupted transcript.
* A timeout must resolve, not hang. A question that waits forever on someone who
  closed the tab is worse than an unanswered one, and a permission request that
  waits forever burns the turn budget on a call nobody was going to allow.
* The structured payloads have to survive `main._sse_escape`. It rewrites
  newlines, and a payload it mangled would reach the browser as a parse error
  that looks like a network fault.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    PermissionResult,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from app import agent, interact
from app.main import _sse_escape
from app.turns import Turn, TurnState


def _turn(*, interactive: bool = True) -> Turn:
    return Turn(id="t1", user_email="dev@localhost", interactive=interactive)


def _impatient(monkeypatch, **overrides) -> None:
    """Shrink the human-wait timeouts. Config is frozen, so replace it wholesale."""
    monkeypatch.setattr(
        interact, "config", dataclasses.replace(interact.config, **overrides)
    )


def _delta(text: str, parent: str | None = None) -> StreamEvent:
    return StreamEvent(
        uuid="u1",
        session_id="s1",
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
        parent_tool_use_id=parent,
    )


def _kinds(rendered) -> list[str]:
    return [kind for kind, _ in rendered]


async def _asked(turn: Turn) -> str:
    """Wait for the request event and return its id, then answer as the route does.

    Uses the turn's own wake mechanism rather than polling, which is also what
    the SSE handler does - `append` wakes every waiter, so this returns on the
    same signal the browser would have been woken by.
    """
    while not turn.pending:
        await turn.wait_for_change(timeout=1)
    return next(iter(turn.pending))


# --- the transcript-corruption bug -----------------------------------------


def test_main_agent_deltas_stream_as_reply_text():
    assert agent._render(_delta("hello")) == [("text_delta", "hello")]


def test_subagent_deltas_never_reach_the_reply():
    """The whole reason Task could not be enabled before this change."""
    rendered = agent._render(_delta("subagent chatter", parent="toolu_123"))

    assert _kinds(rendered) == ["agent_text"]
    payload = json.loads(rendered[0][1])
    assert payload == {"agent": "toolu_123", "text": "subagent chatter"}


def test_subagent_whole_message_text_never_reaches_the_reply():
    message = AssistantMessage(
        content=[TextBlock(text="done")], model="m", parent_tool_use_id="toolu_9"
    )
    assert _kinds(agent._render(message)) == ["agent_text"]


def test_main_agent_whole_message_text_is_the_non_streaming_fallback():
    message = AssistantMessage(content=[TextBlock(text="done")], model="m")
    assert agent._render(message) == [("text", "done")]


# --- the rest of the render vocabulary -------------------------------------


def test_thinking_is_surfaced_rather_than_dropped():
    message = AssistantMessage(
        content=[ThinkingBlock(thinking="weighing it up", signature="sig")], model="m"
    )
    assert agent._render(message) == [("thinking", "weighing it up")]


def test_a_subagents_thinking_is_not_shown_as_the_agents_own():
    message = AssistantMessage(
        content=[ThinkingBlock(thinking="hmm", signature="sig")],
        model="m",
        parent_tool_use_id="toolu_1",
    )
    assert agent._render(message) == []


def test_a_tool_call_carries_what_it_is_doing():
    message = AssistantMessage(
        content=[
            ToolUseBlock(
                id="tu1", name="Read", input={"file_path": "/mnt/kb/memory/a.md"}
            )
        ],
        model="m",
    )
    rendered = agent._render(message)

    assert _kinds(rendered) == ["tool_use"]
    payload = json.loads(rendered[0][1])
    assert payload["id"] == "tu1"
    assert payload["name"] == "Read"
    assert payload["detail"] == "memory/a.md", "the mount prefix is noise on every line"


def test_todowrite_also_emits_the_list_it_wrote():
    todos = [{"content": "read the guide", "status": "in_progress"}]
    message = AssistantMessage(
        content=[ToolUseBlock(id="tu2", name="TodoWrite", input={"todos": todos})],
        model="m",
    )
    rendered = agent._render(message)

    assert _kinds(rendered) == ["tool_use", "todo"]
    assert json.loads(rendered[1][1]) == {"todos": todos}


def test_tool_input_descriptions_are_useful_per_tool():
    assert interact.describe_tool_input("Bash", {"command": "bd ready"}) == "bd ready"
    assert (
        interact.describe_tool_input(
            "Task", {"subagent_type": "kb-query", "description": "look"}
        )
        == "kb-query: look"
    )
    assert interact.describe_tool_input("Grep", {"pattern": "tea"}) == "tea"
    # An unrecognised tool still says something rather than nothing.
    assert interact.describe_tool_input("Frobnicate", {"target": "x.md"}) == "x.md"
    assert interact.describe_tool_input("Read", None) == ""


def test_long_details_are_clipped():
    detail = interact.describe_tool_input("Bash", {"command": "echo " + "x" * 500})
    assert len(detail) == interact.MAX_DETAIL_CHARS
    assert detail.endswith("...")


# --- payloads have to survive the wire -------------------------------------


def test_structured_payloads_are_unchanged_by_sse_escaping():
    """`_sse_escape` rewrites newlines; json.dumps must never emit a raw one."""
    payload = interact.json_event(
        question="line one\nline two", options=["a\nb"], note="tab\there"
    )

    assert _sse_escape(payload) == payload
    assert json.loads(payload)["question"] == "line one\nline two"


# --- the pending-request table ---------------------------------------------


def test_a_request_cannot_be_answered_twice():
    """Two clicks on one form is an ordinary event, and must be a 409 not a 500."""

    async def scenario() -> tuple[bool, bool]:
        turn = _turn()
        turn.open_request("r1")
        return turn.resolve("r1", {"answers": ["yes"]}), turn.resolve(
            "r1", {"answers": ["no"]}
        )

    first, second = asyncio.run(scenario())
    assert first is True
    assert second is False


def test_answering_something_that_was_never_asked_is_refused():
    async def scenario() -> bool:
        return _turn().resolve("nope", {"answers": []})

    assert asyncio.run(scenario()) is False


def test_finishing_a_turn_cancels_whoever_is_waiting_on_a_human():
    """Otherwise an errored turn strands a coroutine on a future nobody resolves."""

    async def scenario() -> bool:
        turn = _turn()
        future = turn.open_request("r1")
        turn.finish(TurnState.ERROR, error="boom")
        await asyncio.sleep(0)
        return future.cancelled()

    assert asyncio.run(scenario()) is True


# --- asking ----------------------------------------------------------------


def test_an_unanswered_question_tells_the_agent_to_proceed(monkeypatch):
    """Permissive on purpose: a stalled turn is the worse failure."""
    _impatient(monkeypatch, ask_timeout_seconds=0.01)

    async def scenario() -> tuple[str, Turn]:
        turn = _turn()
        return await interact._ask(turn, {"question": "which one?"}), turn

    answer, turn = asyncio.run(scenario())

    assert "best judgement" in answer
    assert [e.kind for e in turn.events] == ["ask", "answered"]
    assert json.loads(turn.events[1].data)["timeout"] is True
    assert turn.pending == {}, "a timed-out question must not stay pending forever"


def test_an_answered_question_reaches_the_agent():
    async def scenario() -> tuple[str, Turn]:
        turn = _turn()
        asking = asyncio.create_task(interact._ask(turn, {"question": "which one?"}))
        turn.resolve(
            await _asked(turn), {"answers": ["the second"], "notes": "roughly"}
        )
        return await asking, turn

    answer, turn = asyncio.run(scenario())

    assert "the second" in answer
    assert "roughly" in answer
    assert [e.kind for e in turn.events] == ["ask", "answered"]


def test_an_empty_submission_is_no_preference_not_an_empty_answer():
    """The exact failure that makes the built-in AskUserQuestion unusable here."""
    answer = interact._format_answer({"answers": [], "notes": ""})
    assert "no preference" in answer


def test_a_machine_caller_is_told_nobody_is_there():
    async def scenario() -> tuple[str, Turn]:
        turn = _turn(interactive=False)
        return await interact._ask(turn, {"question": "which one?"}), turn

    answer, turn = asyncio.run(scenario())

    assert "no human" in answer.lower()
    assert turn.events == [], "nothing to show a browser that is not there"


# --- permission ------------------------------------------------------------


class _Context:
    """A stand-in for ToolPermissionContext, whose fields we read but never set."""

    def __init__(self, **kwargs) -> None:
        defaults = {
            "title": None,
            "display_name": None,
            "description": None,
            "blocked_path": None,
            "decision_reason": None,
            "agent_id": None,
        }
        for key, value in {**defaults, **kwargs}.items():
            setattr(self, key, value)


def test_an_unapproved_tool_times_out_denied(monkeypatch):
    """Restrictive on purpose, and the opposite of the question timeout."""
    _impatient(monkeypatch, permission_timeout_seconds=0.01)

    async def scenario() -> tuple[PermissionResult, Turn]:
        turn = _turn()
        callback = interact.can_use_tool_for(turn)
        return await callback("Bash", {"command": "rm -rf /"}, _Context()), turn

    result, turn = asyncio.run(scenario())

    assert result.behavior == "deny"
    assert turn.human_denials == ["Bash"]
    assert [e.kind for e in turn.events] == ["permission", "permission_resolved"]


def test_allowing_a_tool_records_no_denial():
    async def scenario() -> tuple[PermissionResult, Turn]:
        turn = _turn()
        callback = interact.can_use_tool_for(turn)
        deciding = asyncio.create_task(
            callback("WebFetch", {"url": "http://x"}, _Context())
        )
        turn.resolve(await _asked(turn), {"decision": "allow"})
        return await deciding, turn

    result, turn = asyncio.run(scenario())

    assert result.behavior == "allow"
    assert turn.human_denials == []


def test_a_refusal_is_recorded_as_the_humans_and_not_a_defect():
    """turn.human_denials is what stops signals filing a P1 against the person."""

    async def scenario() -> tuple[PermissionResult, Turn]:
        turn = _turn()
        callback = interact.can_use_tool_for(turn)
        deciding = asyncio.create_task(
            callback("Bash", {"command": "curl x"}, _Context())
        )
        turn.resolve(await _asked(turn), {"decision": "deny", "note": "not that one"})
        return await deciding, turn

    result, turn = asyncio.run(scenario())

    assert result.behavior == "deny"
    assert "not that one" in result.message
    assert turn.human_denials == ["Bash"]


def test_the_prompt_prefers_what_the_sdk_composed():
    """The CLI writes these sentences everywhere else; do not reword them here."""

    async def scenario() -> dict:
        turn = _turn()
        context = _Context(
            title="Claude wants to read foo.txt",
            display_name="Read file",
            blocked_path="/etc/passwd",
            agent_id="agent-1",
        )
        callback = interact.can_use_tool_for(turn)
        deciding = asyncio.create_task(
            callback("Read", {"file_path": "/etc/passwd"}, context)
        )
        turn.resolve(await _asked(turn), {"decision": "deny"})
        await deciding
        return json.loads(turn.events[0].data)

    payload = asyncio.run(scenario())

    assert payload["title"] == "Claude wants to read foo.txt"
    assert payload["display_name"] == "Read file"
    assert payload["blocked_path"] == "/etc/passwd"
    assert payload["agent"] == "agent-1"


def test_a_broken_permission_callback_denies_rather_than_allows(monkeypatch):
    """Fails CLOSED, unlike the guards. An approval nobody granted is worse."""

    def explode(*args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(interact, "_request_permission", explode)

    async def scenario():
        return await interact.can_use_tool_for(_turn())("Bash", {}, _Context())

    result = asyncio.run(scenario())
    assert result.behavior == "deny"


# --- the observer hooks ----------------------------------------------------


def _run_hook(hook, payload) -> None:
    asyncio.run(hook(payload, None, None))


def test_a_failed_tool_call_is_visible():
    """It was not before: results arrive on a message _render drops wholesale."""
    turn = _turn()
    _run_hook(
        interact.tool_failure_for(turn),
        {"tool_name": "Write", "tool_use_id": "tu1", "error": "no such directory"},
    )

    assert turn.tool_failures == ["Write"]
    payload = json.loads(turn.events[0].data)
    assert payload["ok"] is False
    assert payload["detail"] == "no such directory"


def test_a_successful_tool_call_is_reported_as_such():
    turn = _turn()
    _run_hook(
        interact.tool_result_for(turn),
        {"tool_name": "Read", "tool_use_id": "tu1", "tool_response": "contents"},
    )

    assert json.loads(turn.events[0].data)["ok"] is True
    assert turn.tool_failures == []


def test_subagent_lifecycle_is_recorded_on_the_turn():
    turn = _turn()
    _run_hook(
        interact.subagent_start_for(turn),
        {"agent_id": "a1", "agent_type": "kb-query"},
    )
    _run_hook(
        interact.subagent_stop_for(turn), {"agent_id": "a1", "agent_type": "kb-query"}
    )

    assert turn.subagents == [{"agent_id": "a1", "agent_type": "kb-query"}]
    assert [e.kind for e in turn.events] == ["agent_start", "agent_stop"]


@pytest.mark.parametrize(
    "factory",
    ["tool_result_for", "tool_failure_for", "subagent_start_for", "subagent_stop_for"],
)
def test_a_broken_observer_does_not_break_the_turn(factory):
    """Same rule as app/guards.py: fail open, never take down what you watch."""
    turn = _turn()
    hook = getattr(interact, factory)(turn)

    # A payload of the wrong type entirely, which is the shape drift these
    # hooks would hit if the SDK renamed a field.
    _run_hook(hook, {"tool_name": object(), "agent_id": object()})


# --- a tool call has to be visible before it finishes ------------------------


def _block_start(block: dict, parent: str | None = None) -> StreamEvent:
    return StreamEvent(
        uuid="u1",
        session_id="s1",
        event={"type": "content_block_start", "content_block": block},
        parent_tool_use_id=parent,
    )


def test_a_tool_call_is_announced_the_moment_it_starts():
    """The earliest possible signal, and the fix for a turn that looked hung.

    The reported case: the agent said "Reading the CSV now.", then read a large
    file. Text streams first, the AssistantMessage carrying the tool call does
    not arrive until the model's response completes, and a successful tool
    result is deliberately quiet - so nothing moved on screen for the length of
    the read. `content_block_start` carries the name and id before the arguments
    and before the tool runs, which is what closes that window.
    """
    rendered = agent._render(
        _block_start({"type": "tool_use", "id": "toolu_1", "name": "Read"})
    )

    assert _kinds(rendered) == ["tool_use"]
    payload = json.loads(rendered[0][1])
    assert payload["id"] == "toolu_1"
    assert payload["name"] == "Read"
    assert payload["detail"] == "", "the arguments have not arrived yet"


def test_the_start_and_the_completed_call_share_an_id():
    """That shared id lets the UI fill the line in rather than duplicate it."""
    start = json.loads(
        agent._render(
            _block_start({"type": "tool_use", "id": "toolu_7", "name": "Read"})
        )[0][1]
    )
    complete = json.loads(
        agent._render(
            AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="toolu_7",
                        name="Read",
                        input={"file_path": "/work/dev/deck.csv"},
                    )
                ],
                model="m",
            )
        )[0][1]
    )

    assert start["id"] == complete["id"] == "toolu_7"
    assert complete["detail"] == "/work/dev/deck.csv", (
        "the second event carries the detail"
    )


def test_a_subagents_tool_call_is_announced_against_the_subagent():
    rendered = agent._render(
        _block_start({"type": "tool_use", "id": "toolu_2", "name": "Grep"}, parent="p1")
    )

    assert json.loads(rendered[0][1])["agent"] == "p1"


def test_a_thinking_block_start_is_not_a_tool_call():
    assert agent._render(_block_start({"type": "thinking"})) == []


def test_a_text_block_start_is_not_a_tool_call():
    assert agent._render(_block_start({"type": "text"})) == []


def test_input_json_deltas_are_not_forwarded():
    """The arguments are assembled by the SDK; we take them whole.

    Forwarding fragments would mean reimplementing describe_tool_input in
    JavaScript and keeping the two in step.
    """
    event = StreamEvent(
        uuid="u1",
        session_id="s1",
        event={
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": '{"file_path": "/a'},
        },
    )
    assert agent._render(event) == []
