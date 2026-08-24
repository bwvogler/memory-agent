#!/usr/bin/env bash
# Mount the TigerFS knowledge base for local development.
#
# Usage:
#   bash scripts/mount-kb.sh --dev              # the docker-compose Postgres
#   bash scripts/mount-kb.sh --prod             # whatever .env points at, READ-ONLY
#   bash scripts/mount-kb.sh --prod --writable  # ...and mean it
#   bash scripts/mount-kb.sh --kill             # unmount and stop the processes
#
# Reads credentials from .env in the repo root (copy from .env.example and
# fill in KB_DATABASE_URL at minimum). That file is gitignored.
#
# .env usually points at the SAME database the deployed machine writes to, so
# this mount is production: an editor save under the mountpoint is a production
# write, with no review and no deploy in between. Nothing about a local path
# suggests that, so mounting it now requires saying so. `--dev` mounts the
# throwaway Postgres from docker-compose instead, at a separate mountpoint.
#
# `--prod` is therefore read-only unless you add `--writable`. Saying "yes" to
# the confirmation prompt establishes that you meant *this database*; it does
# not establish that you meant to write to it, and almost every reason to mount
# production from a laptop is a reason to read. The deployed machine is what
# writes. `--writable` is the separate sentence for the rare time you mean it.
#
# Unmount when done:
#   bash scripts/mount-kb.sh --kill
#
# The credential is passed to tigerfs as a command-line argument, because that
# is the only form it accepts for a `postgres://` connection - no env var, no
# stdin. It is therefore visible in `ps` to anything running as you. Give the
# local mount its own scoped database role if that matters.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

log() { printf '%s mount-kb: %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

# True only if the mount answers a real readdir. A single path lookup is not
# enough: tigerfs resolves individual names before the filesystem can be
# enumerated, so probing one file reports "live" on a mount that still lists
# empty. Requiring .build in the listing also covers a stale mountpoint whose
# tigerfs process has died, where readdir succeeds against the bare directory.
kb_readable() {
  local entries
  entries="$(ls -a "$1" 2>/dev/null)" || return 1
  grep -qx '\.build' <<<"$entries"
}

# The dev database from docker-compose.yml, which publishes 5432:5432. It
# serves no TLS, hence the insecure flag - the same one entrypoint.sh gates
# behind KB_INSECURE_NO_SSL.
#
# Overridable because port 5432 is popular: any Postgres already installed on
# this machine will have bound it, and see check_dev_port below for why that
# one wins.
DEV_DATABASE_URL="${KB_DEV_DATABASE_URL:-postgres://postgres:devpassword@localhost:5432/kb}"

# A native Postgres binds 127.0.0.1:5432 specifically, while Docker's published
# port binds the wildcard. The specific bind wins for loopback connections, so
# `localhost:5432` silently reaches the wrong database and TigerFS reports
# `role "postgres" does not exist` - which reads like a broken container rather
# than a port collision. Say what it actually is.
check_dev_port() {
  command -v lsof >/dev/null || return 0
  local owner
  owner="$(lsof -nP -iTCP@127.0.0.1:5432 -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1}')"
  [[ -z "$owner" || "$owner" == com.docke* ]] && return 0
  die "another Postgres ('$owner') owns 127.0.0.1:5432, so a dev mount would
  connect to it instead of the container. It binds loopback specifically and
  Docker binds the wildcard, and the specific bind wins.

  Either stop it, or publish the container on a free port and point this at it:
    KB_DEV_DATABASE_URL=postgres://postgres:devpassword@127.0.0.1:15432/kb \\
      bash scripts/mount-kb.sh --dev"
}

MODE=""
WRITABLE=0
for arg in "$@"; do
  case "$arg" in
    --dev)      MODE="dev" ;;
    --prod)     MODE="prod" ;;
    --writable) WRITABLE=1 ;;
  esac
done

