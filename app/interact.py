"""Round-trips to the human, and the observability the UI was missing.

Two separate things live here because they share one mechanism - a coroutine
inside a running turn that blocks on an `asyncio.Future` a browser resolves over
HTTP:

* `ask_server_for` gives the agent a way to ask a question mid-turn.
* `can_use_tool_for` turns a permission request into an Allow/Deny in the UI
  instead of a silent refusal.

Plus four hooks (`tool_result_for`, `tool_failure_for`, `subagent_start_for`,
`subagent_stop_for`) that report things the message stream never carried.

--- Why the agent cannot just use AskUserQuestion ---

Claude Code ships an `AskUserQuestion` tool, and under this SDK it is a trap.
With no TTY there is nowhere to draw its prompt, so it resolves immediately with
*empty answers* and the agent proceeds believing it consulted someone. See
anthropics/claude-code#50728. It is therefore in `disallowed_tools`, and this
module provides `mcp__ask__ask_user` in its place: an in-process SDK MCP tool,
which blocks properly because the thing it waits on is a future in this process.

--- Why the two timeouts resolve in opposite directions ---

An unanswered *question* returns text telling the agent to proceed on its own
judgement and say that it did. An unanswered *permission request* is denied.
That asymmetry is deliberate: the first failure mode is a turn that stalls
forever on someone who closed the tab, and the second is a tool call nobody
authorised. Neither timeout is an error - a turn that dies because a human went
to lunch is worse than either outcome.

--- Interactivity belongs to the caller ---

`turn.interactive` is False for a turn a machine started over /mcp. Such a turn
gets no permission callback at all (it falls back to the pre-existing
deny-what-is-not-preapproved behaviour), and `ask_user` still exists but answers
immediately that nobody is there. Removing the tool would be worse: an agent
whose tool vanished guesses, where an agent told it is alone says so in the page
it writes.

Every callable here fails open or fails safe and none of them raise, for the
reason documented at the top of app/guards.py: a broken observer must not take
down the turn it was observing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from .config import config
from .kb import WORKSPACE_NAME

if TYPE_CHECKING:
    from collections.abc import Mapping

    from claude_agent_sdk.types import (
        McpSdkServerConfig,
        PermissionResult,
        SyncHookJSONOutput,
        ToolPermissionContext,
    )

    from .turns import Turn

log = logging.getLogger(__name__)

# Longest tool detail we put on the event stream. The UI shows a one-line
# summary; anything longer is noise that pushes the reply off screen.
MAX_DETAIL_CHARS = 200

_NO_HUMAN = (
    "There is no human in this session - it was started by a machine caller, "
    "so nobody can answer. Proceed on your best judgement, and record the "
    "assumption you made in whatever you write."
)

_NO_ANSWER = (
    "No answer arrived within {seconds:.0f}s, so nobody is watching. Proceed on "
    "your best judgement, and say in your reply that you went ahead unanswered."
)

_DENIED_BY_TIMEOUT = (
    "Refused: nobody approved this within {seconds:.0f}s, so it is denied "
    "rather than left waiting. Carry on with the tools you already have. If "
    "this tool is genuinely necessary, say so plainly in your reply and file a "
    "bead for it rather than trying variations of the same call."
)


def _json(payload: dict[str, Any]) -> str:
    """Serialise an event payload for the SSE stream.

    `json.dumps` never emits a literal newline, so `main._sse_escape` is a no-op
    on the result and the client can `JSON.parse` the frame's data directly.
    """
    return json.dumps(payload, default=str)


def json_event(**payload: Any) -> str:
    """`_json` for callers that find keyword arguments more readable."""
    return _json(payload)


def _clip(text: str, limit: int = MAX_DETAIL_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def describe_tool_input(name: str, tool_input: Mapping[str, Any] | None) -> str:
    """One line saying what this tool call is actually doing.

    The UI used to render a bare `-> read`, which says nothing: reading the KB
    index and reading a 90-page source document looked identical. Pure and
    keyed on the argument names each tool actually uses, with a last-resort
    scan so an unrecognised tool still shows something rather than nothing.
    """
    args = tool_input or {}

    def first(*keys: str) -> str:
        for key in keys:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    if name == "Bash":
        return _clip(first("command"))
    if name == "Task":
        agent = first("subagent_type", "agent_type")
        what = first("description", "prompt")
        return _clip(f"{agent}: {what}" if agent else what)
    if name == "TodoWrite":
        todos = args.get("todos")
        return f"{len(todos)} items" if isinstance(todos, list) else ""

    # A path, pattern or URL, in the order tools tend to name them. Paths are
    # shown relative to the mount: the prefix is the same on every line and
    # spends the whole width budget saying nothing.
    value = first("file_path", "path", "pattern", "url", "query", "notebook_path")
    if value.startswith(config.kb_mount):
        value = value[len(config.kb_mount) :].lstrip("/") or value
    if not value:
        value = first(*sorted(k for k, v in args.items() if isinstance(v, str)))
    return _clip(value)


# Read is deliberately absent: following every Read would flicker the centre
# pane through the whole corpus while the agent researches, for a call that
# changed nothing.
KB_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


def describe_tool_target(
    name: str, tool_input: Mapping[str, Any] | None
) -> dict[str, str]:
    """Where this call landed, as something the UI can open. Pure.

    Not the same value as `describe_tool_input`: that one is a display
    string, clipped to `MAX_DETAIL_CHARS` and only mount-stripped, so a write
    to `$KB_MOUNT/memory/x.md` shows as `memory/x.md` there. This one is an
    identifier, and it has to be byte-identical to what `/api/kb/file?path=`
    accepts - which is rooted at `$KB_MOUNT/memory`, not `$KB_MOUNT` (see
    `kb.workspace_root` and `export_backlog`, which writes `backlog.md`, not
    `memory/backlog.md`). Stripping only the mount, the way `describe_tool_input`
    does, would 404 every path this function is meant to produce.

    Empty dict for anything outside the KB workspace, or for a tool that does
    not write files at all - most calls have nothing openable, and the caller
    treats `{}` as "do not move the pane".
    """
    if name not in KB_WRITE_TOOLS:
        return {}
    args = tool_input or {}
    raw = ""
    for key in ("file_path", "path", "notebook_path"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            raw = value.strip()
            break
    workspace_prefix = f"{config.kb_mount.rstrip('/')}/{WORKSPACE_NAME}/"
    if not raw.startswith(workspace_prefix):
        return {}
    return {"kind": "kb", "path": raw[len(workspace_prefix) :]}


# ---------------------------------------------------------------------------
# Asking a question
# ---------------------------------------------------------------------------


def ask_server_for(turn: Turn) -> McpSdkServerConfig:
    """Build the in-process MCP server carrying this turn's `ask_user` tool.

    Built per turn rather than once at import, because the tool has to close
    over the turn it is asking about - that is what lets it put an event on the
    right SSE stream and wait on the right future.
    """

    @tool(
        "ask_user",
        "Ask the human a question and wait for their answer. Use this when the "
        "answer would change what you write and you cannot find it in the wiki "
        "- not for permission to proceed, and not for anything you could "
        "reasonably decide yourself. Their attention is the expensive part.",
        {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question, as one clear sentence.",
                },
                "header": {
                    "type": "string",
                    "description": "A 1-3 word label for the choice being made.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Suggested answers. The human can always "
                    "write their own instead, so offering none is fine.",
                },
                "multi_select": {
                    "type": "boolean",
                    "description": "True if several options may be chosen at once.",
                },
            },
            "required": ["question"],
        },
    )
    async def ask_user(args: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": await _ask(turn, args)}]}

    return create_sdk_mcp_server("ask", tools=[ask_user])


async def _ask(turn: Turn, args: Mapping[str, Any]) -> str:
    """Put a question on the stream and wait for it. Never raises on timeout."""
    if not turn.interactive:
        return _NO_HUMAN

    question = str(args.get("question") or "").strip()
    if not question:
        return "That call carried no question, so there was nothing to ask."

    options = [str(o) for o in (args.get("options") or []) if str(o).strip()]
    request_id = uuid.uuid4().hex
    payload = {
        "request_id": request_id,
        "question": question,
        "header": str(args.get("header") or "").strip(),
        "options": options,
        "multi_select": bool(args.get("multi_select")),
    }

    future = turn.open_request(request_id)
    turn.append("ask", _json(payload))
    timeout = config.ask_timeout_seconds

    try:
        async with asyncio.timeout(timeout):
            answer = await future
    except TimeoutError:
        turn.pending.pop(request_id, None)
        turn.append("answered", _json({"request_id": request_id, "timeout": True}))
        log.info("turn %s: question went unanswered for %ss", turn.id, timeout)
        return _NO_ANSWER.format(seconds=timeout)

    turn.append(
        "answered",
        _json({"request_id": request_id, "answers": answer.get("answers") or []}),
    )
    return _format_answer(answer)


def _format_answer(answer: Mapping[str, Any]) -> str:
    chosen = [str(a) for a in (answer.get("answers") or []) if str(a).strip()]
    notes = str(answer.get("notes") or "").strip()
    parts = []
    if chosen:
        parts.append("The human answered: " + "; ".join(chosen))
    if notes:
        parts.append(f"They added: {notes}")
    if not parts:
        # An empty submit is a real answer - "I have no preference" - and must
        # not look like the empty-answer failure AskUserQuestion has.
        return (
            "The human submitted the form without choosing anything, which "
            "means they have no preference. Decide it yourself and move on."
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Asking permission
# ---------------------------------------------------------------------------


def can_use_tool_for(turn: Turn):
    """Build the permission callback for one turn.

    Only installed on an interactive turn. `agent.py` omits it entirely
    otherwise, which restores the previous behaviour exactly: anything outside
    `allowed_tools` is refused by the CLI without anyone being asked.
    """

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResult:
        try:
            return await _request_permission(turn, tool_name, tool_input, context)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Fail CLOSED here, unlike the hooks. A guard that breaks should not
            # block legitimate work, but a *permission* callback that breaks
            # must not hand out an approval nobody granted.
            log.exception("permission callback failed for %s; denying", tool_name)
            return PermissionResultDeny(
                message=(
                    "Refused: the approval prompt could not be delivered. "
                    "Continue with the tools you already have."
                )
            )

    return can_use_tool


async def _request_permission(
    turn: Turn,
    tool_name: str,
    tool_input: dict[str, Any],
    context: ToolPermissionContext,
) -> PermissionResult:
    request_id = uuid.uuid4().hex
    payload = {
        "request_id": request_id,
        "tool": tool_name,
        # The SDK composes these for us - "Claude wants to read foo.txt" and a
        # short button label. Reconstructing them from the tool name and input
        # would drift from what the CLI says everywhere else.
        "title": context.title or f"Allow {tool_name}?",
        "display_name": context.display_name or tool_name,
        "description": context.description or "",
        "detail": describe_tool_input(tool_name, tool_input),
        "blocked_path": context.blocked_path or "",
        "reason": context.decision_reason or "",
        "agent": context.agent_id or "",
    }

    future = turn.open_request(request_id)
    turn.append("permission", _json(payload))
    timeout = config.permission_timeout_seconds

    try:
        async with asyncio.timeout(timeout):
            answer = await future
    except TimeoutError:
        turn.pending.pop(request_id, None)
        turn.human_denials.append(tool_name)
        turn.append(
            "permission_resolved",
            _json({"request_id": request_id, "decision": "deny", "timeout": True}),
        )
        log.info("turn %s: %s not approved within %ss", turn.id, tool_name, timeout)
        return PermissionResultDeny(message=_DENIED_BY_TIMEOUT.format(seconds=timeout))

    allowed = answer.get("decision") == "allow"
    note = str(answer.get("note") or "").strip()
    turn.append(
        "permission_resolved",
        _json({"request_id": request_id, "decision": "allow" if allowed else "deny"}),
    )
    if allowed:
        return PermissionResultAllow()

    # Recorded so signals does not file a P1 "check allowed_tools in _options"
    # bead against a person who simply said no. See app/signals.py.
    turn.human_denials.append(tool_name)
    return PermissionResultDeny(
        message="The human refused this tool." + (f" They said: {note}" if note else "")
    )


# ---------------------------------------------------------------------------
# Observability hooks
# ---------------------------------------------------------------------------
#
# These report what the message stream does not. A tool_result arrives on a
# UserMessage, which `_render` drops wholesale, so before this a failed Write
# and a successful one produced byte-identical output in the UI - in a system
# whose documented failure mode is silence.


def _hook(turn: Turn, handler, what: str):
    """Wrap a hook body so a broken observer cannot break the turn."""

    async def hook(
        input_data: Mapping[str, Any],
        _tool_use_id: str | None,
        _context: Any,
    ) -> SyncHookJSONOutput:
        try:
            handler(turn, input_data)
        except Exception:
            log.exception("%s hook failed for turn %s; continuing", what, turn.id)
        return {}

    return hook


def _on_tool_result(turn: Turn, data: Mapping[str, Any]) -> None:
    response = data.get("tool_response")
    turn.append(
        "tool_result",
        _json(
            {
                "id": data.get("tool_use_id") or "",
                "name": data.get("tool_name") or "",
                "ok": True,
                "detail": _clip(response if isinstance(response, str) else ""),
                "agent": data.get("agent_id") or "",
            }
        ),
    )


def _on_tool_failure(turn: Turn, data: Mapping[str, Any]) -> None:
    name = str(data.get("tool_name") or "")
    turn.tool_failures.append(name)
    turn.append(
        "tool_result",
        _json(
            {
                "id": data.get("tool_use_id") or "",
                "name": name,
                "ok": False,
                "detail": _clip(str(data.get("error") or "")),
                "agent": data.get("agent_id") or "",
            }
        ),
    )


def _on_subagent_start(turn: Turn, data: Mapping[str, Any]) -> None:
    entry = {
        "agent_id": str(data.get("agent_id") or ""),
        "agent_type": str(data.get("agent_type") or ""),
    }
    turn.subagents.append(entry)
    turn.append("agent_start", _json(entry))


def _on_subagent_stop(turn: Turn, data: Mapping[str, Any]) -> None:
    turn.append(
        "agent_stop",
        _json(
            {
                "agent_id": str(data.get("agent_id") or ""),
                "agent_type": str(data.get("agent_type") or ""),
            }
        ),
    )


def tool_result_for(turn: Turn):
    return _hook(turn, _on_tool_result, "PostToolUse")


def tool_failure_for(turn: Turn):
    return _hook(turn, _on_tool_failure, "PostToolUseFailure")


def subagent_start_for(turn: Turn):
    return _hook(turn, _on_subagent_start, "SubagentStart")


def subagent_stop_for(turn: Turn):
    return _hook(turn, _on_subagent_stop, "SubagentStop")
