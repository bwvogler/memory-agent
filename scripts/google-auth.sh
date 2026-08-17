#!/usr/bin/env bash
# Get the Google credentials that app/mcp_catalog.py's two servers need, and
# print the `fly secrets set` line that ships them.
#
# This runs on a LAPTOP and cannot run anywhere else. Both servers authenticate
# through a browser consent flow; the container has no browser, and its machine
# suspends when nobody is looking at it. So the flow happens here once, and its
# output travels as a secret. See docs/decisions/0015.
#
# Usage:
#   scripts/google-auth.sh ~/Downloads/client_secret_....json
#   scripts/google-auth.sh --calendar-only ~/Downloads/client_secret_....json
#   scripts/google-auth.sh --gmail-only    ~/Downloads/client_secret_....json
#
# Before running, in the Google Cloud console:
#
#   1. Enable the Google Calendar API and the Gmail API.
#   2. Credentials -> Create credentials -> OAuth client ID -> Desktop app.
#      ONE client serves both servers. Download the JSON and pass it below.
#   3. Set the OAuth consent screen's publishing status to "In production".
#
# Step 3 is not optional and is the one that fails silently. While the consent
# screen is in "Testing", Google expires refresh tokens after SEVEN DAYS - so
# everything works, and a week later every calendar and mail tool starts failing
# with `invalid_grant` for no visible reason. "In production" without
# verification is fine below 100 users; you get a warning screen you click past.
set -euo pipefail

CAL_VERSION=2.6.2   # keep in step with GCAL_MCP_VERSION in the Dockerfile
GM_VERSION=1.3.3    # keep in step with GMAIL_MCP_VERSION in the Dockerfile

# readonly: search and read. compose: drafting - which also grants SENDING,
# because Google made them one scope. app/mcp_catalog.py's `deny` list is what
# stops the send, and it is our own config rather than Google's. If you would
# rather have the hard guarantee than have drafts, drop `,gmail.compose` here
# and delete the `deny` tuple; nothing else changes.
GMAIL_SCOPES="gmail.readonly,gmail.compose"

DO_CAL=1
DO_GMAIL=1
KEYS=""

log() { printf 'google-auth: %s\n' "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --calendar-only) DO_GMAIL=0 ;;
    --gmail-only)    DO_CAL=0 ;;
    -h|--help)       sed -n '2,25p' "$0"; exit 0 ;;
    -*)              die "unknown flag: $1" ;;
    *)               KEYS="$1" ;;
  esac
  shift
done

[ -n "$KEYS" ] || die "pass the path to the OAuth client JSON you downloaded"
[ -f "$KEYS" ] || die "no such file: $KEYS"
command -v npx >/dev/null || die "npx not found; install Node"

# Fail here rather than after two consent flows. A downloaded Desktop-app client
# has an `installed` key; a Web client has `web` and its redirect URI will not
# match what either server listens on.
python3 - "$KEYS" <<'EOF' || die "that file is not a Desktop-app OAuth client"
import json, sys
with open(sys.argv[1]) as fh:
    data = json.load(fh)
sys.exit(0 if "installed" in data else 1)
EOF

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
log "working in $WORK (deleted on exit)"

emit() {  # emit VAR FILE
  printf '%s=%s' "$1" "$(python3 -c 'import json,sys; print(json.dumps(open(sys.argv[1]).read()))' "$2")"
}

SECRETS=()
SECRETS+=("$(emit MCP_GOOGLE_OAUTH_KEYS "$KEYS")")

if [ "$DO_CAL" = 1 ]; then
  log "--- Calendar: a browser will open. Consent as the HOUSEHOLD account. ---"
  GOOGLE_OAUTH_CREDENTIALS="$KEYS" \
  GOOGLE_CALENDAR_MCP_TOKEN_PATH="$WORK/cal-tokens.json" \
    npx -y "@cocal/google-calendar-mcp@${CAL_VERSION}" auth
  [ -s "$WORK/cal-tokens.json" ] || die "calendar auth produced no token file"
  SECRETS+=("$(emit MCP_GCAL_TOKEN "$WORK/cal-tokens.json")")
fi

if [ "$DO_GMAIL" = 1 ]; then
  log "--- Gmail: consent as the SAME household account, scopes $GMAIL_SCOPES ---"
  GMAIL_OAUTH_PATH="$KEYS" \
  GMAIL_CREDENTIALS_PATH="$WORK/gmail-credentials.json" \
    npx -y "@klodr/gmail-mcp@${GM_VERSION}" auth "--scopes=${GMAIL_SCOPES}"
  [ -s "$WORK/gmail-credentials.json" ] || die "gmail auth produced no token file"
  SECRETS+=("$(emit MCP_GMAIL_TOKEN "$WORK/gmail-credentials.json")")
fi

cat >&2 <<'EOF'

--- Done. Run this to ship them (one command, so it restarts the machine once):

EOF
printf 'fly secrets set \\\n'
printf "  '%s' \\\\\n" "${SECRETS[@]}"
printf '  --app "${FLY_APP:-memory-agent-proud-island-3747}"\n'
cat >&2 <<'EOF'

Then check /healthz: `mcp_catalog` should report both servers as "ready".
A server still reading "missing MCP_..." means that variable did not arrive.

These values are credentials to a live mailbox and calendar. Do not paste them
into a file in this repo, a bead, or the knowledge base - `fly secrets` is the
only place they belong.
EOF
