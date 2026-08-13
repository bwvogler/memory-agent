"""Knowledge-base helpers: mount health, savepoints, undo.

TigerFS provides the filesystem (FUSE → Postgres). Savepoints/undo are
implemented with a git repo stored on the local Fly volume so they work
without TimescaleDB, which is unavailable on Neon and Cloud SQL.

Git repo:   $WORK_DIR/kb.git   (Fly persistent volume, fast local disk)
Work tree:  $KB_MOUNT/memory   (TigerFS workspace)

All git commands use --git-dir and --work-tree so no .git file or
directory appears inside the knowledge-base workspace.

Also home to the beads task ledger, which is deliberately NOT in the KB:
Bead graph: $WORK_DIR/{user_slug}/.beads   (embedded Dolt, per user)
See docs/decisions/0006.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

import asyncpg

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


async def _run(
    *argv: str, cwd: Optional[Path] = None, env: Optional[dict[str, str]] = None
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=env,
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


async def _savepoint_sha(name: str) -> Optional[str]:
    _, out, _ = await _run(*_git_args(), "log", "--format=%H %s")
    for line in out.splitlines():
        sha, _, subject = line.partition(" ")
        if subject == f"savepoint:{name}":
            return sha
    return None


async def diff_since_savepoint(name: str) -> str:
    """Summarise what changed since a savepoint, as `git diff --stat`.

    Call this BEFORE undoing: afterwards the working tree matches the
    savepoint and the diff is empty. It exists so a revert can record what was
    actually rolled back rather than just that something was.
    """
    target = await _savepoint_sha(name)
    if not target:
        return ""
    rc, out, _ = await _run(*_git_args(), "diff", "--stat", target)
    return out.strip() if rc == 0 else ""


async def undo_to_savepoint(name: str) -> bool:
    """Roll the knowledge base back to a named savepoint via git reset."""
    target = await _savepoint_sha(name)
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


# ---------------------------------------------------------------------------
# beads — the task ledger. See docs/decisions/0006.
# ---------------------------------------------------------------------------

# Bead IDs read as kb-a3f2dd rather than inheriting the per-user directory
# name, which bd would otherwise use and which is an email slug here.
BEADS_PREFIX = "kb"

BACKLOG_FILE = "backlog.md"

_STATUS_ORDER = ["in_progress", "blocked", "open", "deferred", "closed"]


def _bd_env() -> dict[str, str]:
    # bd prompts on a TTY and would hang a headless turn forever.
    return {**os.environ, "BD_NON_INTERACTIVE": "1", "CI": "true"}


async def ensure_beads(user_slug: str) -> bool:
    """Initialise this user's bead graph if absent. Idempotent.

    The graph lives on the Fly volume beside kb.git rather than in the KB
    mount: it is an embedded Dolt database, and running a binary DB over
    FUSE→SQL invites corruption. bd discovers it from the agent's cwd, which
    is already per-user, so isolation needs no extra work.
    """
    scratch = scratch_dir_for(user_slug)
    rc, _, err = await _run(
        "bd", "init",
        "--init-if-missing",
        "--non-interactive",
        "--prefix", BEADS_PREFIX,
        cwd=scratch,
        env=_bd_env(),
    )
    if rc != 0:
        log.warning("bd init failed for %s: %s", user_slug, err.strip())
        return False
    return True


async def bd_prime(user_slug: str) -> str:
    """Return bd's own workflow context for injection into the system prompt.

    bd ships this text and keeps it current with the binary, so we inject it
    rather than hand-maintaining a copy that silently rots at the next
    version bump. bd installs a SessionStart hook to do this itself, but that
    hook cannot fire here: agent.py sets setting_sources=[] on purpose, so
    project settings are never read. Same benefit, no isolation cost.
    """
    rc, out, err = await _run(
        "bd", "prime", cwd=scratch_dir_for(user_slug), env=_bd_env()
    )
    if rc != 0:
        log.warning("bd prime failed for %s: %s", user_slug, err.strip())
        return ""
    return out.strip()


async def list_beads(user_slug: str, label: Optional[str] = None) -> Optional[list[dict]]:
    """Return the bead graph as dicts, optionally filtered to one label.

    Returns None - not [] - when bd could not be reached. The distinction
    matters: export_backlog overwrites a file with this, and an empty list
    from a transient failure would silently replace a real backlog with
    "Nothing open."
    """
    argv = ["bd", "list", "--json"]
    if label:
        argv += ["--label", label]
    rc, out, err = await _run(*argv, cwd=scratch_dir_for(user_slug), env=_bd_env())
    if rc != 0:
        log.warning("bd list failed for %s: %s", user_slug, err.strip())
        return None
    try:
        return json.loads(out) or []
    except json.JSONDecodeError:
        log.warning("bd list returned unparseable JSON for %s", user_slug)
        return None


async def create_bead(
    user_slug: str,
    title: str,
    description: str = "",
    priority: int = 2,
    labels: tuple[str, ...] = (),
    status: Optional[str] = None,
    issue_type: str = "task",
) -> Optional[str]:
    """Create one bead and return its id, or None if bd could not be reached.

    Arguments go through argv, never a shell, so newlines and quotes in the
    description are safe. Note `bd edit` is never used anywhere in this
    codebase: it opens $EDITOR and a headless turn would hang forever.
    """
    argv = [
        "bd", "create", title,
        "--type", issue_type,
        "--priority", str(priority),
    ]
    if description:
        argv += ["--description", description]
    if labels:
        argv += ["--labels", ",".join(labels)]
    if status:
        argv += ["--status", status]
    argv += ["--json"]

    rc, out, err = await _run(*argv, cwd=scratch_dir_for(user_slug), env=_bd_env())
    if rc != 0:
        log.warning("bd create failed for %s: %s", user_slug, err.strip())
        return None
    try:
        return json.loads(out).get("id")
    except (json.JSONDecodeError, AttributeError):
        log.warning("bd create returned unparseable JSON for %s", user_slug)
        return None


async def export_backlog(user_slug: str) -> bool:
    """Render the bead graph to memory/backlog.md in the KB.

    The Dolt database stays the source of truth; this is a projection. It
    earns its keep twice: it puts a human-readable backlog in the
    Postgres-backed store (the volume holding Dolt is the weaker tier, with
    no replication), and it renders in the existing /kb browser for free.
    """
    issues = await list_beads(user_slug)
    if issues is None:
        return False

    try:
        (workspace_root() / BACKLOG_FILE).write_text(_render_backlog(issues))
    except OSError as exc:
        log.warning("could not write %s: %s", BACKLOG_FILE, exc)
        return False
    return True


def _render_backlog(issues: list[dict]) -> str:
    lines = [
        "# Backlog",
        "",
        "Generated from the bead graph after each turn. Do not edit by hand -",
        "it is overwritten. Use `bd` to change anything here.",
        "",
        "Descriptions are reproduced in full. This file is the only copy of the",
        "ledger that reaches Postgres - the graph itself sits on an unreplicated",
        "volume - so a summary here would be useless for reconstructing anything.",
        "Note it still omits `bd` notes, design and acceptance fields, which",
        "`bd list --json` does not return.",
        "",
    ]
    if not issues:
        lines += ["Nothing open.", ""]
        return "\n".join(lines)

    by_status: dict[str, list[dict]] = {}
    for issue in issues:
        by_status.setdefault(issue.get("status", "open"), []).append(issue)

    # Unknown statuses sort last rather than vanishing.
    for status in sorted(
        by_status, key=lambda s: (_STATUS_ORDER + [s]).index(s)
    ):
        lines.append(f"## {status.replace('_', ' ').title()}")
        lines.append("")
        for issue in sorted(by_status[status], key=lambda i: i.get("priority", 4)):
            lines.append(
                f"- **P{issue.get('priority', 4)}** `{issue['id']}` "
                f"{issue.get('title', '(untitled)')}"
            )
            if labels := issue.get("labels"):
                lines.append(f"  - labels: {', '.join(labels)}")
            if blockers := issue.get("dependency_count"):
                lines.append(f"  - blocked by {blockers} issue(s)")
            if desc := issue.get("description"):
                # In full, deliberately. This used to render the first line
                # capped at 200 characters, which quietly dropped every design
                # note and acceptance criterion - and those are exactly what a
                # reader needs if this file is ever the only copy left.
                lines.append("")
                for line in desc.rstrip().splitlines():
                    lines.append(f"  {line}" if line.strip() else "")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Direct SQL access to TigerFS backing table — one query, no FUSE round trips
# ---------------------------------------------------------------------------

_kb_pool: Optional[asyncpg.Pool] = None

_LIST_SQL = """
WITH RECURSIVE paths AS (
    SELECT id, filetype, body, filename AS path
    FROM   tigerfs.memory
    WHERE  parent_id IS NULL
    UNION ALL
    SELECT m.id, m.filetype, m.body, p.path || '/' || m.filename
    FROM   tigerfs.memory m
    JOIN   paths p ON m.parent_id = p.id
)
SELECT path, body
FROM   paths
WHERE  filetype = 'file' AND path LIKE '%.md'
ORDER  BY path
"""


async def _pool() -> asyncpg.Pool:
    global _kb_pool
    if _kb_pool is None:
        _kb_pool = await asyncpg.create_pool(config.kb_database_url)
    return _kb_pool


async def close_pool() -> None:
    global _kb_pool
    if _kb_pool:
        await _kb_pool.close()
        _kb_pool = None


async def sql_list_files() -> list[str]:
    """Return all markdown file paths in the workspace (single SQL query)."""
    pool = await _pool()
    rows = await pool.fetch(_LIST_SQL)
    return [r["path"] for r in rows]


async def sql_read_file(path: str) -> Optional[str]:
    """Return file content by workspace-relative path (single SQL query)."""
    pool = await _pool()
    rows = await pool.fetch(_LIST_SQL)
    for r in rows:
        if r["path"] == path:
            return r["body"]
    return None
