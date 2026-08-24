#!/usr/bin/env bash
# Pull image-labelled beads from the deployed ledger into this repo's ledger.
#
# The agent is the first user of its own product and files the best ideas about
# it, but it cannot act on any of them: it has no repo, no git, and the image is
# immutable. So those ideas are filed on prod with `--labels image --status
# deferred` and collected here, where the work can actually happen.
#
# Only `image` beads travel. Beads about the *content* of the knowledge base
# stay on the volume, where the agent that filed them can also work them.
#
# Ids are preserved across the pull: kb-1m7 on prod is kb-1m7 here, which is
# what lets docs/shipped-beads.jsonl close it by name after we ship. See
# docs/decisions/0010.
#
# Usage: scripts/beads-pull.sh [user_slug]
set -euo pipefail

cd "$(dirname "$0")/.."

for tool in flyctl jq bd; do
    command -v "$tool" >/dev/null || { echo "need $tool on PATH" >&2; exit 1; }
done

# Waking the suspended machine, discovering which /work directory holds the
# ledger, and getting a quoted command past `flyctl ssh -C` all live in
# scripts/fly.sh. They were inline here first; a second caller is exactly when
# that stops being fine, and the slug in particular was a default that happened
# to be right rather than something anybody had checked.
export FLY_USER_SLUG="${1:-}"

# `bd export` has no --label filter, so filter here. Anything that is not JSON
# is not ours.
raw="$(scripts/fly.sh bd export)"

image="$(printf '%s\n' "$raw" \
    | grep '^{' \
    | jq -c 'select(.labels != null and (.labels | index("image")))')"

if [ -z "$image" ]; then
    echo "no image beads on prod. nothing to pull." >&2
    exit 0
fi

printf '%s\n' "$image" | jq -r '"  \(.id)  \(.title)"' >&2

# Status needs translating, and only in one direction. `deferred` on prod means
# "the agent must not claim this" (it is out of bd ready by construction, the
# same trick signal beads use). Here it means the opposite - this is the work.
# So a bead arriving for the first time opens.
#
# For a bead we already track, status is dropped from the payload instead: this
# is an upsert, and prod goes on saying `deferred` until the image that fixes it
# deploys, so honouring it would reopen work we have already closed here.
# Everything else - title, description, priority - still syncs.
known="$(BD_NON_INTERACTIVE=1 CI=true bd list --all --json | jq -c '[.[].id]')"

printf '%s\n' "$image" \
    | jq -c --argjson known "$known" '
        if (.id as $id | $known | index($id))
        then del(.status)
        elif .status == "deferred" then .status = "open"
        else . end' \
    | BD_NON_INTERACTIVE=1 CI=true bd import -

# .beads/issues.jsonl is what git actually tracks; the Dolt db beside it is
# gitignored. Export now so the pull shows up in `git status`.
BD_NON_INTERACTIVE=1 CI=true bd export -o .beads/issues.jsonl
echo "pulled. review .beads/issues.jsonl and commit." >&2
