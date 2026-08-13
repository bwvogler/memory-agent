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

from .conftest import bd_json

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
