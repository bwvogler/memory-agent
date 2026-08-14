"""Stage 2: notice when a turn went wrong, and record which skills it used.

This module only observes. It changes nothing about how the agent behaves, and
it is deliberately the whole of Stage 2 - the machinery that would *act* on
these signals is Stage 3, and is gated on bead kb-3sv, which asks whether the
signals that actually arrive justify building it. See
docs/decisions/0006-beads-is-the-work-ledger.md and the Stage 3 beads.

Three things worth understanding before changing anything here:

1. **The Revert button is the good signal.** It is an explicit, human-labelled
   "that was wrong", already bound to an exact turn. Almost no agent system
   has one. Everything else in this file is a cheaper, noisier substitute.

2. **Attribution needs a denominator.** Knowing that kb-curator appears in two
   reverted turns is worthless without knowing how many turns used it at all -
   a skill loaded on every turn appears in every failure by construction. So
   the ledger records *every* turn's skill use and its outcome, not just the
   bad ones. This is the only reason `turn_outcomes` exists rather than the
   signal beads being the whole record.

3. **Signal beads are evidence, not work.** They are created `deferred` so
   they stay out of `bd ready`, where they would otherwise present themselves
   to the agent as a backlog to grind through. They are read in bulk, by a
   human or by kb-3sv.

Nothing in here may break a turn. Every entry point swallows its own
exceptions: a failure to record why something went wrong must not itself
become a second thing going wrong.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Optional

from . import kb
from .turns import Turn, TurnState

if TYPE_CHECKING:
    from .session_store import PostgresSessionStore

log = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
SIGNAL_LABEL = "signal"

# Signal bead bodies quote the prompt and the reverted diff. Both are unbounded
# in principle and a bead description is read by an agent with a token budget.
MAX_PROMPT_CHARS = 600
MAX_DIFF_CHARS = 1500

# Outcomes recorded per turn. "reverted" is set later, by the revert handler.
OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_MAX_TURNS = "max_turns"
OUTCOME_REVERTED = "reverted"

_store: Optional["PostgresSessionStore"] = None


def attach_store(store: Optional["PostgresSessionStore"]) -> None:
    """Give this module the durable store, without importing main.

    main imports agent, which imports this; a reverse import would be a cycle.
    The ledger degrades to bead-only if no store is configured.
    """
    global _store
    _store = store


# ---------------------------------------------------------------------------
# Detection - pure functions, no I/O, so they are cheap to test
# ---------------------------------------------------------------------------


def _skill_from_path(value: str) -> Optional[str]:
    """Return the skill name for a path to a SKILL.md, else None."""
    if not value.endswith(SKILL_FILE):
        return None
    parts = PurePosixPath(value).parts
    return parts[-2] if len(parts) >= 2 else None


def skills_from_tool_use(name: str, tool_input: dict) -> set[str]:
    """Name every skill a single tool call implies the agent touched.

    Two paths reach a skill, and both count: the SDK's own Skill tool, and an
    ordinary Read of a SKILL.md file (which is how skills under `add_dirs`
    are usually pulled in here). Scanning every string argument rather than
    one known key is deliberate - the argument names in these tool schemas
    are exactly the kind of thing that drifts between SDK releases, and a
    silently empty ledger is the failure this whole stage exists to avoid.
    """
    found: set[str] = set()

    if name == "Skill":
        for key in ("skill", "name", "command"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                found.add(value.strip().split("/")[-1])
                break

    for value in tool_input.values():
        if isinstance(value, str) and (skill := _skill_from_path(value)):
            found.add(skill)

    return found


def _denied_tool(denial: object) -> str:
    """Pull a tool name out of a permission denial of unknown shape."""
    if isinstance(denial, dict):
        return str(denial.get("tool_name") or denial.get("tool") or "unknown")
    return str(getattr(denial, "tool_name", None) or "unknown")


def observe_message(turn: Turn, message: object) -> None:
    """Fold one SDK message into the turn's signal state. Never raises."""
    try:
        from claude_agent_sdk.types import (
            AssistantMessage,
            ResultMessage,
            ToolUseBlock,
        )

        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    turn.skills |= skills_from_tool_use(block.name, block.input or {})

        elif isinstance(message, ResultMessage):
            # Older CLIs report exhaustion only through subtype.
            turn.terminal_reason = message.terminal_reason or (
                OUTCOME_MAX_TURNS if message.subtype == "error_max_turns" else None
            )
            turn.permission_denials = [
                _denied_tool(d) for d in (message.permission_denials or [])
            ]
    except Exception:  # noqa: BLE001 - observation must never break a turn
        log.exception("failed to observe message for turn %s", turn.id)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def _outcome(turn: Turn) -> str:
    if turn.state is TurnState.ERROR:
        return OUTCOME_ERROR
    if turn.terminal_reason == OUTCOME_MAX_TURNS:
        return OUTCOME_MAX_TURNS
    return OUTCOME_OK


