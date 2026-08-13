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
    """A fake mounted KB workspace that rewrites what it is given.

    tmp_path round-trips bytes perfectly. The real knowledge base does not: it
    is a TigerFS markdown workspace that parses documents and re-serialises
    them, so a file read back after writing is NOT the file that was written.

    That gap hid a real bug for a whole release. Every branch below passed
    against a faithful tmp_path while production had silently stopped
    upgrading skills at all, because the seeder compared a hash of the source
    bytes against a stored file that had been reformatted. A test double that
    is cleaner than production is not a simplification, it is a blind spot -
    so this one reformats too.
    """
    ws = tmp_path / "memory"
    ws.mkdir()
    monkeypatch.setattr(kb, "is_mounted", lambda: True)
    monkeypatch.setattr(kb, "workspace_root", lambda: ws)

    def rewriting_write(path, payload: bytes) -> bytes:
        # Stands in for markdown re-serialisation: any transformation that is
        # stable and not the identity reproduces the failure.
        path.write_bytes(payload.replace(b"description: >\n", b"description: "))
        return path.read_bytes()

    monkeypatch.setattr(agent, "_write_and_read_back", rewriting_write)
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
    # Not every seeded file is a skill: an image skill's LEARNED.md overlay
    # rides the same path, and needs the same "left alone once edited" rule -
    # once reflection appends to it, the shipped stub must never come back.
    assert "kb-curator/LEARNED.md" in shipped


def test_reseed_without_changes_is_a_noop(workspace):
    agent.seed_bootstrap()
    before = _lint(workspace).read_text()

    agent.seed_bootstrap()

    assert _lint(workspace).read_text() == before


@pytest.fixture
def shipped_source(tmp_path, monkeypatch):
    """Control what the image ships, so "a new version" is expressible."""
    root = tmp_path / "bootstrap"
    (root / "skills" / "lint").mkdir(parents=True)

    def ship(body: str) -> None:
        (root / "skills" / "lint" / "SKILL.md").write_text(
            f"---\ndescription: >\n  a skill\nname: lint\n---\n\n{body}\n"
        )

    ship("v1 body")
    monkeypatch.setattr(agent, "BOOTSTRAP_DIR", root)
    return ship


def test_new_shipped_version_replaces_an_untouched_file(workspace):
    """The case that never worked before: a shipped fix reaching a deployment."""
    agent.seed_bootstrap()

    # Simulate an older shipped version sitting in the KB, untouched by anyone,
    # recorded the way the seeder records things: source AND stored form.
    stale = b"OLD SHIPPED CONTENT\n"
    stored = agent._write_and_read_back(_lint(workspace), stale)
    state = json.loads(_state(workspace).read_text())
    state["shipped"]["lint/SKILL.md"] = {
        "source": hashlib.sha256(stale).hexdigest(),
        "stored": hashlib.sha256(stored).hexdigest(),
    }
    _state(workspace).write_text(json.dumps(state))

    agent.seed_bootstrap()

    assert _lint(workspace).read_bytes() != stale
    assert _lint(workspace).read_text().startswith("---")


def test_an_upgrade_reaches_a_store_that_rewrites_what_it_stores(
    workspace, shipped_source
):
    """The bug that shipped: upgrades stopped forever, silently.

    The store reformats documents, so the file on disk never equals the bytes
    that were written. Comparing the source hash against it marked every skill
    as locally edited on every deploy, and no skill could be updated again.
    Production logged two "has local edits" warnings a boot and looked fine.
    """
    agent.seed_bootstrap()
    assert "v1 body" in _lint(workspace).read_text()
    # Precondition: the store really did rewrite it, or this proves nothing.
    assert "description: >" not in _lint(workspace).read_text()

    shipped_source("v2 body")
    agent.seed_bootstrap()

    assert "v2 body" in _lint(workspace).read_text()


def test_legacy_state_is_repaired_when_the_same_version_is_reshipped(
    workspace, shipped_source
):
    """The upgrade path unsticks itself on the next deploy, without guessing.

    Existing deployments carry single-hash state. When the version in hand is
    the one that state names, whatever sits on disk is what we last wrote, so
    its current form can be recorded as the stored form - and upgrades flow
    again from then on.
    """
    agent.seed_bootstrap()
    payload = (agent.BOOTSTRAP_DIR / "skills" / "lint" / "SKILL.md").read_bytes()
    _state(workspace).write_text(
        json.dumps({"shipped": {"lint/SKILL.md": hashlib.sha256(payload).hexdigest()}})
    )

    agent.seed_bootstrap()

    entry = json.loads(_state(workspace).read_text())["shipped"]["lint/SKILL.md"]
    assert isinstance(entry, dict) and entry.get("stored")

    shipped_source("v2 body")
    agent.seed_bootstrap()
    assert "v2 body" in _lint(workspace).read_text()


def test_legacy_state_with_an_unknown_version_leaves_the_file_alone(
    workspace, shipped_source
):
    """Cannot tell a stale copy from a human edit, so must not guess."""
    agent.seed_bootstrap()
    on_disk = _lint(workspace).read_text()
    _state(workspace).write_text(
        json.dumps({"shipped": {"lint/SKILL.md": "0" * 64}})
    )

    shipped_source("v2 body")
    agent.seed_bootstrap()

    assert _lint(workspace).read_text() == on_disk


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
