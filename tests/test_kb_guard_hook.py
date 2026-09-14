"""The laptop-side guards: what they refuse, and what they must not.

These cover `scripts/kb_guard_hook.py`, which enforces the KB's rules for a
Claude Code session on a laptop rather than for the deployed agent. Everything
here is driven through a synthetic repo in `tmp_path` - a real mount is neither
available in this tier nor needed, because the decision the hook makes is a
function of a state file and a path.

The "must allow" cases carry the same weight they do in test_guards.py. This
repo is also where the app itself is developed, and a guard that fired during
an ordinary `app/` refactor would be worse than no guard: it would teach
whoever hit it to reach for `--no-verify`, or for a shell workaround, which is
the incident ADR 0007 already has on record.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_HOOK = Path(__file__).resolve().parent.parent / "scripts" / "kb_guard_hook.py"
_spec = importlib.util.spec_from_file_location("kb_guard_hook", _HOOK)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
sys.modules["kb_guard_hook"] = hook
_spec.loader.exec_module(hook)


def make_repo(tmp_path: Path, *, mounted=False, access="writable", dev=False) -> Path:
    """A repo skeleton, optionally with a mount that looks live.

    "Looks live" is a non-empty mountpoint, which is what `mount_is_live`
    checks - `mnt/` is gitignored and holds nothing but mountpoints, so on a
    real machine non-empty means mounted.
    """
    mount_rel, work_rel = ("mnt/kb-dev", "work-dev") if dev else ("mnt/kb", "work")
    mountpoint = tmp_path / mount_rel
    work = tmp_path / work_rel
    mountpoint.mkdir(parents=True)
    work.mkdir(parents=True)
    if mounted:
        (mountpoint / ".build").write_text("markdown", encoding="utf-8")
        (mountpoint / "memory").mkdir()
        (work / ".mount-state.json").write_text(
            json.dumps(
                {
                    "mountpoint": str(mountpoint),
                    "workspace": str(mountpoint / "memory"),
                    "git_dir": str(work / "kb.git"),
                    "work_dir": str(work),
                    "db_host": "ep-round-wave.neon.tech",
                    "db_kind": "PRODUCTION",
                    "access": access,
                }
            ),
            encoding="utf-8",
        )
    return tmp_path


# --- rule one: naming the mount --------------------------------------------


def test_a_relative_mount_path_is_recognised(tmp_path):
    """The gap this hook exists to close.

    `guards._mentions_kb` tests against an absolute `/mnt/kb`, so
    `echo x >> mnt/kb/memory/a.md` sails straight through it. A laptop session
    types the relative form constantly.
    """
    markers = hook.mount_markers(tmp_path)
    assert hook.command_names_kb("echo x >> mnt/kb/memory/a.md", markers)


def test_an_absolute_mount_path_is_recognised(tmp_path):
    markers = hook.mount_markers(tmp_path)
    assert hook.command_names_kb(f"echo x >> {tmp_path}/mnt/kb/memory/a.md", markers)


def test_the_dev_mount_is_recognised(tmp_path):
    markers = hook.mount_markers(tmp_path)
    assert hook.command_names_kb("echo x >> mnt/kb-dev/memory/a.md", markers)


def test_the_mount_variable_is_recognised(tmp_path):
    """A command can name the mount without ever spelling the path."""
    markers = hook.mount_markers(tmp_path)
    assert hook.command_names_kb('echo x >> "$KB_MOUNT/memory/a.md"', markers)
    assert hook.command_names_kb('echo x >> "${KB_MOUNT}/memory/a.md"', markers)


def test_ordinary_work_does_not_name_the_mount(tmp_path):
    """Scratch and source are unrestricted; this is the false positive to avoid."""
    markers = hook.mount_markers(tmp_path)
    assert not hook.command_names_kb("echo x >> /tmp/draft.md", markers)
    assert not hook.command_names_kb("sed -i '' s/a/b/ app/agent.py", markers)
    assert not hook.command_names_kb("pytest -q", markers)


# --- rule one: the whole PreToolUse verdict --------------------------------


def test_an_append_into_the_mount_is_denied(tmp_path):
    out = hook.handle_pre_bash(
        {"tool_input": {"command": "echo x >> mnt/kb/memory/CLAUDE.md"}}, tmp_path
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "appends" in reason
    # The refusal must name the safe alternative, or it manufactures the
    # workaround-inventing pressure that caused the original incident.
    assert "write it back in full" in reason


def test_a_truncating_write_into_the_mount_is_allowed(tmp_path):
    """`>` writes the whole file from offset 0 - the SAFE pattern."""
    assert (
        hook.handle_pre_bash(
            {"tool_input": {"command": "echo x > mnt/kb/memory/CLAUDE.md"}}, tmp_path
        )
        == {}
    )


def test_an_append_outside_the_mount_is_allowed(tmp_path):
    assert (
        hook.handle_pre_bash(
            {"tool_input": {"command": "echo x >> /tmp/scratch/draft.md"}}, tmp_path
        )
        == {}
    )


def test_reading_the_mount_is_allowed(tmp_path):
    assert (
        hook.handle_pre_bash(
            {"tool_input": {"command": "cat mnt/kb/memory/a.md"}}, tmp_path
        )
        == {}
    )
    assert (
        hook.handle_pre_bash({"tool_input": {"command": "ls mnt/kb/memory/"}}, tmp_path)
        == {}
    )


# --- rule two: a write must be able to land --------------------------------


def test_a_write_to_an_unmounted_mountpoint_is_denied(tmp_path):
    """The failure with no symptom: a real local file that is not the wiki."""
    repo = make_repo(tmp_path, mounted=False)
    verdict = hook.write_verdict(repo / "mnt/kb/memory/page.md", repo)
    assert verdict is not None
    assert "is not mounted" in verdict
    assert "mount-kb.sh" in verdict


def test_a_write_to_a_read_only_mount_is_denied(tmp_path):
    """prod-ops rule 3, which until now had no enforcement anywhere."""
    repo = make_repo(tmp_path, mounted=True, access="read-only")
    verdict = hook.write_verdict(repo / "mnt/kb/memory/page.md", repo)
    assert verdict is not None
    assert "READ-ONLY" in verdict
    # It has to name the database, or the refusal reads as a local quirk.
    assert "ep-round-wave.neon.tech" in verdict
    # And it has to say WHY retrying is futile, since the write reports success.
    assert "reports SUCCESS" in verdict


def test_a_write_to_a_live_writable_mount_is_allowed(tmp_path):
    repo = make_repo(tmp_path, mounted=True, access="writable")
    assert hook.write_verdict(repo / "mnt/kb/memory/page.md", repo) is None


def test_a_mounted_directory_with_no_state_file_is_denied(tmp_path):
    """Mounted by hand, outside the script: we cannot tell read-only from not."""
    repo = make_repo(tmp_path, mounted=True)
    (repo / "work" / ".mount-state.json").unlink()
    verdict = hook.write_verdict(repo / "mnt/kb/memory/page.md", repo)
    assert verdict is not None
    assert "cannot tell which database" in verdict


def test_writes_outside_the_mount_are_never_touched(tmp_path):
    """Editing app/ and tests/ must stay completely unaffected."""
    repo = make_repo(tmp_path, mounted=False)
    assert hook.write_verdict(repo / "app" / "agent.py", repo) is None
    assert hook.write_verdict(repo / "tests" / "test_guards.py", repo) is None
    assert hook.write_verdict(repo.parent / "elsewhere" / "draft.md", repo) is None


def test_the_dev_mount_is_judged_by_its_own_state_file(tmp_path):
    """Two mounts, two work dirs: a live dev mount says nothing about prod."""
    repo = make_repo(tmp_path, mounted=True, dev=True)
    (repo / "mnt/kb").mkdir(parents=True, exist_ok=True)
    assert hook.write_verdict(repo / "mnt/kb-dev/memory/page.md", repo) is None
    assert hook.write_verdict(repo / "mnt/kb/memory/page.md", repo) is not None


# --- path resolution -------------------------------------------------------


def test_a_relative_target_resolves_against_the_tool_cwd(tmp_path):
    repo = make_repo(tmp_path, mounted=False)
    target = hook.resolve_target("mnt/kb/memory/page.md", str(repo))
    assert target == repo / "mnt/kb/memory/page.md"


def test_a_traversing_target_still_lands_inside_the_mount(tmp_path):
    """Normalisation happens before the containment test, not after."""
    repo = make_repo(tmp_path, mounted=False)
    target = hook.resolve_target("mnt/kb/memory/../memory/page.md", str(repo))
    assert hook.mount_for(target, repo) is not None


def test_notebook_edit_uses_a_different_key():
    """NotebookEdit says notebook_path; reading only file_path unguards a tool."""
    assert hook.target_of({"notebook_path": "/x/y.ipynb"}) == "/x/y.ipynb"
    assert hook.target_of({"file_path": "/x/y.md"}) == "/x/y.md"
    assert hook.target_of({}) == ""


# --- rule three: deferred work ---------------------------------------------


def _transcript(tmp_path: Path, rows: list[dict]) -> str:
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    return str(path)


def _user(text: str) -> dict:
    return {"message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _says(text: str, *, sidechain=False) -> dict:
    return {
        "isSidechain": sidechain,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


def test_the_stop_guard_is_silent_with_no_live_mount(tmp_path):
    """Ordinary app/ sessions in this repo must never see this guard."""
    repo = make_repo(tmp_path, mounted=False)
    path = _transcript(tmp_path, [_user("hi"), _says("I'll leave that as a follow-up")])
    assert hook.handle_stop({"transcript_path": path}, repo) == {}


def test_the_stop_guard_blocks_unfiled_deferral_on_a_live_mount(tmp_path):
    repo = make_repo(tmp_path, mounted=True)
    path = _transcript(
        tmp_path,
        [_user("write the page"), _says("Done. The missing GUIDE.md is a follow-up.")],
    )
    out = hook.handle_stop({"transcript_path": path}, repo)
    assert out["decision"] == "block"
    # Both ledgers, because which one is right depends on the subject.
    assert "scripts/fly.sh --write bd create" in out["reason"]
    assert "--labels image" in out["reason"]


def test_the_stop_guard_blocks_at_most_once(tmp_path):
    """A Stop guard that fires on its own re-prompt loops until max_turns."""
    repo = make_repo(tmp_path, mounted=True)
    path = _transcript(tmp_path, [_user("go"), _says("a follow-up for later")])
    assert (
        hook.handle_stop({"transcript_path": path, "stop_hook_active": True}, repo)
        == {}
    )


def test_a_subagents_deferral_does_not_block_the_parent(tmp_path):
    """Sidechain rows share the transcript file and are somebody else's turn."""
    repo = make_repo(tmp_path, mounted=True)
    path = _transcript(
        tmp_path,
        [_user("go"), _says("I'll leave that as a follow-up", sidechain=True)],
    )
    assert hook.handle_stop({"transcript_path": path}, repo) == {}


def test_a_turn_that_filed_a_bead_is_not_blocked(tmp_path):
    repo = make_repo(tmp_path, mounted=True)
    rows = [
        _user("go"),
        {
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "That's a follow-up for a later session."},
                    {
                        "type": "tool_use",
                        "input": {"command": 'scripts/fly.sh --write bd create "x"'},
                    },
                ],
            }
        },
    ]
    assert (
        hook.handle_stop({"transcript_path": _transcript(tmp_path, rows)}, repo) == {}
    )


# --- failing open ----------------------------------------------------------


def test_a_malformed_state_file_is_treated_as_absent(tmp_path):
    repo = make_repo(tmp_path, mounted=True)
    (repo / "work" / ".mount-state.json").write_text("{ not json", encoding="utf-8")
    assert hook.read_state(repo / "work") is None


def test_a_missing_transcript_does_not_raise(tmp_path):
    repo = make_repo(tmp_path, mounted=True)
    assert hook.handle_stop({}, repo) == {}


def test_an_unknown_event_produces_no_verdict():
    assert hook.HANDLERS.get("not-an-event") is None
