"""The KB write guard: what it refuses, and what it must not.

False positives here are expensive in a specific way. The incident this guard
exists to prevent happened because an agent hit a refusal it did not
understand and invented a workaround. A guard that blocks legitimate scratch
work would manufacture exactly that pressure, so the "allows" below matter as
much as the "denies".
"""

from __future__ import annotations

import asyncio
import json

from app import guards
from app.config import config

KB = config.kb_mount


def denied(command: str) -> bool:
    return guards.unsafe_kb_write(command) is not None


# --- must refuse -----------------------------------------------------------


def test_append_redirect_into_the_kb_is_refused():
    assert denied(f"echo hello >> {KB}/memory/CLAUDE.md")


def test_tee_append_is_refused():
    assert denied(f"echo hello | tee -a {KB}/memory/CLAUDE.md")
    assert denied(f"echo hello | tee --append {KB}/memory/CLAUDE.md")


def test_sed_in_place_is_refused():
    assert denied(f"sed -i s/a/b/ {KB}/memory/notes.md")
    assert denied(f"sed --in-place s/a/b/ {KB}/memory/notes.md")


def test_dd_writing_at_an_offset_is_refused():
    assert denied(f"dd if=/tmp/x of={KB}/memory/n.md seek=200 conv=notrunc")


def test_python_append_mode_is_refused():
    assert denied(f"""python -c "open('{KB}/memory/n.md','a').write('x')" """)


def test_the_exact_command_shape_that_destroyed_the_memory_file():
    """Regression, in the user's own terms: 233 bytes of notes became zeroes."""
    assert denied(
        f"printf '%s\\n' '- Lizzy (born June 3, 2022)' >> {KB}/memory/CLAUDE.md"
    )


# --- must allow ------------------------------------------------------------


def test_truncating_redirect_into_the_kb_is_allowed():
    """A whole-file write from offset 0 is the SAFE pattern, not the hazard."""
    assert not denied(f"echo hello > {KB}/memory/CLAUDE.md")


def test_appending_in_scratch_is_allowed():
    """Scratch is an ordinary filesystem; restricting it invents the problem."""
    assert not denied("echo hello >> /work/dev_localhost/draft.md")
    assert not denied("sed -i s/a/b/ /work/dev_localhost/draft.md")


def test_staging_then_copying_is_allowed():
    """The recommended escape hatch for incremental work must not be blocked."""
    assert not denied(f"cp /work/dev_localhost/draft.md {KB}/memory/notes.md")


def test_reading_from_the_kb_is_allowed():
    assert not denied(f"grep -r tea {KB}/memory/")
    assert not denied(f"cat {KB}/memory/CLAUDE.md")


def test_bd_commands_are_untouched():
    assert not denied("bd ready --json")


# --- the hook wrapper ------------------------------------------------------


class _Turn:
    def __init__(self):
        self.guard_denials = []


def _run(tool_name: str, command: str, turn=None) -> dict:
    return asyncio.run(
        guards.kb_write_guard_for(turn)(
            {"tool_name": tool_name, "tool_input": {"command": command}},
            "tu_1",
            None,
        )
    )


def test_the_hook_denies_and_explains_the_safe_pattern():
    out = _run("Bash", f"echo x >> {KB}/memory/CLAUDE.md")
    decision = out["hookSpecificOutput"]

    assert decision["permissionDecision"] == "deny"
    # A bare refusal is what drove the last agent to invent a workaround.
    reason = decision["permissionDecisionReason"]
    assert "write it back in full" in reason
    assert "scratch" in reason


def test_the_hook_allows_anything_it_does_not_recognise():
    assert _run("Bash", "ls -la") == {}
    assert _run("Read", "irrelevant") == {}


def test_a_malformed_payload_does_not_break_the_turn():
    """A guard that throws would take down every turn it was meant to protect."""
    guard = guards.kb_write_guard_for(None)
    assert asyncio.run(guard({}, None, None)) == {}
    assert asyncio.run(guard({"tool_name": "Bash"}, None, None)) == {}


def test_a_refusal_is_recorded_as_ours_not_as_a_deployment_defect():
    """The SDK reports a hook denial through the same channel as a missing
    allowed_tools entry, and signals.py files a P1 bead for the latter. Without
    this the guard doing its job reports itself as a bug - into the ledger a
    reflection turn then reads."""
    turn = _Turn()
    _run("Bash", f"echo x >> {KB}/memory/CLAUDE.md", turn)
    assert turn.guard_denials == ["Bash"]

    quiet = _Turn()
    _run("Bash", "ls -la", quiet)
    assert quiet.guard_denials == []


# --- the deferred-work guard ----------------------------------------------
#
# Same asymmetry as above, for the same reason: a guard that fires on ordinary
# turns trains the model to work around it, so the "lets it stop" cases carry
# as much weight as the blocks.


