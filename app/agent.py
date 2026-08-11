"""The agent runtime: one Claude Agent SDK session per conversation.

Design notes that matter more than the code:

1. Memory is loaded EXPLICITLY, not discovered.
   We read the memory file out of the KB mount ourselves and pass it as an
   appended system prompt, rather than letting the SDK find a CLAUDE.md by
   walking the filesystem. Three reasons:
     * A missing CLAUDE.md is silently skipped by the SDK - no error, no
       warning - so a mount that is not up yet produces an agent that is
       quietly dumber rather than one that fails loudly.
     * `@path` imports pointing outside the working directory trigger a
       one-time interactive approval dialog, which never gets answered in a
       headless container.
     * `setting_sources=[]` keeps one user's filesystem settings from leaking
       into another user's session, and explicit loading is compatible with it.
   Loading memory this way costs nothing in prompt-cache terms: the SDK injects
   CLAUDE.md-style content into the conversation, not the cached system-prompt
   prefix. See docs/decisions/0004-memory-lives-in-the-kb.md.

2. Scratch space is local, the knowledge base is a mounted extra directory.
   `cwd` is per-user local disk. If cwd were the mount, every temp file the
   agent writes would become a versioned row in the knowledge base.

3. Skills ship in the image, reference content lives in the KB.
   Skills are code: you want them reviewed, diffed, and deployed atomically,
   and you do not want the agent editing its own operating instructions by
   accident. Reference material is data: it belongs where versioning and undo
   earn their keep.

    !! Option names to confirm against your installed SDK version !!
    `add_dirs`, `permission_mode` and the `system_prompt` preset shape are the
    parts of this file most likely to drift between SDK releases. They are all
    set in one place (`_options`) on purpose.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

from . import kb
from .config import config
from .turns import Turn, TurnState

log = logging.getLogger(__name__)

MEMORY_RELATIVE_PATH = "memory/CLAUDE.md"
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def _read_memory() -> str:
    """Load the agent's memory from the knowledge base.

    Returns an empty string if unavailable - but logs a warning, because a
    silently missing memory file is the single most confusing failure mode in
    this architecture.
    """
    path = kb.mount_root() / MEMORY_RELATIVE_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "memory file %s unreadable (%s) - the agent will run WITHOUT its "
            "accumulated memory. Check that the KB mount is live.",
            path,
            exc,
        )
        return ""
    log.info("loaded %d bytes of memory from %s", len(text), path)
    return text


def _system_prompt_append() -> str:
    memory = _read_memory()
    parts = [
        f"You have a knowledge base mounted at {config.kb_mount}.",
        "It is a TigerFS filesystem backed by PostgreSQL: ordinary file tools "
        "work on it, every write is versioned, and the control directories "
        f"{config.kb_mount}/.history/, .log/, .savepoint/ and .undo/ let you "
        "inspect and roll back changes.",
        "Note that some control paths are path-accessible but deliberately "
        "hidden from `ls`, so do not conclude they are absent just because a "
        "directory listing does not show them.",
        "Before you add to the knowledge base, look for an existing document "
        "to extend rather than creating a near-duplicate.",
    ]
    if memory:
        parts.append("--- Accumulated memory ---\n" + memory)
    return "\n\n".join(parts)


def _options(user_slug: str, resume: str | None) -> ClaudeAgentOptions:
    scratch = kb.scratch_dir_for(user_slug)
    config_dir = Path(config.work_dir) / f".claude-{user_slug}"
    config_dir.mkdir(parents=True, exist_ok=True)

    return ClaudeAgentOptions(
        model=config.agent_model,
        cwd=str(scratch),
        max_turns=config.max_turns,
        resume=resume,
        # Multi-tenant isolation: load nothing from the filesystem, and give
        # each user their own config dir so they do not share ~/.claude.json.
        setting_sources=[],
        # Give the agent access to the KB and to the skills that teach it how
        # to navigate the KB. VERIFY the option name for your SDK version.
        add_dirs=[config.kb_mount, str(SKILLS_DIR)],
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": _system_prompt_append(),
        },
        # Headless: nobody is present to answer a permission prompt.
        permission_mode="acceptEdits",
        # Stream tokens as they arrive so the UI can show them in real time.
        include_partial_messages=True,
        env={
            **os.environ,
            # Auto memory loads into the system prompt regardless of
            # setting_sources, so it must be disabled explicitly or one user's
            # accumulated notes leak into another user's session.
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CONFIG_DIR": str(config_dir),
        },
    )


async def run_turn(turn: Turn, prompt: str, user_slug: str, resume: str | None = None) -> None:
    """Run one agent turn to completion, streaming events into the turn buffer.

    Every turn is wrapped in a TigerFS savepoint named after the turn id. If
    the agent makes a mess of the knowledge base, the whole turn is one atomic
    undo away - and because TigerFS undo is itself reversible, the revert is
    safe to expose as a button in the UI. This is the single best reason to put
    a knowledge base on TigerFS rather than in a vector store.
    """
    savepoint = f"turn-{turn.id}"
    if await kb.create_savepoint(savepoint):
        turn.savepoint = savepoint

    turn.append("status", "started")

    try:
        async for message in query(prompt=prompt, options=_options(user_slug, resume)):
            for kind, data in _render(message):
                turn.append(kind, data)
            session_id = _extract_session_id(message)
            if session_id and not turn.session_id:
                turn.session_id = session_id
                turn.append("session", session_id)
        turn.finish(TurnState.DONE)
    except Exception as exc:  # noqa: BLE001 - surface everything to the client
        log.exception("turn %s failed", turn.id)
        turn.append("error", str(exc))
        turn.finish(TurnState.ERROR, error=str(exc))


def _extract_session_id(message: object) -> str | None:
    """Pull the session id out of an SDK message."""
    from claude_agent_sdk.types import AssistantMessage, SystemMessage

    if isinstance(message, AssistantMessage) and message.session_id:
        return message.session_id
    if isinstance(message, SystemMessage) and message.subtype == "init":
        value = message.data.get("session_id")
        return str(value) if value else None
    # Fallback for forward-compatibility with future message shapes.
    value = getattr(message, "session_id", None)
    return str(value) if value else None


def _render(message: object) -> list[tuple[str, str]]:
    """Flatten an SDK message into (kind, text) pairs for the event stream.

    With include_partial_messages=True the SDK emits StreamEvent objects for
    each raw API event. We forward text deltas immediately so the UI streams
    tokens as they arrive. The subsequent AssistantMessage (sent once the full
    turn completes) is used only for tool events — text was already streamed.
    """
    from claude_agent_sdk.types import (
        AssistantMessage,
        ServerToolUseBlock,
        StreamEvent,
        TextBlock,
        ToolUseBlock,
    )

    if isinstance(message, StreamEvent):
        event = message.event
        if event.get("type") == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta" and delta.get("text"):
                return [("text_delta", delta["text"])]
        return []

    if not isinstance(message, AssistantMessage):
        return []

    # Emit text as a "text" fallback event AND tool names.
    # The client ignores "text" events if it already received "text_delta" events
    # (which means --include-partial-messages is working). If the bundled CLI
    # version doesn't support that flag, "text" acts as the non-streaming path.
    out: list[tuple[str, str]] = []
    for block in message.content:
        if isinstance(block, TextBlock) and block.text:
            out.append(("text", block.text))
        elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
            out.append(("tool", block.name))
    return out
