"""Uploads: the filename is hostile, and the size is unbounded.

Both halves of this file guard the same property, which is the one attachments
put at risk: a client-supplied string must not be able to choose where a file
lands. The name arrives from a browser and nothing between there and
`Path.write_bytes` inspects it, so `../../.beads/issues.jsonl` would overwrite
the ledger and an absolute path would leave scratch entirely.

The size cap is the other unbounded input. Attachments are base64 inside a JSON
body, which has no natural limit - without a ceiling one request is decoded
into memory and written to the volume before anything can object.
"""

from __future__ import annotations

import base64
import types

import pytest
from fastapi import HTTPException

from app import agent, kb, main


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """Point kb at a temp WORK_DIR so uploads never touch /work or /mnt/kb.

    `config` is a frozen dataclass, so the module-level name is replaced rather
    than mutated - the same approach as the `isolated` fixture in
    test_agent_options.py.
    """
    monkeypatch.setattr(
        kb,
        "config",
        types.SimpleNamespace(work_dir=str(tmp_path), kb_mount="/mnt/kb"),
    )
    return tmp_path


@pytest.fixture
def caps(monkeypatch):
    """Tiny size limits, so the cap tests are arithmetic rather than megabytes."""
    fake = types.SimpleNamespace(max_upload_bytes=16, max_upload_total_bytes=24)
    monkeypatch.setattr(main, "config", fake)
    return fake


# --- filenames -------------------------------------------------------------


def test_a_plain_name_survives():
    assert kb.safe_upload_name("Fairplay - Sheet1.csv") == "Fairplay - Sheet1.csv"


def test_traversal_is_reduced_to_its_last_component():
    assert kb.safe_upload_name("../../.beads/issues.jsonl") == "issues.jsonl"


def test_an_absolute_path_is_reduced_to_its_last_component():
    assert kb.safe_upload_name("/mnt/kb/memory/CLAUDE.md") == "CLAUDE.md"


def test_a_windows_path_is_reduced_too():
    assert kb.safe_upload_name(r"C:\Users\brian\deck.csv") == "deck.csv"


def test_control_characters_are_stripped():
    """A NUL truncates the path at the syscall boundary, silently."""
    assert kb.safe_upload_name("deck\x00.csv") == "deck.csv"


@pytest.mark.parametrize("name", ["", "   ", ".", "..", "../", "/", "\x00"])
def test_names_with_nothing_usable_left_are_refused(name):
    assert kb.safe_upload_name(name) is None


def test_a_very_long_name_is_truncated_not_refused():
    result = kb.safe_upload_name("x" * 500 + ".csv")
    assert result is not None
    assert len(result) <= kb.MAX_UPLOAD_NAME_LENGTH


def test_resolved_upload_paths_stay_inside_the_user_scratch_dir(scratch):
    home = kb.scratch_dir_for("someone_example_com").resolve()

    for hostile in ("../../etc/passwd", "/etc/passwd", r"..\..\ledger.jsonl"):
        path = kb.resolve_upload_path("someone_example_com", "turn1", hostile)
        assert path is not None
        assert path.resolve().is_relative_to(home), hostile


def test_uploads_never_land_in_the_knowledge_base(scratch):
    """The invariant assert_scratch_outside_kb protects, one layer down."""
    path = kb.resolve_upload_path("dev_localhost", "turn1", "deck.csv")

    assert path is not None
    assert not str(path).startswith("/mnt/kb")


def test_two_turns_do_not_collide_on_the_same_filename(scratch):
    first = kb.resolve_upload_path("dev_localhost", "turn1", "deck.csv")
    second = kb.resolve_upload_path("dev_localhost", "turn2", "deck.csv")

    assert first != second


# --- size and encoding -----------------------------------------------------


def payload(name: str, blob: bytes) -> dict:
    return {"name": name, "data": base64.b64encode(blob).decode()}


def test_a_normal_attachment_decodes():
    decoded = main._decode_attachments([payload("deck.csv", b"a,b\n1,2\n")])

    assert decoded == [("deck.csv", b"a,b\n1,2\n")]


def test_an_oversized_attachment_is_refused_with_413(caps):
    with pytest.raises(HTTPException) as caught:
        main._decode_attachments([payload("big.csv", b"x" * 17)])

    assert caught.value.status_code == 413
    # The detail names the file, because the UI shows it verbatim.
    assert "big.csv" in str(caught.value.detail)


def test_many_small_attachments_cannot_add_up_past_the_total(caps):
    """The per-file cap alone lets ten files each just under it through."""
    files = [payload(f"{n}.csv", b"x" * 12) for n in "abc"]

    with pytest.raises(HTTPException) as caught:
        main._decode_attachments(files)

    assert caught.value.status_code == 413


def test_a_nameless_attachment_is_refused_with_400():
    with pytest.raises(HTTPException) as caught:
        main._decode_attachments([payload("", b"data")])

    assert caught.value.status_code == 400


def test_non_base64_data_is_refused_with_400():
    with pytest.raises(HTTPException) as caught:
        main._decode_attachments([{"name": "deck.csv", "data": "not base64!!"}])

    assert caught.value.status_code == 400


def test_an_empty_attachment_is_refused():
    """A zero-byte file is a browser or drag-drop mishap, not an input."""
    with pytest.raises(HTTPException) as caught:
        main._decode_attachments([payload("deck.csv", b"")])

    assert caught.value.status_code == 400


# --- the prompt the agent actually sees ------------------------------------


def test_the_attachment_note_gives_paths_not_contents(tmp_path):
    """A 5 MB CSV must cost nothing until the agent decides to read it."""
    note = agent._attachment_note([tmp_path / "deck.csv"])

    assert str(tmp_path / "deck.csv") in note
    assert "Read" in note
    # Says what it cannot do, rather than letting the agent find out mid-turn.
    assert ".xlsx" in note
