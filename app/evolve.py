"""Bounded self-evolution: the rules a reflection turn cannot talk its way out of.

The agent may rewrite a skill's `description` and append to a `## Learned`
section. That is the whole permitted vocabulary of self-modification. It may
not touch a single existing body line, delete anything, or edit any file that
is not a skill in the knowledge base.

**Why the bound is here and not in the skill's instructions.** ADR 0007 records
two rules that were written into the prompt, agreed to by the model, and then
broken anyway - one of which cost a user their memory file. Self-modification
is the worst possible place to rely on a promise: a reflection turn that
misreads its remit edits the instructions every later turn depends on, and the
damage compounds silently. So the remit is enforced by a `PreToolUse` hook over
the proposed file content, computed by comparing what is on disk against what
the agent is about to write. `bounded_skill_edit` is pure, which means the
entire policy is unit-testable without an agent.

**Why description and Learned specifically.** The description is the only
routing signal that exists before a skill loads, so it is where an
under-triggering skill is actually broken - high value. And an append-only
section is monotonic: it can be read as a diff, trimmed by a human, and can
never destroy what it is adding to. Both are recoverable by construction.

**What is deliberately out of reach.**

- Skills shipped in the image (`skills/kb-curator/`). They are code, reviewed
  and deployed atomically. An edit there would also silently vanish on the next
  deploy, which is worse than being refused.
- `AGENT_GUIDE.md`, the human's own schema document.
- Everything in ADR 0007. Two of this system's load-bearing behaviours turned
  out not to be skill-shaped at all and now live in hooks. Those are guarantees,
  not config, and evolution must not reach them.

**Edits, not just appends, are refused wholesale.** A reflection turn must use
Write with the complete file. This is the KB-safe pattern anyway (the mount has
no read-modify-write) and it means the bound sees the entire proposed document
rather than a fragment it would have to apply itself to judge.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import kb

log = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
LEARNED_HEADING = re.compile(r"^#{2,3}\s+Learned\s*$", re.IGNORECASE)
_FRONTMATTER_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:")
MUTABLE_FIELD = "description"

EVOLUTION_LOG = "evolution.md"

# Reflection is a short, tightly-scoped errand, and a bounded budget is itself
# a safety property: a turn that starts wandering hits the wall before it can do
# much, and the max_turns signal records that it did. Measured rather than
# guessed - the first value tried was 12, and a real reflection spent all of it
# reading evidence and never reached the edit.
MAX_REFLECTION_TURNS = 30


@dataclass
class Change:
    """One accepted self-edit, recorded so it can be logged and undone."""

    skill: str
    path: str
    described: bool  # the description was rewritten
    learned: int  # lines appended under ## Learned

    def summary(self) -> str:
        bits = []
        if self.described:
            bits.append("rewrote description")
        if self.learned:
            bits.append(f"appended {self.learned} line(s) under ## Learned")
        return f"{self.skill}: {' and '.join(bits) or 'no effective change'}"


# --- the bound -------------------------------------------------------------


def _split_frontmatter(text: str) -> tuple[Optional[list[str]], str]:
    """Return (frontmatter lines, body). Frontmatter is None if absent."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i], "\n".join(lines[i + 1 :])
    return None, text  # unterminated: treat as no frontmatter, and it will fail


def _fields(frontmatter: list[str]) -> list[tuple[str, list[str]]]:
    """Group frontmatter into (key, block) pairs, keeping order.

    Deliberately not a YAML parse. We are comparing two documents for
    equality-except-one-field, and a parse would hide exactly the reformatting
    we want to notice - a folded block quietly becoming a flow scalar changes
    the file even though the parsed value is identical.

    Note that the *store* also re-serialises markdown frontmatter: it reorders
    keys and unfolds `description: >` blocks, which is the same behaviour that
    broke bootstrap seeding (see `seed_bootstrap`). That is harmless here only
    because the agent reads the stored form before writing, so both sides of
    the comparison are already in the store's shape. An agent that composed a
    skill file from memory instead of reading it would be refused for
    reordering it had not intended - which is the correct outcome, but is worth
    knowing before anyone debugs it as a bug.
    """
    out: list[tuple[str, list[str]]] = []
    for line in frontmatter:
        match = _FRONTMATTER_KEY.match(line)
        if match and not line[:1].isspace():
            out.append((match.group(1), [line]))
        elif out:
            out[-1][1].append(line)
        else:
            out.append(("", [line]))
    return out


