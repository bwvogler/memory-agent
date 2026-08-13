"""A PreToolUse hook that refuses shell commands which would corrupt the KB.

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

import logging
import re
from typing import Any, Optional

from .config import config

log = logging.getLogger(__name__)

# Each entry: (regex over the command, what the agent should do instead).
# `>` is deliberately absent - a truncating redirect writes the whole file from
# offset 0, which is exactly the safe pattern.
_HAZARDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r">>"),
     "`>>` appends, which zeroes everything already in the file"),
    (re.compile(r"\btee\s+(-a\b|--append\b)"),
     "`tee -a` appends, which zeroes everything already in the file"),
    (re.compile(r"\bsed\s+(-[a-zA-Z]*i|--in-place)"),
     "`sed -i` rewrites in place and cannot be trusted on this mount"),
    (re.compile(r"\bdd\b.*\b(seek|conv=notrunc)"),
     "`dd` writing at an offset leaves the untouched bytes as NULs"),
    (re.compile(r"\btruncate\b"),
     "`truncate` resizes without rewriting the content"),
    (re.compile(r"\bpatch\b"),
     "`patch` edits in place and cannot be trusted on this mount"),
    (re.compile(r"""open\s*\([^)]*['"]a['"]?"""),
     "opening a file in append mode zeroes everything already in it"),
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


def unsafe_kb_write(command: str) -> Optional[str]:
    """Return why this command endangers the KB, or None if it is fine.

    Pure and string-only, so the hazard list can be tested without an agent.
    """
    if not command or not _mentions_kb(command):
        return None
    for pattern, reason in _HAZARDS:
        if pattern.search(command):
            return reason
    return None


async def pre_tool_use_guard(
    input_data: dict[str, Any],
    tool_use_id: Optional[str],
    context: Any,
) -> dict[str, Any]:
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

        log.warning(
            "blocked a knowledge-base write that would have corrupted a file "
            "(%s): %s",
            reason,
            command[:200],
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Refused: {reason}.\n\n{_GUIDANCE}"
                ),
            }
        }
    except Exception:  # noqa: BLE001 - a broken guard must not break the turn
        log.exception("KB write guard failed; allowing the command through")
        return {}