if [[ "${1:-}" == "--kill" ]]; then
  [[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }
  # `tigerfs unmount` rather than umount/fusermount3, because the process is
  # the thing to get rid of and the bare unmount does not touch it. This exact
  # gap left two tigerfs processes running for 12 and 21 hours after a --kill
  # that reported success: one of them was still serving NFS on 127.0.0.1, and
  # the noise in the kernel log was blamed on a `fly deploy` for a while.
  #
  # Both mountpoints, since --dev has its own and forgetting it leaves a live
  # mount behind that the next run reports as "already mounted".
  for target in "${KB_MOUNT:-$REPO_ROOT/mnt/kb}" "$REPO_ROOT/mnt/kb-dev"; do
    if mount | grep -q " on $target "; then
      tigerfs unmount "$target" \
        || fusermount3 -u "$target" 2>/dev/null \
        || umount "$target"
      log "unmounted $target"
    else
      log "$target is not mounted"
    fi
  done

  # A mountpoint can be gone while its process is not: an earlier plain umount,
  # a crashed mount, or a tigerfs that lost its mountpoint all leave one behind
  # holding a database connection. Match on this repo's mnt/ path so that any
  # unrelated tigerfs on this machine is left alone.
  #
  # pgrep rather than parsing `tigerfs list`, which is a human-readable table:
  # this needs PIDs, and pgrep already has them. `tigerfs stop` is still the
  # mechanism - it is a graceful SIGTERM that unmounts on the way out.
  for pid in $(pgrep -f "tigerfs mount .*$REPO_ROOT/mnt/" || true); do
    tigerfs stop "$pid" >/dev/null 2>&1 || kill "$pid" 2>/dev/null || true
    log "stopped tigerfs process $pid"
  done

  sleep 1
  remaining=$(pgrep -f "tigerfs mount .*$REPO_ROOT/mnt/" || true)
  if [[ -n "$remaining" ]]; then
    log "WARNING: tigerfs is STILL running for this repo (PIDs: $(tr '\n' ' ' <<<"$remaining"))"
    log "  each one holds a database connection. Stop it with: tigerfs stop <pid>"
    exit 1
  fi
  log "no tigerfs processes remain for this repo"
  exit 0
fi

# Load .env
if [[ ! -f "$ENV_FILE" ]]; then
  die ".env not found at $ENV_FILE — copy .env.example and fill in KB_DATABASE_URL"
fi
# shellcheck source=/dev/null
set -a; source "$ENV_FILE"; set +a

KB_MOUNT="${KB_MOUNT:-$REPO_ROOT/mnt/kb}"
WORK_DIR="${WORK_DIR:-$REPO_ROOT/work}"
MOUNT_FLAGS=()

if [[ "$MODE" == "dev" ]]; then
  # Its own mountpoint and its own work dir, so a dev mount can never be
  # mistaken for the production one or write savepoints into its kb.git.
  [[ -n "${KB_DEV_DATABASE_URL:-}" ]] || check_dev_port
  KB_DATABASE_URL="$DEV_DATABASE_URL"
  KB_MOUNT="$REPO_ROOT/mnt/kb-dev"
  WORK_DIR="$REPO_ROOT/work-dev"
  MOUNT_FLAGS+=(--insecure-no-ssl)
fi

[[ -n "${KB_DATABASE_URL:-}" ]] || die "KB_DATABASE_URL is not set in $ENV_FILE"

# Production is read-only unless asked otherwise. Consenting to the database is
# not the same sentence as consenting to write to it, and nearly every reason
# to mount production from a laptop is a reason to read: checking what the
# agent wrote, diffing, reading a guide. The deployed machine is what writes.
#
# Not applied to --dev. That mountpoint is a throwaway Postgres and is also
# what the local stack writes through, so a read-only dev mount would break the
# thing it exists to support.
#
# CAVEAT, verified on macOS: --read-only protects the DATABASE, not your shell.
# The NFS client accepts a write into its cache and reports success, so
# `echo x > mnt/kb/memory/f.md` appears to work and `ls` shows the file - and
# nothing reaches Postgres. Confirmed by writing two probe files through a
# --read-only mount and finding no rows and no changed modified_at afterwards.
# So a "successful" edit under a read-only mount is a silent no-op. That is the
# safe direction to fail, but do not read `ls` as proof that a write landed.
READ_ONLY=0
if [[ "$MODE" != "dev" && "$WRITABLE" -eq 0 ]]; then
  READ_ONLY=1
  MOUNT_FLAGS+=(--read-only)
fi

# Say which database, always. The mountpoint is a local path either way, so
# there is otherwise nothing on screen to tell the two apart.
DB_HOST="$(printf '%s' "$KB_DATABASE_URL" | sed -E 's|^[^@]*@||; s|[:/?].*$||')"
if [[ "$MODE" == "dev" ]]; then
  # `--dev` picked the database, so it is dev by construction. Classifying by
  # hostname here would be wrong the moment the container is reached by
  # anything but `localhost` - which is exactly what KB_DEV_DATABASE_URL is
  # for, and what a port collision forces. The host is still printed.
  DB_KIND="dev"
else
  case "$DB_HOST" in
    localhost|127.0.0.1|db|"") DB_KIND="dev" ;;
    *)                         DB_KIND="PRODUCTION" ;;
  esac
