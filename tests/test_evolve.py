"""The self-modification remit.

`bounded_skill_edit` is the whole policy: everything a reflection turn is
allowed to do to its own instructions. It is enforced in a hook rather than an
instruction because ADR 0007 records two rules the model agreed with and broke
anyway, and this is the worst place to rely on a promise - a bad self-edit
damages every later turn, silently.

So the refusals below are not paranoia, they are the specification. The
allowances matter just as much: a remit so tight that nothing useful fits
produces a reflection loop that can only ever fail, which teaches nothing and
wastes a turn every time a signal arrives.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app import evolve

SKILL = """\
---
name: lint
description: >
  Check the wiki for defects.
---

# Linting the wiki

Read every page and look for contradictions.

## Checks

- Undated claims.
"""


def refuse(current: str, proposed: str) -> str | None:
    return evolve.bounded_skill_edit(current, proposed)


# --- allowed ---------------------------------------------------------------


def test_rewriting_the_description_is_allowed():
    """The whole point: a skill that under-triggers is broken here."""
    proposed = SKILL.replace(
        "  Check the wiki for defects.",
        '  Check the wiki for defects. Triggers include "lint the wiki".',
    )
    assert refuse(SKILL, proposed) is None


def test_adding_a_learned_section_is_allowed():
    proposed = SKILL + "\n## Learned\n\n- 2026-08-13: dated claims matter.\n"
    assert refuse(SKILL, proposed) is None


def test_appending_to_an_existing_learned_section_is_allowed():
    current = SKILL + "\n## Learned\n\n- 2026-08-13: one thing.\n"
    proposed = current + "- 2026-08-14: another thing.\n"
    assert refuse(current, proposed) is None


def test_both_at_once_is_allowed():
    proposed = SKILL.replace("Check the wiki", "Check the whole wiki")
    proposed += "\n## Learned\n\n- 2026-08-13: something.\n"
    assert refuse(SKILL, proposed) is None


def test_a_trailing_newline_difference_is_not_an_edit():
    """The store normalises this. Treating it as tampering would refuse every
    write - see ADR 0007 on the false `Write verification failed`."""
    proposed = SKILL.replace("Check the wiki", "Check the whole wiki").rstrip("\n")
    assert refuse(SKILL, proposed) is None


# --- refused ---------------------------------------------------------------


def test_editing_the_body_is_refused():
    proposed = SKILL.replace(
        "Read every page and look for contradictions.",
        "Read every page carefully and look for contradictions.",
    )
    assert "byte-identical" in (refuse(SKILL, proposed) or "")


def test_deleting_body_content_is_refused():
    proposed = SKILL.replace("- Undated claims.\n", "")
    assert refuse(SKILL, proposed) is not None


def test_rewriting_an_existing_learned_entry_is_refused():
    """Append-only is what makes the section auditable and non-destructive."""
    current = SKILL + "\n## Learned\n\n- 2026-08-13: one thing.\n"
    proposed = SKILL + "\n## Learned\n\n- 2026-08-13: one thing, revised.\n"
    assert "append-only" in (refuse(current, proposed) or "")


def test_deleting_a_learned_entry_is_refused():
    current = SKILL + "\n## Learned\n\n- a\n- b\n"
    proposed = SKILL + "\n## Learned\n\n- a\n"
    assert "append-only" in (refuse(current, proposed) or "")


def test_renaming_the_skill_is_refused():
    """`name` is the identity other things reference. Only description moves."""
    proposed = SKILL.replace("name: lint", "name: linter")
    assert "may not be changed" in (refuse(SKILL, proposed) or "")


def test_adding_a_frontmatter_field_is_refused():
    proposed = SKILL.replace("name: lint", "name: lint\nallowed-tools: Bash")
    assert "added, removed or reordered" in (refuse(SKILL, proposed) or "")


def test_dropping_the_frontmatter_entirely_is_refused():
    assert "frontmatter" in (refuse(SKILL, "# Linting the wiki\n") or "")


def test_replacing_the_whole_file_is_refused():
    assert refuse(SKILL, "---\nname: lint\ndescription: x\n---\n\n# New\n") is not None


def test_a_no_op_write_is_refused():
    """Not a safety property - it keeps the evolution log honest, and stops a
    reflection turn 'succeeding' without having done anything."""
    assert "changes nothing" in (refuse(SKILL, SKILL) or "")


def test_smuggling_a_body_edit_under_a_learned_heading_is_refused():
    """The obvious way around an append-only rule: move the heading up so the
    text you want to rewrite falls inside the appendable region."""
    proposed = SKILL.replace(
        "# Linting the wiki",
        "# Linting the wiki\n\n## Learned",
    )
    assert refuse(SKILL, proposed) is not None


# --- which files are reachable at all --------------------------------------


def test_only_kb_skill_files_are_writable(monkeypatch, tmp_path):
    monkeypatch.setattr(evolve.kb, "workspace_root", lambda: tmp_path)
    skills = tmp_path / "skills" / "lint"
    skills.mkdir(parents=True)

    assert evolve.mutable_skill_path(str(skills / "SKILL.md")) is not None
    # Reference material next to a skill is not the skill.
    assert evolve.mutable_skill_path(str(skills / "references" / "x.md")) is None
    # The human's schema document.
    assert evolve.mutable_skill_path(str(tmp_path / "AGENT_GUIDE.md")) is None
    # Wiki content is not reflection's business.
    assert evolve.mutable_skill_path(str(tmp_path / "wiki" / "SKILL.md")) is None


def test_image_skills_are_out_of_reach(monkeypatch, tmp_path):
    """Skills shipped in the image are code: reviewed and deployed atomically.
    An edit there would also vanish on the next deploy, silently."""
    monkeypatch.setattr(evolve.kb, "workspace_root", lambda: tmp_path)
    assert evolve.mutable_skill_path("/srv/skills/kb-curator/SKILL.md") is None


def test_traversal_out_of_the_skills_dir_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(evolve.kb, "workspace_root", lambda: tmp_path)
    assert (
        evolve.mutable_skill_path(f"{tmp_path}/skills/../../etc/SKILL.md") is None
    )


# --- the hook --------------------------------------------------------------


class _Turn:
    def __init__(self):
        self.evolved = []
        self.guard_denials = []


def _run(guard, tool: str, tool_input: dict) -> dict:
    return asyncio.run(guard({"tool_name": tool, "tool_input": tool_input}, None, None))


def test_the_hook_records_what_it_allows(monkeypatch, tmp_path):
    """The evolution log and the immune memory are both built from this."""
    monkeypatch.setattr(evolve.kb, "workspace_root", lambda: tmp_path)
    path = tmp_path / "skills" / "lint" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(SKILL, encoding="utf-8")

    turn = _Turn()
    guard = evolve.write_guard_for(turn)
    proposed = SKILL.replace("Check the wiki", "Check the whole wiki")
    proposed += "\n## Learned\n\n- 2026-08-13: a thing.\n"

    assert _run(guard, "Write", {"file_path": str(path), "content": proposed}) == {}
    assert len(turn.evolved) == 1
    change = turn.evolved[0]
    assert change.skill == "lint"
    assert change.described is True
    assert change.learned == 1
    assert "rewrote description" in change.summary()


def test_the_hook_refuses_edit_and_says_what_to_use(monkeypatch, tmp_path):
    monkeypatch.setattr(evolve.kb, "workspace_root", lambda: tmp_path)
    guard = evolve.write_guard_for(_Turn())
    out = _run(guard, "Edit", {"file_path": "x", "old_string": "a", "new_string": "b"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Write" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_refusals_are_recorded_as_ours(monkeypatch, tmp_path):
    """Reflection runs with a deliberately narrow surface, so it hits refusals
    routinely. Each one must not become a bead claiming a deployment defect."""
    monkeypatch.setattr(evolve.kb, "workspace_root", lambda: tmp_path)
    turn = _Turn()
    guard = evolve.write_guard_for(turn)
    _run(guard, "Edit", {"file_path": "x"})
    _run(guard, "Write", {"file_path": str(tmp_path / "AGENT_GUIDE.md"), "content": "x"})
    assert turn.guard_denials == ["Edit", "Write"]


def test_the_hook_refuses_a_write_outside_the_remit(monkeypatch, tmp_path):
    monkeypatch.setattr(evolve.kb, "workspace_root", lambda: tmp_path)
    guard = evolve.write_guard_for(_Turn())
    out = _run(guard, "Write", {"file_path": str(tmp_path / "AGENT_GUIDE.md"),
                                "content": "anything"})
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "file a bead" in reason.lower()


def test_the_hook_refuses_creating_a_new_skill(monkeypatch, tmp_path):
    """Reflection improves what exists. A new skill is a human's decision."""
    monkeypatch.setattr(evolve.kb, "workspace_root", lambda: tmp_path)
    (tmp_path / "skills").mkdir(parents=True)
    guard = evolve.write_guard_for(_Turn())
    out = _run(guard, "Write", {"file_path": str(tmp_path / "skills/new/SKILL.md"),
                                "content": SKILL})
    assert "does not exist" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_reading_is_untouched(monkeypatch, tmp_path):
    monkeypatch.setattr(evolve.kb, "workspace_root", lambda: tmp_path)
    guard = evolve.write_guard_for(_Turn())
    assert _run(guard, "Read", {"file_path": "/anything"}) == {}
    assert _run(guard, "Bash", {"command": "bd ready --json"}) == {}


