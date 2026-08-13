"""Fast unit tests for signal detection.

Detection is the part of Stage 2 most likely to break silently: it reads tool
argument names out of SDK message shapes, and a rename upstream would empty the
ledger without erroring anywhere. These tests pin the shapes we rely on.
"""

from __future__ import annotations

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
    assert signals.skills_from_tool_use(
        "Read", {"file_path": "/mnt/kb/memory/wiki/notes/tea.md"}
    ) == set()


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
