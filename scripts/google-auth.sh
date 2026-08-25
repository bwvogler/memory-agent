#!/usr/bin/env bash
# Get the Google credentials that app/mcp_catalog.py's two servers need, and
# print the `fly secrets set` line that ships them.
#
# This runs on a LAPTOP and cannot run anywhere else. Both servers authenticate
# through a browser consent flow; the container has no browser, and its machine
# suspends when nobody is looking at it. So the flow happens here once, and its
# output travels as a secret. See docs/decisions/0015.
#
# Usage and the Google Cloud console steps live in usage() below rather than
# here, so that running this with no arguments prints them. Two copies would
# drift, and the copy a comment holds is the one nobody sees.
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

# Printed by --help AND when run with no argument. The second is the important
# one: "pass the path to the JSON" is useless to someone who does not yet have a
# JSON, and that is precisely who runs this script with no arguments.
usage() {
  cat <<'HELP_END'
Get the Google credentials the calendar and gmail MCP servers need, and print
the `fly secrets set` line that ships them. Runs on a laptop: both servers
authenticate through a browser, and the container has neither one nor a way to
be reached by one.

USAGE
  scripts/google-auth.sh <path-to-oauth-client.json>
  scripts/google-auth.sh --calendar-only <path>
  scripts/google-auth.sh --gmail-only    <path>

FIRST, in the Google Cloud console — about ten minutes, once.

  Do all of this signed in as the HOUSEHOLD account, not your own. The token
  this produces reaches every calendar shared with that account, and that
  account's own mailbox.

  1. Create or pick a project.
       https://console.cloud.google.com/projectcreate

  2. Enable both APIs. They are separate; missing one fails only at first use.
       https://console.cloud.google.com/apis/library/calendar-json.googleapis.com
       https://console.cloud.google.com/apis/library/gmail.googleapis.com

  3. Configure the OAuth consent screen. User type "Internal" if the household
     account is on a Google Workspace domain — see step 4 for why that is worth
     it. "External" otherwise.
       https://console.cloud.google.com/apis/credentials/consent

  4. LEAVE publishing status on "Testing", and add the household account
     under "Test users".

     Do NOT press "Publish app". The Gmail scopes below are RESTRICTED, not
     merely sensitive, and Google does not allow an unverified app to use them
     in production - verification means a CASA third-party security audit,
     which for gmail.readonly is a full penetration test. Publish without it
     and Google disables the OAuth client: the consent screen loads, the
     "Advanced -> Go to ... (unsafe)" link fails with "Something went wrong",
     and the next attempt is `401: disabled_client`. Setting it back to
     "Testing" recovers it.

     The cost of Testing is real and is the thing to plan around: Google
     expires refresh tokens after SEVEN DAYS. Everything works, then a week
     later every calendar and mail tool fails with `invalid_grant`, with no
     deploy to blame. Re-running this script and re-setting the secrets fixes
     it, for another week.

     Verification is NOT the way out, and the console's "Push to production?"
     dialog makes it look like one. For restricted scopes it means: a public
     homepage on a domain you own, verified through Search Console; a privacy
     policy on that domain; an unlisted YouTube video demonstrating each scope;
     and an ANNUAL CASA Tier 2 assessment - a third-party security scan of your
     production app, roughly $800-6000 a year, several weeks each time.

     Google does exempt this shape of app from verification: "if you are the
     only user of your app or if your app is used by only a few users, all of
     whom are known personally to you". Read what that exempts, though. It
     permits staying in Testing and clicking through the warning. It does not
     stop the seven-day clock.

     The way out is a Google WORKSPACE account on a domain you own, with the
     consent screen's user type set to "Internal". Internal apps need no
     verification, show no warning screen, and their refresh tokens do not
     expire on a timer. That is roughly one Workspace seat for the household
     account, and it is the only durable option that does not involve an audit.

  5. Credentials -> Create credentials -> OAuth client ID -> "Desktop app".
     ONE client serves both servers. Download the JSON.
       https://console.cloud.google.com/apis/credentials

THEN run this script with the path to that download, e.g.

  scripts/google-auth.sh ~/Downloads/client_secret_1234-abcd.apps.googleusercontent.com.json

A browser opens twice — once for Calendar, once for Gmail. Consent as the
household account both times. The script prints one `fly secrets set` command;
nothing is sent anywhere until you run it.
HELP_END
}

