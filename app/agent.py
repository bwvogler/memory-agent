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

4. Task state lives in beads, and its instructions come from `bd prime`.
   bd ships its own workflow context and keeps it current with the binary, so
   we run `bd prime` and inject the output rather than hand-maintaining a copy
   that rots at the next version bump. bd would normally install a SessionStart
   hook to do this, but `setting_sources=[]` means project settings are never
   read, so we do it explicitly - the same pattern as memory in (1).
   See docs/decisions/0006-beads-is-the-work-ledger.md.

    !! Option names to confirm against your installed SDK version !!
    `add_dirs`, `permission_mode` and the `system_prompt` preset shape are the
    parts of this file most likely to drift between SDK releases. They are all
    set in one place (`_options`) on purpose.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

from . import kb
from .config import config
from .turns import Turn, TurnState

log = logging.getLogger(__name__)

MEMORY_RELATIVE_PATH = "memory/CLAUDE.md"
GUIDE_RELATIVE_PATH = "AGENT_GUIDE.md"
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
BOOTSTRAP_DIR = Path(__file__).resolve().parent.parent / "bootstrap"
SEED_STATE_FILE = ".bootstrap-state.json"


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


def _read_guide() -> str:
    """Load the per-instance workspace guide from the knowledge base.

    Returns an empty string if the file does not exist yet (expected on first
    run before seeding completes).
    """
    path = kb.workspace_root() / GUIDE_RELATIVE_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        log.debug("guide file %s not found - running without workspace guide", path)
        return ""
    log.info("loaded %d bytes of workspace guide from %s", len(text), path)
    return text


def seed_guide() -> None:
    """Write a starter AGENT_GUIDE.md if one does not already exist."""
    if not kb.is_mounted():
        return
    path = kb.workspace_root() / GUIDE_RELATIVE_PATH
    if path.exists():
        return
    starter = f"""\
# Workspace guide

Your wiki workspace is at `{kb.workspace_root()}`. All content files go here using
absolute paths.

## Directory layout

The workspace has two top-level sections. These are the only two that should
exist at the root level — never create other top-level directories.

- `wiki/` — human knowledge: notes, recipes, references, research, etc.
  Subdirectories inside `wiki/` organise content by topic (e.g. `wiki/recipes/`).
- `skills/` — reusable agent skills (SKILL.md files the human invokes explicitly).

Each top-level section has its own `GUIDE.md` that describes what belongs there.
When the human tells you about a new content type, add a subdirectory under
`wiki/` (or `skills/` if it is a skill), then create a `GUIDE.md` inside it
describing the expected format. Update this document to record the new
subdirectory.

## Conventions

Before writing anything in a directory, check if a `GUIDE.md` exists there and
follow its format requirements. If no guide exists and you are creating structured
content (recipes, notes, etc.), ask the human how they would like it formatted,
then write a `GUIDE.md` documenting that format for future turns.

When adding content, look for an existing file to extend before creating a new one.
Correct facts in place — the version history preserves what was there before.
"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(starter, encoding="utf-8")
        log.info("seeded workspace guide at %s", path)
    except OSError as exc:
        log.warning("could not seed guide at %s: %s", path, exc)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_seed_state(path: Path) -> dict[str, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("shipped", {})
    except (OSError, ValueError):
        return {}


def seed_bootstrap() -> None:
    """Copy bootstrap skill files into the KB workspace.

    These skills live in the KB so the human can improve them, which means we
    cannot simply overwrite on upgrade. But never overwriting is worse: a
    shipped fix would silently never reach any existing deployment, and the
    seeder would look like it worked.

    So we record a hash of what we last shipped. A file still matching that
    hash is untouched and safe to replace; a file that differs has been edited
    and is left alone with a warning naming it. Deployments predating the
    state file have no recorded hash, so we cannot tell and do not guess.
    """
    if not kb.is_mounted():
        return
    skills_src = BOOTSTRAP_DIR / "skills"
    if not skills_src.is_dir():
        return

    skills_dst = kb.workspace_root() / "skills"
    state_path = skills_dst / SEED_STATE_FILE
    shipped = _read_seed_state(state_path)
    updated: dict[str, str] = {}

    for src_file in sorted(skills_src.rglob("*")):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(skills_src)
        key = rel.as_posix()
        dst_file = skills_dst / rel
        payload = src_file.read_bytes()
        updated[key] = _digest(payload)

        try:
            if dst_file.exists():
                current = _digest(dst_file.read_bytes())
                if current == updated[key]:
                    continue  # already current
                if key not in shipped:
                    log.warning(
                        "skill %s predates seed tracking and differs from the "
                        "shipped version; leaving it alone. Delete it to take "
                        "the new one.",
                        dst_file,
                    )
                    # Leave it untracked. Recording the current hash would make
                    # the next run mistake it for something we shipped.
                    updated.pop(key, None)
                    continue
                if current != shipped[key]:
                    log.warning(
                        "skill %s has local edits; not overwriting with the "
                        "newer shipped version. Previous content stays in "
                        "TigerFS history if you want to compare.",
                        dst_file,
                    )
                    # Keep the ORIGINAL shipped hash, not the edited file's.
                    # Recording the edit here would make it match on the next
                    # run and silently clobber the human's work one seed later.
                    updated[key] = shipped[key]
                    continue
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            dst_file.write_bytes(payload)
            log.info("seeded bootstrap skill file %s", dst_file)
        except OSError as exc:
            log.warning("could not seed %s: %s", dst_file, exc)
            updated.pop(key, None)

    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"shipped": updated}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("could not write seed state %s: %s", state_path, exc)


# bd claims two things this deployment has already decided differently, and
# `bd prime` states both as rules. Left alone the agent gets contradictory
# orders, so we override them explicitly. bd's own text defers to "explicit
# user or orchestrator instructions", which is what this is.
_BEADS_OVERRIDES = """\
--- Overrides to the beads instructions above ---

