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

import asyncpg

from .config import config

log = logging.getLogger(__name__)

WORKSPACE_NAME = "memory"

# .info is mount-level metadata present at the root of any live TigerFS mount.
MOUNT_PROBE = ".info"

GIT_DIR = str(Path(config.work_dir) / "kb.git")

# ext4 and most filesystems cap a single name at 255 bytes. Truncating here
# turns a pathological name into a working upload rather than an ENAMETOOLONG
# the user sees as "attachments are broken".
MAX_UPLOAD_NAME_LENGTH = 200


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
    *argv: str, cwd: Path | None = None, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=env,
    )
    out, err = await proc.communicate()
    return (
        proc.returncode or 0,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )


async def create_savepoint(name: str) -> bool:
    """Commit the current KB state as a named savepoint.

    Failure is logged and swallowed: a turn that cannot be checkpointed should
    still run, it just will not be revertible. The alternative - refusing to
    answer because bookkeeping failed - is worse.
    """
    await _run(*_git_args(), "add", "-A")
    rc, _, err = await _run(
        *_git_args(), "commit", "-m", f"savepoint:{name}", "--allow-empty"
    )
    if rc != 0:
        log.warning("savepoint commit failed: %s", err)
        return False
    log.info("created savepoint %s", name)
    return True


async def _savepoint_sha(name: str) -> str | None:
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


def uploads_dir_for(user_slug: str, turn_id: str) -> Path:
    """Where a turn's attachments land: inside that user's scratch, per turn.

    Scratch and not the KB, for the reason in docs/decisions/0003 - a file
    written under the mount becomes a versioned row in the wiki, and an
    attachment is raw input, not knowledge. Per turn rather than a flat
    directory so two uploads of the same filename cannot silently overwrite
    each other, and so a turn's inputs stay legible next to the bead a revert
    files about it.
    """
    path = scratch_dir_for(user_slug) / "uploads" / turn_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_upload_name(name: str) -> str | None:
    """Reduce a client-supplied filename to a single safe path component.

    The client controls this string completely, so it is treated as hostile:
    `../../.beads/issues.jsonl` would otherwise let an upload overwrite the
    ledger, and an absolute path would escape scratch entirely. Everything up
    to the last separator is discarded rather than rejected, because browsers
    legitimately send bare names and the occasional full path.

    Returns None when nothing usable survives, which the caller reports as a
    400 - guessing a name for a file the user cannot then refer to is worse
    than refusing it.
    """
    # Both separators, since the name may come from a Windows client.
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    # NUL would truncate the path at the syscall boundary; the rest are
    # ordinary control characters that have no business in a filename.
    base = "".join(c for c in base if c.isprintable())
    if base in {"", ".", ".."}:
        return None
    return base[:MAX_UPLOAD_NAME_LENGTH]


def resolve_upload_path(user_slug: str, turn_id: str, name: str) -> Path | None:
    """Absolute path for one attachment, or None if the name is unusable.

    Belt and braces: `safe_upload_name` already removed every separator, so
    the containment check below cannot fail today. It stays because it is the
    invariant that actually matters - the same posture as
    `assert_scratch_outside_kb`, which also guards a condition that is true by
    construction until the day someone changes the construction.
    """
    base = safe_upload_name(name)
    if base is None:
        return None
    directory = uploads_dir_for(user_slug, turn_id)
    candidate = (directory / base).resolve()
    if not candidate.is_relative_to(directory.resolve()):
        log.warning("rejected upload name escaping scratch: %r", name)
        return None
    return candidate


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
        "bd",
        "init",
        "--init-if-missing",
        "--non-interactive",
        "--prefix",
        BEADS_PREFIX,
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


async def list_beads(user_slug: str, label: str | None = None) -> list[dict] | None:
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
    status: str | None = None,
    issue_type: str = "task",
) -> str | None:
    """Create one bead and return its id, or None if bd could not be reached.

    Arguments go through argv, never a shell, so newlines and quotes in the
    description are safe. Note `bd edit` is never used anywhere in this
    codebase: it opens $EDITOR and a headless turn would hang forever.
    """
    argv = [
        "bd",
        "create",
        title,
        "--type",
        issue_type,
        "--priority",
        str(priority),
    ]
    if description:
        argv += ["--description", description]
    if labels:
        argv += ["--labels", ",".join(labels)]
    argv += ["--json"]

    rc, out, err = await _run(*argv, cwd=scratch_dir_for(user_slug), env=_bd_env())
    if rc != 0:
        log.warning("bd create failed for %s: %s", user_slug, err.strip())
        return None
    try:
        bead_id = json.loads(out).get("id")
    except (json.JSONDecodeError, AttributeError):
        log.warning("bd create returned unparseable JSON for %s", user_slug)
        return None

    # Status is set in a second call because `bd create --status` is a 1.2.x-only
    # flag, and the pinned 1.2.2 is the tested 1.1 line: it answers `unknown
    # flag: --status` and creates nothing. `bd update --status` works on both.
    #
    # The cost is a window - between the two calls the bead is `open` - and for a
    # signal bead, whose whole point is to stay out of `bd ready`, that window is
    # exactly the wrong state. It is sub-second and unavoidable without the flag,
    # so the failure is made loud instead: a bead that could not be moved is
    # still returned, because it exists and losing its id is the worse outcome.
    if status and bead_id:
        rc, _, err = await _run(
            "bd",
            "update",
            bead_id,
            "--status",
            status,
            cwd=scratch_dir_for(user_slug),
            env=_bd_env(),
        )
        if rc != 0:
            log.warning(
                "bd could not set %s to %s for %s, so it stays open: %s",
                bead_id,
                status,
                user_slug,
                err.strip(),
            )
    return bead_id


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
    for status in sorted(by_status, key=lambda s: [*_STATUS_ORDER, s].index(s)):
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

