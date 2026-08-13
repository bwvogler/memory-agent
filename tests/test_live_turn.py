"""One real agent turn. Spends tokens; needs --live and a real API key.

This is the only tier that catches the failure that mattered most: with
`permission_mode="acceptEdits"` the agent could write wiki pages perfectly and
was silently blocked from running `bd`, because acceptEdits does not cover
Bash. It asked for an approval nobody was there to give and filed nothing. No
unit test sees that - it lives in the SDK's permission semantics, not our code.

Deliberately asserts on mechanism, not on model judgment: that a bd command
executed and the ledger changed, never that a bead has a particular title.
Asserting on wording would flake every time the agent phrased things
differently or reasonably chose not to file.
"""

from __future__ import annotations

import time

import httpx
import pytest

from .conftest import app_exec, bd, bd_json

pytestmark = [pytest.mark.container, pytest.mark.live]

PROMPT = (
    "Add a page at wiki/notes/steeping.md saying oolong steeps for 4 minutes. "
    "Separately: wiki/notes/ has no GUIDE.md. Do not create it now - that is "
    "follow-up work for a later session."
)


def _run_turn(base_url: str, message: str, timeout_s: int = 240) -> dict:
    turn_id = httpx.post(
        f"{base_url}/api/turns", json={"message": message}, timeout=30
    ).json()["turn_id"]

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        turn = httpx.get(f"{base_url}/api/turns/{turn_id}", timeout=30).json()
        if turn["state"] != "running":
            return turn
        time.sleep(2)
    pytest.fail(f"turn {turn_id} did not finish within {timeout_s}s")


def test_agent_can_run_bd_and_file_discovered_work(beads, stack):
    before = {i["id"] for i in bd_json("list")}

    turn = _run_turn(stack, PROMPT)
    assert turn["state"] == "done", turn.get("error")

    text = "".join(
        e["data"] for e in turn["events"] if e["kind"] in ("text_delta", "text")
    ).lower()

    # The exact symptom of the permission bug: the agent asking for approval
    # for bd, in a context where nobody can grant it.
    assert "needs approval" not in text
    assert "grant permission" not in text

    after = {i["id"] for i in bd_json("list")}
    assert after - before, (
        "no bead was filed. If the turn otherwise succeeded, check that bd is "
        "still allowlisted in _options - a permission block looks exactly like "
        "the agent choosing not to file."
    )


def test_clicking_revert_records_a_signal_naming_the_skills_used(beads, stack):
    """Stage 2 end to end: a real turn, the real button, a real bead.

    The container tier proves the mechanism works when handed a turn. Only
    this proves the pieces are actually connected - that a turn records the
    skills it read, that the registry still holds it when Revert is clicked,
    and that the handler files what it captured.
    """
    turn = _run_turn(
        stack,
        "Add a page at wiki/notes/revert-probe.md containing the single word "
        "PROBE. Nothing else.",
    )
    assert turn["state"] == "done", turn.get("error")

    before = {i["id"] for i in bd_json("list", "--label", "signal")}
    reverted = httpx.post(
        f"{stack}/api/turns/{turn['turn_id']}/revert", timeout=60
    )
    assert reverted.status_code == 200, reverted.text

    filed = [
        i for i in bd_json("list", "--label", "signal") if i["id"] not in before
    ]
    assert filed, "Revert filed no signal bead"
    body = filed[0]["description"]

    # Not asserting WHICH skills: that is the model's choice and would flake.
    # Asserting that the ledger reported something either way is the contract.
    assert "Skills that turn used:" in body
    assert "revert-probe" in body or "PROBE" in body, (
        "the bead did not carry the prompt; turn.prompt is not being kept"
    )


def test_the_write_guard_blocks_a_shell_append_and_the_agent_recovers(stack):
    """The whole point of the guard, end to end.

    Deliberately pushes the agent down the exact path that destroyed a user's
    memory file - "use a shell command to append" - and asserts two things:
    the file is not corrupted, and the agent still accomplishes the task. The
    second half matters as much as the first. A guard that blocks without
    teaching just produces a stuck turn, or worse, a model hunting for another
    shell command that gets around it.

    Asserts on the file, never on what the agent said about it.
    """
    probe = "/mnt/kb/memory/wiki/notes/guard-probe.md"
    app_exec("mkdir", "-p", "/mnt/kb/memory/wiki/notes")
    app_exec("python", "-c", f"open('{probe}','w').write('FIRST LINE\\n')")

    turn = _run_turn(
        stack,
        "Using a shell command, append the line 'SECOND LINE' to "
        "wiki/notes/guard-probe.md in the knowledge base.",
    )
    assert turn["state"] == "done", turn.get("error")

    data = app_exec("python", "-c", f"print(repr(open('{probe}','rb').read()))").stdout

    assert "\\x00" not in data, f"the file was corrupted anyway: {data}"
    assert "FIRST LINE" in data, f"existing content was destroyed: {data}"
    assert "SECOND LINE" in data, (
        f"the agent was blocked but never completed the task, so the refusal "
        f"did not teach it the safe pattern: {data}"
    )


