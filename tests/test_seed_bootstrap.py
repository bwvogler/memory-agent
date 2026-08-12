"""The bootstrap seeding upgrade matrix.

Seeding has to thread a needle: bootstrap skills live in the KB so the human
can improve them, so we must not clobber their edits - but never overwriting
means a shipped fix silently never reaches an existing deployment while the
seeder still logs success. The hash-tracking logic that resolves this is the
only real branching in agent.py, and getting it wrong destroys the human's work
without a word, so every branch is pinned here.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app import agent, kb


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A fake mounted KB workspace on local disk."""
    ws = tmp_path / "memory"
    ws.mkdir()
    monkeypatch.setattr(kb, "is_mounted", lambda: True)
    monkeypatch.setattr(kb, "workspace_root", lambda: ws)
    return ws


def _lint(ws):
    return ws / "skills" / "lint" / "SKILL.md"


def _state(ws):
    return ws / "skills" / agent.SEED_STATE_FILE


def test_fresh_seed_writes_skills_and_records_hashes(workspace):
    agent.seed_bootstrap()

    assert _lint(workspace).exists()
    shipped = json.loads(_state(workspace).read_text())["shipped"]
    assert "lint/SKILL.md" in shipped
    assert "ingest/SKILL.md" in shipped


def test_reseed_without_changes_is_a_noop(workspace):
    agent.seed_bootstrap()
    before = _lint(workspace).read_text()

    agent.seed_bootstrap()

    assert _lint(workspace).read_text() == before


def test_new_shipped_version_replaces_an_untouched_file(workspace):
    """The case that never worked before: a shipped fix reaching a deployment."""
    agent.seed_bootstrap()

    # Simulate an older shipped version sitting in the KB, untouched by anyone.
    stale = "OLD SHIPPED CONTENT"
    _lint(workspace).write_text(stale)
    state = json.loads(_state(workspace).read_text())
    state["shipped"]["lint/SKILL.md"] = hashlib.sha256(stale.encode()).hexdigest()
    _state(workspace).write_text(json.dumps(state))

    agent.seed_bootstrap()

    assert _lint(workspace).read_text() != stale
    assert _lint(workspace).read_text().startswith("---")


def test_human_edits_are_never_overwritten(workspace):
    agent.seed_bootstrap()
    edited = "# My own lint rules, please do not clobber"
    _lint(workspace).write_text(edited)

    agent.seed_bootstrap()

    assert _lint(workspace).read_text() == edited


def test_untracked_legacy_file_is_left_alone(workspace):
    """Deployments predating hash tracking: we cannot tell, so we must not guess."""
    agent.seed_bootstrap()
    _state(workspace).unlink()
    legacy = "content from before the state file existed"
    _lint(workspace).write_text(legacy)

    agent.seed_bootstrap()

    assert _lint(workspace).read_text() == legacy


def test_legacy_file_stays_untouched_across_repeated_seeds(workspace):
    """Same trap as above for the untracked case: it must never become tracked."""
    agent.seed_bootstrap()
    _state(workspace).unlink()
    legacy = "content from before the state file existed"
    _lint(workspace).write_text(legacy)

    agent.seed_bootstrap()
    agent.seed_bootstrap()

    assert _lint(workspace).read_text() == legacy


def test_edited_file_stays_edited_across_repeated_seeds(workspace):
    """Regression: the recorded hash must follow the edit, not the shipped file.

    If seeding re-recorded the shipped hash after declining to overwrite, the
    next run would see a match and silently clobber the human's edit.
    """
    agent.seed_bootstrap()
    edited = "# edited once"
    _lint(workspace).write_text(edited)

    agent.seed_bootstrap()
    agent.seed_bootstrap()

    assert _lint(workspace).read_text() == edited
