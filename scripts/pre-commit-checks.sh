#!/usr/bin/env bash
# The static gate that runs before a commit: ruff and ty, at the versions
# requirements-dev.txt pins, over the same paths CI checks.
#
# This is a separate script rather than a hook body on purpose. The hook lives in
# .beads/hooks (see scripts/install-hooks.sh for why) and, in this repo, is
# committed - but the part of it we own is *generated* by that installer, so
# changing it means every checkout re-running the installer. This file is source:
# changing what the gate does is an ordinary commit, and it gets linted, typed
# over and tested like the rest of the repo. The hook is two lines that call it.
#
# Not here: the test suite. The fast tier would be affordable but the
# --container tier builds the real image and stands up Postgres and FUSE, and a
# commit hook that takes minutes gets bypassed with --no-verify until it may as
# well not exist. Tests run in CI, where blocking a merge is the right lever.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

log() { printf 'pre-commit: %s\n' "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

# A doc-only commit cannot break either tool, and a gate that charges two
# seconds for editing a README is a gate people learn to skip. pyproject.toml
# carries the ruff configuration and requirements-dev.txt carries the pins, so
# both count as code for this purpose.
staged="$(git diff --cached --name-only --diff-filter=ACMR)"
if ! printf '%s\n' "$staged" | grep -qE '\.py$|^pyproject\.toml$|^requirements.*\.txt$'; then
  exit 0
fi

command -v uvx >/dev/null || die "uvx not found; install uv (https://docs.astral.sh/uv/)"

# Read the pins rather than restate them. CLAUDE.md asks for three files to be
# changed together and this would have been the fourth, which is one more place
# for the hook to pass what CI then rejects.
pin() {
  local version
  version="$(sed -n "s/^$1==\([^ #]*\).*/\1/p" requirements-dev.txt | head -1)"
  [ -n "$version" ] || die "no exact pin for $1 in requirements-dev.txt"
  printf '%s' "$version"
}
RUFF="$(pin ruff)"
TY="$(pin ty)"

# ty needs the environment: fastapi, claude_agent_sdk, asyncpg and jwt all have
# to resolve, or every annotation naming them is Unknown and the check passes by
# knowing nothing. Refuse rather than deliver that reassurance.
[ -d .venv ] || die "no .venv, so ty would check nothing and say it passed. Create it:
       uv venv .venv && uv pip install -r requirements-dev.txt --python .venv/bin/python"

# Said once, and not as a refusal. These tools read the working tree while the
# commit records the index, so a fix you have made but not staged is counted here
# and will not be in the commit. pre-commit stashed to close that gap; this does
# not, and saying so is better than implying a guarantee it lacks.
git diff --quiet || log "note: unstaged changes present, so this checked the working tree, not the commit"

# No --fix. An auto-fixing hook commits something other than what was staged and
# reviewed, and the second commit to correct it is cheaper than that surprise.
log "ruff check"
uvx "ruff@$RUFF" check app tests
log "ruff format --check"
uvx "ruff@$RUFF" format --check app tests
# Whole-project: types are not a per-file property, so checking only the staged
# files would miss every error a change causes somewhere else.
log "ty check"
uvx "ty@$TY" check app tests --python .venv