def _skill_list(turn: Turn) -> str:
    return ", ".join(sorted(turn.skills)) if turn.skills else "none recorded"


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


async def _already_open(user_slug: str, title: str) -> bool:
    """True if an unclosed signal bead already carries this exact title.

    Repeated identical failures - a missing allowlist entry, say - would
    otherwise file one bead per turn forever. Reverts are deliberately NOT
    deduped: each is a distinct human judgement and the count is the data.
    """
    issues = await kb.list_beads(user_slug, label=SIGNAL_LABEL)
    if issues is None:
        # bd is unreachable. Filing will fail too, so the answer does not
        # matter much - but claim "already open" so a broken ledger cannot
        # produce a flood once it recovers.
        return True
    return any(i.get("title") == title and i.get("status") != "closed" for i in issues)


async def _file_signal(
    user_slug: str,
    title: str,
    body: str,
    priority: int,
    labels: tuple[str, ...],
    dedupe: bool = True,
) -> Optional[str]:
    if dedupe and await _already_open(user_slug, title):
        log.info("signal bead already open, not filing again: %s", title)
        return None
    footer = (
        "\n\nThis bead is evidence, not a task. It is deferred so it stays out "
        "of `bd ready`. Read it in bulk alongside the other `signal` beads "
        "when working kb-3sv."
    )
    return await kb.create_bead(
        user_slug,
        title,
        description=body + footer,
        priority=priority,
        labels=(SIGNAL_LABEL,) + labels,
        status="deferred",
    )


async def record_turn(turn: Turn, user_slug: str) -> list[str]:
    """Persist the turn's skill use and file beads for cheap failure signals.

    Called once, after the turn finishes, for every turn - including failed
    ones, which is the point.

    Returns the ids of any beads filed, which is how the caller knows whether
    this turn produced a signal worth reflecting on.
    """
    filed: list[str] = []
    try:
        outcome = _outcome(turn)
        # Two independent jobs, and they must fail independently. These used to
        # share this function's `try`, with the ledger write first - so a
        # session store that was down took the signal beads with it, and the
        # only trace was one log line. Losing the denominator is survivable;
        # losing the evidence a turn went wrong is the thing this module
        # exists to prevent. Found by two container tests failing together and
        # looking like two unrelated flakes.
        if _store:
            try:
                await _store.record_turn_outcome(
                    turn.id,
                    turn.user_email,
                    outcome,
                    turn.terminal_reason,
                    sorted(turn.skills),
                )
            except Exception:  # noqa: BLE001 - the beads below still matter
                log.exception(
                    "could not record the turn outcome for %s; filing signals anyway",
                    turn.id,
                )

        if outcome == OUTCOME_ERROR:
            filed.append(
                await _file_signal(
                    user_slug,
                    f"Turn failed: {_error_key(turn)}",
                    f"A turn ended in error rather than completing.\n\n"
                    f"Skills that turn used: {_skill_list(turn)}\n\n"
                    f"Prompt:\n> {_clip(turn.prompt, MAX_PROMPT_CHARS)}\n\n"
                    f"Error:\n```\n{_clip(turn.error or '', 500)}\n```\n\n"
                    "Weak evidence about any skill: most errors are infrastructure "
                    "or model failures, not bad guidance.",
                    priority=3,
                    labels=("error",),
                )
            )

        elif outcome == OUTCOME_MAX_TURNS:
            filed.append(
                await _file_signal(
                    user_slug,
                    "Turn exhausted max_turns without finishing",
                    f"The agent used its entire turn budget and stopped without "
                    f"finishing. That usually means it was going in circles, "
                    f"which can indicate guidance that sends it round a loop.\n\n"
                    f"Skills that turn used: {_skill_list(turn)}\n\n"
                    f"Prompt:\n> {_clip(turn.prompt, MAX_PROMPT_CHARS)}",
                    priority=3,
                    labels=("max-turns",),
                )
            )

        # Denials our own hooks made are the guards working, not a defect.
        # Filing them would put P1 'check allowed_tools' beads into the very
        # ledger reflection reads, every time a guard did its job.
        unexpected = set(turn.permission_denials) - set(turn.guard_denials)
        for tool in sorted(unexpected):
            filed.append(
                await _file_signal(
                    user_slug,
                    f"Agent was denied permission to use: {tool}",
                    f"The agent tried to use `{tool}` and was refused, in a "
                    f"headless context where nobody can grant it. This is almost "
                    f"always a deployment defect rather than anything the model "
                    f"did: check `allowed_tools` in `_options` in app/agent.py.\n\n"
                    f"This exact failure once made the agent silently unable to "
                    f"run `bd` at all while looking completely healthy, which is "
                    f"why it is worth a bead rather than a log line.\n\n"
                    f"Skills that turn used: {_skill_list(turn)}",
                    priority=1,
                    labels=("permission",),
                )
            )
    except Exception:  # noqa: BLE001 - recording must never break a turn
        log.exception("failed to record signals for turn %s", turn.id)
    return [b for b in filed if b]


