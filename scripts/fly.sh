#!/usr/bin/env bash
# Look at the deployed machine and its volume from a laptop.
#
# The knowledge base needs none of this - it lives in Postgres and
# scripts/mount-kb.sh mounts it locally. This is for the other tier: the bead
# ledger, kb.git, per-user scratch and the SDK transcripts, all of which live on
# a Fly volume attached to exactly one machine (docs/decisions/0009).
#
# Usage:
#   scripts/fly.sh doctor                  # slug, disk, bd versions
#   scripts/fly.sh bd ready --json         # the deployed ledger
#   scripts/fly.sh run ls -la /work
#   scripts/fly.sh shell                   # interactive
#
# Read-only by default. Anything that mutates the ledger needs --write, because
# that graph sits on an unreplicated volume with no savepoint covering it.
set -euo pipefail

APP="${FLY_APP:-memory-agent-proud-island-3747}"
SLUG_OVERRIDE="${FLY_USER_SLUG:-}"
WRITE=0

log() { printf 'fly.sh: %s\n' "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

# --- the two traps -----------------------------------------------------------

# fly.toml runs min_machines_running = 0 and an SSH session is not proxy
# traffic, so a suspended machine answers `fly ssh` with a forcibly-closed
# session rather than anything that says "stopped". One HTTP request wakes it
# and auto_start_machines does the rest. /healthz needs no credentials.
#
# Called once by the dispatcher rather than lazily per request. A lazy version
# tried to cache "already awake" in a variable and could not: every caller
# reaches this through a command substitution, which is a subshell, so the flag
# never made it back to the parent and the wake ran on every call anyway.
wake() {
    log "waking $APP..."
    curl -fsS -m 90 -o /dev/null "https://${APP}.fly.dev/healthz" \
        || die "could not reach $APP; is it deployed?"
}

# `flyctl ssh console -C` runs no shell, strips quote characters, and splits on
# whitespace to build argv. So a command with a quoted argument arrives mangled
# and there is no escaping that survives. The way through is to send something
# with no quotes and no spaces in it at all: a Python program encoded as a list
# of byte values. Digits, commas, brackets and dots only.
#
# python3 is guaranteed - the image is python:3.12-slim.
remote_py() {
    local program="$1" encoded
    encoded="$(PROGRAM="$program" python3 -c '
import os
print(",".join(str(b) for b in os.environ["PROGRAM"].encode()))
')"

    # Captured rather than piped, so the remote exit status survives. Piping
    # into a filter hands the pipeline's status to the filter instead, and
    # `grep -v` returns 1 when it emits nothing - so a remote command that
    # failed would report success and one that printed nothing would report
    # failure. Both were observed before this was written.
    local out status
    set +e
    out="$(flyctl ssh console --app "$APP" \
        -C "python3 -c exec(bytes([${encoded}]).decode())")"
    status=$?
    set -e

    # Defensive: this announcement is on stderr in the flyctl we tested, but a
    # version that put it on stdout would corrupt anything piped into jq.
    printf '%s\n' "$out" | grep -v '^Connecting to ' || true
    return $status
}

# Run an ordinary command, with its argv preserved exactly.
remote_run() {
    local argv
    argv="$(python3 -c '
import sys
print(repr(sys.argv[1:]))
' "$@")"
    remote_py "import subprocess,sys;sys.exit(subprocess.run(${argv}).returncode)"
}

# --- which directory is ours -------------------------------------------------

# Discovered rather than assumed. The slug is derived from the authenticated
# email (Identity.slug in app/auth.py), so it differs between a dev-bypass
# deployment and a real one, and guessing it wrong fails quietly: bd against a
# missing path exits cleanly with an empty ledger, which reads as "nothing
# there" rather than "wrong place".
slug() {
    [[ -n "$SLUG_OVERRIDE" ]] && { printf '%s\n' "$SLUG_OVERRIDE"; return 0; }

    local found
    found="$(remote_py '
import os
base = "/work"
print("\n".join(sorted(
    d for d in os.listdir(base)
    if os.path.isdir(os.path.join(base, d, ".beads"))
)))
')"
    found="$(printf '%s' "$found" | tr -d '\r' | sed '/^$/d')"

    [[ -z "$found" ]] && die "no ledger found under /work on $APP. \
Run: scripts/fly.sh run ls -la /work"
    if [[ "$(printf '%s\n' "$found" | wc -l)" -gt 1 ]]; then
        die "several users have a ledger on $APP:
$found
Pick one with --user <slug> or FLY_USER_SLUG."
    fi
    printf '%s\n' "$found"
}

# --- read-only by default ----------------------------------------------------

# Everything that changes the graph. The deployed ledger is an unreplicated
# Dolt database and no savepoint covers it, so a mutation from a laptop is not
# undoable the way a knowledge-base write is.
#
# The list is the guard, so a verb nobody added is not a weaker guard - it is no
# guard, and silently. `sql` is the one that proves it: it reads like a query,
# takes arbitrary SQL, and `fly.sh bd sql 'DELETE FROM issues'` went straight
# through with no flag and no confirmation.
#
# `dolt`, `admin` and `migrate` are here for the same reason, and being coarse
# about them is deliberate: `bd dolt status` and `bd migrate --dry-run` only
# read, and now need --write anyway. Matching on the verb rather than parsing
# each subcommand's flags is what keeps this list auditable, and a guard that
# over-refuses costs a retyped flag while one that under-refuses costs the
# ledger.
#
# `run` is deliberately NOT guarded. It is the documented escape hatch for
# arbitrary commands, and `run rm -rf /work` announces itself in a way that
# `bd sql` does not. This guard is against accidents, not against someone who
# means it.
MUTATING="create close update note priority delete dep init import reopen assign
label tag comment edit link merge-slot promote supersede set-state remember
forget migrate-personal sync backup restore sql admin migrate dolt"

check_read_only() {
    local verb="$1" m
    for m in $MUTATING; do
        if [[ "$verb" == "$m" ]]; then
            [[ $WRITE == 1 ]] && return 0
            die "\`bd $verb\` changes the deployed ledger, which is read-only from here.
Pass --write if you mean it:
    scripts/fly.sh --write bd $verb ...
That graph is on an unreplicated volume and no savepoint covers it."
        fi
    done
}

# --- subcommands -------------------------------------------------------------

cmd_bd() {
    [[ $# -gt 0 ]] || die "usage: scripts/fly.sh bd <args...>"
    check_read_only "$1"   # before wake: a refusal should cost nothing
    wake
    local dir argv
    dir="/work/$(slug)"
    argv="$(python3 -c 'import sys;print(repr(["bd","-C",sys.argv[1]]+sys.argv[2:]))' "$dir" "$@")"
    # Matches _bd_env() in app/kb.py: bd prompts on a TTY and would hang a
    # headless invocation forever.
    remote_py "import subprocess,sys,os
env = dict(os.environ, BD_NON_INTERACTIVE='1', CI='true')
sys.exit(subprocess.run(${argv}, env=env).returncode)"
}

cmd_doctor() {
    local s
    s="$(slug)"
    printf 'app        %s\n' "$APP"
    printf 'user slug  %s\n' "$s"
    printf 'bd local   %s\n' "$(bd version 2>/dev/null | head -1 || echo 'not installed')"
    printf 'bd pinned  %s (Dockerfile)\n' \
        "$(grep -o 'BEADS_VERSION=[0-9.]*' "$(dirname "$0")/../Dockerfile" | cut -d= -f2)"
    echo
    remote_py '
import os, shutil, subprocess
print("bd remote  " + subprocess.run(["bd","version"],capture_output=True,text=True).stdout.strip())
u = shutil.disk_usage("/work")
print("volume     %.2f GB used of %.2f GB" % (u.used/1e9, u.total/1e9))
print()
print("/work:")
for d in sorted(os.listdir("/work")):
    p = os.path.join("/work", d)
    kind = "ledger" if os.path.isdir(os.path.join(p, ".beads")) else "dir"
    print("  %-34s %s" % (d, kind))
'
    echo
    echo "bd versions must match: bd refuses to open a database written by a"
    echo "newer schema, and a newer local bd would silently upgrade .beads/ here."
}

usage() {
    cat >&2 <<'EOF'
usage: scripts/fly.sh [--write] [--user SLUG] <command>

  doctor            slug, volume usage, bd versions local vs deployed
  slug              print the discovered user slug
  bd <args...>      run bd against the deployed ledger (read-only)
  run <cmd...>      run any command on the machine
  shell             interactive session

The knowledge base is not here - it is in Postgres. See scripts/mount-kb.sh.
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --write) WRITE=1; shift ;;
        --user) SLUG_OVERRIDE="${2:-}"; shift 2 ;;
        --app) APP="${2:-}"; shift 2 ;;
        -h|--help) usage ;;
        *) break ;;
    esac
done

[[ $# -gt 0 ]] || usage
command -v flyctl >/dev/null || die "flyctl not on PATH"

# Each case validates first and wakes second, so a refused command costs
# nothing and a typo does not start a suspended machine.
subcommand="$1"; shift
case "$subcommand" in
    doctor) wake; cmd_doctor ;;
    slug)   wake; slug ;;
    bd)     cmd_bd "$@" ;;
    run)    [[ $# -gt 0 ]] || die "usage: scripts/fly.sh run <cmd...>"
            wake; remote_run "$@" ;;
    shell)  wake; exec flyctl ssh console --app "$APP" --pty ;;
    *)      usage ;;
esac
