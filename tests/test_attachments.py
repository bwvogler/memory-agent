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

import asyncio
import base64
import json as jsonlib
import types
import uuid

import pytest
from fastapi import HTTPException

from app import agent, auth, kb, main
from app.conversations import conversations
from app.turns import Registry


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


# --- the attachment lands on the event stream, not just on disk ------------


def _coro(value):
    async def _call():
        return value

    return _call


def _request(body: dict):
    return types.SimpleNamespace(json=_coro(body))


def test_an_attachment_is_named_on_the_event_stream_before_spawn(
    scratch, caps, monkeypatch
):
    """A reload replays a conversation's events from Postgres/the in-memory
    tail - so a chip surviving a refresh depends on this landing on the
    stream itself, not on a Turn.files field, and not deferred until the
    agent coroutine (which spawn only schedules, and which this test never
    lets run) does it.
    """
    fresh = Registry()
    monkeypatch.setattr(main, "registry", fresh)
    # spawn() only schedules agent.run_turn; closing the coroutine here rather
    # than letting it run keeps this test from touching the real SDK.
    monkeypatch.setattr(main, "spawn", lambda coro, **kw: coro.close())

    conversation_id = uuid.uuid4().hex
    conv = asyncio.run(conversations.get_or_load(conversation_id))

    body = {"message": "here's a file", "files": [payload("deck.csv", b"a,b\n1,2\n")]}
    response = asyncio.run(
        main.post_message(conversation_id, _request(body), _identity())
    )
    turn_id = jsonlib.loads(bytes(response.body))["turn_id"]

    attachment_events = [e for e in conv.events if e.kind == "attachment"]
    assert len(attachment_events) == 1
    payload_out = jsonlib.loads(attachment_events[0].data)
    assert payload_out["name"] == "deck.csv"
    assert payload_out["url"] == f"/api/uploads/{turn_id}/deck.csv"


# --- GET /api/uploads/{turn_id}/{name} --------------------------------------
#
# Ownership here is proven by the path, not by looking up a Turn: the slug
# comes from the verified Identity and never appears in the URL. What has to
# be tested instead is that a URL-supplied turn_id can neither create a
# directory nor read across a slug boundary.


def _identity(email: str = "dev@localhost") -> auth.Identity:
    return auth.Identity(email=email, subject="s")


def test_media_type_allowlist_is_a_pure_table():
    assert main._upload_media_type("notes.md") == ("text/plain; charset=utf-8", True)
    assert main._upload_media_type("photo.PNG") == ("image/png", True)
    assert main._upload_media_type("evil.html") == ("application/octet-stream", False)
    assert main._upload_media_type("evil.svg") == ("application/octet-stream", False)
    assert main._upload_media_type("noext") == ("application/octet-stream", False)


def test_a_malformed_turn_id_is_refused_before_the_filesystem_is_touched(scratch):
    """A GET must not be able to `mkdir` on the volume just by naming a path."""
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.uploaded_file("../../etc", "passwd", _identity()))

    assert caught.value.status_code == 404
    assert not (scratch / "dev_localhost").exists()


def test_a_well_formed_but_unknown_turn_id_creates_no_directory(scratch):
    """The read path must never mkdir - only the write path may."""
    turn_id = "0" * 32
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.uploaded_file(turn_id, "deck.csv", _identity()))

    assert caught.value.status_code == 404
    assert not (scratch / "dev_localhost").exists()


def test_a_file_under_another_slug_is_unreachable(scratch):
    turn_id = "1" * 32
    other = kb.uploads_dir_for("someone_else_example_com", turn_id)
    (other / "deck.csv").write_bytes(b"not yours")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.uploaded_file(turn_id, "deck.csv", _identity()))

    assert caught.value.status_code == 404


def test_a_valid_upload_is_served_back_with_safe_headers(scratch):
    turn_id = "2" * 32
    path = kb.resolve_upload_path("dev_localhost", turn_id, "deck.csv")
    assert path is not None
    path.write_bytes(b"a,b\n1,2\n")

    response = asyncio.run(main.uploaded_file(turn_id, "deck.csv", _identity()))

    assert response.media_type == "text/plain; charset=utf-8"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_an_unrecognised_extension_is_an_opaque_download(scratch):
    """The one thing this route must never do: serve an upload as text/html
    or image/svg+xml, both scripting contexts on this origin."""
    turn_id = "3" * 32
    path = kb.resolve_upload_path("dev_localhost", turn_id, "evil.html")
    assert path is not None
    path.write_bytes(b"<script>alert(1)</script>")

    response = asyncio.run(main.uploaded_file(turn_id, "evil.html", _identity()))

    assert response.media_type == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")


# --- the prompt the agent actually sees ------------------------------------


def test_the_attachment_note_gives_paths_not_contents(tmp_path):
    """A 5 MB CSV must cost nothing until the agent decides to read it."""
    note = agent._attachment_note([tmp_path / "deck.csv"])

    assert str(tmp_path / "deck.csv") in note
    assert "Read" in note
    # Says what it cannot do, rather than letting the agent find out mid-turn.
    assert ".xlsx" in note
