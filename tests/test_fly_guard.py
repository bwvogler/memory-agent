"""The read-only guard in `scripts/fly.sh`.

That script reaches a bead graph on an unreplicated Fly volume with no savepoint
covering it, so `--write` is the whole of the protection: past the flag there is
no confirmation and no undo. The guard works by matching the bd verb against a
list, which makes it exactly as good as that list is complete - and a verb nobody
added is not a weaker guard, it is no guard, silently.

`bd sql` is the case that motivated these tests. It reads like a query and takes
arbitrary SQL, so `fly.sh bd sql 'DELETE FROM issues'` looked read-only and went
straight through.

Nothing here touches the network. `check_read_only` runs *before* `wake` by
design - the script's own comment says a refusal should cost nothing - and the
stubs below prove it rather than trust it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FLY = REPO / "scripts" / "fly.sh"

# Verbs that change the deployed graph and must be refused without --write.
MUTATING = ["create", "close", "update", "sql", "admin", "migrate", "dolt"]

# Verbs that only read, and must stay usable without ceremony.
READING = ["ready", "list", "show", "export"]


@pytest.fixture
def env(tmp_path):
    """A PATH where flyctl and curl are stubs that record being called.

    The recording is the point. "The guard refused" and "the guard refused
    before reaching the network" are different claims, and only the second one
    means a typo against production costs nothing.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"

    for name in ("flyctl", "curl"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 0\n')
        stub.chmod(0o755)

    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        # Skip slug discovery, which would otherwise ask the stub where the
        # ledgers are and correctly conclude there are none.
        "FLY_USER_SLUG": "someone_example",
        "_CALL_LOG": str(log),
    }


def run(env, *args):
    return subprocess.run(
        [str(FLY), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        timeout=60,
        # A refusal is the expected outcome of half these tests, so the exit
        # status is the assertion rather than an error.
        check=False,
    )


def calls(env) -> str:
    log = Path(env["_CALL_LOG"])
    return log.read_text() if log.exists() else ""


@pytest.mark.parametrize("verb", MUTATING)
def test_a_mutating_verb_is_refused_without_write(env, verb):
    result = run(env, "bd", verb, "whatever")

    assert result.returncode != 0, f"`bd {verb}` was allowed through: {result.stdout}"
    assert "--write" in result.stderr, result.stderr
    # Naming the verb matters: the message is the only thing telling the reader
    # which of several arguments tripped it.
    assert verb in result.stderr, result.stderr


@pytest.mark.parametrize("verb", MUTATING)
def test_the_refusal_never_reaches_the_network(env, verb):
    """A refused command must not wake a suspended machine, or even try."""
    run(env, "bd", verb, "whatever")

    assert calls(env) == "", f"`bd {verb}` was refused, but only after: {calls(env)}"


@pytest.mark.parametrize("verb", MUTATING)
def test_write_lets_a_mutating_verb_through(env, verb):
    """The flag is an override, not a second refusal."""
    result = run(env, "--write", "bd", verb, "whatever")

    assert result.returncode == 0, result.stderr
    assert "flyctl" in calls(env)


@pytest.mark.parametrize("verb", READING)
def test_a_reading_verb_needs_no_flag(env, verb):
    """The guard has to stay cheap to live with, or it gets worked around."""
    result = run(env, "bd", verb)

    assert result.returncode == 0, result.stderr
    assert "flyctl" in calls(env)


def test_sql_is_guarded_even_though_it_reads_like_a_query(env):
    """The specific hole these tests were written for.

    `bd sql` was absent from the list, so a DELETE against the deployed ledger
    needed no --write and got no confirmation.
    """
    result = run(env, "bd", "sql", "DELETE FROM issues")

    assert result.returncode != 0
    assert calls(env) == ""
