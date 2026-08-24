"""The argv `kb.create_bead` builds, and why status is not part of it.

`bd create --status` exists only on the 1.2.x line. The pin moved to 1.2.2,
which is the tested 1.1 code re-released, and there the flag does not exist: bd
answers `unknown flag: --status` and creates nothing at all. That is a silent
hole rather than a loud one, because `create_bead` logs-and-returns-None and the
callers - signals, guards, evolve - are all written to survive a ledger they
cannot reach. So the whole signal-capture path went quiet and the only symptom
was beads that never appeared.

These tests are about the shape of the calls, not about bd. The container tier
is what proves bd still accepts them.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from app import kb


@pytest.fixture
def calls(tmp_path, monkeypatch):
    """Record every bd invocation; `create` answers with a plausible id."""
    recorded: list[list[str]] = []
    monkeypatch.setattr(
        kb,
        "config",
        types.SimpleNamespace(work_dir=str(tmp_path), kb_mount="/nonexistent"),
    )

    async def fake_run(*argv, cwd=None, env=None):
        recorded.append(list(argv))
        if argv[1] == "create":
            return 0, json.dumps({"id": "kb-abc"}), ""
        return 0, "", ""

    monkeypatch.setattr(kb, "_run", fake_run)
    return recorded


def _create(**kwargs) -> str | None:
    return asyncio.run(kb.create_bead("someone_example", "a title", **kwargs))


def test_create_never_passes_status_to_bd(calls):
    """The regression itself: this flag is not on the pinned binary."""
    assert _create(status="deferred") == "kb-abc"

    create = next(c for c in calls if c[1] == "create")
    assert "--status" not in create, (
        "bd create --status is 1.2.x-only; the pin is the tested 1.1 line"
    )


def test_a_requested_status_is_applied_in_a_second_call(calls):
    _create(status="deferred")

    assert ["bd", "update", "kb-abc", "--status", "deferred"] in calls


def test_no_status_means_no_second_call(calls):
    """An ordinary bead is one bd invocation, as it always was."""
    _create()

    assert [c[1] for c in calls] == ["create"]


def test_a_bead_whose_status_could_not_be_set_is_still_returned(tmp_path, monkeypatch):
    """Losing the id is worse than a bead left open.

    A signal bead that stays `open` pollutes `bd ready`, which is exactly what
    `deferred` exists to prevent - but it is recoverable by hand, and a caller
    that believes nothing was filed will file it again.
    """
    monkeypatch.setattr(
        kb,
        "config",
        types.SimpleNamespace(work_dir=str(tmp_path), kb_mount="/nonexistent"),
    )

    async def fake_run(*argv, cwd=None, env=None):
        if argv[1] == "create":
            return 0, json.dumps({"id": "kb-abc"}), ""
        return 1, "", "unknown flag: --status"

    monkeypatch.setattr(kb, "_run", fake_run)

    assert _create(status="deferred") == "kb-abc"


def test_labels_and_type_still_ride_on_the_create(calls):
    _create(labels=("signal", "revert"), issue_type="task", priority=1)

    create = next(c for c in calls if c[1] == "create")
    assert "--labels" in create
    assert create[create.index("--labels") + 1] == "signal,revert"
    assert create[create.index("--priority") + 1] == "1"