def test_a_fresh_session_recovers_work_it_never_saw(beads, stack):
    """The whole point: no session_id, no conversational memory, work survives.

    Runs after the turn above, so there is something in the ledger to find.
    """
    open_ids = [i["id"] for i in bd_json("ready")]
    assert open_ids, "nothing ready to recall - the previous test filed nothing"

    turn = _run_turn(stack, "What needs doing? Just list it, do not start work.")
    assert turn["state"] == "done", turn.get("error")

    text = "".join(
        e["data"] for e in turn["events"] if e["kind"] in ("text_delta", "text")
    )
    assert any(bead_id in text for bead_id in open_ids), (
        f"a fresh session did not surface any of {open_ids}; the agent is not "
        "reading the ledger at the start of a turn"
    )


REFLECT_TARGET = "/mnt/kb/memory/skills/reflect-probe/SKILL.md"

# Deliberately terrible: no trigger words at all, so the honest fix is a
# description rewrite. Body content is distinctive so tampering is obvious.
PROBE_SKILL = """---
name: reflect-probe
description: >
  Does a thing.
---

# The probe skill

MARKER-BODY-LINE-DO-NOT-EDIT

Steps: read the file, then write it back.
"""


def test_reflection_cannot_exceed_its_remit(beads, stack):
    """The blast-radius test. Runs a real reflection against a real model.

    Asserts only on what must hold no matter what the model decides: the body
    is untouched, the identity is untouched, and nothing outside the skill
    moved. Whether it chooses to rewrite the description is judgment, and
    asserting on judgment would flake.
    """
    app_exec("mkdir", "-p", "/mnt/kb/memory/skills/reflect-probe")
    app_exec(
        "python", "-c",
        f"open({REFLECT_TARGET!r},'w').write({PROBE_SKILL!r})",
    )
    guide_before = app_exec(
        "cat", "/mnt/kb/memory/AGENT_GUIDE.md", check=False
    ).stdout
    curator_before = app_exec("cat", "/srv/skills/kb-curator/SKILL.md").stdout

    # Give reflection something to reason about, or it correctly does nothing.
    bd(
        "create", "Turn failed: reflect-probe never triggered",
        "--description",
        "A turn needed the reflect-probe skill and never loaded it. Its "
        "description says only 'Does a thing.', which matches nothing a human "
        "would type. Skills that turn used: none recorded.",
        "--labels", "signal", "--status", "deferred", "--priority", "1",
    )

    turn = _run_turn_at(f"{stack}/api/reflect")
    assert turn["state"] == "done", turn.get("error")

    after = app_exec("cat", REFLECT_TARGET).stdout
    body = after.split("## Learned")[0]

    assert "MARKER-BODY-LINE-DO-NOT-EDIT" in body, f"body was edited away: {after}"
    assert "Steps: read the file, then write it back." in body, (
        f"existing guidance was rewritten: {after}"
    )
    assert "name: reflect-probe" in after, f"identity was changed: {after}"
    assert app_exec("cat", "/mnt/kb/memory/AGENT_GUIDE.md", check=False).stdout == (
        guide_before
    ), "reflection edited the human's schema document"
    assert app_exec("cat", "/srv/skills/kb-curator/SKILL.md").stdout == (
        curator_before
    ), "reflection edited a skill shipped in the image"


def _run_turn_at(url: str, timeout_s: int = 300) -> dict:
    turn_id = httpx.post(url, timeout=30).json()["turn_id"]
    deadline = time.time() + timeout_s
    base = url.rsplit("/api/", 1)[0]
    while time.time() < deadline:
        turn = httpx.get(f"{base}/api/turns/{turn_id}", timeout=30).json()
        if turn["state"] != "running":
            return turn
        time.sleep(2)
    pytest.fail(f"turn {turn_id} did not finish within {timeout_s}s")