_kb_pool: asyncpg.Pool | None = None

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
    global _kb_pool  # noqa: PLW0603 - one connection pool per process
    if _kb_pool is None:
        _kb_pool = await asyncpg.create_pool(config.kb_database_url)
    return _kb_pool


async def close_pool() -> None:
    global _kb_pool  # noqa: PLW0603 - one connection pool per process
    if _kb_pool:
        await _kb_pool.close()
        _kb_pool = None


async def sql_list_files() -> list[str]:
    """Return all markdown file paths in the workspace (single SQL query)."""
    pool = await _pool()
    rows = await pool.fetch(_LIST_SQL)
    return [r["path"] for r in rows]


async def sql_read_file(path: str) -> str | None:
    """Return file content by workspace-relative path (single SQL query)."""
    pool = await _pool()
    rows = await pool.fetch(_LIST_SQL)
    for r in rows:
        if r["path"] == path:
            return r["body"]
    return None


# ---------------------------------------------------------------------------
# Closing the loop: what this image shipped. See docs/decisions/0010.
# ---------------------------------------------------------------------------

# Repo-relative, and baked into the image by a COPY of this one file.
SHIPPED_MANIFEST = (
    Path(__file__).resolve().parent.parent / "docs" / "shipped-beads.jsonl"
)

# Beside .beads in the per-user scratch dir, mirroring .bootstrap-state.json.
SHIPPED_STATE_FILE = ".shipped-beads.json"


def _read_manifest(path: Path | None = None) -> list[dict]:
    """Parse the shipped-beads manifest. Never raises.

    The path is resolved at call time, not bound as a default: a default
    argument is evaluated once at import and would quietly ignore any later
    override, which is both untestable and wrong the moment anything relocates
    the manifest.

    A hand-appended file will eventually contain a bad line, and the cost of
    that must be one skipped bead rather than a boot that closes nothing - or,
    worse, a boot that fails. Lines carrying only `_comment` are the file's own
    documentation and are skipped in silence.
    """
    path = path or SHIPPED_MANIFEST
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("could not read shipped manifest %s: %s", path, exc)
        return []

    entries: list[dict] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            log.warning("shipped manifest line %d is not JSON; skipping", lineno)
            continue
        if not isinstance(entry, dict):
            log.warning("shipped manifest line %d is not an object; skipping", lineno)
            continue
        if not entry.get("id"):
            if "_comment" not in entry:
                log.warning("shipped manifest line %d has no id; skipping", lineno)
            continue
        entries.append(entry)
    return entries


def _ledger_slugs() -> list[str]:
    """Every user slug on this machine that has a bead ledger.

    The user set is not known at startup - it is whoever has ever taken a turn -
    so it is read off the volume. A directory with no .beads is skipped rather
    than initialised: creating one here would put a ledger under a directory
    that may not be a user at all (kb.git, lost+found).
    """
    try:
        candidates = sorted(Path(config.work_dir).iterdir())
    except OSError as exc:
        log.warning("could not scan %s for ledgers: %s", config.work_dir, exc)
        return []
    return [d.name for d in candidates if (d / ".beads").is_dir()]


