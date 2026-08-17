#!/usr/bin/env bash
# Install this repo's pre-commit gate into wherever git actually looks for hooks.
#
# `pre-commit install` cannot do this here, and the reason is worth stating
# because the failure is confusing rather than loud in the usual way. `bd` sets
# core.hooksPath to .beads/hooks when it installs its own hooks, and
# core.hooksPath REPLACES .git/hooks rather than adding to it - so anything in
# .git/hooks is dead. pre-commit knows this and refuses:
#
#   [ERROR] Cowardly refusing to install hooks with `core.hooksPath` set.
#
# It offers no flag to write elsewhere, and unsetting core.hooksPath would
# disable beads' hooks to enable ours. So we share the file instead. Two facts
# make that safe, both measured rather than assumed: `git rev-parse --git-path
# hooks` reports the directory core.hooksPath names, so this script always finds
# the live one; and `bd hooks install` rewrites only the region between its own
# BEGIN/END BEADS markers and leaves the rest of the file alone, so a beads
# upgrade does not clobber our block.
#
# Idempotent: re-running replaces our block rather than stacking copies. See
# bead img-bl4.
#
# This repo commits .beads/hooks, so the block below usually arrives with a
# checkout and running this again is a no-op. What does not arrive is
# core.hooksPath, which is local config a clone never inherits - so on a fresh
# clone git looks in .git/hooks until `bd hooks install` redirects it. Targeting
# whatever `git rev-parse --git-path hooks` reports means this script is correct
# on either side of that, and running it twice around a `bd init` is harmless.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

BEGIN='# --- BEGIN MEMORY-AGENT STATIC CHECKS ---'
END='# --- END MEMORY-AGENT STATIC CHECKS ---'

HOOKS="$(git rev-parse --git-path hooks)"
mkdir -p "$HOOKS"
HOOK="$HOOKS/pre-commit"

if [ ! -f "$HOOK" ]; then
  printf '#!/usr/bin/env sh\n' > "$HOOK"
fi

# Strip any previous copy of our block, then append a fresh one. Rewriting via a
# temp file and mv keeps the hook either wholly old or wholly new; an in-place
# edit interrupted halfway leaves a syntactically broken hook, which fails every
# commit in the repo.
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
awk -v b="$BEGIN" -v e="$END" '
  $0 == b { skip = 1 }
  skip != 1 { print }
  $0 == e { skip = 0 }
' "$HOOK" > "$TMP"

cat >> "$TMP" <<EOF
$BEGIN
# Managed by scripts/install-hooks.sh. The gate itself is a versioned script, so
# improving it is a commit rather than a re-install everywhere. Guarded on -x
# because checking out a commit that predates the script must not break every
# commit after it.
_ma_root="\$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "\$_ma_root" ] && [ -x "\$_ma_root/scripts/pre-commit-checks.sh" ]; then
  "\$_ma_root/scripts/pre-commit-checks.sh" || exit 1
fi
$END
EOF

mv "$TMP" "$HOOK"
trap - EXIT
chmod +x "$HOOK"

printf 'installed the static-check block into %s\n' "$HOOK"
printf 'it runs: scripts/pre-commit-checks.sh (ruff + ty, pinned by requirements-dev.txt)\n'
