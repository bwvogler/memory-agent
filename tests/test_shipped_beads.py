"""Closing prod's beads from the image that resolved them.

This is the return path for an idea the agent had about the product it runs on:
it files the bead on prod, the work happens in the repo, and the image carries
the news back. Nothing else closes those beads, so a silent failure here means
prod's ledger slowly fills with work that is actually done - the exact rot the
ledger exists to prevent.

Every test below is about *not* doing something: not raising, not closing
twice, not overruling a human. See docs/decisions/0010.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from app import kb


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A per-user scratch dir with a .beads in it, and a recording fake bd."""
    scratch = tmp_path / "someone_example"
    (scratch / ".beads").mkdir(parents=True)
    _work_dir(monkeypatch, tmp_path)
    return scratch


class FakeBd(list):
    """Every bd invocation, in order.

    Add to .fail to make a subcommand fail the way bd fails on an id this
    ledger does not have. Add to .blocked_by to make one bead's close be
    refused until its blocker has been closed.

    Both error strings are bd's own, measured against the pinned version rather
    than invented: reconcile_shipped now tells the two apart, so a fake that
    paraphrased them would let a regression through.
    """

    fail: set[str]
    blocked_by: dict[str, str]


@pytest.fixture
def calls(monkeypatch):
    """Record every bd invocation; succeed unless a subcommand is failed first."""
    recorded = FakeBd()
    recorded.fail = set()
    recorded.blocked_by = {}
    done: set[str] = set()

    async def fake_run(*argv, cwd=None, env=None):
        recorded.append(list(argv))
        if argv[1] in recorded.fail:
            return (
                1,
                "",
                f'Error: resolving ID {argv[2]}: no issue found matching "{argv[2]}"',
            )
        if argv[1] == "close":
            blocker = recorded.blocked_by.get(argv[2])
            if blocker is not None and blocker not in done:
                return (
                    1,
                    "",
                    (
                        f"cannot close blocked issue: {argv[2]} is blocked by "
                        f"[{blocker}] (use --force to override)"
                    ),
                )
            done.add(argv[2])
        return 0, "", ""

    monkeypatch.setattr(kb, "_run", fake_run)
    return recorded


def _work_dir(monkeypatch, path):
    """config is a frozen dataclass, so stand in a namespace rather than mutate."""
    monkeypatch.setattr(
        kb, "config", types.SimpleNamespace(work_dir=str(path), kb_mount="/nonexistent")
    )


def _manifest(tmp_path, monkeypatch, *lines: str):
    path = tmp_path / "shipped-beads.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(kb, "SHIPPED_MANIFEST", path)
    return path


def _entry(bead_id: str, **extra) -> str:
    return json.dumps({"id": bead_id, "summary": "did the thing", **extra})


def test_closes_each_bead_and_notes_the_commit(tmp_path, monkeypatch, ledger, calls):
    _manifest(tmp_path, monkeypatch, _entry("kb-1m7", commit="abc1234"))

    assert asyncio.run(kb.reconcile_shipped("someone_example")) == ["kb-1m7"]

    assert ["bd", "close", "kb-1m7", "--reason", "shipped in local"] in calls
    # The reason says an image shipped it; the note carries what can be audited.
    note = next(c for c in calls if c[1] == "note")
    assert "abc1234" in note[3]


def test_a_bead_is_closed_once_and_never_again(tmp_path, monkeypatch, ledger, calls):
    _manifest(tmp_path, monkeypatch, _entry("kb-1m7"))

    assert asyncio.run(kb.reconcile_shipped("someone_example")) == ["kb-1m7"]
    calls.clear()

    # Redeploying the same image must be a no-op, not a second note.
    assert asyncio.run(kb.reconcile_shipped("someone_example")) == []
    assert calls == []


def test_a_reopened_bead_stays_open(tmp_path, monkeypatch, ledger, calls):
    """The one case the state file exists for.

    Reopening is how a human says "that did not actually ship". A reconciler
    keyed on current status would close it again on the next boot and overrule
    the person it was supposed to be informing.
    """
    _manifest(tmp_path, monkeypatch, _entry("kb-1m7"))
    asyncio.run(kb.reconcile_shipped("someone_example"))
    calls.clear()

    asyncio.run(kb.reconcile_shipped("someone_example"))

    assert not [c for c in calls if c[1] == "close"]


def test_a_malformed_line_is_skipped_not_fatal(tmp_path, monkeypatch, ledger, calls):
    _manifest(
        tmp_path,
        monkeypatch,
        "{not json at all",
        json.dumps({"summary": "no id here"}),
        json.dumps(["a list, not an object"]),
        _entry("kb-good"),
    )

    # The hand-appended file will eventually contain a bad line; the cost of
    # that is one skipped bead, never a failed boot.
    assert asyncio.run(kb.reconcile_shipped("someone_example")) == ["kb-good"]