def test_a_broken_guard_does_not_break_the_turn():
    guard = evolve.write_guard_for(_Turn())
    assert asyncio.run(guard({}, None, None)) == {}


# --- the visible record ----------------------------------------------------


def test_the_evolution_log_is_newest_first(monkeypatch, tmp_path):
    monkeypatch.setattr(evolve.kb, "workspace_root", lambda: tmp_path)
    change = evolve.Change("lint", "/kb/skills/lint/SKILL.md", True, 0)

    asyncio.run(evolve.log_changes([change], "reflect-aaa", "signal kb-1"))
    asyncio.run(evolve.log_changes([change], "reflect-bbb", "manual"))

    text = (tmp_path / evolve.EVOLUTION_LOG).read_text()
    assert text.index("reflect-bbb") < text.index("reflect-aaa")
    assert text.startswith("# Evolution log")
    assert text.endswith("\n")  # ADR 0007: a missing newline cries wolf
    assert "rewrote description" in text


def test_nothing_is_logged_when_nothing_changed(monkeypatch, tmp_path):
    monkeypatch.setattr(evolve.kb, "workspace_root", lambda: tmp_path)
    asyncio.run(evolve.log_changes([], "reflect-aaa", "manual"))
    assert not (tmp_path / evolve.EVOLUTION_LOG).exists()


def test_repeated_edits_to_one_skill_collapse_to_their_net_effect():
    """Observed live: a reflection turn wrote the same file twice - once, then
    again after the store's re-serialisation changed how it read back. Both
    writes were permitted and it was one net change, but the log said two."""
    a = evolve.Change("probe", "/kb/skills/probe/SKILL.md", True, 1)
    b = evolve.Change("probe", "/kb/skills/probe/SKILL.md", True, 1)
    other = evolve.Change("lint", "/kb/skills/lint/SKILL.md", False, 2)

    merged = evolve.merge([a, b, other])
    assert len(merged) == 2
    probe = next(c for c in merged if c.skill == "probe")
    assert probe.described is True
    assert probe.learned == 2  # entries do accumulate; the file does not
