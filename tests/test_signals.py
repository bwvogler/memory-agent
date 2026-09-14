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

    async def no_skill_totals(self, *args, **kwargs) -> dict:
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


# --- img-b7u: a denied call's target is named, not guessed at later ---------


def test_a_denied_kb_write_names_its_path_in_the_bead(monkeypatch):
    created = []

    async def fake_create_bead(user_slug, title, **kwargs):
        created.append((title, kwargs.get("description", "")))
        return "kb-1"

    monkeypatch.setattr(signals.kb, "create_bead", fake_create_bead)
    monkeypatch.setattr(signals.kb, "list_beads", _empty_list)

    turn = _turn(
        state=TurnState.DONE,
        permission_denials=["Write"],
        human_denials=["Write"],
        denial_details={"Write": ["kb:people/x.md"]},
    )
    asyncio.run(signals.record_turn(turn, "dev_localhost"))

    assert len(created) == 1
    _, body = created[0]
    assert "Target: kb:people/x.md" in body


def test_a_denied_scratch_write_says_it_left_the_workspace(monkeypatch):
    created = []

    async def fake_create_bead(user_slug, title, **kwargs):
        created.append((title, kwargs.get("description", "")))
        return "kb-1"

    monkeypatch.setattr(signals.kb, "create_bead", fake_create_bead)
    monkeypatch.setattr(signals.kb, "list_beads", _empty_list)

    turn = _turn(
        state=TurnState.DONE,
        permission_denials=["Write"],
        human_denials=["Write"],
        denial_details={"Write": ["/work/dev/x.md (outside the KB workspace)"]},
    )
    asyncio.run(signals.record_turn(turn, "dev_localhost"))

    _, body = created[0]
    assert "Target: /work/dev/x.md (outside the KB workspace)" in body


def test_a_denial_with_no_recorded_target_still_says_so(monkeypatch):
    """Pre-img-b7u denials, or ones the CLI refused before our callback saw
    them, must not silently omit the line - that reads as "nothing to see"
    rather than "this predates the fix"."""
    created = []

    async def fake_create_bead(user_slug, title, **kwargs):
        created.append((title, kwargs.get("description", "")))
        return "kb-1"

    monkeypatch.setattr(signals.kb, "create_bead", fake_create_bead)
    monkeypatch.setattr(signals.kb, "list_beads", _empty_list)

    turn = _turn(state=TurnState.DONE, permission_denials=["WebFetch"])
    asyncio.run(signals.record_turn(turn, "dev_localhost"))

    _, body = created[0]
    assert "Target: not recorded" in body


# --- a tool call that failed without taking the whole turn down -------------


def test_a_tool_failure_files_a_weak_evidence_bead(monkeypatch):
    created = []

    async def fake_create_bead(user_slug, title, **kwargs):
        created.append((title, kwargs.get("priority")))
        return "kb-1"

    monkeypatch.setattr(signals.kb, "create_bead", fake_create_bead)
    monkeypatch.setattr(signals.kb, "list_beads", _empty_list)

    turn = _turn(
        state=TurnState.DONE,
        tool_failures=["download_attachment"],
        tool_failure_details={"download_attachment": ["quota exceeded"]},
    )
    asyncio.run(signals.record_turn(turn, "dev_localhost"))

    assert len(created) == 1, created
    title, priority = created[0]
    assert title == "Tool call failed: download_attachment: quota exceeded"
    assert priority == 3, "weak evidence, not a P1 defect report"


def test_a_turn_with_no_tool_failures_files_nothing_extra(monkeypatch):
    created = []

    async def fake_create_bead(user_slug, title, **kwargs):
        created.append(title)
        return "kb-1"

    monkeypatch.setattr(signals.kb, "create_bead", fake_create_bead)
    monkeypatch.setattr(signals.kb, "list_beads", _empty_list)

    turn = _turn(state=TurnState.DONE)
    filed = asyncio.run(signals.record_turn(turn, "dev_localhost"))

    assert filed == []
    assert created == []