fi
ACCESS=$([[ "$READ_ONLY" -eq 1 ]] && echo "read-only" || echo "WRITABLE")
log "database: $DB_HOST  ($DB_KIND, $ACCESS)"

# Gates an actual mount, and is therefore called below the already-mounted
# check rather than here: re-running this script is how you ask "is it up?",
# and that answer should not require consenting to anything.
confirm_production() {
  [[ "$DB_KIND" == "PRODUCTION" && "$MODE" != "prod" ]] || return 0
  cat >&2 <<EOF

  $DB_HOST is not a local database. If it is the one the deployed machine
  uses, then everything under $KB_MOUNT is production.

  Mount it deliberately:   bash scripts/mount-kb.sh --prod   (read-only)
  Or use the throwaway:    bash scripts/mount-kb.sh --dev    (needs docker compose up)

EOF
  # Non-interactive callers have no prompt to answer - they get a refusal.
  [[ -t 0 ]] || die "refusing to mount $DB_KIND without --prod"
  read -r -p "  Mount production at $KB_MOUNT? [y/N] " reply
  [[ "$reply" == "y" || "$reply" == "Y" ]] || die "not mounting"
}

# `--prod` alone is read-only and needs no further consent. `--prod --writable`
# is the one combination that can destroy production from a laptop, and it is
# the one that previously got NO prompt at all, because naming --prod satisfied
# the check above. It asks separately, and refuses outright without a terminal.
confirm_writable_production() {
  [[ "$DB_KIND" == "PRODUCTION" && "$WRITABLE" -eq 1 ]] || return 0
  cat >&2 <<EOF

  WRITABLE mount of PRODUCTION ($DB_HOST).

  Everything under $KB_MOUNT is the live wiki: an editor save, a stray agent
  write or a careless rm lands there with no review and no deploy. There are
  savepoints, but they live on the Fly volume and cover what the deployed
  machine did - not what you are about to do from here.

  Drop --writable to mount it read-only.

EOF
  [[ -t 0 ]] || die "refusing a writable production mount without a terminal"
  read -r -p "  Type the database host to confirm: " reply
  [[ "$reply" == "$DB_HOST" ]] || die "not mounting"
}
command -v tigerfs &>/dev/null || die "tigerfs not found on PATH — see https://tigerfs.io for install instructions"

if [[ "$WORK_DIR" == "$KB_MOUNT" || "$WORK_DIR" == "$KB_MOUNT/"* ]]; then
  die "WORK_DIR ($WORK_DIR) is inside KB_MOUNT ($KB_MOUNT); agent scratch files would pollute the KB"
fi

if [[ "$(uname)" == "Linux" && ! -e /dev/fuse ]]; then
  die "/dev/fuse is missing — this host cannot run TigerFS (need CAP_SYS_ADMIN + fuse device)"
fi

if mount | grep -q " on $KB_MOUNT "; then
  if kb_readable "$KB_MOUNT"; then
    log "$KB_MOUNT is already mounted and readable — nothing to do"
    exit 0
  fi
  die "$KB_MOUNT is in the mount table but cannot be read — the mount is stale.
  Run 'bash scripts/mount-kb.sh --kill' and then mount again."
