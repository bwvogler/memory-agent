"""Knowledge-base helpers: mount health, savepoints, undo.

TigerFS exposes its control surface as synthesised dot-directories inside the
mount (`.savepoint/`, `.undo/`, `.log/`, `.history/`). Those paths are generated
by the FUSE layer in Go - they are NOT rows you can query over SQL - which is
precisely why this project mounts the filesystem instead of talking to Postgres
directly. See docs/decisions/0001-fuse-is-the-constraint.md.

    !! VERIFY BEFORE RELYING ON THIS MODULE !!
    The exact write syntax for creating a savepoint and performing an undo is
    the one thing in this repo that has not been validated against a live
    TigerFS mount. Everything savepoint-related is deliberately confined to
    this file so there is exactly one place to fix. Run
    `scripts/spike-fuse.sh` first: it prints the real contents of the control
    directories so you can confirm the interface, and `probe_control_surface()`
    below is called at startup to warn loudly if a directory is missing.
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
# .savepoint/.log/.history/.undo live inside each workspace, not the mount root.
MOUNT_PROBE = ".info"
WORKSPACE_CONTROL_DIRS = (".savepoint", ".undo", ".log", ".history")


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
    """Report which TigerFS control directories are reachable.

    Note that TigerFS deliberately hides parts of its control surface from
    `ls` - some paths are path-accessible but not enumerable - so absence here
    means "could not stat", not necessarily "does not exist".
    """
    root = mount_root()
    ws = workspace_root()
    found: dict[str, bool] = {}
    try:
        found[MOUNT_PROBE] = (root / MOUNT_PROBE).exists()
    except OSError:
        found[MOUNT_PROBE] = False
    for d in WORKSPACE_CONTROL_DIRS:
        try:
            found[d] = (ws / d).exists()
        except OSError:
            found[d] = False
    return found


async def _run(*argv: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def create_savepoint(name: str) -> bool:
    """Create a named savepoint. Returns True on success.

    Failure is logged and swallowed: a turn that cannot be checkpointed should
    still run, it just will not be revertible. The alternative - refusing to
    answer because bookkeeping failed - is worse.
    """
    target = workspace_root() / ".savepoint" / f"{name}.json"
    try:
        import json
        await asyncio.to_thread(
            target.write_text, json.dumps({"description": f"before turn {name}"}), "utf-8"
        )
        log.info("created savepoint %s", name)
        return True
    except OSError as exc:
        log.warning("could not create savepoint %s: %s", name, exc)
        return False


async def undo_to_savepoint(name: str) -> bool:
    """Roll the knowledge base back to a named savepoint.

    TigerFS undo is itself reversible, so this is safe to expose in a UI.
    """
    apply_path = workspace_root() / ".undo" / "to-savepoint" / name / ".apply"
    try:
        await asyncio.to_thread(apply_path.touch)
        log.info("undid to savepoint %s", name)
        return True
    except OSError as exc:
        log.warning("could not undo to savepoint %s: %s", name, exc)
        return False


async def recent_log(limit: int = 50) -> list[str]:
    """Read recent operation-log entries, newest first.

    The log records per-user attribution, which is what makes a shared,
    multi-writer knowledge base auditable.
    """
    log_dir = workspace_root() / ".log"
    try:
        entries = sorted(
            (p for p in log_dir.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        return [p.name for p in entries]
    except OSError as exc:
        log.warning("could not read operation log: %s", exc)
        return []


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
