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

# Kept out of the test body because `python -c` cannot take a compound
# statement after a semicolon, so this one has to be genuinely multi-line.
LEDGER_SCRIPT = """
import asyncio
from app.config import config
from app.session_store import PostgresSessionStore

async def main():
    s = PostgresSessionStore(config.session_database_url)
    await s.start()
    await s.record_turn_outcome('led-1', 'dev@localhost', 'ok', None, ['lint'])
    await s.record_turn_outcome('led-2', 'dev@localhost', 'ok', None, ['lint'])
    await s.mark_turn_outcome('led-2', 'reverted')
    rows = await s.skill_signal_summary()
    hit = [r for r in rows if r['skill'] == 'lint'][0]
    assert hit['turns'] == 2, hit
    assert hit['reverted'] == 1, hit
    await s.close()

asyncio.run(main())
"""


OVERLAY_APPEND_SCRIPT = """
import sys
from app import evolve

path = "/mnt/kb/memory/skills/kb-curator/LEARNED.md"
current = open(path).read()
reason = evolve.bounded_overlay_edit(current, current + "\\n- 2026-08-13: a lesson.\\n")
if reason:
    print(reason)
    sys.exit(1)
"""


def test_the_durable_store_is_reachable_and_says_so(stack):
    """The ledger is the denominator every signal rate is read against, and a
    store that failed to start is invisible at runtime by design - kb.py
    logs-and-continues so a turn can still answer the user. `transcripts` is
    how that state stops being silent."""
    assert httpx.get(f"{stack}/healthz", timeout=10).json()["transcripts"] == "ready"


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


def test_the_image_skill_overlay_is_seeded_and_can_be_appended_to(stack):
    """kb-curator ships in the image and cannot be edited, so its lessons need
    a file in the KB to land in. Seeding a stub rather than creating it on
    demand: the evolution guard refuses a Write to a path that does not exist,
    and the skill would otherwise read a missing file on every curation turn.

    Then the part only the real store can answer. The overlay's header is
    immutable under the bound, and TigerFS re-serialises markdown rather than
    round-tripping bytes - the same behaviour that once silently disabled
    bootstrap upgrades. If re-serialisation moved the header, the very first
    append reflection attempts would be refused as a body edit, in production,
    for a reason no unit test using tmp_path could ever reproduce.
    """
    overlay = app_exec("cat", "/mnt/kb/memory/skills/kb-curator/LEARNED.md").stdout

    assert "## Learned" in overlay

    out = app_exec("python", "-c", OVERLAY_APPEND_SCRIPT, check=False)
    assert out.returncode == 0, (
        f"appending to the stored overlay was refused: {out.stdout}"
    )


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
        "python",
        "-c",
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
        "python",
        "-c",
        "import asyncio;from app import kb;"
        f"assert asyncio.run(kb.export_backlog('{USER_SLUG}'))",
    )

    body = httpx.get(
        f"{beads}/api/kb/file", params={"path": "backlog.md"}, timeout=10
    ).json()["content"]
    assert body.startswith("# Backlog")


APPEND_PROBE = """
from pathlib import Path
p = Path('/mnt/kb/memory/.append-probe.md')
p.write_text('hello world\\n')
with open(p, 'a') as f:
    f.write('appended\\n')
data = p.read_bytes()
p.unlink()
print(repr(data))
assert b'\\x00' in data, 'APPEND IS SAFE NOW - see the test for what to change'
assert b'hello' not in data
"""


def test_appending_to_a_kb_file_still_destroys_it(stack):
    """Pins a live data-loss hazard, and will fail loudly once it is fixed.

    Writing at a non-zero offset on the mount does not preserve what was
    already there: the prior bytes become NULs. This destroyed a user's
    memory/CLAUDE.md - an agent hit a file-tool error, fell back to a shell
    append, and turned 233 bytes of personal notes into 233 zeroes.

    The defence is instructional (the system prompt and the kb-curator
    reference both forbid appending), so this test is what tells us whether
    that instruction is still needed. If it fails, appending has become safe:
    relax the warnings and delete this test.
    """
    out = app_exec("python", "-c", APPEND_PROBE).stdout

    assert "\\x00" in out, out


def test_a_revert_files_a_signal_bead(beads):
    """Stage 2's core path, exercised without spending a model call.

    Drives signals.on_revert directly with a fabricated turn. The live tier
    covers the real button; this covers the mechanism, which is what breaks.
    """
    before = {i["id"] for i in bd_json("list", "--label", "signal")}

    app_exec(
        "python",
        "-c",
        "import asyncio;from app import signals;from app.turns import Turn;"
        "t=Turn(id='sig-smoke', user_email='dev@localhost');"
        "t.prompt='add a page about oolong';t.skills={'kb-curator'};"
        "t.savepoint='turn-sig-smoke';"
        "print(asyncio.run(signals.on_revert(t,'dev_localhost',"
        "' memory/wiki/tea.md | 2 +-')))",
    )

    filed = [i for i in bd_json("list", "--label", "signal") if i["id"] not in before]
    assert filed, "a revert filed no bead"
    bead = filed[0]
    try:
        assert "revert" in bead["labels"]
        # Evidence, not work: it must not show up as claimable.
        assert bead["status"] == "deferred"
        assert bead["id"] not in {i["id"] for i in bd_json("ready")}
        # Attribution and context both have to survive into the bead.
        assert "kb-curator" in bead["description"]
        assert "oolong" in bead["description"]
        assert "tea.md" in bead["description"]
    finally:
        bd("close", bead["id"], "--reason=smoke test cleanup", check=False)


def test_repeated_identical_failures_do_not_flood_the_ledger(beads):
    """A missing allowlist entry would otherwise file one bead every turn."""
    snippet = (
        "import asyncio;from app import signals;from app.turns import Turn;"
        "from app.turns import TurnState;"
        "t=Turn(id='dup-smoke', user_email='dev@localhost');"
        "t.prompt='x';t.permission_denials=['Bash'];"
        "t.state=TurnState.DONE;"
        "asyncio.run(signals.record_turn(t,'dev_localhost'))"
    )
    before = {i["id"] for i in bd_json("list", "--label", "signal")}
    app_exec("python", "-c", snippet)
    app_exec("python", "-c", snippet)

    filed = [i for i in bd_json("list", "--label", "signal") if i["id"] not in before]
    try:
        assert len(filed) == 1, f"expected one deduped bead, got {len(filed)}"
    finally:
        for bead in filed:
            bd("close", bead["id"], "--reason=smoke test cleanup", check=False)


def test_the_skill_ledger_records_every_turn_not_just_bad_ones(stack):
    """Without the denominator, a per-skill revert count means nothing."""
    app_exec("python", "-c", LEDGER_SCRIPT)

    summary = httpx.get(f"{stack}/api/signals", timeout=10).json()
    assert summary["totals"]["turns"] >= 2
    assert any(s["skill"] == "lint" for s in summary["skills"])


def test_ready_work_excludes_blocked_beads(beads):
    """Sequencing must live in edges: `bd ready` is what a fresh session reads."""
    blocker = bd(
        "create", "--title=smoke blocker", "--type=task", "--priority=2", "--json"
    ).stdout
    blocked = bd(
        "create", "--title=smoke blocked", "--type=task", "--priority=2", "--json"
    ).stdout
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
