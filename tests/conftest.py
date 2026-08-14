"""Test configuration: opt-in tiers and the Docker stack fixture.

The fast tier imports app modules directly and needs nothing installed beyond
the runtime requirements. The container tier builds and runs the real image,
because the bugs this suite exists to catch were all silent: a missing COPY, a
Postgres too old for the schema, a permission mode that blocked a subprocess.
None of them raised anything - they just quietly did nothing. Only running the
real thing tells "worked" apart from "did nothing".
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

# A project name of its own, so teardown's `down -v` cannot touch the volumes
# of a dev stack started with a plain `docker compose up`.
PROJECT = "memory-agent-test"
BASE_URL = "http://localhost:18080"

# The per-user scratch dir for DEV_FAKE_EMAIL=dev@localhost.
USER_SLUG = "dev_localhost"

_COMPOSE = [
    "docker",
    "compose",
    "-p",
    PROJECT,
    "-f",
    str(REPO_ROOT / "docker-compose.yml"),
    "-f",
    str(REPO_ROOT / "tests" / "compose.test.yml"),
]


def pytest_addoption(parser):
    parser.addoption(
        "--container",
        action="store_true",
        default=False,
        help="run the Docker smoke tier (slow, ~3 min, no API key needed)",
    )
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run one real agent turn (spends tokens; implies --container)",
    )


def pytest_collection_modifyitems(config, items):
    want_live = config.getoption("--live")
    # A live turn needs the stack, so --live implies --container.
    want_container = config.getoption("--container") or want_live

    skip_container = pytest.mark.skip(reason="needs --container")
    skip_live = pytest.mark.skip(reason="needs --live")
    for item in items:
        if "live" in item.keywords and not want_live:
            item.add_marker(skip_live)
        elif "container" in item.keywords and not want_container:
            item.add_marker(skip_container)


def compose(*args, check=True, capture=True):
    """Run a docker compose subcommand against the test project.

    `check` is handled here rather than by subprocess, because
    CalledProcessError prints the command and the exit status and drops the
    stderr - so a failure inside the container arrives as sixty lines of
    subprocess internals ending in "returned non-zero exit status 1", and the
    Python traceback that actually says what went wrong is thrown away. That
    happened, cost a diagnosis, and is the reason this is not `check=check`.
    """
    result = subprocess.run(
        [*_COMPOSE, *args],
        check=False,
        capture_output=capture,
        text=True,
        cwd=REPO_ROOT,
    )
    if check and result.returncode != 0:
        pytest.fail(
            f"docker compose {' '.join(args)}\nexit {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}",
            pytrace=False,
        )
    return result


def app_exec(*argv: str, workdir: str | None = None, check=True):
    """Run a command inside the running app container."""
    args = ["exec", "-T"]
    if workdir:
        args += ["-w", workdir]
    args += ["-e", "BD_NON_INTERACTIVE=1", "app", *argv]
    return compose(*args, check=check)


def bd(*argv: str, check=True):
    """Run bd in the dev user's scratch dir, where their bead graph lives."""
    return app_exec("bd", *argv, workdir=f"/work/{USER_SLUG}", check=check)


def bd_json(*argv: str):
    out = bd(*argv, "--json").stdout
    return json.loads(out) if out.strip() else []


@pytest.fixture(scope="session")
def stack(request):
    """Build and run the real stack for the session, then destroy it."""
    # A shell variable beats .env in compose, so a placeholder set here would
    # shadow the developer's real key. Load .env first, and only fall back to
    # a placeholder when no live turn is going to be made.
    if os.environ.get("ANTHROPIC_API_KEY") is None:
        load_dotenv(REPO_ROOT / ".env")

    if os.environ.get("ANTHROPIC_API_KEY") is None:
        if request.config.getoption("--live"):
            pytest.fail("--live needs ANTHROPIC_API_KEY in the environment or .env")
        os.environ["ANTHROPIC_API_KEY"] = "unused-by-the-smoke-tier"

    compose("up", "-d", "--build")
    try:
        _wait_for_health()
        yield BASE_URL
    finally:
        compose("down", "-v", check=False)


@pytest.fixture(scope="session")
def beads(stack):
    """Initialise the bead graph for the dev user.

    ensure_beads() only ever runs from run_turn(), so a stack that has never
    taken a turn has no graph and every bd call fails on a missing directory.
    Calling it here is both the setup bead tests need and the test of
    ensure_beads itself.
    """
    app_exec(
        "python",
        "-c",
        "import asyncio;from app import kb;"
        f"assert asyncio.run(kb.ensure_beads('{USER_SLUG}'))",
    )
    return stack


def _wait_for_health(timeout_s: int = 180) -> None:
    """Wait for every subsystem the suite goes on to assert about.

    `kb_mounted` alone was not enough. The session store starts once, and when
    it lost a boot race the app logged it, set the store to None and carried on
    reporting a healthy mount - so the suite started against a stack that could
    not record anything, and failed twelve tests later in two places that read
    like unrelated flakes. Gating on `transcripts` here turns that into one
    failure, at startup, naming the subsystem.
    """
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        try:
            r = httpx.get(f"{BASE_URL}/healthz", timeout=5)
            body = r.json() if r.status_code == 200 else {}
            if body.get("kb_mounted") and body.get("transcripts") == "ready":
                return
            last = r.text
        except Exception as exc:  # noqa: BLE001 - the stack is still starting
            last = str(exc)
        time.sleep(2)

    logs = compose("logs", "app", check=False).stdout or ""
    pytest.fail(
        f"stack never became healthy within {timeout_s}s.\n"
        f"last /healthz: {last}\n\n--- app logs (tail) ---\n"
        + "\n".join(logs.splitlines()[-30:])
    )
