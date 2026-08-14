"""Fast unit tests for signal detection.

Detection is the part of Stage 2 most likely to break silently: it reads tool
argument names out of SDK message shapes, and a rename upstream would empty the
ledger without erroring anywhere. These tests pin the shapes we rely on.
"""

from __future__ import annotations

import asyncio

from app import signals
from app.turns import Turn, TurnState


def test_read_of_a_skill_file_is_attributed_to_the_skill():
    found = signals.skills_from_tool_use(
        "Read", {"file_path": "/app/skills/kb-curator/SKILL.md"}
    )
    assert found == {"kb-curator"}


def test_skill_tool_is_attributed_without_a_path():
    assert signals.skills_from_tool_use("Skill", {"skill": "lint"}) == {"lint"}


def test_kb_hosted_skills_are_attributed_too():
    """Bootstrap skills live in the KB, not the image, and still count."""
    found = signals.skills_from_tool_use(
        "Read", {"file_path": "/mnt/kb/memory/skills/ingest/SKILL.md"}
    )
    assert found == {"ingest"}


def test_ordinary_file_reads_are_not_skills():
    assert (
        signals.skills_from_tool_use(
            "Read", {"file_path": "/mnt/kb/memory/wiki/notes/tea.md"}
        )
        == set()
    )


def test_a_bare_skill_md_with_no_parent_directory_is_ignored():
    """Guards the parts[-2] index rather than letting it throw."""
    assert signals.skills_from_tool_use("Read", {"file_path": "SKILL.md"}) == set()


def test_non_string_arguments_do_not_break_detection():
    found = signals.skills_from_tool_use(
        "Edit",
        {"file_path": "/app/skills/lint/SKILL.md", "replace_all": True, "n": 3},
    )
    assert found == {"lint"}


def _turn(**kwargs) -> Turn:
    turn = Turn(id="t1", user_email="dev@localhost")
    for key, value in kwargs.items():
        setattr(turn, key, value)
    return turn


def test_outcome_is_ok_for_a_clean_turn():
    assert signals._outcome(_turn(state=TurnState.DONE)) == signals.OUTCOME_OK


def test_outcome_reports_max_turns_exhaustion():
    turn = _turn(state=TurnState.DONE, terminal_reason="max_turns")
    assert signals._outcome(turn) == signals.OUTCOME_MAX_TURNS


def test_an_errored_turn_outranks_its_terminal_reason():
    turn = _turn(state=TurnState.ERROR, terminal_reason="max_turns")
    assert signals._outcome(turn) == signals.OUTCOME_ERROR


def test_permission_denials_are_read_from_either_shape():
    """The denial payload is opaque and has changed shape before."""
    assert signals._denied_tool({"tool_name": "Bash"}) == "Bash"
    assert signals._denied_tool({"tool": "Bash"}) == "Bash"
    assert signals._denied_tool(object()) == "unknown"


def test_long_prompts_are_clipped_for_bead_bodies():
    clipped = signals._clip("x" * 900, signals.MAX_PROMPT_CHARS)
    assert len(clipped) == signals.MAX_PROMPT_CHARS
    assert clipped.endswith("...")


def test_skill_list_is_readable_when_nothing_was_recorded():
    assert signals._skill_list(_turn()) == "none recorded"
    assert signals._skill_list(_turn(skills={"b", "a"})) == "a, b"


# --- the ledger and the evidence fail independently -------------------------


class _BrokenStore:
    """A store that is down, not one method of which happens to fail.

    Every method on the TurnOutcomeStore protocol raises, so this stays a
    faithful stand-in as that protocol grows rather than quietly reverting to
    a working store for whatever was added.
    """

    async def record_turn_outcome(self, *args, **kwargs) -> None:
        raise RuntimeError("session store is down")

    async def mark_turn_outcome(self, *args, **kwargs) -> None:
        raise RuntimeError("session store is down")

    async def skill_signal_summary(self, *args, **kwargs) -> list[dict]:
        raise RuntimeError("session store is down")

    async def turn_totals(self, *args, **kwargs) -> dict:
        raise RuntimeError("session store is down")