def _learned_split(body: str) -> tuple[str, list[str]]:
    """Split a body into (everything before ## Learned, the Learned lines)."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if LEARNED_HEADING.match(line):
            return "\n".join(lines[:i]), lines[i + 1 :]
    return body, []


def _trimmed(lines: list[str]) -> list[str]:
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def bounded_skill_edit(current: str, proposed: str) -> Optional[str]:
    """Return why this rewrite exceeds the remit, or None if it is allowed.

    Pure on purpose: this is the entire self-modification policy, and it should
    be arguable in a test rather than only observable in production.
    """
    cur_fm, cur_body = _split_frontmatter(current)
    new_fm, new_body = _split_frontmatter(proposed)

    if cur_fm is None or new_fm is None:
        return (
            "a skill must keep its `---` frontmatter block; the proposed file "
            "has none or leaves it unterminated"
        )

    cur_fields = _fields(cur_fm)
    new_fields = _fields(new_fm)
    if [k for k, _ in cur_fields] != [k for k, _ in new_fields]:
        return (
            f"frontmatter fields may not be added, removed or reordered "
            f"(was {[k for k, _ in cur_fields]}, proposed "
            f"{[k for k, _ in new_fields]})"
        )
    for (key, before), (_, after) in zip(cur_fields, new_fields):
        if key == MUTABLE_FIELD or before == after:
            continue
        return f"`{key}:` may not be changed; only `{MUTABLE_FIELD}:` is mutable"

    cur_head, cur_learned = _learned_split(cur_body)
    new_head, new_learned = _learned_split(new_body)
    if cur_head.rstrip("\n") != new_head.rstrip("\n"):
        return (
            "the body above `## Learned` must be byte-identical; existing "
            "guidance may not be edited, reworded, reordered or deleted"
        )

    cur_learned = _trimmed(cur_learned)
    new_learned = _trimmed(new_learned)
    if new_learned[: len(cur_learned)] != cur_learned:
        return (
            "`## Learned` is append-only; existing entries may not be changed "
            "or removed"
        )

    described = _block_of(cur_fields, MUTABLE_FIELD) != _block_of(
        new_fields, MUTABLE_FIELD
    )
    if not described and len(new_learned) == len(cur_learned):
        return "this rewrite changes nothing; do not write the file back unchanged"
    return None


def _block_of(fields: list[tuple[str, list[str]]], key: str) -> list[str]:
    for k, block in fields:
        if k == key:
            return block
    return []


def describe_edit(current: str, proposed: str, path: Path) -> Change:
    """What an already-approved edit actually did. Call only after the bound passes."""
    cur_fm, cur_body = _split_frontmatter(current)
    new_fm, new_body = _split_frontmatter(proposed)
    cur_learned = _trimmed(_learned_split(cur_body)[1])
    new_learned = _trimmed(_learned_split(new_body)[1])
    return Change(
        skill=path.parent.name,
        path=str(path),
        described=_block_of(_fields(cur_fm or []), MUTABLE_FIELD)
        != _block_of(_fields(new_fm or []), MUTABLE_FIELD),
        # Non-blank only: the blank line after the heading is layout, not an
        # entry, and "appended 2 lines" for one bullet reads as a lie in the log.
        learned=_nonblank(new_learned) - _nonblank(cur_learned),
    )


def _nonblank(lines: list[str]) -> int:
    return sum(1 for line in lines if line.strip())


def merge(changes: list[Change]) -> list[Change]:
    """Collapse repeated edits to one skill into their net effect.

    A reflection turn often writes the same file more than once - refining the
    wording, or rewriting after the store's markdown re-serialisation makes the
    file read back differently from what was sent. Each write is a real,
    permitted edit, but logging them separately reports "two changes" for one
    net change and makes the evolution log read as churn.
    """
    out: dict[str, Change] = {}
    for change in changes:
        if prior := out.get(change.path):
            out[change.path] = Change(
                skill=change.skill,
                path=change.path,
                described=prior.described or change.described,
                learned=prior.learned + change.learned,
            )
        else:
            out[change.path] = change
    return list(out.values())


# --- what a reflection turn may touch at all -------------------------------


def mutable_skill_path(path_str: str) -> Optional[Path]:
    """Return the resolved path if it is a KB skill file, else None.

    Image skills are excluded by construction: this only accepts paths under
    the knowledge base's own `skills/` directory, and the image copy lives in
    the container filesystem instead.
    """
    try:
        path = Path(path_str).resolve()
    except (OSError, ValueError):
        return None
    skills_root = (kb.workspace_root() / "skills").resolve()
    if path.name != SKILL_FILE:
        return None
    if skills_root not in path.parents:
        return None
    return path


_REFUSED_TOOLS = {"Edit", "MultiEdit", "NotebookEdit"}

_WRITE_WHOLE = (
    "Use Write with the complete file instead. The knowledge base has no "
    "read-modify-write, so a partial edit is unsafe here regardless - and a "
    "whole-file write is what lets the remit be checked before it lands."
)


def write_guard_for(turn: Any) -> Any:
    """Build the PreToolUse hook enforcing the remit, recording what it allows.

    A closure over the turn because the accepted changes have to survive into
    the evolution log and into the immune memory that a later Revert consults.
    """

    async def guard(
        input_data: dict[str, Any],
        tool_use_id: Optional[str],
        context: Any,
    ) -> dict[str, Any]:
        try:
            tool = input_data.get("tool_name") or ""
            tool_input = input_data.get("tool_input") or {}

            if tool in _REFUSED_TOOLS:
                return _deny(
                    f"`{tool}` is not available while reflecting. {_WRITE_WHOLE}",
                    turn, tool,
                )
            if tool != "Write":
                return {}

            path = mutable_skill_path(tool_input.get("file_path") or "")
            if path is None:
                return _deny_write(
                    turn,
                    "reflection may only write SKILL.md files inside the "
                    f"knowledge base at {kb.workspace_root()}/skills/. Skills "
                    "shipped in the image are code and are reviewed and "
                    "deployed like code; AGENT_GUIDE.md belongs to the human. "
                    "If the fix is somewhere else, file a bead describing it "
                    "instead of making the change."
                )

            try:
                current = path.read_text(encoding="utf-8")
            except OSError:
                return _deny_write(
                    turn,
                    f"{path} does not exist yet. Reflection improves existing "
                    "skills; it does not create new ones. File a bead if a new "
                    "skill is what is needed."
                )

            proposed = tool_input.get("content") or ""
            reason = bounded_skill_edit(current, proposed)
            if reason:
                return _deny_write(
                    turn,
                    f"Refused: {reason}.\n\nYou may rewrite the `description:` "
                    "frontmatter field and append entries under a `## Learned` "
                    "heading. Nothing else. If the skill needs a deeper change "
                    "than that, say so and file a bead for a human - that is a "
                    "successful outcome of reflecting, not a failure."
                )

            change = describe_edit(current, proposed, path)
            turn.evolved.append(change)
            log.info("reflection accepted a bounded self-edit: %s", change.summary())
            return {}
        except Exception:  # noqa: BLE001 - see ADR 0007; guards fail open
            log.exception("evolution guard failed")
            return {}

    return guard


def _deny_write(turn: Any, reason: str) -> dict[str, Any]:
    return _deny(reason, turn, "Write")


def _deny(reason: str, turn: Any = None, tool: str = "") -> dict[str, Any]:
    if turn is not None and tool:
        turn.guard_denials.append(tool)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


# --- the visible record ----------------------------------------------------


async def log_changes(changes: list[Change], savepoint: str, trigger: str) -> None:
    """Append to memory/evolution.md, newest first.

    Written by the application rather than by the reflecting agent, because the
    failure mode to design against is not a bad edit - it is a bad edit nobody
    noticed. A log the agent writes is a log the agent can forget to write.
    """
    if not changes:
        return
    changes = merge(changes)
    path = kb.workspace_root() / EVOLUTION_LOG
    entry = [
        f"## {savepoint}",
        "",
        f"Triggered by: {trigger}",
        "",
    ]
    entry += [f"- {c.summary()} (`{c.path}`)" for c in changes]
    entry += [
        "",
        f"To undo: revert this turn in the UI, or roll back to savepoint "
        f"`{savepoint}`. A revert also marks the proposal rejected so it is "
        "not proposed again.",
        "",
    ]

    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        existing = ""
    if not existing.strip():
        existing = (
            "# Evolution log\n\n"
            "Every change the agent has made to its own skills, newest first. "
            "Written by the application, not by the agent. See "
            "docs/decisions/0008.\n"
        )

    header, _, rest = existing.partition("\n## ")
    body = "\n".join(entry)
    updated = header.rstrip("\n") + "\n\n" + body + ("\n## " + rest if rest else "")
    try:
        # Whole file, trailing newline. Both are load-bearing: see ADR 0007.
        path.write_text(updated.rstrip("\n") + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)


REFLECTION_PROMPT = """\
You are reflecting on your own skills. This is not a normal turn: there is no
human waiting, and the knowledge base is not what you are here to change.

