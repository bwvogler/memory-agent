#!/usr/bin/env bash
# PHASE 0, STEP 2 - the number that decides whether this design is pleasant.
#
# TigerFS turns every stat/read/glob into SQL. An agent exploring a knowledge
# base is extremely chatty at the filesystem layer: a single recursive grep can
# be hundreds of round trips. If the container and the database are in different
# regions, the agent will feel broken and you will blame the model.
#
# Run this from INSIDE the deployed container, against the real database.
#
#   usage: scripts/spike-latency.sh [mountpoint]
set -euo pipefail
MNT="${1:-${KB_MOUNT:-/mnt/kb}}"

[[ -d "$MNT" ]] || { echo "no mount at $MNT"; exit 1; }

t() { local label="$1"; shift; local s
  s=$(date +%s.%N)
  "$@" >/dev/null 2>&1 || true
  printf '%-28s %6.2fs\n' "$label" "$(echo "$(date +%s.%N) - $s" | bc)"
}

echo "measuring against $MNT"
echo
t "stat mountpoint"        stat "$MNT"
t "ls top level"           ls "$MNT"
t "ls -R (recursive)"      ls -R "$MNT"
t "find all files"         find "$MNT" -type f
t "grep -r 'the'"          grep -r --include='*.md' -l "the" "$MNT"
echo
cat <<'GUIDE'
Rough interpretation:
  ls -R under ~1s          same region, healthy. Proceed.
  ls -R 1-5s               tolerable, but agent exploration will feel sluggish.
  ls -R over 5s            fix the region first. If it is already co-located,
                           consider the no-FUSE fallback in the architecture
                           doc: an MCP server over the same Postgres turns
                           hundreds of round trips into one per tool call, at
                           the cost of losing undo and history semantics.
GUIDE
