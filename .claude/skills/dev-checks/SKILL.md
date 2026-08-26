---
name: dev-checks
description: >
  Set up a fresh clone of this repo, pick the right pytest tier for the
  change at hand, and bump a pinned dependency (ruff, ty, playwright, bd, the
  Google MCP servers) without missing one of its paired files. Use when
  asked to set up the dev environment, run tests, lint, or bump a pin.
---

# Dev Setup, Test Tiers, and Pin Bumps

## Start the dev stack

```sh
cp .env.example .env   # fill in ANTHROPIC_API_KEY, KB_DATABASE_URL
docker compose up
```

Chat: http://localhost:8080 — Wiki: http://localhost:8080/kb

`docker-compose.override.yml` bind-mounts `static/` over the copy in the
image, so a CSS or JS change needs only a browser reload. Compose loads that
file automatically for a bare `docker compose up` and **not** when a file
list is passed with `-f` (which is how the container test tier and the
`browser-test` skill invoke it, deliberately, so they keep exercising what
the image actually contains). `app/` is never bind-mounted, so a Python
change needs `docker compose up -d --build app` even for the plain dev stack.

`docker compose down -v` destroys the local bead ledger and every savepoint —
see CLAUDE.md's "Local dev" note before running it against the dev stack.

## Fresh clone setup

```sh
uv venv .venv
uv pip install -r requirements-dev.txt --python .venv/bin/python
scripts/install-hooks.sh
git config blame.ignoreRevsFile .git-blame-ignore-revs
```

`core.hooksPath` is local git config, so a fresh clone does **not** inherit
it. `install-hooks.sh` targets whatever `git rev-parse --git-path hooks`
reports at the time it runs, which is correct whether or not `bd` has
redirected it yet — order between `bd init` and this script doesn't matter.

## Test tiers — pick the cheapest one that actually exercises the change

```sh
.venv/bin/python -m pytest                       # fast units, ~1s
.venv/bin/python -m pytest --container           # + real Docker/Postgres stack, ~1 min
.venv/bin/python -m pytest --container --live    # + one real agent turn, spends tokens
```

- `--live` implies `--container`; passing `--live` alone still runs both.
- Neither slow tier needs a real `ANTHROPIC_API_KEY` — `--container` is
  deliberately forced onto a placeholder key even if you have a real one set,
  because its tests assert on a turn that fails fast, and a real key would
  make it call the model and blow the wait deadline instead.
- Reach for `--container` when the change touches anything `app/kb.py`
  logs-and-continues around (a broken `bd`, a dead mount) — the fast tier
  imports modules directly and can't see a truly broken subprocess or mount.
- Reach for `--live` only when the change touches `allowed_tools`,
  `permission_mode`, or the `can_use_tool` streaming callback — it's the only
  tier that drives a real permission prompt and asserts a real `bd` command
  ran; both regressions raise nothing anywhere else.
- For a change under `static/`, pair this with the `browser-test` skill —
  no pytest tier executes served JavaScript.

## Lint and types

```sh
ruff check app tests
ruff format app tests
ty check app tests --python .venv
```

`pyproject.toml` selects ruff's `ALL` and opts out with a reason written next
to each ignore — keep that pattern for any new ignore; the reason is what
goes stale silently if it isn't beside the rule.

## Bumping a pinned version — every listed file, same commit

| Pin | Lives in | Also touch |
|---|---|---|
| `ruff`, `ty` | `requirements-dev.txt` **and** `.github/workflows/ci.yml`'s `RUFF_VERSION`/`TY_VERSION` | nothing else — `scripts/pre-commit-checks.sh` reads these pins rather than duplicating them |
| `playwright` | `requirements-dev.txt` | re-run `.venv/bin/python -m playwright install chromium` |
| `BEADS_VERSION` | `Dockerfile` | `scripts/fly.sh doctor` compares this against the deployed binary — read its migration-plan warning in CLAUDE.md before bumping |
| Google MCP servers | `Dockerfile`'s `GCAL_MCP_VERSION`/`GMAIL_MCP_VERSION` | `scripts/google-auth.sh`'s `CAL_VERSION`/`GM_VERSION` constants — the script's own comment says to keep them in step |

There is **no** `.pre-commit-config.yaml` in this repo — an old comment in
`requirements-dev.txt` used to say otherwise; if you see a reference to it
anywhere else, it's equally stale.

## Notes

- CI runs four jobs on every push: `ruff`, `ty`, fast `pytest`, and
  `pytest --container`. The commit hook only runs the static pair (ruff+ty)
  deliberately — a hook slow enough to need Docker gets `--no-verify`'d until
  it may as well not be installed.
- Full reasoning: CLAUDE.md's "Local dev", "Tests", and "Linting and types"
  sections.