def test_the_files_own_comment_is_not_a_warning(tmp_path, monkeypatch, ledger, calls):
    _manifest(tmp_path, monkeypatch, json.dumps({"_comment": "how this file works"}))

    assert asyncio.run(kb.reconcile_shipped("someone_example")) == []
    assert calls == []


def test_a_missing_manifest_is_survivable(tmp_path, monkeypatch, ledger, calls):
    monkeypatch.setattr(kb, "SHIPPED_MANIFEST", tmp_path / "nope.jsonl")

    assert asyncio.run(kb.reconcile_shipped("someone_example")) == []


def test_a_bead_this_ledger_never_had_is_not_retried_forever(
    tmp_path, monkeypatch, ledger, calls
):
    """Every user's ledger sees the same manifest, so most ids are absent.

    That is the common case rather than a fault, and retrying it would log the
    same failure on every boot for the life of the deployment.
    """
    calls.fail.add("close")
    _manifest(tmp_path, monkeypatch, _entry("kb-1m7"))

    assert asyncio.run(kb.reconcile_shipped("someone_example")) == []
    calls.clear()

    assert asyncio.run(kb.reconcile_shipped("someone_example")) == []
    assert calls == []


def test_state_survives_as_json_beside_the_ledger(tmp_path, monkeypatch, ledger, calls):
    _manifest(tmp_path, monkeypatch, _entry("kb-1m7"))
    asyncio.run(kb.reconcile_shipped("someone_example"))

    state = json.loads((ledger / kb.SHIPPED_STATE_FILE).read_text())
    assert state == {"applied": ["kb-1m7"]}


def test_unreadable_state_is_treated_as_empty(tmp_path, monkeypatch, ledger, calls):
    (ledger / kb.SHIPPED_STATE_FILE).write_text("{{{ truncated", encoding="utf-8")
    _manifest(tmp_path, monkeypatch, _entry("kb-1m7"))

    # Closing twice is harmless; refusing to close because the bookkeeping is
    # corrupt would strand the bead forever.
    assert asyncio.run(kb.reconcile_shipped("someone_example")) == ["kb-1m7"]


def test_every_ledger_on_the_volume_is_reconciled(tmp_path, monkeypatch, calls):
    for slug in ("alice_example", "bob_example"):
        (tmp_path / slug / ".beads").mkdir(parents=True)
    # Scratch without a ledger, and a stray file: neither is a user.
    (tmp_path / "no_ledger_here").mkdir()
    (tmp_path / "kb.git").mkdir()
    _work_dir(monkeypatch, tmp_path)
    _manifest(tmp_path, monkeypatch, _entry("kb-1m7"))

    assert asyncio.run(kb.reconcile_shipped_all()) == {
        "alice_example": ["kb-1m7"],
        "bob_example": ["kb-1m7"],
    }


def test_a_missing_work_dir_does_not_take_down_the_boot(tmp_path, monkeypatch, calls):
    _work_dir(monkeypatch, tmp_path / "gone")

    assert asyncio.run(kb.reconcile_shipped_all()) == {}


def test_a_bead_blocked_by_a_later_manifest_line_is_still_closed(
    tmp_path, monkeypatch, ledger, calls
):
    """The bug this fix-point loop exists for.

    Manifest order is append order. bd refuses to close a bead whose blocker is
    still open, so listing the blocked bead first meant its close was refused,
    recorded as applied, and never retried - which is how kb-068 stayed open on
    prod after the image that resolved it shipped.
    """
    calls.blocked_by["kb-068"] = "kb-b82"
    _manifest(tmp_path, monkeypatch, _entry("kb-068"), _entry("kb-b82"))

    closed = asyncio.run(kb.reconcile_shipped("someone_example"))

    assert sorted(closed) == ["kb-068", "kb-b82"]
    state = json.loads((ledger / kb.SHIPPED_STATE_FILE).read_text())
    assert state == {"applied": ["kb-068", "kb-b82"]}


def test_a_refusal_that_is_not_absence_is_retried_next_boot(
    tmp_path, monkeypatch, ledger, calls
):
    """Absence is permanent; every other refusal is a state that can change.

    A bead whose blocker never ships stays pending and warns on every boot.
    That is the point: shipped work still open is somebody's problem to resolve,
    unlike an id this ledger simply never had.
    """
    calls.blocked_by["kb-068"] = "kb-never"
    _manifest(tmp_path, monkeypatch, _entry("kb-068"))

    assert asyncio.run(kb.reconcile_shipped("someone_example")) == []
    assert json.loads((ledger / kb.SHIPPED_STATE_FILE).read_text()) == {"applied": []}

    # Now its blocker closes out of band, and the next boot finishes the job.
    calls.blocked_by.clear()
    assert asyncio.run(kb.reconcile_shipped("someone_example")) == ["kb-068"]


def test_the_shipped_manifest_ships():
    """The manifest is read from inside the image, so the path must be real.

    A wrong path here fails the way everything in this system fails: silently,
    by closing nothing, on a deployment nobody is watching.
    """
    assert kb.SHIPPED_MANIFEST.is_file()
    assert kb._read_manifest() is not None
