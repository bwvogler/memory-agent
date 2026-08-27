"""How a skill becomes reachable at all.

This file exists because of a measured failure, not a hypothetical one. Three
live turns recorded ZERO skills used - `kb-curator` had never loaded on any
turn, despite CLAUDE.md and ADR 0018 both describing it as loading on every
one. `add_dirs` grants access to a path; it does not make the path a skill
source, and `setting_sources=[]` rules out the CLI's own scan. Even
`skills="all"` changed nothing, because it enables *discovered* skills and
nothing was discoverable.

So the system prompt is the routing table, and these tests are what keep it
honest. The live tier deliberately does not assert which skills a turn used -
that is model judgment and would flake - which is exactly why nothing caught
the original defect. Everything here is mechanism.
"""

from __future__ import annotations

import pytest

from app import agent, evolve, kb

SKILL = """\
---
name: {name}
description: >
  Does the {name} thing across two lines.
  Use when someone asks for {name}.
---

# {name}

Body that must never reach the system prompt.
"""


def _write(root, name, text=None):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        text if text is not None else SKILL.format(name=name)
    )
    return directory


@pytest.fixture
def skills(tmp_path, monkeypatch):
    """An image skills dir and a mounted KB skills dir, independently loaded."""
    image = tmp_path / "image-skills"
    workspace = tmp_path / "memory"
    (workspace / "skills").mkdir(parents=True)
    image.mkdir()
    monkeypatch.setattr(agent, "SKILLS_DIR", image)
    monkeypatch.setattr(kb, "is_mounted", lambda: True)
    monkeypatch.setattr(kb, "workspace_root", lambda: workspace)
    return image, workspace / "skills"


def test_both_tiers_reach_the_prompt_with_an_absolute_path(skills):
    """A bootstrap skill is not routed and an image skill is not either, so the
    only thing that makes either reachable is its path being written down."""
    image, kb_skills = skills
    _write(image, "kb-curator")
    _write(kb_skills, "views")

    listing = agent._read_skills()

    assert str(image / "kb-curator" / "SKILL.md") in listing
    assert str(kb_skills / "views" / "SKILL.md") in listing


def test_the_description_is_offered_and_the_body_is_not(skills):
    """Level one of progressive disclosure, and only level one. Inlining the
    body would put every skill's full procedure in every turn."""
    image, _ = skills
    _write(image, "views")

    listing = agent._read_skills()

    assert "Use when someone asks for views." in listing
    assert "must never reach the system prompt" not in listing


def test_a_folded_description_arrives_as_one_line(skills):
    """Skills ship `description: >` and the store unfolds it. Both shapes have
    to render identically or the listing is unreadable in one of them."""
    image, _ = skills
    _write(image, "views")

    entry = [ln for ln in agent._read_skills().splitlines() if "Does the views" in ln]

    assert len(entry) == 1, entry
    assert "Does the views thing across two lines. Use when" in entry[0]


def test_an_evolved_description_takes_effect_without_a_restart(skills):
    """Reflection's ENTIRE permitted change is a skill's description. Caching
    the listing would route on the old text forever and make the one thing
    evolve.py allows a no-op."""
    _, kb_skills = skills
    directory = _write(kb_skills, "views")
    before = agent._read_skills()

    (directory / "SKILL.md").write_text(
        SKILL.format(name="views").replace(
            "Does the views thing", "REWRITTEN BY REFLECTION"
        )
    )

    assert "REWRITTEN BY REFLECTION" not in before
    assert "REWRITTEN BY REFLECTION" in agent._read_skills()


def test_an_image_skill_is_not_displaced_by_its_kb_overlay_directory(skills):
    """`memory/skills/kb-curator/` holds only LEARNED.md. Letting the KB tier
    win by name would drop the real skill and offer nothing in its place."""
    image, kb_skills = skills
    _write(image, "kb-curator")
    (kb_skills / "kb-curator").mkdir()
    (kb_skills / "kb-curator" / "LEARNED.md").write_text("## Learned\n")

    listing = agent._read_skills()

    assert str(image / "kb-curator" / "SKILL.md") in listing
    assert str(kb_skills / "kb-curator") not in listing


def test_a_skill_with_no_description_is_left_out_rather_than_listed_blank(skills):
    image, _ = skills
    _write(image, "broken", text="# no frontmatter at all\n")
    _write(image, "views")

    listing = agent._read_skills()

    assert "broken" not in listing
    assert "views" in listing


def test_an_unmounted_kb_still_offers_the_image_skills(skills, monkeypatch):
    """The KB can be down; the image never is. Returning nothing here would
    silently strip the agent of the skill that teaches it to use the KB."""
    image, _ = skills
    _write(image, "kb-curator")
    monkeypatch.setattr(kb, "is_mounted", lambda: False)

    assert "kb-curator" in agent._read_skills()


def test_no_skills_anywhere_is_empty_and_warned_rather_than_a_stray_heading(
    skills, caplog
):
    listing = agent._read_skills()

    assert listing == ""
    assert any("no SKILL.md found" in r.message for r in caplog.records)


def test_one_enormous_description_cannot_crowd_out_the_others(skills):
    image, _ = skills
    _write(
        image,
        "hog",
        text="---\nname: hog\ndescription: " + ("x" * 5000) + "\n---\n\nbody\n",
    )

    listing = agent._read_skills()

    assert len(listing) < 2000
    assert "…" in listing


def test_the_listing_reaches_the_system_prompt(skills):
    """The unit under test is worthless if nothing appends it."""
    image, _ = skills
    _write(image, "views")

    assert "views" in agent._system_prompt_append()


def test_the_real_shipped_skills_all_offer_a_description():
    """Guards the actual files, not a fixture: a skill whose description fails
    to parse is silently unreachable, which is the defect this file is about."""
    for skill in sorted(agent.SKILLS_DIR.glob("*/SKILL.md")):
        assert evolve.description_of(skill.read_text(encoding="utf-8")), skill
    for skill in sorted((agent.BOOTSTRAP_DIR / "skills").glob("*/SKILL.md")):
        assert evolve.description_of(skill.read_text(encoding="utf-8")), skill
