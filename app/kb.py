"""Knowledge-base helpers: mount health, savepoints, undo.

TigerFS provides the filesystem (FUSE → Postgres). Savepoints/undo are
implemented with a git repo stored on the local Fly volume so they work
without TimescaleDB, which is unavailable on Neon and Cloud SQL.

Git repo:   $WORK_DIR/kb.git   (Fly persistent volume, fast local disk)
Work tree:  $KB_MOUNT/memory   (TigerFS workspace)

All git commands use --git-dir and --work-tree so no .git file or
directory appears inside the knowledge-base workspace.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .config import config

log = logging.getLogger(__name__)

WORKSPACE_NAME = "memory"

# .info is mount-level metadata present at the root of any live TigerFS mount.
MOUNT_PROBE = ".info"

GIT_DIR = str(Path(config.work_dir) / "kb.git")


def _git_args() -> list[str]:
    return ["git", f"--git-dir={GIT_DIR}", f"--work-tree={workspace_root()}"]


def mount_root() -> Path:
    return Path(config.kb_mount)


def workspace_root() -> Path:
    """Root of the primary knowledge-base workspace."""
    return mount_root() / WORKSPACE_NAME


def is_mounted() -> bool:
    """True if the KB mount looks live.

    A missing CLAUDE.md is silently ignored by the agent SDK, and a missing
    knowledge base just makes the agent quietly less useful with no error
    anywhere. So we assert the mount up front rather than discovering it later
    from confused answers. See docs/decisions/0004.
    """
    root = mount_root()
    try:
        if not root.is_dir():
            return False
        # .info is the mount-level metadata directory that TigerFS synthesises
        # at the root of every live mount. An empty dir = unmounted mountpoint.
        return (root / MOUNT_PROBE).exists()
    except OSError:
        return False


def probe_control_surface() -> dict[str, bool]:
    """Report mount and savepoint health."""
    found: dict[str, bool] = {}
    try:
        found[MOUNT_PROBE] = (mount_root() / MOUNT_PROBE).exists()
    except OSError:
        found[MOUNT_PROBE] = False
    found["git_savepoints"] = Path(GIT_DIR).is_dir()
    return found


async def _run(*argv: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def create_savepoint(name: str) -> bool:
    """Commit the current KB state as a named savepoint.

    Failure is logged and swallowed: a turn that cannot be checkpointed should
    still run, it just will not be revertible. The alternative - refusing to
    answer because bookkeeping failed - is worse.
    """
    await _run(*_git_args(), "add", "-A")
    rc, _, err = await _run(*_git_args(), "commit", "-m", f"savepoint:{name}", "--allow-empty")
    if rc != 0:
        log.warning("savepoint commit failed: %s", err)
        return False
    log.info("created savepoint %s", name)
    return True


async def undo_to_savepoint(name: str) -> bool:
    """Roll the knowledge base back to a named savepoint via git reset."""
    rc, out, _ = await _run(*_git_args(), "log", "--format=%H %s")
    target = None
    for line in out.splitlines():
        sha, _, subject = line.partition(" ")
        if subject == f"savepoint:{name}":
            target = sha
            break
    if not target:
        log.warning("savepoint %s not found in git history", name)
        return False
    rc2, _, err = await _run(*_git_args(), "reset", "--hard", target)
    if rc2 != 0:
        log.warning("git reset failed: %s", err)
        return False
    log.info("undid to savepoint %s", name)
    return True


async def recent_log(limit: int = 50) -> list[str]:
    """Return recent savepoint log entries, newest first."""
    rc, out, _ = await _run(
        *_git_args(), "log", f"--max-count={limit}", "--format=%ai %s"
    )
    if rc != 0:
        return []
    return out.strip().splitlines()


def scratch_dir_for(user_slug: str) -> Path:
    """Per-user scratch space on LOCAL disk, never inside the KB mount.

    If the agent's cwd is the mount, every temp file it writes becomes a
    versioned row in the knowledge base. See docs/decisions/0003.
    """
    path = Path(config.work_dir) / user_slug
    path.mkdir(parents=True, exist_ok=True)
    return path


def assert_scratch_outside_kb() -> None:
    """Fail fast on a misconfiguration that silently pollutes the KB."""
    work = os.path.realpath(config.work_dir)
    kb = os.path.realpath(config.kb_mount)
    if work == kb or work.startswith(kb.rstrip("/") + "/"):
        raise RuntimeError(
            f"WORK_DIR ({config.work_dir}) is inside KB_MOUNT ({config.kb_mount}). "
            "Agent scratch files would be written into the knowledge base as "
            "versioned rows. Point WORK_DIR at local disk."
        )
