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

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import (
    AgentDefinition,
    AssistantMessage,
    HookEvent,
    HookMatcher,
    ServerToolUseBlock,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from . import evolve, guards, interact, kb, signals
from .config import config
from .turns import Turn, TurnInProgressError, TurnState, registry, spawn

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


# What one seed-state entry can be on disk. The dict is the current shape;
# a bare string is legacy state from before we tracked stored-vs-source, and
# is preserved verbatim rather than upgraded on a guess. See seed_bootstrap.
SeedEntry = dict[str, str] | str


def _read_seed_state(path: Path) -> dict[str, SeedEntry]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    shipped = raw.get("shipped", {}) if isinstance(raw, dict) else {}
    return shipped if isinstance(shipped, dict) else {}


def _write_and_read_back(path: Path, payload: bytes) -> bytes:
    """Write a file into the KB and return it as the KB now stores it.

    A seam, not a convenience. The knowledge base does NOT round-trip bytes -
    see seed_bootstrap - so every caller that wants to remember what it wrote
    must remember what came back, and tests need somewhere to simulate that.
    """
    path.write_bytes(payload)
    return path.read_bytes()


def seed_bootstrap() -> None:
    """Copy bootstrap skill files into the KB workspace.

    These skills live in the KB so the human can improve them, which means we
    cannot simply overwrite on upgrade. But never overwriting is worse: a
    shipped fix would silently never reach any existing deployment, and the
    seeder would look like it worked.

    So we track what we last shipped and only replace files that still match
    it. A file that differs has been edited and is left alone with a warning.
    Deployments predating the state file have no record, so we cannot tell and
    do not guess.

    **Two hashes per file, and the reason is subtle.** The KB is a TigerFS
    *markdown* workspace: it parses documents and re-serialises them, so what
    reads back is not what was written. A folded YAML `description: >` block
    comes back as one long line, frontmatter keys get reordered, and the file
    is a different length. Comparing a hash of the source bytes against the
    stored file therefore reports "locally edited" for every file, forever -
    silently disabling the upgrade path this tracking exists to provide, which
    is exactly the failure it was written to prevent. It went unnoticed because
    the tests wrote to tmp_path, which round-trips faithfully and so was a
    *less* accurate double than the real store.

    So we record both: `source` (the bytes we shipped, to notice that a newer
    version exists) and `stored` (the file as it read back, to notice a human
    edit). Only `stored` is ever compared against what is on disk.
    """
    if not kb.is_mounted():
        return
    skills_src = BOOTSTRAP_DIR / "skills"
    if not skills_src.is_dir():
        return

    skills_dst = kb.workspace_root() / "skills"
    state_path = skills_dst / SEED_STATE_FILE
    shipped = _read_seed_state(state_path)
    updated: dict[str, SeedEntry] = {}

    for src_file in sorted(skills_src.rglob("*")):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(skills_src)
        key = rel.as_posix()
        dst_file = skills_dst / rel
        payload = src_file.read_bytes()
        source_hash = _digest(payload)
        entry = shipped.get(key)

        try:
            if dst_file.exists():
                current_hash = _digest(dst_file.read_bytes())

                if entry is None:
                    log.warning(
                        "skill %s predates seed tracking; leaving it alone. "
                        "Delete it to take the shipped version.",
                        dst_file,
                    )
                    # Stays untracked. Recording a hash now would make the next
                    # run mistake it for something we shipped.
                    continue

                if isinstance(entry, str):
                    # Legacy state: a single hash, of the source bytes. Because
                    # the store rewrites what it is given, that hash never
                    # matched the file and everything looked edited. We can
                    # repair the bookkeeping only when we are shipping that
                    # same version - then whatever is on disk IS what we last
                    # wrote, so its current form is the stored form. With a
                    # newer version in hand we cannot tell a stale copy from an
                    # edit, and must not guess.
                    if entry == source_hash:
                        updated[key] = {"source": source_hash, "stored": current_hash}
                    else:
                        log.warning(
                            "skill %s has legacy seed state and differs from "
                            "the shipped version; leaving it alone. Delete it "
                            "to take the new one.",
                            dst_file,
                        )
                        updated[key] = entry
                    continue

                if current_hash != entry.get("stored"):
                    log.warning(
                        "skill %s has local edits; not overwriting with the "
                        "shipped version. Previous content stays in TigerFS "
                        "history if you want to compare.",
                        dst_file,
                    )
                    # Keep the ORIGINAL record, not the edited file's hash.
                    # Recording the edit would make it match next run and
                    # silently clobber the human's work one seed later.
                    updated[key] = entry
                    continue

                if entry.get("source") == source_hash:
                    updated[key] = entry  # already current, nothing to ship
                    continue

            dst_file.parent.mkdir(parents=True, exist_ok=True)
            stored = _write_and_read_back(dst_file, payload)
            updated[key] = {"source": source_hash, "stored": _digest(stored)}
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
"""

# Stated separately, last, and as an obligation rather than a clarification.
# As a third bullet inside the overrides above it was agreed with and ignored:
# the agent would write the page, say "noted as follow-up for a later session",
# and file nothing. The Stop hook in app/guards.py is the backstop for when
# this still does not take. See bead kb-3cl.
_LEDGER_RULE = """\
--- Finishing a turn ---

If at any point in a turn you notice, mention, or are told about work you are
not doing right now, you must file it as a bead BEFORE you finish replying:

    bd create --title="..." --description="..." --type=task --priority=2

This is not optional and it is not satisfied by mentioning it in your reply.
Chat scrolls away and the next session starts with no memory of this one, so
work that is only described is work that is lost. Phrases like "a follow-up for
a later session" or "worth revisiting" in your own reply are the signal that a
bead was owed - if you write one, file the bead.

Write the description for someone with no memory of this conversation: what is
wrong, where, and how you would fix it.

This applies to durable work only. Do not file beads for the steps of the task
you are actively completing.
"""


_ASKING = """\
--- Reaching the human ---

You can ask the person you are talking to a question mid-turn with the
`mcp__ask__ask_user` tool, and you can be granted a tool you were not
pre-approved for by simply calling it - they get an Allow/Deny prompt.

Both cost their attention, which is the scarcest thing here, so:

* Search the wiki first. A question it already answers is a question you should
  not have asked.
* Ask when the answer changes what you write - which of two people a task
  belongs to, which of two readings of an ambiguous note is right. Do not ask
  for permission to proceed, or for a decision you can defensibly make yourself.
* One question, with suggested options where you have them. If nobody answers,
  you will be told so and you should carry on and say that you did.

Do NOT use `AskUserQuestion`. It cannot work in this deployment - it returns
empty answers without anyone seeing it - and it is blocked for that reason.

--- Delegating ---

`Task` runs a subagent with its own context, which is how you read widely
without spending yours. `kb-query` answers a question from the wiki read-only;
`kb-lint` audits it and files beads. A general-purpose subagent is available for
anything else. Dispatch a few at a time rather than one wide fan-out - a large
parallel dispatch from one session hits API rate limits.

A subagent cannot ask the human and its work is summarised back to you rather
than shown, so keep anything that needs judgement or leaves a visible edit in
this turn where you and the person can both see it.
"""


def _named_agents() -> dict[str, AgentDefinition]:
    """The subagents this deployment declares, alongside the generic one.

    Each prompt POINTS AT the skill in the knowledge base rather than restating
    it. Those skills are seeded from `bootstrap/` and then edited by the human
    (and, within its remit, by reflection), so a copy of their contents in this
    file would be a second source of truth that silently drifts from the one the
    human is actually maintaining.

    `ingest` and `reflect` are deliberately absent even though skills exist for
    both. `ingest` is written to check in with the person before it writes, and
    a subagent cannot - its output is summarised, not shown. `reflect` is bounded
    by `evolve.write_guard_for`, which is installed only in
    `_reflection_options`, plus four things an AgentDefinition cannot express: a
    cold start with no `resume`, the `reflect-` savepoint namespace, the
    process-wide reflection lock, and the evolution log. Both are reachable
    headlessly through app/mcp_server.py, which runs them as real turns.
    """
    workspace = kb.workspace_root()
    return {
        "kb-query": AgentDefinition(
            description=(
                "Answer a question from the knowledge base. Read-only: it never "
                "writes a page and never files a bead. Use it for lookups, and "
                "to read across many pages without spending your own context."
            ),
            prompt=(
                f"You are a read-only researcher for the wiki at {workspace}.\n\n"
                "Find what the wiki actually says about the question you were "
                "given. Read the relevant pages, follow links between them, and "
                "report what you found with the paths of the pages you used.\n\n"
                "Two things matter more than being helpful. Say plainly when the "
                "wiki does not answer the question, rather than filling the gap "
                "from your own knowledge - the caller cannot tell the two apart "
                "unless you separate them. And do not propose edits: you cannot "
                "make them, and the turn that called you can."
            ),
            tools=["Read", "Glob", "Grep"],
            model="inherit",
        ),
        "kb-lint": AgentDefinition(
            description=(
                "Audit the knowledge base for staleness, gaps and broken links, "
                "and file what it finds as beads. Read-mostly."
            ),
            prompt=(
                f"Read {workspace}/skills/lint/SKILL.md and follow it for the "
                "scope you were given.\n\n"
                "That skill is the specification; this prompt does not restate "
                "it. Its own instruction stands: file findings as beads rather "
                "than reporting them back as prose, and dedupe against the "
                "ledger before filing. Report a short summary of what you filed."
            ),
            tools=["Read", "Glob", "Grep", "Bash"],
            model="inherit",
        ),
    }


def _system_prompt_append(bd_context: str = "") -> str:
    guide = _read_guide()
    memory = _read_memory()
    parts = [
        f"You have a knowledge base mounted at {config.kb_mount}.",
        f"Your writeable knowledge-base workspace is `{kb.workspace_root()}`. "
        "Always use full absolute paths when creating or editing KB files — "
        "your working directory is scratch space and is not part of the "
        "knowledge base.",
        "The KB is a TigerFS filesystem backed by PostgreSQL: ordinary file tools "
        "work on it, every write is versioned, and the control directories "
        f"{config.kb_mount}/.history/, .log/, .savepoint/ and .undo/ let you "
        "inspect and roll back changes.",
        "Note that some control paths are path-accessible but deliberately "
        "hidden from `ls`, so do not conclude they are absent just because a "
        "directory listing does not show them.",
        # Kept deliberately short. Appends are refused structurally by the
        # PreToolUse hook in app/guards.py, whose denial message explains the
        # safe pattern at the moment it is needed. An earlier version spelled
        # all of that out here instead, and the extra paragraphs measurably
        # crowded out the beads instructions below - the agent stopped filing
        # discovered work and went back to mentioning it in chat.
        "Write knowledge-base files whole, and end them with a newline. The "
        "store adds a missing trailing newline, so the Write tool may report "
        "`Write verification failed: ... N bytes on disk, expected N-1` - that "
        "is a FALSE ALARM and your write succeeded. If any file tool reports a "
        "failure, re-read the file: if the content is right, you are done. "
        "Never fall back to shell redirection to work around it.",
    ]
    parts.append(_ASKING)
    if bd_context:
        parts.append(bd_context)
        parts.append(_BEADS_OVERRIDES)
        parts.append(_LEDGER_RULE)
    if guide:
        parts.append("--- Workspace guide ---\n" + guide)
    if memory:
        parts.append("--- Accumulated memory ---\n" + memory)
    return "\n\n".join(parts)


def _observer_hooks(turn: Turn | None) -> dict[HookEvent, list[HookMatcher]]:
    """Hooks that only watch: tool outcomes and subagent lifecycle.

    Separate from the two enforcing hooks above so it stays obvious which hooks
    can refuse something and which cannot. These are the only route to a tool
    RESULT: results arrive on a UserMessage, which `_render` drops wholesale, so
    without them a failed Write and a successful one look identical in the UI.
    """
    if turn is None:
        return {}
    return {
        "PostToolUse": [HookMatcher(hooks=[interact.tool_result_for(turn)])],
        "PostToolUseFailure": [HookMatcher(hooks=[interact.tool_failure_for(turn)])],
        "SubagentStart": [HookMatcher(hooks=[interact.subagent_start_for(turn)])],
        "SubagentStop": [HookMatcher(hooks=[interact.subagent_stop_for(turn)])],
    }


def _options(
    user_slug: str, resume: str | None, bd_context: str = "", turn: Turn | None = None
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
        # Still acceptEdits, so writing the wiki never prompts - that is the
        # product. What changed is that a tool this does NOT cover used to be
        # refused with nobody asked, and now reaches `can_use_tool` below.
        #
        # Not everything outside `allowed_tools` gets there, though. The CLI
        # approves some read-only shell commands itself, ahead of the callback -
        # a live test asking for `echo` ran the command and raised no prompt at
        # all. Where that line falls is the CLI's business, so do not build
        # anything on a specific Bash command prompting.
        permission_mode="acceptEdits",
        # acceptEdits covers file writes but NOT Bash, so without this the
        # agent can write to the wiki and never once run `bd` - it asks for
        # approval nobody is there to give, and silently files nothing.
        # Scoped to bd alone rather than opening up Bash generally.
        #
        # An entry that allows a WHOLE tool auto-approves it before the
        # permission callback is consulted, which is exactly what the three
        # additions want: asking a question, tracking progress and spawning a
        # subagent must never themselves raise a prompt. `Bash(bd:*)` carries a
        # specifier, so non-bd shell commands still fall through to the callback.
        allowed_tools=[
            "Bash(bd:*)",
            "mcp__ask__ask_user",
            "TodoWrite",
            "Task",
        ],
        # Present in the CLI, unusable here: with no TTY it resolves instantly
        # with EMPTY answers and the agent believes it consulted someone. See
        # anthropics/claude-code#50728 and app/interact.py.
        disallowed_tools=["AskUserQuestion"],
        # Two named subagents plus the built-in general-purpose one.
        agents=_named_agents(),
        # The question tool. Built per turn because it closes over the turn it
        # puts its question on.
        mcp_servers={"ask": interact.ask_server_for(turn)} if turn else {},
        # cwd is the agent's own WRITABLE scratch directory, so without this it
        # could drop a .mcp.json there and grant itself servers. setting_sources
        # governs settings files, not this one.
        strict_mcp_config=True,
        # Only on a turn a human is watching. Omitted otherwise, which restores
        # the previous behaviour exactly: unapproved tools are refused and
        # nobody is asked. Requires the streaming prompt built below - the SDK
        # raises ValueError if the prompt is a plain string.
        can_use_tool=(
            interact.can_use_tool_for(turn) if turn and turn.interactive else None
        ),
        # Refuse shell commands that would corrupt a KB file. Passed in
        # process, so setting_sources=[] does not suppress it the way it
        # suppresses .claude/settings.json hooks - and the agent cannot author
        # this one, because it is not in its writable cwd. See app/guards.py.
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="Bash", hooks=[guards.kb_write_guard_for(turn)])
            ],
            # Catches the turn that names future work and then drops it. The
            # instruction below was not enough on its own; see app/guards.py.
            "Stop": [HookMatcher(hooks=[guards.stop_guard])],
            **_observer_hooks(turn),
        },
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


def _reflection_options(
    user_slug: str, turn: Turn, bd_context: str
) -> ClaudeAgentOptions:
    """Options for a reflection turn: a deliberately narrower surface.

    Differences from an ordinary turn, each of them a safety property rather
    than a preference:

    * No `resume`. Reflection starts cold every time, so it judges the evidence
      in the ledger rather than a conversation it half-remembers.
    * A small `max_turns`. A reflection that starts wandering hits the wall
      early, and the max_turns signal records that it did.
    * The evolution guard replaces nothing - it is added *alongside* the KB
      write guard, so a reflection turn is strictly more constrained than a
      normal one, never less.
    * No workspace guide and no accumulated memory in the prompt: neither is
      evidence about a skill, and both are things reflection must not treat as
      editable.
    * None of the interaction surface from app/interact.py: no question tool, no
      permission callback, no subagents. Reflection is triggered by a signal,
      not by a person - `maybe_reflect` gives it the synthetic owner
      `reflection@{slug}` - so there is nobody to answer, and a prompt could only
      spend its timeout. It keeps the observer hooks, which ask nothing.
    """
    scratch = kb.scratch_dir_for(user_slug)
    config_dir = Path(config.work_dir) / f".claude-{user_slug}"
    config_dir.mkdir(parents=True, exist_ok=True)

    return ClaudeAgentOptions(
        model=config.agent_model,
        cwd=str(scratch),
        max_turns=evolve.MAX_REFLECTION_TURNS,
        setting_sources=[],
        add_dirs=[config.kb_mount, str(SKILLS_DIR)],
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": "\n\n".join(
                p
                for p in (
                    f"You have a knowledge base mounted at {config.kb_mount}, "
                    f"with your skills under {kb.workspace_root()}/skills/.",
                    "Write files whole and end them with a newline. The store "
                    "adds a missing trailing newline, so a `Write verification "
                    "failed ... expected N-1` message is a FALSE ALARM and your "
                    "write succeeded.",
                    bd_context,
                    _BEADS_OVERRIDES if bd_context else "",
                )
                if p
            ),
        },
        permission_mode="acceptEdits",
        allowed_tools=["Bash(bd:*)"],
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="Bash", hooks=[guards.kb_write_guard_for(turn)]),
                HookMatcher(hooks=[evolve.write_guard_for(turn)]),
            ],
            # Deliberately no Stop guard: reflection has no "defer the work"
            # failure mode, and filing a bead is one of its correct endings.
            **_observer_hooks(turn),
        },
        include_partial_messages=True,
        env={
            **os.environ,
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CONFIG_DIR": str(config_dir),
        },
    )


async def run_reflection(turn: Turn, user_slug: str, trigger: str) -> None:
    """Let the agent improve one of its own skills, within the evolve.py remit.

    Savepointed like any other turn, so the whole thing is one Revert away -
    which is the single reason self-modification is defensible here at all.
    """
    savepoint = f"reflect-{turn.id}"
    if await kb.create_savepoint(savepoint):
        turn.savepoint = savepoint
    turn.reflection = True
    turn.prompt = f"[reflection: {trigger}]"
    turn.append("status", "started")

    bd_context = ""
    if await kb.ensure_beads(user_slug):
        bd_context = await kb.bd_prime(user_slug)

    try:
        async for message in query(
            prompt=evolve.reflection_prompt(await signals.evidence_summary()),
            options=_reflection_options(user_slug, turn, bd_context),
        ):
            for kind, data in _render(message):
                turn.append(kind, data)
            signals.observe_message(turn, message)
        await evolve.log_changes(turn.evolved, savepoint, trigger)
        await evolve.request_consolidation(user_slug, evolve.merge(turn.evolved))
        await kb.export_backlog(user_slug)
        turn.finish(TurnState.DONE)
        log.info("reflection %s finished with %d change(s)", turn.id, len(turn.evolved))
    except Exception as exc:
        log.exception("reflection turn %s failed", turn.id)
        turn.append("error", str(exc))
        turn.finish(TurnState.ERROR, error=str(exc))
    finally:
        await signals.record_turn(turn, user_slug)


async def _stream_prompt(text: str, images: list[dict] | None = None):
    """Yield the turn's single user message, in the SDK's streaming-input form.

    Every turn goes through this, images or not. A plain string prompt is the
    simpler call, but `can_use_tool` is only honoured in streaming mode - the SDK
    raises ValueError on a string - and a permission prompt nobody can answer is
    the whole thing this replaced. Still unidirectional: one message, then the
    generator closes.
    """
    content: list[dict] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img["media_type"],
                "data": img["data"],
            },
        }
        for img in images or []
    ]
    if text:
        content.append({"type": "text", "text": text})
    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
        "session_id": None,
    }


def _attachment_note(files: list[Path]) -> str:
    """Tell the agent where its attachments are, without inlining them.

    Paths rather than content: a document the agent has not decided to read
    should cost nothing, and `Read` handles text, CSV and PDF from disk. It
    cannot open .docx or .xlsx - saying so here is cheaper than the agent
    discovering it mid-turn and guessing at a workaround.

    These files are in scratch, which is writable and NOT the knowledge base.
    Copying one into the wiki verbatim is almost never right; the agent's job
    is to read it and write what it learned.
    """
    listed = "\n".join(f"- {path}" for path in files)
    return (
        "The user attached the following files. They are on ordinary local "
        "disk in your working directory, not in the knowledge base:\n"
        f"{listed}\n"
        "Read them with the Read tool. If a file is a format you cannot read "
        "(.docx and .xlsx are not readable here), say so plainly rather than "
        "guessing at its contents."
    )


async def run_turn(
    turn: Turn,
    prompt: str,
    user_slug: str,
    resume: str | None = None,
    images: list[dict] | None = None,
    files: list[Path] | None = None,
) -> None:
    """Run one agent turn to completion, streaming events into the turn buffer.

    Every turn is wrapped in a TigerFS savepoint named after the turn id. If
    the agent makes a mess of the knowledge base, the whole turn is one atomic
    undo away - and because TigerFS undo is itself reversible, the revert is
    safe to expose as a button in the UI. This is the single best reason to put
    a knowledge base on TigerFS rather than in a vector store.

    The body lives in `_run_turn` below, wrapped here so that every path out of
    it reaches a terminal state - including the savepoint and the beads
    priming. Those three awaits used to sit above the try, on the theory
    that kb.py logs-and-continues rather than raising. That theory only had to
    be wrong once: a turn that raises before `finish()` stays RUNNING forever,
    it is never evicted (only finished turns are), and now that Registry.begin
    admits one turn at a time it would wedge every later turn behind it. A
    savepoint that could not be created is a turn that should say so, not a
    turn that silently becomes the last one this process ever runs.
    """
    try:
        await _run_turn(turn, prompt, user_slug, resume, images, files)
    except Exception as exc:  # surface everything to the client
        # Guarded on `finished` because _run_turn has an inner handler that
        # already reported the agent loop's own failures, and the tail it runs
        # afterwards - recording signals, maybe_reflect - happens on a turn that
        # is deliberately already DONE. Re-finishing would rewrite a successful
        # turn as an error for something that happened after it succeeded.
        log.exception("turn %s failed outside the agent loop", turn.id)
        if not turn.finished:
            turn.append("error", str(exc))
            turn.finish(TurnState.ERROR, error=str(exc))
    finally:
        # begin() refuses while this turn is unfinished, so a turn that somehow
        # reached here still RUNNING must be closed out. Belt and braces: the
        # paths above already finish it, and this is what stops an unforeseen
        # fourth path from taking the whole instance down with it.
        if not turn.finished:
            log.error("turn %s ended without a terminal state", turn.id)
            turn.finish(TurnState.ERROR, error="turn ended unexpectedly")


async def _run_turn(
    turn: Turn,
    prompt: str,
    user_slug: str,
    resume: str | None,
    images: list[dict] | None,
    files: list[Path] | None,
) -> None:
    savepoint = f"turn-{turn.id}"
    if await kb.create_savepoint(savepoint):
        turn.savepoint = savepoint

    if files:
        # Prepended to the prompt itself rather than to the system prompt, so
        # the attachment is part of what a revert bead quotes. Reconstructing
        # "which file was this turn given?" from the volume afterwards is
        # exactly the archaeology the signal layer exists to avoid.
        prompt = (
            f"{_attachment_note(files)}\n\n{prompt}"
            if prompt
            else _attachment_note(files)
        )

    # Kept for the signal layer: a revert files a bead quoting the prompt, and
    # by then run_turn is long gone.
    turn.prompt = prompt
    turn.append("status", "started")

    bd_context = ""
    if await kb.ensure_beads(user_slug):
        bd_context = await kb.bd_prime(user_slug)

    try:
        async for message in query(
            prompt=_stream_prompt(prompt, images),
            options=_options(user_slug, resume, bd_context, turn),
        ):
            for kind, data in _render(message):
                turn.append(kind, data)
            signals.observe_message(turn, message)
            session_id = _extract_session_id(message)
            if session_id and not turn.session_id:
                turn.session_id = session_id
                turn.append("session", session_id)
        await kb.export_backlog(user_slug)
        turn.finish(TurnState.DONE)
    except Exception as exc:  # surface everything to the client
        log.exception("turn %s failed", turn.id)
        turn.append("error", str(exc))
        turn.finish(TurnState.ERROR, error=str(exc))
    finally:
        # After finish() on both paths: a turn that failed is precisely the one
        # worth recording, and the client is no longer waiting on us.
        filed = await signals.record_turn(turn, user_slug)

    if filed:
        await maybe_reflect(user_slug, trigger=f"signal {', '.join(filed)}")


# One reflection at a time, process-wide. Two concurrent reflections would race
# on the same skill file, and each would savepoint over the other's work.
_reflecting = asyncio.Lock()


async def maybe_reflect(user_slug: str, trigger: str) -> str | None:
    """Start a reflection turn if it is safe to, otherwise skip it quietly.

    Skipping is the common case and is not a failure: the signal that would
    have triggered this is in the ledger either way, and the next signal - or a
    manual POST /api/reflect - will pick it up. Never blocks the caller.
    """
    if _reflecting.locked():
        log.info("reflection already running; not starting another (%s)", trigger)
        return None

    # Non-interactive: a signal triggered this, not a person, so there is nobody
    # to answer a question. _reflection_options installs no question tool or
    # permission callback either; this keeps the two statements consistent.
    #
    # Caught rather than propagated: the 2GB suspend ceiling is real and a
    # second agent is not free, so the user's turn wins and reflection is never
    # urgent. The signal that would have triggered this is in the ledger either
    # way, and the next signal - or a manual POST /api/reflect - picks it up.
    try:
        turn = registry.begin(user_email=f"reflection@{user_slug}", interactive=False)
    except TurnInProgressError:
        log.info("a turn is running; skipping reflection (%s)", trigger)
        return None
    turn.reflection = True

    async def _run() -> None:
        async with _reflecting:
            await run_reflection(turn, user_slug, trigger)

    spawn(_run(), name=f"reflection-{turn.id}")
    return turn.id


def _extract_session_id(message: object) -> str | None:
    """Pull the session id out of an SDK message."""
    if isinstance(message, AssistantMessage) and message.session_id:
        return message.session_id
    if isinstance(message, SystemMessage) and message.subtype == "init":
        value = message.data.get("session_id")
        return str(value) if value else None
    # Fallback for forward-compatibility with future message shapes.
    value = getattr(message, "session_id", None)
    return str(value) if value else None


def _render(message: object) -> list[tuple[str, str]]:
    """Flatten an SDK message into (kind, data) pairs for the event stream.

    With include_partial_messages=True the SDK emits StreamEvent objects for
    each raw API event. We forward text deltas immediately so the UI streams
    tokens as they arrive. The subsequent AssistantMessage (sent once the full
    turn completes) is used only for tool events — text was already streamed.

    **Subagent output must not join the reply.** Anything a subagent says
    arrives here with `parent_tool_use_id` set, and the pre-Task version of this
    function forwarded every delta regardless - which would have spliced a
    subagent's tokens into the middle of a sentence the user was reading. Those
    go out as `agent_text` and the UI nests them.

    Structured kinds carry a JSON payload; `text`, `text_delta` and
    `thinking_delta` stay raw strings, which is what the older client expects.
    """
    if isinstance(message, StreamEvent):
        return _render_stream(message)

    if not isinstance(message, AssistantMessage):
        return []

    agent = message.parent_tool_use_id
    out: list[tuple[str, str]] = []
    for block in message.content:
        if isinstance(block, TextBlock) and block.text:
            # "text" is the non-streaming fallback: the client ignores it once it
            # has seen any text_delta. A subagent gets no such fallback, because
            # its deltas are the only place its text appears.
            if agent:
                out.append(
                    ("agent_text", interact.json_event(agent=agent, text=block.text))
                )
            else:
                out.append(("text", block.text))
        elif isinstance(block, ThinkingBlock) and block.thinking and not agent:
            out.append(("thinking", block.thinking))
        elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
            out.extend(_render_tool_use(block, agent))
    return out


def _render_stream(message: StreamEvent) -> list[tuple[str, str]]:
    """Forward the raw API stream events worth showing as they arrive.

    `content_block_start` for a tool_use block is the EARLIEST moment anything
    can be said about a tool call: the name and id are there, the arguments are
    not yet, and the tool has not run. Announcing it here is what stops a slow
    tool looking like a finished turn - the reported case was an agent that said
    "Reading the CSV now.", then read a large file with nothing on screen moving.

    The detail arrives later, on the AssistantMessage, which the CLI sends once
    the model's response completes and still before the tool executes. Both
    events carry the same `id`, so the client fills the line in rather than
    drawing a second one. Two events instead of one accumulator: it keeps this
    function pure, and keeps `describe_tool_input` in one language.
    """
    event = message.event
    agent = message.parent_tool_use_id

    if event.get("type") == "content_block_start":
        block = event.get("content_block") or {}
        if block.get("type") != "tool_use":
            return []
        return [
            (
                "tool_use",
                interact.json_event(
                    id=block.get("id") or "",
                    name=block.get("name") or "",
                    detail="",
                    agent=agent or "",
                ),
            )
        ]

    if event.get("type") != "content_block_delta":
        return []
    delta = event.get("delta", {})
    kind = delta.get("type")
    agent = message.parent_tool_use_id

    if kind == "text_delta" and delta.get("text"):
        if agent:
            return [
                ("agent_text", interact.json_event(agent=agent, text=delta["text"]))
            ]
        return [("text_delta", delta["text"])]
    if kind == "thinking_delta" and delta.get("thinking") and not agent:
        return [("thinking_delta", delta["thinking"])]
    return []


def _render_tool_use(
    block: ToolUseBlock | ServerToolUseBlock, agent: str | None
) -> list[tuple[str, str]]:
    """A tool call, plus the todo list when that is what the call was.

    TodoWrite is rendered from the call's own input rather than tracked
    separately: the list the agent just wrote IS the state, so there is nothing
    to keep in sync. It is progress display only - the bead ledger remains the
    only place work survives the turn, which the Stop guard still enforces.
    """
    tool_input = block.input or {}
    out = [
        (
            "tool_use",
            interact.json_event(
                id=block.id,
                name=block.name,
                detail=interact.describe_tool_input(block.name, tool_input),
                agent=agent or "",
            ),
        )
    ]
    if block.name == "TodoWrite" and isinstance(tool_input.get("todos"), list):
        out.append(("todo", interact.json_event(todos=tool_input["todos"])))
    return out
