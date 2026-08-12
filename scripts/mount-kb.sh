#!/usr/bin/env bash
# Mount the TigerFS knowledge base for local development.
#
# Usage:
#   bash scripts/mount-kb.sh
#
# Reads credentials from .env in the repo root (copy from .env.example and
# fill in KB_DATABASE_URL at minimum). That file is gitignored.
#
# Unmount when done:
#   fusermount3 -u "$KB_MOUNT"   # Linux
#   umount "$KB_MOUNT"           # macOS
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

log() { printf '%s mount-kb: %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

if [[ "${1:-}" == "--kill" ]]; then
  [[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }
  KB_MOUNT="${KB_MOUNT:-$REPO_ROOT/mnt/kb}"
  if mount | grep -q " on $KB_MOUNT "; then
    fusermount3 -u "$KB_MOUNT" 2>/dev/null || umount "$KB_MOUNT"
    log "unmounted $KB_MOUNT"
  else
    log "$KB_MOUNT is not mounted"
  fi
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

[[ -n "${KB_DATABASE_URL:-}" ]] || die "KB_DATABASE_URL is not set in $ENV_FILE"
command -v tigerfs &>/dev/null || die "tigerfs not found on PATH — see https://tigerfs.io for install instructions"

if [[ "$WORK_DIR" == "$KB_MOUNT" || "$WORK_DIR" == "$KB_MOUNT/"* ]]; then
  die "WORK_DIR ($WORK_DIR) is inside KB_MOUNT ($KB_MOUNT); agent scratch files would pollute the KB"
fi

if [[ "$(uname)" == "Linux" && ! -e /dev/fuse ]]; then
  die "/dev/fuse is missing — this host cannot run TigerFS (need CAP_SYS_ADMIN + fuse device)"
fi

if mount | grep -q " on $KB_MOUNT "; then
  log "$KB_MOUNT is already mounted — nothing to do"
  exit 0
fi

if [[ ! -d "$KB_MOUNT" ]]; then
  log "creating mountpoint at $KB_MOUNT"
  mkdir -p "$KB_MOUNT"
fi
mkdir -p "$WORK_DIR"

log "mounting TigerFS at $KB_MOUNT"
tigerfs mount "$KB_DATABASE_URL" "$KB_MOUNT" &
MOUNT_PID=$!

for i in $(seq 1 30); do
  if [[ -e "$KB_MOUNT/.info" ]]; then
    log "mount is live after ${i}s"
    break
  fi
  if ! kill -0 "$MOUNT_PID" 2>/dev/null; then
    die "tigerfs exited while mounting — check KB_DATABASE_URL and TigerFS install"
  fi
  sleep 1
done

[[ -e "$KB_MOUNT/.info" ]] || die "mount never became live after 30s"

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
echo "  Mount:     $KB_MOUNT"
echo "  Workspace: $KB_MOUNT/memory"
echo "  Savepoints: $GIT_DIR_PATH"
echo ""
echo "  Unmount:   fusermount3 -u $KB_MOUNT   (Linux)"
echo "             umount $KB_MOUNT            (macOS)"
echo ""
echo "  The mount process (PID $MOUNT_PID) is running in the background."
echo "  It will keep running until you unmount or kill it."