def _user(text: str) -> dict:
    return {"message": {"role": "user", "content": text}}


def _says(text: str) -> dict:
    return {
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}
    }


def _runs(command: str) -> dict:
    return {
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": command}}
            ],
        }
    }


def _tool_result() -> dict:
    """A tool result: role=user, but NOT a new turn."""
    return {
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": "ok"}],
        }
    }


def test_the_exact_turn_that_lost_the_work():
    """Regression for kb-3cl, in the words the agent actually used."""
    rows = [
        _user("Add a page. Separately, wiki/notes/ has no GUIDE.md - not now."),
        _runs("cat /mnt/kb/memory/AGENT_GUIDE.md"),
        _tool_result(),
        _says(
            "Done. The missing GUIDE.md is noted as a follow-up for a later session."
        ),
    ]
    assert guards.unfiled_deferral(rows) == "follow-up"


def test_filing_the_bead_satisfies_the_guard():
    rows = [
        _user("Add a page."),
        _says("I noticed wiki/notes/ has no GUIDE.md - filing that as follow-up work."),
        _runs('bd create --title="Add a GUIDE.md to wiki/notes/" --type=task'),
        _tool_result(),
        _says("Done, and filed as kb-abc."),
    ]
    assert guards.unfiled_deferral(rows) is None


def test_annotating_an_existing_bead_counts_as_filing():
    """The requirement is that the work reached the ledger, not which verb."""
    rows = [
        _user("Fix the notes page."),
        _says("The rest is follow-up work."),
        _runs("bd note kb-abc 'also needs metric conversion'"),
    ]
    assert guards.unfiled_deferral(rows) is None


def test_an_ordinary_turn_is_not_blocked():
    rows = [
        _user("What does the wiki say about tea?"),
        _says("Oolong steeps 4 minutes."),
    ]
    assert guards.unfiled_deferral(rows) is None


def test_deferral_language_from_an_earlier_turn_does_not_re_fire():
    """Otherwise one unfiled deferral would block every turn thereafter."""
    rows = [
        _user("Add a page."),
        _says("Noted as a follow-up for a later session."),
        _user("Thanks. What does the wiki say about tea?"),
        _says("Oolong steeps for four minutes."),
    ]
    assert guards.unfiled_deferral(rows) is None


def test_a_tool_result_does_not_start_a_new_turn():
    """Tool results arrive as role=user; treating one as a turn boundary would
    hide every deferral the agent made before its last tool call."""
    rows = [
        _user("Add a page."),
        _says("Adding a GUIDE.md here is follow-up work."),
        _runs("cat /mnt/kb/memory/wiki/notes/tea.md"),
        _tool_result(),
        _says("Done."),
    ]
    assert guards.unfiled_deferral(rows) == "follow-up"


def test_bare_later_is_not_enough_to_block():
    """ "later" turns up in ordinary prose ("a later version", "later in the
    file"). Blocking on it would fire on turns with nothing to file."""
    rows = [_user("Explain the format."), _says("A later version changed this.")]
    assert guards.unfiled_deferral(rows) is None


def test_the_block_names_the_phrase_and_the_command_to_run(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(r)
            for r in [_user("Add a page."), _says("Filed as a follow-up for later.")]
        ),
        encoding="utf-8",
    )
    out = asyncio.run(
        guards.stop_guard(
            {"stop_hook_active": False, "transcript_path": str(transcript)}, None, None
        )
    )
    assert out["decision"] == "block"
    assert "follow-up" in out["reason"]
    assert "bd create" in out["reason"]
    # A bare "you must file it" with no escape hatch is how a model ends up
    # filing junk beads to get past the guard.
    assert "no durable work" in out["reason"]


def test_the_guard_blocks_at_most_once(tmp_path):
    """Without this it re-fires on its own re-prompt until max_turns."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps(_says("a follow-up for later")), encoding="utf-8")
    assert (
        asyncio.run(
            guards.stop_guard(
                {"stop_hook_active": True, "transcript_path": str(transcript)},
                None,
                None,
            )
        )
        == {}
    )


def test_an_unreadable_transcript_lets_the_turn_end(tmp_path):
    """The transcript format is a CLI internal. If it drifts, the turn still
    finishes - a stuck turn would be far worse than a missed bead."""
    assert asyncio.run(guards.stop_guard({}, None, None)) == {}
    assert (
        asyncio.run(
            guards.stop_guard(
                {"transcript_path": str(tmp_path / "nope.jsonl")}, None, None
            )
        )
        == {}
    )


def test_a_half_written_final_line_is_skipped(tmp_path):
    """The CLI appends to this file while we read it."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps(_says("a follow-up for later")) + '\n{"partial": ',
        encoding="utf-8",
    )
    out = asyncio.run(
        guards.stop_guard({"transcript_path": str(transcript)}, None, None)
    )
    assert out["decision"] == "block"
