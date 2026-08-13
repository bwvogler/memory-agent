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


# --- the overlay for a skill that ships in the image ------------------------
#
# An image skill's text is code, so its lessons live beside it in the KB. Same
# append-only promise, different file, and no frontmatter to argue about.

OVERLAY = """\
# What curating taught us

kb-curator ships in the image, so its lessons live here.

## Learned
"""


def refuse_overlay(current: str, proposed: str) -> str | None:
    return evolve.bounded_overlay_edit(current, proposed)


def test_appending_to_an_overlay_is_allowed():
    assert refuse_overlay(OVERLAY, OVERLAY + "\n- 2026-08-13: a thing.\n") is None


def test_editing_the_overlay_header_is_refused():
    """The header explains what the file is and who may write it. It is as
    load-bearing as a skill body, and just as out of reach."""
    proposed = OVERLAY.replace("lessons live here", "lessons used to live here")
    assert "byte-identical" in (refuse_overlay(OVERLAY, proposed) or "")


def test_rewriting_an_overlay_entry_is_refused():
    current = OVERLAY + "\n- 2026-08-13: one thing.\n"
    proposed = OVERLAY + "\n- 2026-08-13: one thing, revised.\n"
    assert "append-only" in (refuse_overlay(current, proposed) or "")


def test_deleting_an_overlay_entry_is_refused():
    current = OVERLAY + "\n- a\n- b\n"
    assert "append-only" in (refuse_overlay(current, OVERLAY + "\n- a\n") or "")


def test_an_overlay_may_not_become_a_skill():
    """A frontmatter block would make it routable, and a routable overlay is a
    second skill competing with the one it belongs to."""
    proposed = "---\nname: kb-curator\ndescription: x\n---\n\n" + OVERLAY
    assert "data file" in (refuse_overlay(OVERLAY, proposed) or "")


def test_a_no_op_overlay_write_is_refused():
    assert "changes nothing" in (refuse_overlay(OVERLAY, OVERLAY) or "")


# --- which files are reachable at all --------------------------------------


def test_only_kb_skill_files_are_writable(monkeypatch, tmp_path):
    monkeypatch.setattr(evolve.kb, "workspace_root", lambda: tmp_path)
    skills = tmp_path / "skills" / "lint"
    skills.mkdir(parents=True)

    assert evolve.mutable_skill_path(str(skills / "SKILL.md")) is not None
    assert evolve.mutable_skill_path(str(skills / evolve.OVERLAY_FILE)) is not None
    # Reference material next to a skill is not the skill.
    assert evolve.mutable_skill_path(str(skills / "references" / "x.md")) is None
    # The human's schema document.
    assert evolve.mutable_skill_path(str(tmp_path / "AGENT_GUIDE.md")) is None
    # Wiki content is not reflection's business.
    assert evolve.mutable_skill_path(str(tmp_path / "wiki" / "SKILL.md")) is None
    # ...and neither is a LEARNED.md that happens to be filed somewhere else.
    assert evolve.mutable_skill_path(str(tmp_path / "wiki" / "LEARNED.md")) is None


def test_image_skills_are_out_of_reach(monkeypatch, tmp_path):
    """Skills shipped in the image are code: reviewed and deployed atomically.
    An edit there would also vanish on the next deploy, silently. The overlay
    is the way in, and it is in the knowledge base, not next to the image."""
    monkeypatch.setattr(evolve.kb, "workspace_root", lambda: tmp_path)
    assert evolve.mutable_skill_path("/srv/skills/kb-curator/SKILL.md") is None
    assert evolve.mutable_skill_path("/srv/skills/kb-curator/LEARNED.md") is None


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


def test_the_hook_records_an_overlay_append(monkeypatch, tmp_path):
    """The skill name comes from the directory, so the log, the merge and the
    consolidation bead need to know nothing about which shape this was."""
    monkeypatch.setattr(evolve.kb, "workspace_root", lambda: tmp_path)
    path = tmp_path / "skills" / "kb-curator" / evolve.OVERLAY_FILE
    path.parent.mkdir(parents=True)
    path.write_text(OVERLAY, encoding="utf-8")

    turn = _Turn()
    guard = evolve.write_guard_for(turn)
    proposed = OVERLAY + "\n- 2026-08-13: a thing.\n"

    assert _run(guard, "Write", {"file_path": str(path), "content": proposed}) == {}
    change = turn.evolved[0]
    assert change.skill == "kb-curator"
    assert change.described is False
    assert change.learned == 1


def test_refusing_an_image_write_names_the_overlay(monkeypatch, tmp_path):
    """A bare denial is what drove the agent into inventing a shell workaround
    the last time (ADR 0007). The refusal must say where the lesson does go."""
    monkeypatch.setattr(evolve.kb, "workspace_root", lambda: tmp_path)
    guard = evolve.write_guard_for(_Turn())
    out = _run(guard, "Write", {"file_path": "/srv/skills/kb-curator/SKILL.md",
                                "content": SKILL})
    assert evolve.OVERLAY_FILE in out["hookSpecificOutput"]["permissionDecisionReason"]


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


# --- keeping ## Learned prunable -------------------------------------------