These win wherever they conflict with the beads block.

1. Memory stays in the knowledge base. Ignore the instruction to use
   `bd remember` / `bd memories` for persistent knowledge. This deployment
   keeps accumulated memory in the KB at memory/CLAUDE.md, where it is
   Postgres-backed, versioned, and readable by the human in the /kb browser.
   The bead graph lives on a volume with no replication, so memory placed
   there would be both less durable and invisible. See
   docs/decisions/0004-memory-lives-in-the-kb.md.

2. Never run git commands. Ignore the session-close steps about `git status`,
   commits, pushes, or Dolt remote sync. Git here is savepoint infrastructure
   owned by the application, which checkpoints every turn automatically. There
   is no remote to sync to.

3. Use beads for durable work, not for this turn's checklist. File a bead when
   you notice work you are not doing right now - that is the point of it. Do
   not file one for each step of the task in front of you.
"""


def _system_prompt_append(bd_context: str = "") -> str:
    guide = _read_guide()
    memory = _read_memory()
    parts = [
        f"You have a knowledge base mounted at {config.kb_mount}.",
        f"Your writeable knowledge-base workspace is `{kb.workspace_root()}`. "
        "Always use full absolute paths when creating or editing KB files — "
        "your working directory is scratch space and is not part of the knowledge base.",
        "The KB is a TigerFS filesystem backed by PostgreSQL: ordinary file tools "
        "work on it, every write is versioned, and the control directories "
        f"{config.kb_mount}/.history/, .log/, .savepoint/ and .undo/ let you "
        "inspect and roll back changes.",
        "Note that some control paths are path-accessible but deliberately "
        "hidden from `ls`, so do not conclude they are absent just because a "
        "directory listing does not show them.",
    ]
    if bd_context:
        parts.append(bd_context)
        parts.append(_BEADS_OVERRIDES)
    if guide:
        parts.append("--- Workspace guide ---\n" + guide)
    if memory:
        parts.append("--- Accumulated memory ---\n" + memory)
    return "\n\n".join(parts)


def _options(
    user_slug: str, resume: str | None, bd_context: str = ""
) -> ClaudeAgentOptions:
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
            "append": _system_prompt_append(bd_context),
        },
        # Headless: nobody is present to answer a permission prompt.
        permission_mode="acceptEdits",
        # acceptEdits covers file writes but NOT Bash, so without this the
        # agent can write to the wiki and never once run `bd` - it asks for
        # approval nobody is there to give, and silently files nothing.
        # Scoped to bd alone rather than opening up Bash generally.
        allowed_tools=["Bash(bd:*)"],
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


async def _image_prompt(text: str, images: list[dict]):
    """Async generator yielding a single user message with image content blocks."""
    content: list[dict] = []
    for img in images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img["media_type"],
                "data": img["data"],
            },
        })
    if text:
        content.append({"type": "text", "text": text})
    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
        "session_id": None,
    }


async def run_turn(
    turn: Turn,
    prompt: str,
    user_slug: str,
    resume: str | None = None,
    images: list[dict] | None = None,
) -> None:
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

    bd_context = ""
    if await kb.ensure_beads(user_slug):
        bd_context = await kb.bd_prime(user_slug)

    prompt_arg: str | object = prompt
    if images:
        prompt_arg = _image_prompt(prompt, images)

    try:
        async for message in query(
            prompt=prompt_arg, options=_options(user_slug, resume, bd_context)
        ):
            for kind, data in _render(message):
                turn.append(kind, data)
            session_id = _extract_session_id(message)
            if session_id and not turn.session_id:
                turn.session_id = session_id
                turn.append("session", session_id)
        await kb.export_backlog(user_slug)
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
