#!/usr/bin/env python3
"""Enforce the knowledge base's load-bearing rules for Claude Code on a laptop.

`app/guards.py` protects the deployed agent. It cannot protect a Claude Code
session on a laptop, because those guards are Python callables handed to
`ClaudeAgentOptions` inside the app's own process - so mounting the wiki with
`scripts/mount-kb.sh` and editing it from here leaves every one of them behind.
That is the gap this file closes. See docs/decisions/0019.

It is a second enforcement path, not a second implementation. Claude Code's
hook protocol turns out to be the same wire protocol the SDK uses - the deny
and block payloads below are the exact dicts `app/guards.py` already returns -
so the hazard list and the deferral detector are imported from that module
rather than restated. A second copy would drift, and the drift would be silent
in the worst direction: a command the deployed agent is refused and a laptop
session is allowed.

Three rules, two inherited and one new.

**Inherited: no shell append into the mount.** The mount has no
read-modify-write, so `>>` zeroes everything already in the file. This already
destroyed a user's memory/CLAUDE.md once (ADR 0007). The laptop needs one
addition: `guards._mentions_kb` is a substring test against an absolute
`/mnt/kb`, and a laptop session types `mnt/kb/memory/...` relatively all day,
which that test misses entirely. Hence `mount_markers`.

**Inherited: no deferring work without filing it.** Same detector, different
answer - there are two ledgers reachable from here, and which one a bead
belongs in depends on whether the work is about the wiki or about the app.

**New: a write must actually be able to land.** The container's mount is always
live and always writable, so the deployed agent never needed this. A laptop has
two ways to write into something that looks exactly like the wiki and is not
it, and both report success:

  - the mountpoint is not mounted, so `mnt/kb/memory/x.md` is an ordinary local
    file in a gitignored directory; or
  - the mount is read-only, where macOS's NFS client caches the write, reports
    success, and nothing ever reaches Postgres (`prod-ops` rule 3, which until
    now had no enforcement anywhere).

Both are denied. On a live writable mount the write is allowed, and a savepoint
is opened first if this session has not opened one - taking the action rather
than nagging about it is what restores revertability.

Every handler fails open: on any internal error this prints `{}` and exits 0. A
guard that crashed would take down the session it was written to protect, which
is ADR 0007's third rule and applies just as much here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# The two mountpoints scripts/mount-kb.sh knows how to create, each paired with
# the work dir holding its savepoint repo and its state file. Kept in the same
# order the script creates them, and deliberately a fixed list: a mountpoint
# this file has never heard of is one no savepoint covers either.
KNOWN_MOUNTS: tuple[tuple[str, str], ...] = (
    ("mnt/kb", "work"),
    ("mnt/kb-dev", "work-dev"),
)

MOUNT_STATE_NAME = ".mount-state.json"


def repo_root() -> Path:
    """The project directory, from Claude Code's own env var where available.

    `CLAUDE_PROJECT_DIR` is what the hook command is invoked with; the fallback
    matters for tests and for running this by hand.
    """
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


# --- what scripts/mount-kb.sh recorded --------------------------------------


def read_state(work_dir: Path) -> dict[str, Any] | None:
    """Parse one `.mount-state.json`, or None if it is absent or unreadable.

    Unreadable is treated as absent on purpose. A half-written state file is
    the one case where believing it would be worse than ignoring it.
    """
    try:
        raw = (work_dir / MOUNT_STATE_NAME).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        state = json.loads(raw)
    except ValueError:
        return None
    return state if isinstance(state, dict) else None


def mount_is_live(mountpoint: Path) -> bool:
    """True if something is actually mounted here.

    The state file says what was mounted, not what still is: a crashed tigerfs,
    a plain `umount`, or a reboot all leave the file behind pointing at an empty
    directory. `iterdir` is the cheap version of `mount-kb.sh`'s readdir probe -
    an unmounted mountpoint in this repo is always empty, because `mnt/` is
    gitignored and holds nothing but mountpoints.
    """
    try:
        return any(mountpoint.iterdir())
    except OSError:
        return False


def live_states(repo: Path) -> list[dict[str, Any]]:
    """Every recorded mount that is still actually mounted."""
    found = []
    for mount_rel, work_rel in KNOWN_MOUNTS:
        state = read_state(repo / work_rel)
        if state and mount_is_live(repo / mount_rel):
            found.append(state)
    return found


# --- rule one: no shell append into the mount -------------------------------


def mount_markers(repo: Path) -> list[str]:
    """Every string that means "this command is talking about the wiki".

    Both the absolute and the repo-relative spelling of each known mountpoint,
    unconditionally - a `>>` into `mnt/kb/memory/x.md` is wrong whether or not
    anything is mounted there, and denying it while the mount is down is how
    the agent learns the rule before it can cost anything.

    `$KB_MOUNT` is included because a command can name the mount through the
    variable without ever spelling the path.
    """
    markers = ["$KB_MOUNT", "${KB_MOUNT}"]
    for mount_rel, _ in KNOWN_MOUNTS:
        markers.append(mount_rel)
        markers.append(str(repo / mount_rel))
    return markers


def command_names_kb(command: str, markers: list[str]) -> bool:
    """True if the command references the knowledge base at all.

    Scratch is unrestricted on purpose, exactly as in `guards._mentions_kb`: a
    command that never names the mount is none of this hook's business.
    """
    return any(marker in command for marker in markers)


# --- rule two: a write must be able to land ---------------------------------


def target_of(tool_input: dict[str, Any]) -> str:
    """The path a file tool is about to write.

    `Write` and `Edit` call it `file_path`; `NotebookEdit` calls it
    `notebook_path`. Reading only the first would leave a whole tool silently
    unguarded, which is the shape of defect this file exists to prevent.
    """
    for key in ("file_path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def resolve_target(file_path: str, cwd: str | None) -> Path | None:
    """Absolute path of a tool's target, or None if there isn't one."""
    if not file_path:
        return None
    target = Path(file_path)
    if not target.is_absolute():
        target = Path(cwd or Path.cwd()) / target
    # No `resolve()`: it follows symlinks, and on a dead mount that can block.
    # Lexical normalisation is all the containment test below needs.
    return Path(os.path.normpath(str(target)))


def mount_for(target: Path, repo: Path) -> tuple[Path, Path] | None:
    """The (mountpoint, work_dir) this path belongs to, or None if outside."""
    for mount_rel, work_rel in KNOWN_MOUNTS:
        mountpoint = Path(os.path.normpath(str(repo / mount_rel)))
        if target == mountpoint or mountpoint in target.parents:
            return mountpoint, repo / work_rel
    return None


def write_verdict(target: Path, repo: Path) -> str | None:
    """Why this write cannot be trusted to land, or None if it can.

    Pure apart from reading the state file, so the three-way decision is
    testable without mounting anything.
    """
    found = mount_for(target, repo)
    if found is None:
        return None  # not the wiki; not this hook's business
    mountpoint, work_dir = found

    if not mount_is_live(mountpoint):
        return (
            f"Refused: {mountpoint} is not mounted.\n\n"
            "This write would not reach the wiki. It would create an ordinary "
            "local file inside a gitignored directory that looks exactly like "
            "the knowledge base and is not it - no database row, no savepoint, "
            "and invisible to everyone else.\n\n"
            "Mount it first:\n\n"
            "    bash scripts/mount-kb.sh --dev      # throwaway local Postgres\n"
            "    bash scripts/mount-kb.sh --prod --writable   # the live wiki\n\n"
            "If you meant to write a scratch file, write it somewhere outside "
            f"{mountpoint}."
        )

    state = read_state(work_dir)
    if state is None:
        return (
            f"Refused: {mountpoint} has something mounted, but "
            f"{work_dir / MOUNT_STATE_NAME} is missing, so this hook cannot "
            "tell which database is behind it or whether it is writable.\n\n"
            "Remount through the script, which records both:\n\n"
            "    bash scripts/mount-kb.sh --kill\n"
            "    bash scripts/mount-kb.sh --prod   # --writable only if you mean it"
        )

    if state.get("access") != "writable":
        return (
            f"Refused: {mountpoint} is mounted READ-ONLY "
            f"(database {state.get('db_host', 'unknown')}, "
            f"{state.get('db_kind', 'unknown')}).\n\n"
            "This is the trap that makes a read-only mount dangerous rather "
            "than merely limited: the NFS client accepts the write into its "
            "cache and reports SUCCESS, and nothing reaches Postgres. Reading "
            "the file back afterwards shows your new content, from the cache. "
            "The wiki is unchanged.\n\n"
            "So do not retry this with a different tool, and do not treat a "
            "successful `ls` or `cat` as proof. Either you meant to read - in "
            "which case stop here - or remount deliberately:\n\n"
            "    bash scripts/mount-kb.sh --kill\n"
            "    bash scripts/mount-kb.sh --prod --writable\n\n"
            "That second command asks you to type the database host, because "
            "it is the one combination that can destroy production from a "
            "laptop."
        )

    return None


def savepoint_name(session_id: str) -> str:
    """One savepoint per Claude Code session, named so its origin is obvious.

    `kb log` and the git history are read by people looking for what changed;
    "laptop-" is the word that tells them this was not a deployed turn.
    """
    return f"laptop-{session_id or 'unknown'}"


def ensure_savepoint(state: dict[str, Any], session_id: str) -> str | None:
    """Open this session's savepoint if it has none. Returns a warning, or None.

    Reuses `kb.create_savepoint` rather than reissuing its two git commands, so
    there is exactly one definition of what a savepoint is. That function reads
    `config.work_dir` and `config.kb_mount`, which are frozen at import time -
    hence the deferred import below, after the environment has been pointed at
    the mount this write is actually for.
    """
    git_dir = state.get("git_dir")
    workspace = state.get("workspace")
    if not git_dir or not workspace:
        return "savepoint skipped: the mount state file names no git_dir/workspace"

    name = savepoint_name(session_id)
    try:
        existing = subprocess.run(  # noqa: S603
            ["git", f"--git-dir={git_dir}", "log", "--format=%s"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if f"savepoint:{name}" in existing.stdout.splitlines():
            return None  # already opened for this session

        os.environ["WORK_DIR"] = state.get("work_dir") or str(Path(git_dir).parent)
        os.environ["KB_MOUNT"] = state.get("mountpoint") or ""
        sys.path.insert(0, str(repo_root()))
        import asyncio  # noqa: PLC0415

        from app import kb  # noqa: PLC0415

        asyncio.run(kb.create_savepoint(name))
    except Exception as exc:  # noqa: BLE001 - a failed savepoint must not block the write
        return f"savepoint failed ({exc!r}); this write will not be revertable"
    return None


# --- rule three: no deferring work without filing it ------------------------

FILE_IT = (
    'You wrote "{phrase}" but ran no `bd` command, and this session is working '
    "directly on the knowledge base - so nothing else is recording it. The "
    "deployed agent's Stop guard covers its own turns, not this one, and there "
    "is no signal capture and no conversation event log on this path either. "
    "Said in chat, this work is gone when the session ends.\n\n"
    "File it against whichever ledger owns it.\n\n"
    "About the WIKI's content - a page to write, a missing GUIDE.md, a "
    "restructure - goes to prod's volume ledger, where the household agent "
    "that could act on it will actually see it:\n\n"
    '    scripts/fly.sh --write bd create "Short, specific title" \\\n'
    '      --description="What needs doing, where, and why - written for '
    'someone with no memory of this session." \\\n'
    "      --type=task --priority=2\n\n"
    "About the APP or this image - a defect, a missing capability, anything "
    "needing a deploy - goes to this repo's ledger, which is committed:\n\n"
    '    bd create "Short, specific title" \\\n'
    '      --description="..." --type=task --priority=2 --labels image\n'
    "    bd update <id> --status deferred\n\n"
    "Two commands for that one: `bd create --status` is not a flag on the "
    "pinned bd, and passing it creates nothing at all.\n\n"
    "Then finish your reply as normal. If there is genuinely no durable work "
    "here - you were describing what you just did, or the user said not to "
    "track it - say so in one line and stop. You will not be asked twice."
)


def transcript_rows(path: str) -> list[dict[str, Any]]:
    """Parse a Claude Code transcript, dropping subagent rows.

    Same JSONL shape `guards._transcript_rows` reads, with one addition: a
    Claude Code transcript interleaves subagent rows into the same file, marked
    `isSidechain`. Without dropping them a subagent's deferral language would
    block the parent session, which cannot file a bead on its behalf.
    """
    rows: list[dict[str, Any]] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a partially flushed final line is normal
        if isinstance(row, dict) and not row.get("isSidechain"):
            rows.append(row)
    return rows


# --- hook plumbing ----------------------------------------------------------


def deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def handle_pre_bash(data: dict[str, Any], repo: Path) -> dict[str, Any]:
    from app import guards  # noqa: PLC0415 - see module docstring on reuse

    command = (data.get("tool_input") or {}).get("command", "")
    if not command_names_kb(command, mount_markers(repo)):
        return {}
    reason = guards.kb_hazard(command)
    if not reason:
        return {}
    return deny(f"Refused: {reason}.\n\n{guards.GUIDANCE}")


def handle_pre_write(data: dict[str, Any], repo: Path) -> dict[str, Any]:
    target = resolve_target(target_of(data.get("tool_input") or {}), data.get("cwd"))
    if target is None:
        return {}

    refusal = write_verdict(target, repo)
    if refusal:
        return deny(refusal)

    found = mount_for(target, repo)
    if found is None:
        return {}
    state = read_state(found[1])
    if state is None:
        return {}

    warning = ensure_savepoint(state, data.get("session_id", ""))
    # Allowed either way. A savepoint that could not be opened is worth saying
    # out loud, because the alternative is losing revertability silently - but
    # it is not worth blocking a write the user asked for.
    return {"systemMessage": f"kb-guard: {warning}"} if warning else {}


def handle_stop(data: dict[str, Any], repo: Path) -> dict[str, Any]:
    # Scoped to sessions that are actually working on the wiki. This repo is
    # also where the app itself is developed, and blocking an ordinary `app/`
    # refactor for saying "follow-up" would be the false positive ADR 0007
    # warns costs as much as a miss.
    if not live_states(repo):
        return {}
    if data.get("stop_hook_active"):
        return {}  # one nudge per turn, or a guard re-fires on its own re-prompt
    path = data.get("transcript_path")
    if not path:
        return {}

    from app import guards  # noqa: PLC0415 - see module docstring on reuse

    phrase = guards.unfiled_deferral(transcript_rows(path))
    if not phrase:
        return {}
    return {"decision": "block", "reason": FILE_IT.format(phrase=phrase)}


HANDLERS = {
    "pre-bash": handle_pre_bash,
    "pre-write": handle_pre_write,
    "stop": handle_stop,
}


def main() -> int:
    try:
        event = sys.argv[1] if len(sys.argv) > 1 else ""
        handler = HANDLERS.get(event)
        if handler is None:
            sys.stdout.write("{}")
            return 0
        repo = repo_root()
        sys.path.insert(0, str(repo))
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            sys.stdout.write("{}")
            return 0
        sys.stdout.write(json.dumps(handler(data, repo)))
    except Exception as exc:  # noqa: BLE001 - fail open; see the module docstring
        sys.stderr.write(f"kb_guard_hook: {exc!r}\n")
        sys.stdout.write("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
