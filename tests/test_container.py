"""The smoke tier: build the real image and run the real stack.

Every bug this file guards against was silent. `bootstrap/` was missing from
the image, so the seeder found no source directory and returned cleanly.
Postgres 16 has no uuidv7(), so the mount came up healthy and every workspace
write then failed. And app/kb.py deliberately logs-and-continues on bd
failures - a turn that cannot reach its ledger should still answer the user -
which means a broken `bd` is indistinguishable from a working one at runtime.

Nothing here needs an API key: no model calls are made.
"""

from __future__ import annotations

import httpx
import pytest

from .conftest import USER_SLUG, app_exec, bd, bd_json

pytestmark = pytest.mark.container


def test_stack_is_healthy_and_the_kb_is_mounted(stack):
    health = httpx.get(f"{stack}/healthz", timeout=10).json()

    assert health["ok"] is True
    assert health["kb_mounted"] is True, (
        "TigerFS mounted but the workspace is unusable - check the Postgres "
        "version provides uuidv7()"
    )


def test_bd_is_installed_and_pinned(stack):
    out = app_exec("bd", "version").stdout

    assert "bd version" in out
    # Unpinned, a newer bd would write a schema the old binary cannot reopen.
    assert "1.2.1" in out, f"bd version drifted from the Dockerfile pin: {out}"


def test_bootstrap_skills_are_seeded_into_the_kb(stack):
    """Regression: bootstrap/ was absent from the image, so this never ran."""
    listing = app_exec("ls", "/mnt/kb/memory/skills/").stdout

    assert "lint" in listing
    assert "ingest" in listing


def test_seeded_skills_are_the_beads_aware_versions(stack):
    """A stale copy in the KB would leave the skills reporting into chat."""
    lint = app_exec("cat", "/mnt/kb/memory/skills/lint/SKILL.md").stdout

    assert "bd create" in lint


def test_the_workspace_guide_is_seeded(stack):
    assert "AGENT_GUIDE.md" in app_exec("ls", "/mnt/kb/memory/").stdout


def test_bead_graph_is_created_outside_the_kb(beads):
    """Dolt is a binary DB; on FUSE it would risk corruption."""
    app_exec("ls", f"/work/{USER_SLUG}/.beads")

    # And must NOT be in the knowledge base.
    listing = app_exec("ls", "-a", "/mnt/kb/memory/").stdout
    assert ".beads" not in listing


def test_ensure_beads_is_idempotent(beads):
    """It runs on every single turn; a second call must not fail or reset."""
    app_exec(
        "python", "-c",
        "import asyncio;from app import kb;"
        f"assert asyncio.run(kb.ensure_beads('{USER_SLUG}'))",
    )


def test_bd_prime_returns_context_for_the_system_prompt(beads):
    """If this silently returns nothing the agent loses all bd instructions."""
    out = bd("prime").stdout

    assert "Beads Workflow Context" in out


def test_backlog_page_is_exported_into_the_kb(beads):
    """The durable, human-readable copy of a graph that lives on a volume."""
    app_exec(
        "python", "-c",
        "import asyncio;from app import kb;"
        f"assert asyncio.run(kb.export_backlog('{USER_SLUG}'))",
    )

    body = httpx.get(f"{beads}/api/kb/file", params={"path": "backlog.md"},
                     timeout=10).json()["content"]
    assert body.startswith("# Backlog")


def test_ready_work_excludes_blocked_beads(beads):
    """Sequencing must live in edges: `bd ready` is what a fresh session reads."""
    blocker = bd("create", "--title=smoke blocker", "--type=task",
                 "--priority=2", "--json").stdout
    blocked = bd("create", "--title=smoke blocked", "--type=task",
                 "--priority=2", "--json").stdout
    import json
    blocker_id = json.loads(blocker)["id"]
    blocked_id = json.loads(blocked)["id"]

    try:
        bd("dep", "add", blocked_id, blocker_id)

        ready = {i["id"] for i in bd_json("ready")}
        assert blocker_id in ready
        assert blocked_id not in ready, "a blocked bead must not be offered as ready"

        # Closing the blocker releases it.
        bd("close", blocker_id, "--reason=smoke test")
        assert blocked_id in {i["id"] for i in bd_json("ready")}
    finally:
        bd("close", blocked_id, "--reason=smoke test cleanup", check=False)
        bd("close", blocker_id, "--reason=smoke test cleanup", check=False)