def _read_shipped_state(path: Path) -> set[str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    applied = raw.get("applied", []) if isinstance(raw, dict) else []
    return {str(i) for i in applied} if isinstance(applied, list) else set()


def _bead_absent(message: str) -> bool:
    """True when bd's refusal means this ledger simply has no such bead.

    The distinction is load-bearing, not cosmetic: absence is permanent and
    every other refusal is not. `bd close` reports a missing id as
    `no issue found matching "kb-x"`, and reports a bead whose blocker is still
    open as `cannot close blocked issue` - which stops being true the moment the
    blocker closes, possibly on this same run.
    """
    return "no issue found" in message.lower()


async def reconcile_shipped(user_slug: str) -> list[str]:
    """Close the beads this image resolves, in one user's ledger.

    An idea about the image originates on prod, where the agent can file it and
    nothing more: it has no repo, no git, and the image it is running is
    immutable. The work happens in the repo, and this is the return path - the
    image itself carries the news of what it fixed, so closing the loop needs no
    ssh, no credentials and no human step at deploy time.

    Startup is the hook because a deploy is the event: it is the only moment at
    which "what this image resolves" changes.

    **Applied entries are recorded, and status is deliberately not consulted.**
    Reopening a bead is how a human says "this did not actually ship" - and a
    reconciler that checked status instead would close it again on the next
    boot, overruling exactly the person it exists to inform.

    Returns the ids closed on this run. Never raises: a ledger that cannot be
    reached must not take down the boot it was only reporting to.
    """
    entries = _read_manifest()
    if not entries:
        return []

    scratch = scratch_dir_for(user_slug)
    state_path = scratch / SHIPPED_STATE_FILE
    applied = _read_shipped_state(state_path)

    pending = [e for e in entries if str(e["id"]) not in applied]
    if not pending:
        return []

    image = os.environ.get("FLY_IMAGE_REF", "local")
    closed: list[str] = []

    # Manifest order is append order, which is not dependency order - and bd
    # refuses to close a bead whose blocker is still open, even when the blocker
    # is a line further down the same manifest. So make passes until one closes
    # nothing new. This is what a single pass got wrong: kb-068 depends on
    # kb-b82 and was listed first, so its close was refused, recorded as applied
    # and never retried, leaving shipped work open on prod with one log line as
    # the only trace.
    while pending:
        refused: list[tuple[dict, str]] = []
        progressed = False

        for entry in pending:
            bead_id = str(entry["id"])
            summary = str(entry.get("summary") or "shipped")
            commit = str(entry.get("commit") or "unknown")

            rc, _, err = await _run(
                "bd",
                "close",
                bead_id,
                "--reason",
                f"shipped in {image}",
                cwd=scratch,
                env=_bd_env(),
            )
            if rc != 0:
                message = err.strip() or f"rc={rc}"
                if _bead_absent(message):
                    # A bead this ledger never had is the common case, not a
                    # fault: every user's ledger sees the same manifest. Record
                    # it as applied - there is nothing a later boot could fix.
                    log.info(
                        "shipped bead %s is not in %s's ledger (%s)",
                        bead_id,
                        user_slug,
                        message,
                    )
                    applied.add(bead_id)
                else:
                    refused.append((entry, message))
                continue

            # The note, not the reason, carries the audit trail: a manifest
            # entry is hand-written and can claim something that never shipped,
            # so the commit is the thing that lets a reader check.
            await note_bead(
                user_slug, bead_id, f"{summary} (commit {commit}, image {image})"
            )
            applied.add(bead_id)
            closed.append(bead_id)
            progressed = True
            log.info("closed shipped bead %s for %s", bead_id, user_slug)

        pending = [entry for entry, _ in refused]
        if not progressed:
            # Nothing left to unblock on this run. Deliberately *not* recorded
            # as applied: the next boot retries, and a warning every boot is the
            # right cost for shipped work that is still open. Warning, not info,
            # because unlike absence this is a state someone has to resolve.
            for entry, message in refused:
                log.warning(
                    "shipped bead %s still open in %s: %s",
                    entry["id"],
                    user_slug,
                    message,
                )
            break

    try:
        state_path.write_text(
            json.dumps({"applied": sorted(applied)}, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        # Worth a warning: without the state file the next boot re-closes
        # everything, which is how a deliberately reopened bead gets overruled.
        log.warning("could not write %s: %s", state_path, exc)

    return closed


async def reconcile_shipped_all() -> dict[str, list[str]]:
    """Run reconcile_shipped for every user ledger on this machine."""
    results: dict[str, list[str]] = {}
    for slug in _ledger_slugs():
        closed = await reconcile_shipped(slug)
        if closed:
            results[slug] = closed
    return results


async def note_bead(user_slug: str, bead_id: str, text: str) -> bool:
    """Append a note to an existing bead. Never raises."""
    rc, _, err = await _run(
        "bd",
        "note",
        bead_id,
        text,
        cwd=scratch_dir_for(user_slug),
        env=_bd_env(),
    )
    if rc != 0:
        log.warning("bd note failed for %s/%s: %s", user_slug, bead_id, err.strip())
    return rc == 0


async def set_priority(user_slug: str, bead_id: str, priority: int) -> bool:
    """Raise or lower a bead's priority. Never raises."""
    rc, _, err = await _run(
        "bd",
        "update",
        bead_id,
        "--priority",
        str(priority),
        cwd=scratch_dir_for(user_slug),
        env=_bd_env(),
    )
    if rc != 0:
        log.warning("bd update failed for %s/%s: %s", user_slug, bead_id, err.strip())
    return rc == 0
