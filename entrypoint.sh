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
  if [[ ! -d "$KB_MOUNT/memory" ]]; then
    log "creating 'memory' workspace"
    echo 'markdown,history' > "$KB_MOUNT/.build/memory"
  fi
  # Seed a minimal CLAUDE.md so the agent has a starting point.
  if [[ ! -f "$KB_MOUNT/memory/CLAUDE.md" ]]; then
    log "seeding memory/CLAUDE.md"
    printf '# Memory\n\nThis is the agent knowledge base. Add notes here.\n' \
      > "$KB_MOUNT/memory/CLAUDE.md"
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