fi

confirm_production
confirm_writable_production

if [[ ! -d "$KB_MOUNT" ]]; then
  log "creating mountpoint at $KB_MOUNT"
  mkdir -p "$KB_MOUNT"
fi
mkdir -p "$WORK_DIR"

log "mounting TigerFS at $KB_MOUNT"
# Expanded the long way round because macOS ships bash 3.2, where `"${arr[@]}"`
# on an EMPTY array is an unbound-variable error under `set -u` - so the plain
# form would abort every production mount and work only for --dev.
tigerfs mount ${MOUNT_FLAGS[@]+"${MOUNT_FLAGS[@]}"} "$KB_DATABASE_URL" "$KB_MOUNT" &
MOUNT_PID=$!

for i in $(seq 1 30); do
  if kb_readable "$KB_MOUNT"; then
    log "mount is live after ${i}s"
    break
  fi
  if ! kill -0 "$MOUNT_PID" 2>/dev/null; then
    die "tigerfs exited while mounting — check KB_DATABASE_URL and TigerFS install"
  fi
  sleep 1
done

kb_readable "$KB_MOUNT" || die "mount never became readable after 30s"

# Initialise the memory workspace if this is a fresh database.
if [[ ! -d "$KB_MOUNT/memory" ]]; then
  log "creating 'memory' workspace"
  echo 'markdown' > "$KB_MOUNT/.build/memory"
  for i in $(seq 1 10); do
    [[ -d "$KB_MOUNT/memory" ]] && break
    sleep 1
  done
  [[ -d "$KB_MOUNT/memory" ]] || log "WARNING: memory workspace did not appear — check TigerFS logs"
fi

if [[ -d "$KB_MOUNT/memory" && ! -f "$KB_MOUNT/memory/CLAUDE.md" ]]; then
  log "seeding memory/CLAUDE.md"
  printf '# Memory\n\nThis is the agent knowledge base. Add notes here.\n' \
    > "$KB_MOUNT/memory/CLAUDE.md" \
    || log "WARNING: could not seed CLAUDE.md"
fi

# Initialise git savepoint repo (mirrors entrypoint.sh exactly).
GIT_DIR_PATH="$WORK_DIR/kb.git"
if [[ -d "$GIT_DIR_PATH" && ! -f "$GIT_DIR_PATH/HEAD" ]]; then
  log "removing invalid git dir, will re-init"
  rm -rf "$GIT_DIR_PATH"
fi
if [[ ! -d "$GIT_DIR_PATH" ]]; then
  log "initialising git repo for savepoints at $GIT_DIR_PATH"
  git init --bare "$GIT_DIR_PATH"
  git --git-dir="$GIT_DIR_PATH" config core.bare false
  git --git-dir="$GIT_DIR_PATH" config user.email "agent@memory-agent"
  git --git-dir="$GIT_DIR_PATH" config user.name "Memory Agent"
  git --git-dir="$GIT_DIR_PATH" --work-tree="$KB_MOUNT/memory" add -A
  git --git-dir="$GIT_DIR_PATH" --work-tree="$KB_MOUNT/memory" commit -m "init" --allow-empty
  log "git repo ready"
fi

log "KB is live"
echo ""
echo "  Database:  $DB_HOST  ($DB_KIND)"
if [[ "$READ_ONLY" -eq 1 ]]; then
  echo "  Access:    read-only (writes are silently dropped, not refused)"
else
  echo "  Access:    WRITABLE — edits here are live"
fi
echo "  Mount:     $KB_MOUNT"
echo "  Workspace: $KB_MOUNT/memory"
echo "  Savepoints: $GIT_DIR_PATH"
echo ""
echo "  Unmount:   bash scripts/mount-kb.sh --kill"
echo ""
echo "  The mount process (PID $MOUNT_PID) is running in the background, and"
echo "  holds a database connection until stopped. --kill stops the process;"
echo "  a plain umount leaves it running."