while [ $# -gt 0 ]; do
  case "$1" in
    --calendar-only) DO_GMAIL=0 ;;
    --gmail-only)    DO_CAL=0 ;;
    -h|--help)       usage; exit 0 ;;
    -*)              usage >&2; die "unknown flag: $1" ;;
    *)               KEYS="$1" ;;
  esac
  shift
done

if [ -z "$KEYS" ]; then
  usage >&2
  echo >&2
  die "no OAuth client JSON given - see the steps above to produce one"
fi
# A literal ~ means the path was quoted: "~/x.json" does not expand, and the
# resulting "no such file" names a path that looks exactly right, so the reader
# rechecks the filename instead of the quotes. Say what actually happened.
case "$KEYS" in
  '~'*) die "the ~ was not expanded, so this path is literal: $KEYS
             Quotes suppress it. Re-run without them and let tab-completion
             fill the name in, or write \$HOME instead of ~." ;;
esac

if [ ! -f "$KEYS" ]; then
  case "$KEYS" in
    # Google's download ends .apps.googleusercontent.com.json, and the name is
    # long enough that a truncated paste looks complete.
    *.apps.googleusercontent.com)
      die "no such file: $KEYS
           That name is missing its .json - try $KEYS.json" ;;
    *) die "no such file: $KEYS" ;;
  esac
fi
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
  # The printed `fly secrets set` line wraps VAR=VALUE in single quotes, which
  # bash passes through literally with no escaping - so the raw file content
  # IS what belongs after `=`. json.dumps()-ing it here was double-encoding:
  # the secret ended up holding a JSON string literal (its own file content
  # escaped and re-quoted) instead of the file content itself, and every
  # server reading it back failed with "Invalid credentials file format."
  printf '%s=%s' "$1" "$(cat "$2")"
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
  # gmail-mcp's OAuth callback hardcodes localhost:3000 unless a positional
  # http(s):// argv entry overrides it (any arg starting with http(s):// -
  # see parseCallbackArg in the package). 3000 is a common dev-server port,
  # so a laptop with something else already listening there fails auth with
  # no way around it short of this override. A Desktop-app OAuth client
  # accepts any localhost port for its loopback redirect, so this needs no
  # change on Google's side - 3501 just avoids calendar's own 3500 above.
  GMAIL_OAUTH_PATH="$KEYS" \
  GMAIL_CREDENTIALS_PATH="$WORK/gmail-credentials.json" \
    npx -y "@klodr/gmail-mcp@${GM_VERSION}" auth "--scopes=${GMAIL_SCOPES}" \
      "http://localhost:3501/oauth2callback"
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

Then check /healthz. Under `mcp_catalog`, both servers should report
`"state": "ready"`, and within fifteen minutes `"refresh": "valid"` - which is
Google itself confirming the grant, rather than us confirming the variable is set.

  "state": "missing"   that variable did not arrive
  "state": "expired"   Google refused the grant; consent again, or check that the
                       publishing status is still Testing and not "In production"
  "refresh": "unknown" the check could not be completed - not an expiry

`days_left` counts the seven-day Testing clock down from the grant, and the chat
page warns when it is under two. Put a reminder in your own calendar as well: this
app has no scheduler, so nothing chases you.

These values are credentials to a live mailbox and calendar. Do not paste them
into a file in this repo, a bead, or the knowledge base - `fly secrets` is the
only place they belong.
EOF
