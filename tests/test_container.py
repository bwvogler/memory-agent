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

import base64
import json
import re
import time

import httpx
import pytest

from .conftest import REPO_ROOT, USER_SLUG, app_exec, bd, bd_json

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


def wait_until_idle(stack, timeout: float = 60.0) -> None:
    """Block until no turn is in flight, so a POST can expect 202.

    These tests share one live app, and since img-lsp only one turn runs at a
    time - savepoints are a `git add -A` over the whole workspace, so a second
    turn would savepoint over the first. Every earlier test that starts a real
    turn therefore holds the gate until that turn reaches a terminal state.

    Waiting rather than sleeping is the point: `busy` going false is the app
    asserting that a turn which failed on the placeholder API key still
    finished. A turn that never finishes is never evicted and would wedge every
    later turn, so this timing out is a real failure, not a flake.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not httpx.get(f"{stack}/healthz", timeout=10).json()["busy"]:
            return
        time.sleep(0.5)
    msg = f"a turn was still running after {timeout}s; the gate never released"
    raise AssertionError(msg)


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


def test_healthz_reports_the_outbound_mcp_catalog(stack):
    """Both Google servers present and dark, because this stack has no secrets.

    An outbound server whose credential is unset is dropped from the agent's
    toolset silently, so this field is the only difference between "the tools
    are gone" and "the tools were never configured". Three things are being
    pinned against the real image: that the key survives in the response at
    all, that a server with no credential reports WHICH variable it wants - an
    operator staring at a calendar that does nothing has nothing else to go on -
    and that the nested shape survives serialisation. See docs/decisions/0015.

    `missing` outranks every other state, so a stack with no secrets is also the
    case that proves nothing tried to probe Google on the way to answering.
    """
    health = httpx.get(f"{stack}/healthz", timeout=10).json()

    assert set(health["mcp_catalog"]) == {"calendar", "gmail"}
    for name, entry in health["mcp_catalog"].items():
        assert entry["state"] == "missing", f"{name}: {entry}"
        assert entry["missing"], f"{name}: state is missing but names no variable"
        assert all(v.startswith("MCP_") for v in entry["missing"]), f"{name}: {entry}"
        # Nothing was asked of Google, so nothing may be claimed about it.
        assert "refresh" not in entry, f"{name}: {entry}"


def test_the_outbound_mcp_servers_are_installed_and_pinned(stack):
    """The catalog names bare binaries, so PATH is where the pin has to hold.

    A missing binary is invisible until a turn actually calls a tool, and then
    it surfaces as an SDK subprocess failure rather than as anything naming the
    package. Same reasoning as test_bd_is_installed_and_pinned below.
    """
    assert "2.6.2" in app_exec("google-calendar-mcp", "version").stdout
    assert app_exec("gmail-mcp", "--help").returncode == 0


def test_bd_is_installed_and_pinned(stack):
    out = app_exec("bd", "version").stdout

    assert "bd version" in out
    # Read the pin rather than restate it. Unpinned, a newer bd would write a
    # schema the old binary cannot reopen - and a hardcoded literal here is its
    # own drift, since it disagrees with the Dockerfile silently until someone
    # reads both. What matters is that the image ships what the Dockerfile says.
    pin = re.search(
        r"^ENV BEADS_VERSION=(\S+)",
        (REPO_ROOT / "Dockerfile").read_text(),
        re.MULTILINE,
    )
    assert pin, "Dockerfile no longer declares ENV BEADS_VERSION"
    assert pin.group(1) in out, (
        f"bd version drifted from the Dockerfile pin {pin.group(1)}: {out}"
    )


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


def test_the_shipped_manifest_is_in_the_image(stack):
    """The reconciler reads this path from inside the container.

    A missing COPY is exactly the kind of silence this tier exists for: the
    manifest would be unreadable, reconcile_shipped would close nothing, and
    every log line and health check would look completely normal.
    """
    app_exec(
        "python",
        "-c",
        "from app import kb;assert kb.SHIPPED_MANIFEST.is_file(), kb.SHIPPED_MANIFEST",
    )


def test_a_shipped_bead_is_closed_once_by_the_image_that_shipped_it(beads):
    """The return path for an idea the agent had about its own image.

    Nothing else closes these beads: the agent cannot, and no human step runs
    at deploy time. See docs/decisions/0010.
    """
    created = bd(
        "create",
        "--title=smoke shipped bead",
        "--type=feature",
        "--priority=3",
        "--json",
    ).stdout
    bead_id = json.loads(created)["id"]
    state = f"/work/{USER_SLUG}/.shipped-beads.json"

    script = (
        "import asyncio,json,pathlib;from app import kb;"
        "m=pathlib.Path('/tmp/shipped.jsonl');"
        f"m.write_text(json.dumps({{'id':'{bead_id}','summary':'smoke',"
        "'commit':'deadbee'})+chr(10));"
        "kb.SHIPPED_MANIFEST=m;"
        "print(asyncio.run(kb.reconcile_shipped_all()))"
    )
    try:
        first = app_exec("python", "-c", script).stdout
        assert bead_id in first, first
        assert bd_json("show", bead_id)[0]["status"] == "closed"

        # The state file is what makes a redeploy a no-op rather than a second
        # note - and what stops a reopened bead being closed again behind the
        # back of whoever reopened it.
        assert bead_id in app_exec("cat", state).stdout
        bd("update", bead_id, "--status=open")
        assert bead_id not in app_exec("python", "-c", script).stdout
        assert bd_json("show", bead_id)[0]["status"] == "open"
    finally:
        bd("close", bead_id, "--reason=smoke test cleanup", check=False)
        app_exec("rm", "-f", state, check=False)


def test_an_attachment_lands_in_scratch_and_never_in_the_kb(stack):
    """The whole point of the feature, against the real volume and real mount.

    Unit tests cover the filename and size rules as arithmetic. What they
    cannot cover is the thing that actually goes wrong here: a path that looks
    fine in a tmp_path but resolves onto the FUSE mount in the image, turning
    every attachment into a versioned row in the wiki.

    The POST does start a turn, which fails immediately on the placeholder API
    key. That is fine and deliberate - staging happens before the agent is
    spawned, so the file is on disk either way and no model call is made.
    """
    body = base64.b64encode(b"Card,Category\nGarbage,Home\n").decode()
    before = app_exec("ls", "/mnt/kb/memory").stdout

    wait_until_idle(stack)
    response = httpx.post(
        f"{stack}/api/turns",
        json={
            "message": "what is in this file?",
            "files": [{"name": "deck.csv", "data": body}],
        },
        timeout=30,
    )
    assert response.status_code == 202, response.text
    turn_id = response.json()["turn_id"]

    staged = f"/work/{USER_SLUG}/uploads/{turn_id}/deck.csv"
    assert app_exec("cat", staged).stdout.startswith("Card,Category")

    # Nothing new in the workspace, and the file is nowhere under it. Scoped to
    # the workspace and NOT to /mnt/kb: the mount root exposes TigerFS's own
    # control surface under .schemas, where an index directory contains itself,
    # and a find rooted there does not terminate.
    assert app_exec("ls", "/mnt/kb/memory").stdout == before
    found = app_exec(
        "find", "/mnt/kb/memory", "-name", "deck.csv", check=False
    ).stdout.strip()
    assert not found, f"an attachment reached the knowledge base: {found}"


def test_an_oversized_attachment_is_refused_before_a_turn_starts(stack):
    """413 from the real route, with no turn left behind streaming forever."""
    oversized = base64.b64encode(b"x" * (11 * 1024 * 1024)).decode()

    response = httpx.post(
        f"{stack}/api/turns",
        json={"message": "too big", "files": [{"name": "huge.bin", "data": oversized}]},
        timeout=60,
    )

    assert response.status_code == 413, response.status_code
    assert "huge.bin" in response.json()["detail"]


def test_ready_work_excludes_blocked_beads(beads):
    """Sequencing must live in edges: `bd ready` is what a fresh session reads."""
    blocker = bd(
        "create", "--title=smoke blocker", "--type=task", "--priority=2", "--json"
    ).stdout
    blocked = bd(
        "create", "--title=smoke blocked", "--type=task", "--priority=2", "--json"
    ).stdout
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


def test_a_second_turn_is_refused_by_the_real_stack(stack):
    """The img-lsp regression, end to end: two tabs used to both get a 202.

    Only the real stack proves this. A unit test can hold Registry.begin to the
    rule, but the rule is worthless if the route reaches it after a stray await
    or a later refactor puts an unguarded constructor back - and both of those
    look completely fine in isolation. Here a second POST arrives while a real
    turn is genuinely in flight in a real process.

    What it protects is the revert button. Savepoints are a `git add -A` over
    one shared workspace, so overlapping turns sweep each other's half-written
    files into the wrong savepoint and reverting either rolls back both.
    """
    wait_until_idle(stack)
    first = httpx.post(f"{stack}/api/turns", json={"message": "hello"}, timeout=30)
    assert first.status_code == 202, first.text

    second = httpx.post(
        f"{stack}/api/turns", json={"message": "also hello"}, timeout=30
    )

    assert second.status_code == 409, second.text
    # The UI renders `detail` verbatim, so it has to explain itself.
    assert "already running" in second.json()["detail"]

    # And the refusal is temporary, not a wedge: the gate reopens on its own.
    wait_until_idle(stack)
    assert httpx.get(f"{stack}/healthz", timeout=10).json()["busy"] is False


# --- the interaction surface, against the real image -------------------------


def test_the_answer_and_permission_routes_reject_what_they_should(stack):
    """Both let a caller unblock a RUNNING agent, so the refusals matter.

    Unit tests cover `Turn.resolve` as arithmetic. What only the real stack
    covers is that these routes exist at all, are wired to the auth dependency,
    and answer 404/409 rather than 500 - a 500 here would mean an unhandled
    InvalidStateError on an ordinary double click.
    """
    for path, body in (
        ("answer", {"request_id": "nope", "answers": []}),
        ("permission", {"request_id": "nope", "decision": "deny"}),
    ):
        missing = httpx.post(
            f"{stack}/api/turns/deadbeef/{path}", json=body, timeout=10
        )
        assert missing.status_code == 404, (path, missing.text)

    # A real turn, so the 409 path is reached rather than the 404 one. The turn
    # fails immediately on the placeholder API key, which is all this needs.
    wait_until_idle(stack)
    started = httpx.post(f"{stack}/api/turns", json={"message": "hello"}, timeout=30)
    assert started.status_code == 202, started.text
    turn_id = started.json()["turn_id"]

    stale = httpx.post(
        f"{stack}/api/turns/{turn_id}/answer",
        json={"request_id": "never-asked", "answers": ["yes"]},
        timeout=10,
    )
    assert stale.status_code == 409, stale.text

    malformed = httpx.post(
        f"{stack}/api/turns/{turn_id}/permission",
        json={"request_id": "x", "decision": "maybe"},
        timeout=10,
    )
    assert malformed.status_code == 400, malformed.text


def test_the_machine_surface_answers_a_real_mcp_handshake(stack):
    """A real client conversation against the real image, over real HTTP.

    This is the only tier where the app's own lifespan runs, and the lifespan is
    what starts the streamable-HTTP session manager. A mounted sub-app's lifespan
    is never run for it, so if that wiring were dropped every request here would
    500 with "Task group is not initialized" - the happy path failing, from code
    that imports and mounts perfectly. The unit tier has to enter the manager by
    hand; only this proves production does it.

    It also covers two things a mount breaks quietly: FastMCP serving at
    /mcp/mcp (a 404 at the published URL) and its localhost-only Host header
    check (a 421 to every real caller).
    """
    health = httpx.get(f"{stack}/healthz", timeout=10).json()
    assert health["mcp"] is True, "the dev stack bypasses auth, so it reports on"

    headers = {"Accept": "application/json, text/event-stream"}
    opened = httpx.post(
        f"{stack}/mcp/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "container-probe", "version": "1"},
            },
        },
        timeout=30,
    )
    assert opened.status_code == 200, opened.text
    session = opened.headers.get("mcp-session-id")
    assert session, f"no session id, so the manager never started: {opened.text}"

    headers["mcp-session-id"] = session
    httpx.post(
        f"{stack}/mcp/",
        headers=headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        timeout=30,
    )
    listed = httpx.post(
        f"{stack}/mcp/",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        timeout=30,
    )
    assert listed.status_code == 200, listed.text
    assert _mcp_tools(listed.text) == {"ingest", "query", "lint", "reflect"}


def _mcp_tools(body: str) -> set[str]:
    """Tool names out of a streamable-HTTP response, which is an SSE stream."""
    for line in body.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[len("data: ") :])
            return {t["name"] for t in payload["result"]["tools"]}
    raise AssertionError(f"no data frame in the response: {body[:300]}")
