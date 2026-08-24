"""Hooks that enforce two rules prompts alone have failed to enforce.

`pre_tool_use_guard` refuses shell commands that would corrupt a KB file.
`stop_guard` refuses to end a turn that deferred work without filing it.

Both follow the same three rules, learned from the incident documented below:
scope narrowly enough that legitimate work is never blocked, always explain the
safe alternative in the refusal itself, and never raise - a broken guard must
not take down the turn it was written to protect.

--- The KB write guard ---

The knowledge-base mount has no read-modify-write: opening a file yields a
zero-filled buffer, and on close that buffer becomes the whole file. Anything
you did not write in that session is lost - zeroed before your write, truncated
after it. See skills/kb-curator/references/tigerfs.md and bead kb-wk2.

That already destroyed a user's memory/CLAUDE.md. An agent hit a file-tool
error, misdiagnosed it, fell back to a shell append, and turned 233 bytes of
personal notes into 233 zeroes while the write reported success.

**Why a hook rather than an instruction.** The system prompt already forbids
this. Prompts are advice, and the incident happened precisely because a model
under pressure invented a workaround. These callables are passed to the SDK
in-process, so - unlike the `.claude/settings.json` hooks discussed in
docs/decisions/0006 - `setting_sources=[]` does not stop them firing, and the
agent cannot author them: they live in this file, not in its writable cwd.

**Two design rules learned from the incident.**

1. Scope strictly to the mount. Scratch is an ordinary filesystem where every
   one of these commands is fine and useful. Refusing them there would be a
   false positive on legitimate work.
2. Always explain. A refusal without a reason is what pushed the last agent
   into inventing a shell workaround; a refusal that names the safe pattern
   costs one turn and teaches the model in-context.

This is best-effort pattern matching on shell text, not a parser, so it is a
speed bump rather than a wall. The durable net is a post-write check for NUL
bytes, which catches mechanisms nobody has characterised yet.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import config

if TYPE_CHECKING:
    from collections.abc import Mapping

    from claude_agent_sdk.types import SyncHookJSONOutput

log = logging.getLogger(__name__)

# Each entry: (regex over the command, what the agent should do instead).
# `>` is deliberately absent - a truncating redirect writes the whole file from
# offset 0, which is exactly the safe pattern.
_HAZARDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r">>"), "`>>` appends, which zeroes everything already in the file"),
    (
        re.compile(r"\btee\s+(-a\b|--append\b)"),
        "`tee -a` appends, which zeroes everything already in the file",
    ),
    (
        re.compile(r"\bsed\s+(-[a-zA-Z]*i|--in-place)"),
        "`sed -i` rewrites in place and cannot be trusted on this mount",
    ),
    (
        re.compile(r"\bdd\b.*\b(seek|conv=notrunc)"),
        "`dd` writing at an offset leaves the untouched bytes as NULs",
    ),
    (re.compile(r"\btruncate\b"), "`truncate` resizes without rewriting the content"),
    (
        re.compile(r"\bpatch\b"),
        "`patch` edits in place and cannot be trusted on this mount",
    ),
    (
        re.compile(r"""open\s*\([^)]*['"]a['"]?"""),
        "opening a file in append mode zeroes everything already in it",
    ),
]

_GUIDANCE = (
    "The knowledge base does not read a file before writing it: opening one "
    "gives a zero-filled buffer, and on close that buffer becomes the entire "
    "file. Bytes you do not write are lost - zeroed before your write, "
    "truncated after it.\n\n"
    "Do this instead: read the whole file, build the complete new content, and "
    "write it back in full (the Write tool, or a single `>` redirect - a "
    "truncating write from offset 0 is safe). For work that genuinely needs "
    "several passes, build the file in your scratch directory, where every "
    "normal tool works, then copy it over with `cp` as a final step.\n\n"
    "Do not look for another shell command that achieves the same append. "
    "There isn't one, and trying is how a memory file was previously "
    "overwritten with zeroes."
)


def _mentions_kb(command: str) -> bool:
    """True if the command references the knowledge-base mount at all.

    Scratch is unrestricted on purpose, so a command that never names the
    mount is none of this hook's business.
    """
    return config.kb_mount in command


def unsafe_kb_write(command: str) -> str | None:
    """Return why this command endangers the KB, or None if it is fine.

    Pure and string-only, so the hazard list can be tested without an agent.
    """
    if not command or not _mentions_kb(command):
        return None
    for pattern, reason in _HAZARDS:
        if pattern.search(command):
            return reason
    return None


def kb_write_guard_for(turn: Any = None) -> Any:
    """Build the hook, optionally recording refusals on the turn.

    The turn matters because the SDK reports a hook denial through the same
    `permission_denials` channel as a missing `allowed_tools` entry. Signals
    files P1 beads for the latter - "check allowed_tools in _options" - so
    without this a guard doing exactly its job reports itself as a deployment
    defect, into the ledger reflection reads.
    """

    async def guard(
        input_data: Mapping[str, Any],
        _tool_use_id: str | None,
        _context: Any,
    ) -> SyncHookJSONOutput:
        return await _pre_tool_use(input_data, turn)

    return guard