async def note_rejected_proposals(turn: Turn, user_slug: str) -> None:
    """Immune memory: a self-edit the human reverted must not be re-proposed.

    Without this the loop oscillates. Reflection reads the same evidence next
    time, reaches the same conclusion, makes the same edit, and is reverted
    again - forever, and each cycle looks locally reasonable.

    The record goes on the signal beads themselves rather than into a separate
    store, because that is what a reflection turn already reads.
    """
    if not turn.evolved:
        return
    summary = "; ".join(c.summary() for c in turn.evolved)
    # Labelled `signal` as well as `evolution-rejected` so it arrives in the
    # same `bd list --label signal` that reflection already reads. A rejection
    # nobody looks at is not immune memory, it is just a record.
    await _file_signal(
        user_slug,
        f"REJECTED self-edit: {_clip(summary, 60)}",
        "A reflection turn made this change to its own skills and a human "
        "reverted it:\n\n"
        f"    {summary}\n\n"
        f"Reflection turn: {turn.id}\nSavepoint: {turn.savepoint}\n\n"
        "Do not make this change again. If the evidence that prompted it still "
        "looks actionable, propose something materially different, or conclude "
        "that no skill change is warranted - which is a correct outcome.\n\n"
        "Without this record the loop oscillates: reflection reads the same "
        "evidence, reaches the same conclusion, is reverted again, forever, "
        "and every cycle looks locally reasonable.",
        priority=1,
        labels=("evolution-rejected",),
        dedupe=False,
    )
    log.info(
        "recorded %d rejected self-edit(s) from turn %s", len(turn.evolved), turn.id
    )


async def on_revert(turn: Turn, user_slug: str, diff_stat: str) -> Optional[str]:
    """Record a Revert click. The highest-value signal in the system.

    `diff_stat` must be captured BEFORE the undo runs - afterwards the working
    tree matches the savepoint and there is nothing left to describe.
    """
    try:
        if _store:
            await _store.mark_turn_outcome(turn.id, OUTCOME_REVERTED)

        body = (
            "The human clicked Revert on this turn - an explicit, precisely "
            "scoped 'that was wrong', bound to an exact turn.\n\n"
            f"Turn: {turn.id}\n"
            f"Savepoint: {turn.savepoint}\n"
            f"Skills that turn used: {_skill_list(turn)}\n\n"
            f"Prompt:\n> {_clip(turn.prompt, MAX_PROMPT_CHARS)}\n\n"
            "Knowledge-base changes that were rolled back:\n```\n"
            f"{_clip(diff_stat, MAX_DIFF_CHARS) or '(none recorded)'}\n```\n\n"
            "Do not assume a skill is at fault. A revert can equally mean the "
            "human changed their mind, or asked for the wrong thing. Deciding "
            "which is the hard part, and is exactly what kb-3sv is meant to "
            "judge with real examples in hand."
        )
        # One bead per revert, naming every skill involved, rather than one per
        # skill: splitting them up would presume the attribution that the whole
        # exercise is supposed to determine.
        return await _file_signal(
            user_slug,
            f"Revert: {_clip(turn.prompt, 60) or turn.id}",
            body,
            priority=1,
            labels=("revert",),
            dedupe=False,
        )
    except Exception:  # noqa: BLE001 - a failed recording must not fail the revert
        log.exception("failed to record revert signal for turn %s", turn.id)
        return None


def _error_key(turn: Turn) -> str:
    """A stable-ish title fragment, so repeats of one error dedupe."""
    text = (turn.error or "unknown").strip().splitlines()[0]
    return _clip(text, 80)


async def evidence_summary() -> str:
    """Outcome rates, as text a reflection prompt can carry.

    Injected rather than fetched: a reflection turn holds `Bash(bd:*)` and
    nothing else, so it cannot call `GET /api/signals` however clearly it is
    told to. An instruction the agent has no tool to follow does not merely
    fail, it burns turns being retried.
    """
    if not _store:
        return ""
    try:
        totals = await _store.turn_totals()
        rows = await _store.skill_signal_summary()
    except Exception:  # noqa: BLE001
        log.exception("could not summarise signal evidence")
        return ""

    lines = [
        f"Turns recorded: {totals.get('turns', 0)} "
        f"(reverted {totals.get('reverted', 0)}, "
        f"errored {totals.get('errored', 0)}, "
        f"max_turns {totals.get('max_turns', 0)})"
    ]
    for row in rows:
        lines.append(
            f"  {row.get('skill')}: used on {row.get('turns')} turn(s), "
            f"reverted {row.get('reverted')}, errored {row.get('errored')}"
        )
    return "\n".join(lines)