1. Read the evidence. `bd list --label signal --json` returns observation beads
   - reverts, errors, exhausted turns - each carrying the prompt, the skills
   that turn read, and what was rolled back. Read them together rather than one
   at a time: a single revert is a noisy label, and the pattern across several
   is worth more than any one of them.

2. Decide honestly whether a skill was actually at fault. Most failures are the
   situation, not the guidance: the human changed their mind, the model erred,
   the infrastructure broke. Concluding "no skill change is warranted" is a
   correct and common outcome. Say so and stop.

3. Check what has already been rejected. A bead noted REJECTED PROPOSAL carries
   a change that was made and then reverted by the human. Do not propose it
   again; propose something different or nothing.

4. If a skill is genuinely at fault, make exactly one bounded change to it:
   rewrite its `description:` frontmatter, or append an entry under a
   `## Learned` heading, or both. Write the whole file with the Write tool.
   You may not change anything else, and attempts will be refused.

   Prefer the description when the skill failed to trigger - it is the only
   routing signal that exists before a skill loads. Prefer `## Learned` when
   the skill triggered but its guidance was wrong.

5. Close or note the beads you acted on, so the next reflection does not redo
   this. File a bead for anything you noticed but could not fix within the
   remit; a change too deep to make here is exactly what a human should see.

Change at most one skill. A small, reversible, well-explained edit beats a
thorough one. You have a limited turn budget: spend it on the decision, not on
exploring the knowledge base.

--- Outcome rates so far ---

{totals}

A skill that loads on every turn appears in every failure by construction, so
read any count against the number of turns that used the skill at all. A rate
is evidence; a raw count is a popularity contest.
"""


def reflection_prompt(totals: str) -> str:
    return REFLECTION_PROMPT.format(totals=totals or "(no outcome data recorded yet)")