class _FakeBd:
    """Records bd calls so the escalation policy can be asserted without bd."""

    def __init__(self, beads=None):
        self.beads = beads if beads is not None else []
        self.created, self.notes, self.priorities = [], [], []

    async def list_beads(self, user_slug, label=None):
        return self.beads

    async def create_bead(self, user_slug, title, description="", priority=2,
                          labels=(), status=None, issue_type="task"):
        self.created.append((title, priority, description))
        return "kb-new"

    async def note_bead(self, user_slug, bead_id, text):
        self.notes.append((bead_id, text))
        return True

    async def set_priority(self, user_slug, bead_id, priority):
        self.priorities.append((bead_id, priority))
        return True


def _patch_bd(monkeypatch, fake):
    for name in ("list_beads", "create_bead", "note_bead", "set_priority"):
        monkeypatch.setattr(evolve.kb, name, getattr(fake, name))


def _learned_change(skill="lint", n=1):
    return evolve.Change(skill, f"/kb/skills/{skill}/SKILL.md", False, n)


def test_appending_a_lesson_asks_for_it_to_be_folded_back_in(monkeypatch):
    """Append-only means the section can only grow, and it loads with the skill
    every time. Something has to ask for it to be pruned."""
    fake = _FakeBd()
    _patch_bd(monkeypatch, fake)

    asyncio.run(evolve.request_consolidation("u", [_learned_change()]))

    assert len(fake.created) == 1
    title, priority, body = fake.created[0]
    assert title == evolve.consolidation_title("lint")
    assert priority == evolve.FIRST_PRIORITY
    # The bead is the only context a future turn gets - it must say why
    # reflection did not just do this itself.
    assert "remit withholds" in body
    assert "read-modify-write" in body


def test_a_description_only_change_asks_for_nothing():
    """Nothing accumulated, so nothing to prune."""
    fake = _FakeBd()
    asyncio.run(evolve.request_consolidation("u", [evolve.Change("lint", "p", True, 0)]))
    assert fake.created == []


def test_more_lessons_escalate_one_bead_rather_than_filing_more(monkeypatch):
    """Five lessons in one skill is one job that got more urgent, not five
    jobs. Filing per lesson would recreate the scrolling list beads replaced."""
    fake = _FakeBd([
        {"id": "kb-abc", "title": evolve.consolidation_title("lint"),
         "status": "open", "priority": 3},
    ])
    _patch_bd(monkeypatch, fake)

    asyncio.run(evolve.request_consolidation("u", [_learned_change(n=2)]))

    assert fake.created == []
    assert fake.priorities == [("kb-abc", 2)]
    assert "2 new entries" in fake.notes[0][1]


def test_escalation_stops_at_the_top(monkeypatch):
    fake = _FakeBd([
        {"id": "kb-abc", "title": evolve.consolidation_title("lint"),
         "status": "open", "priority": evolve.MOST_URGENT},
    ])
    _patch_bd(monkeypatch, fake)
    asyncio.run(evolve.request_consolidation("u", [_learned_change()]))
    assert fake.priorities == []
    assert fake.notes  # still says the section grew


def test_a_closed_bead_does_not_suppress_a_new_one(monkeypatch):
    """Consolidation done once does not mean it never needs doing again."""
    fake = _FakeBd([
        {"id": "kb-old", "title": evolve.consolidation_title("lint"),
         "status": "closed", "priority": 3},
    ])
    _patch_bd(monkeypatch, fake)
    asyncio.run(evolve.request_consolidation("u", [_learned_change()]))
    assert len(fake.created) == 1


def _overlay_change(skill="kb-curator", n=1):
    return evolve.Change(skill, f"/kb/skills/{skill}/{evolve.OVERLAY_FILE}", False, n)


def test_an_overlay_asks_to_be_pruned_not_folded_in(monkeypatch):
    """There is no body to fold into: the skill ships in the image. The bead
    has to say so, or a future turn will try and be refused."""
    fake = _FakeBd()
    _patch_bd(monkeypatch, fake)

    asyncio.run(evolve.request_consolidation("u", [_overlay_change()]))

    title, priority, body = fake.created[0]
    assert title == evolve.consolidation_title("kb-curator", overlay=True)
    assert title != evolve.consolidation_title("kb-curator")
    assert priority == evolve.FIRST_PRIORITY
    assert "ships in the image" in body
    # The escape hatch for a lesson that has outgrown the overlay.
    assert "skills/kb-curator/SKILL.md" in body


def test_the_two_shapes_do_not_share_a_bead(monkeypatch):
    """Same directory name, genuinely different jobs. One escalating bead each,
    rather than two rows in the ledger that read identically."""
    fake = _FakeBd()
    _patch_bd(monkeypatch, fake)

    asyncio.run(evolve.request_consolidation(
        "u", [_learned_change("probe"), _overlay_change("probe")]
    ))

    assert len({title for title, _, _ in fake.created}) == 2


def test_an_unreachable_ledger_does_not_break_reflection(monkeypatch):
    fake = _FakeBd()
    _patch_bd(monkeypatch, fake)
    monkeypatch.setattr(evolve.kb, "list_beads", lambda *a, **k: _none())
    asyncio.run(evolve.request_consolidation("u", [_learned_change()]))
    assert fake.created == []


async def _none():
    return None