async def _pre_tool_use(
    input_data: Mapping[str, Any], turn: Any = None
) -> SyncHookJSONOutput:
    """Deny shell commands that would corrupt knowledge-base files.

    Returns an empty dict to allow, which is the default for everything this
    does not recognise: the hook is a speed bump for one known-destructive
    class, not an allowlist.
    """
    try:
        if input_data.get("tool_name") != "Bash":
            return {}
        command = (input_data.get("tool_input") or {}).get("command", "")
        reason = unsafe_kb_write(command)
        if not reason:
            return {}

        if turn is not None:
            turn.guard_denials.append("Bash")
        log.warning(
            "blocked a knowledge-base write that would have corrupted a file (%s): %s",
            reason,
            command[:200],
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (f"Refused: {reason}.\n\n{_GUIDANCE}"),
            }
        }
    except Exception:  # a broken guard must not break the turn
        log.exception("KB write guard failed; allowing the command through")
        return {}


# --- the deferred-work guard ----------------------------------------------
#
# The failure this exists for, observed verbatim: asked to write a page and
# told that a missing GUIDE.md was "follow-up work for a later session", the
# agent wrote the page, replied "noted as a follow-up for a later session", and
# filed nothing. The work was named, acknowledged, and lost - which is the
# precise failure the whole ledger was built to end. See bead kb-3cl.
#
# It is not a permissions problem: asked directly, the same deployment runs
# `bd ready` and `bd create` happily. It is an instruction the model agrees
# with and then does not act on, which is the class of problem a prompt cannot
# fix by being stated once more, more loudly.
#
# So: catch it at the moment it happens. A Stop hook returning decision=block
# hands the model its own deferral language back and lets it finish the job
# while the context is still warm and the bead costs one tool call.

_DEFERRAL = re.compile(
    r"""
      follow[-\s]?up
    | later\s+(session|turn|time)
    | (another|a\s+future|a\s+separate|the\s+next)\s+(session|turn)
    | (for|until)\s+later
    | leav(e|ing)\s+(that|this|it)\s+for
    | (did\s?n[o']t|have\s?n[o']t|not)\s+creat(e|ed|ing)
    | worth\s+(doing|fixing|revisiting)\s+later
    | \bTODO\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# `bd create` is the filing verb; `bd quick` is its shorthand. Reopening or
# annotating an existing bead counts too - the work reached the ledger either
# way, which is the whole requirement.
_FILED = re.compile(r"\bbd\s+(create|quick|note|update|dep\s+add)\b")

_FILE_IT = (
    "You described work you are not doing, but did not put it anywhere it will "
    "survive this conversation. Saying it in chat is exactly the failure the "
    "bead ledger exists to end - the next session has no memory of this one and "
    "will never see it.\n\n"
    "File it now:\n\n"
    '    bd create --title="Short, specific title" \\\n'
    '      --description="What needs doing, where, and why - written for '
    'someone with no memory of this conversation." \\\n'
    "      --type=task --priority=2\n\n"
    "Then finish your reply as normal. If on reflection there is genuinely no "
    "durable work here - you were describing what you just did, or the user "
    "explicitly said not to track it - say so in one line and stop; you will "
    "not be asked twice."
)


def _transcript_rows(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a partially flushed final line is normal
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _this_turn(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows since the last real user message.

    Tool results also arrive as role=user, so "real" means a message carrying
    actual user text. Without this the guard would re-fire on deferral language
    from three turns ago, every turn, forever.
    """
    start = 0
    for i, row in enumerate(rows):
        message = row.get("message") or {}
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) or (
            isinstance(content, list)
            and any(
                block.get("type") == "text"
                for block in content
                if isinstance(block, dict)
            )
        ):
            start = i
    return rows[start:]


def unfiled_deferral(rows: list[dict[str, Any]]) -> str | None:
    """Return the deferral phrase that was never filed, or None.

    Pure over parsed transcript rows so the whole decision is unit-testable
    without a live agent.
    """
    said: list[str] = []
    for row in _this_turn(rows):
        message = row.get("message") or {}
        if message.get("role") != "assistant":
            continue
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                said.append(block.get("text") or "")
            elif block.get("type") == "tool_use":
                command = (block.get("input") or {}).get("command") or ""
                if _FILED.search(command):
                    return None  # it reached the ledger; nothing to enforce
    match = _DEFERRAL.search("\n".join(said))
    return match.group(0) if match else None


# Mapping, not dict: the SDK hands hooks a TypedDict, which is not assignable
# to dict[str, Any] because dict permits destructive operations. We only read.
async def stop_guard(
    input_data: Mapping[str, Any],
    _tool_use_id: str | None,
    _context: Any,
) -> SyncHookJSONOutput:
    """Block a turn that named future work and did not file it.

    Blocks at most once per turn: `stop_hook_active` is set on the re-entry,
    and a guard that could fire on its own re-prompt would loop until
    `max_turns`. One nudge, then the model's judgment stands.
    """
    try:
        if input_data.get("stop_hook_active"):
            return {}
        path = input_data.get("transcript_path")
        if not path:
            return {}
        phrase = unfiled_deferral(_transcript_rows(path))
        if not phrase:
            return {}

        log.info("stop guard: deferred work (%r) was never filed as a bead", phrase)
        return {
            "decision": "block",
            "reason": (f'You wrote "{phrase}" but ran no `bd` command.\n\n{_FILE_IT}'),
        }
    except Exception:  # a broken guard must not break the turn
        log.exception("deferred-work guard failed; letting the turn end")
        return {}
