#!/usr/bin/env bash
# PHASE 0, STEP 1 - prove TigerFS can mount here at all.
#
# Run this BEFORE writing any application code on a new host. If it fails, no
# amount of application work will save the deployment: pick a different host.
#
#   usage: scripts/spike-fuse.sh <postgres-url> [mountpoint]
set -euo pipefail

DB="${1:-${KB_DATABASE_URL:?usage: spike-fuse.sh <postgres-url> [mountpoint]}}"
MNT="${2:-/tmp/kb-spike}"

echo "== host capability =="
[[ -e /dev/fuse ]] && echo "/dev/fuse: present" || { echo "/dev/fuse: MISSING - this host cannot run TigerFS"; exit 1; }
command -v fusermount3 >/dev/null && echo "fusermount3: present" || echo "fusermount3: missing (may be fine)"
command -v tigerfs >/dev/null || { echo "tigerfs: not installed"; exit 1; }
tigerfs version || true

echo
echo "== mounting at $MNT =="
mkdir -p "$MNT"
tigerfs mount "$DB" "$MNT" &
PID=$!
trap 'fusermount3 -u "$MNT" 2>/dev/null || umount "$MNT" 2>/dev/null || true; kill $PID 2>/dev/null || true' EXIT

for i in $(seq 1 30); do
  [[ -e "$MNT/.log" || -e "$MNT/.savepoint" ]] && break
  sleep 1
done

echo
echo "== control surface =="
# Some control paths are path-accessible but deliberately hidden from ls, so
# test each explicitly rather than trusting a directory listing.
for d in .log .history .savepoint .undo .info; do
  if [[ -e "$MNT/$d" ]]; then echo "  $d: present"; else echo "  $d: not visible"; fi
done

echo
echo "== savepoint / undo interface =="
echo "This is the part the reference implementation could not verify."
echo "Inspect these by hand and confirm the write gesture, then fix app/kb.py:"
ls -la "$MNT/.savepoint" 2>/dev/null || echo "  (cannot list .savepoint)"
cat "$MNT/.info/help" 2>/dev/null || true

echo
echo "Mounted. Explore in another shell, then press Ctrl-C here to unmount."
wait $PID
