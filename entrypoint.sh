#!/usr/bin/env bash
# Mount the knowledge base BEFORE serving traffic.
#
# This ordering is not cosmetic. A missing CLAUDE.md is silently skipped by the
# agent SDK and an absent knowledge base produces no error anywhere - just an
# agent that mysteriously knows nothing. Failing loudly here turns a two-hour
# debugging session into a five-second one.
set -euo pipefail

KB_MOUNT="${KB_MOUNT:-/mnt/kb}"
WORK_DIR="${WORK_DIR:-/work}"
PORT="${PORT:-8080}"

log() { printf '%s entrypoint: %s\n' "$(date -Is)" "$*" >&2; }

if [[ -z "${KB_DATABASE_URL:-}" ]]; then
  log "FATAL: KB_DATABASE_URL is not set"
  exit 1
fi

if [[ "$(realpath -m "$WORK_DIR")" == "$(realpath -m "$KB_MOUNT")"* ]]; then
  log "FATAL: WORK_DIR is inside KB_MOUNT; agent scratch files would be"
  log "       written into the knowledge base as versioned rows."
  exit 1
fi

mkdir -p "$KB_MOUNT" "$WORK_DIR"

if [[ ! -e /dev/fuse ]]; then
  log "FATAL: /dev/fuse is missing. This host cannot run TigerFS."
  log "       Grant the device and CAP_SYS_ADMIN, or pick a different host."
  log "       See docs/decisions/0002-choosing-a-host.md"
  exit 1
fi

log "mounting TigerFS at $KB_MOUNT"
tigerfs mount "$KB_DATABASE_URL" "$KB_MOUNT" &
MOUNT_PID=$!

# Wait for the mount-level .info directory, which TigerFS synthesises at the
# root of every live mount. (.log/.savepoint live inside workspaces, not here.)
for i in $(seq 1 30); do
  if [[ -e "$KB_MOUNT/.info" ]]; then
    log "mount is live after ${i}s"
    break
  fi
  if ! kill -0 "$MOUNT_PID" 2>/dev/null; then
    log "FATAL: tigerfs exited while mounting"
    exit 1
  fi
  sleep 1
done

if [[ ! -e "$KB_MOUNT/.info" ]]; then
  log "WARNING: mount never became live. Serving anyway, but the agent"
  log "         will have no knowledge base. /healthz will report 503."
else
  # Initialise the memory workspace on first boot if it does not exist yet.
  # Use plain markdown (no history) so it works without TimescaleDB.
  if [[ ! -d "$KB_MOUNT/memory" ]]; then
    log "creating 'memory' workspace"
    echo 'markdown' > "$KB_MOUNT/.build/memory"
    # Wait for the FUSE layer to materialise the directory.
    for i in $(seq 1 10); do
      [[ -d "$KB_MOUNT/memory" ]] && break
      sleep 1
    done
  fi
  # Seed a minimal CLAUDE.md so the agent has a starting point.
  if [[ -d "$KB_MOUNT/memory" && ! -f "$KB_MOUNT/memory/CLAUDE.md" ]]; then
    log "seeding memory/CLAUDE.md"
    printf '# Memory\n\nThis is the agent knowledge base. Add notes here.\n' \
      > "$KB_MOUNT/memory/CLAUDE.md" \
      || log "WARNING: could not seed CLAUDE.md — agent will start without memory"
  fi

  # Initialise the git repo that backs savepoints/undo. The repo lives on the
  # local Fly volume (fast, no TimescaleDB needed); the work tree is the TigerFS
  # workspace. Using --git-dir keeps no .git entry inside the KB mount itself.
  GIT_DIR_PATH="${WORK_DIR}/kb.git"
  # Remove a previously mis-initialised non-bare repo (no HEAD file at root).
  if [[ -d "$GIT_DIR_PATH" && ! -f "$GIT_DIR_PATH/HEAD" ]]; then
    log "removing invalid git dir, will re-init"
    rm -rf "$GIT_DIR_PATH"
  fi
  if [[ ! -d "$GIT_DIR_PATH" ]]; then
    log "initialising git repo for savepoints"
    git init --bare "$GIT_DIR_PATH"
    # bare repos default core.bare=true which blocks work-tree operations.
    git --git-dir="$GIT_DIR_PATH" config core.bare false
    git --git-dir="$GIT_DIR_PATH" config user.email "agent@memory-agent"
    git --git-dir="$GIT_DIR_PATH" config user.name "Memory Agent"
    git --git-dir="$GIT_DIR_PATH" --work-tree="$KB_MOUNT/memory" add -A
    git --git-dir="$GIT_DIR_PATH" --work-tree="$KB_MOUNT/memory" \
      commit -m "init" --allow-empty
    log "git repo ready"
  fi
fi

cleanup() {
  log "shutting down"
  fusermount3 -u "$KB_MOUNT" 2>/dev/null || umount "$KB_MOUNT" 2>/dev/null || true
  kill "$MOUNT_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Optional: run the tunnel in-process so nothing about this container is
# publicly routable. Set TUNNEL_TOKEN in your secrets to enable.
if [[ -n "${TUNNEL_TOKEN:-}" ]]; then
  log "starting cloudflared tunnel"
  cloudflared tunnel --no-autoupdate run --token "$TUNNEL_TOKEN" &
fi

log "starting API on :$PORT"
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --timeout-keep-alive 75