def test_identical_tool_failures_dedupe_but_a_different_one_does_not(monkeypatch):
    """Fingerprinted on (tool, first line of error): a flaky integration
    failing the same way every turn must not flood the ledger, but a new
    failure mode for the same tool must not hide behind an old bead either."""
    open_titles = []

    async def fake_create_bead(user_slug, title, **kwargs):
        return "kb-1"

    async def list_open(*args, **kwargs):
        return [{"title": t, "status": "open"} for t in open_titles]

    monkeypatch.setattr(signals.kb, "create_bead", fake_create_bead)
    monkeypatch.setattr(signals.kb, "list_beads", list_open)

    turn = _turn(
        state=TurnState.DONE,
        tool_failures=["download_attachment"],
        tool_failure_details={"download_attachment": ["quota exceeded"]},
    )
    filed = asyncio.run(signals.record_turn(turn, "dev_localhost"))
    assert filed == ["kb-1"]
    open_titles.append("Tool call failed: download_attachment: quota exceeded")

    # Same tool, same error again: deduped against the still-open bead.
    turn = _turn(
        state=TurnState.DONE,
        tool_failures=["download_attachment"],
        tool_failure_details={"download_attachment": ["quota exceeded"]},
    )
    filed = asyncio.run(signals.record_turn(turn, "dev_localhost"))
    assert filed == []

    # Same tool, a genuinely different error: must file, not hide behind it.
    turn = _turn(
        state=TurnState.DONE,
        tool_failures=["download_attachment"],
        tool_failure_details={"download_attachment": ["token expired"]},
    )
    filed = asyncio.run(signals.record_turn(turn, "dev_localhost"))
    assert filed == ["kb-1"]


# --- the bucket no skill can own --------------------------------------------


def _labels_of(monkeypatch, turn) -> tuple[str, ...]:
    """File one turn's signals and return the labels the bead carried."""
    seen: list[tuple[str, ...]] = []

    async def fake_create_bead(user_slug, title, **kwargs):
        seen.append(tuple(kwargs.get("labels") or ()))
        return "kb-1"

    monkeypatch.setattr(signals.kb, "create_bead", fake_create_bead)
    monkeypatch.setattr(signals.kb, "list_beads", _empty_list)
    asyncio.run(signals.record_turn(turn, "dev_localhost"))
    return seen[0]


def test_a_failure_with_no_skill_loaded_is_labelled_as_such(monkeypatch):
    """The label reflection needs to tell two conclusions apart.

    "No skill change is warranted" and "no skill CAN be at fault, because none
    was loaded" read identically in a bead body. Tagged at capture time, the
    second becomes a query rather than a re-derivation from 24 bead bodies -
    which is what a real reflection turn did, before concluding the pattern was
    unfixable and dropping it.
    """
    turn = _turn(state=TurnState.ERROR, error="boom")
    assert signals.NO_SKILL_LABEL in _labels_of(monkeypatch, turn)


def test_a_failure_with_a_skill_loaded_is_not(monkeypatch):
    turn = _turn(state=TurnState.ERROR, error="boom", skills={"kb-curator"})
    assert signals.NO_SKILL_LABEL not in _labels_of(monkeypatch, turn)


class _CountingStore(_BrokenStore):
    """Only the no-skill query answers; everything else is still down."""

    def __init__(self, row: dict) -> None:
        self._row = row

    async def no_skill_totals(self, *args, **kwargs) -> dict:
        return self._row


def test_no_skill_failures_counts_only_the_bad_outcomes():
    """A no-skill turn that went fine is not evidence of anything."""
    signals.attach_store(
        _CountingStore({"turns": 40, "reverted": 2, "errored": 3, "max_turns": 1})
    )
    try:
        assert asyncio.run(signals.no_skill_failures()) == 6
    finally:
        signals.attach_store(None)


def test_no_skill_failures_is_zero_when_the_ledger_is_unreachable():
    """A broken store must relax the reflection guard, never wedge it.

    The count gates a Stop hook. If an unreachable ledger answered anything but
    zero, every reflection turn would be blocked behind a bead it has no
    evidence to write.
    """
    signals.attach_store(_BrokenStore())
    try:
        assert asyncio.run(signals.no_skill_failures()) == 0
    finally:
        signals.attach_store(None)
