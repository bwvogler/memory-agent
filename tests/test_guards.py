"""The KB write guard: what it refuses, and what it must not.

False positives here are expensive in a specific way. The incident this guard
exists to prevent happened because an agent hit a refusal it did not
understand and invented a workaround. A guard that blocks legitimate scratch
work would manufacture exactly that pressure, so the "allows" below matter as
much as the "denies".
"""

from __future__ import annotations

import asyncio

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


def _run(tool_name: str, command: str) -> dict:
    return asyncio.run(
        guards.pre_tool_use_guard(
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
    assert asyncio.run(guards.pre_tool_use_guard({}, None, None)) == {}
    assert asyncio.run(
        guards.pre_tool_use_guard({"tool_name": "Bash"}, None, None)
    ) == {}