def test_a_broken_ledger_does_not_swallow_the_signal_beads(monkeypatch):
    """Regression, and the failure mode was the usual one here: silence.

    The store write and the bead filing shared one `try`, with the store first.
    A session store that was down therefore filed no beads at all and left one
    log line behind - so a turn that hit a permission denial, the signal most
    likely to be a real deployment defect, recorded nothing anywhere. It
    surfaced as two container tests failing together and reading like two
    unrelated flakes.
    """
    created = []

    async def fake_create_bead(user_slug, title, **kwargs):
        created.append(title)
        return "kb-1"

    monkeypatch.setattr(signals.kb, "create_bead", fake_create_bead)
    monkeypatch.setattr(signals.kb, "list_beads", _empty_list)
    signals.attach_store(_BrokenStore())
    try:
        turn = _turn(state=TurnState.DONE, permission_denials=["Bash"])
        filed = asyncio.run(signals.record_turn(turn, "dev_localhost"))
    finally:
        signals.attach_store(None)

    assert filed == ["kb-1"]
    assert "Bash" in created[0]


async def _empty_list(*args, **kwargs):
    return []


# --- a person saying no is not a deployment defect ---------------------------


def test_a_human_denial_does_not_file_a_p1_deployment_defect(monkeypatch):
    """The SDK reports a human Deny through the same channel as a missing
    allowlist entry. Before this distinction, clicking Deny filed a P1 bead
    telling a future reflection to go and 'check allowed_tools in _options' -
    against a person who had simply said no.
    """
    created = []

    async def fake_create_bead(user_slug, title, **kwargs):
        created.append((title, kwargs.get("priority")))
        return f"kb-{len(created)}"

    monkeypatch.setattr(signals.kb, "create_bead", fake_create_bead)
    monkeypatch.setattr(signals.kb, "list_beads", _empty_list)

    turn = _turn(
        state=TurnState.DONE,
        permission_denials=["Bash"],
        human_denials=["Bash"],
    )
    asyncio.run(signals.record_turn(turn, "dev_localhost"))

    assert len(created) == 1, created
    title, priority = created[0]
    assert "human refused" in title.lower()
    assert priority == 3, "evidence, not a P1 defect report"
    assert not any("allowed_tools" in t for t, _ in created)


def test_an_unexplained_denial_is_still_a_p1(monkeypatch):
    """The original signal must survive the new subtraction."""
    created = []

    async def fake_create_bead(user_slug, title, **kwargs):
        created.append((title, kwargs.get("priority")))
        return "kb-1"

    monkeypatch.setattr(signals.kb, "create_bead", fake_create_bead)
    monkeypatch.setattr(signals.kb, "list_beads", _empty_list)

    turn = _turn(state=TurnState.DONE, permission_denials=["WebFetch"])
    asyncio.run(signals.record_turn(turn, "dev_localhost"))

    assert created == [("Agent was denied permission to use: WebFetch", 1)]


def test_repeated_refusals_are_each_recorded(monkeypatch):
    """Not deduped, for the same reason reverts are not: the count is the data."""

    async def fake_create_bead(user_slug, title, **kwargs):
        return "kb-1"

    async def already_open(*args, **kwargs):
        return [{"title": "The human refused a tool: Bash", "status": "open"}]

    monkeypatch.setattr(signals.kb, "create_bead", fake_create_bead)
    monkeypatch.setattr(signals.kb, "list_beads", already_open)

    turn = _turn(state=TurnState.DONE, human_denials=["Bash"])
    filed = asyncio.run(signals.record_turn(turn, "dev_localhost"))

    assert filed == ["kb-1"], "an open bead with the same title must not suppress this"
