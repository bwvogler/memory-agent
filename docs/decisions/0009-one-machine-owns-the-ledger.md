# 0009 — One machine owns the ledger, and the ceiling is shared state

**Status:** accepted

## Context

The knowledge base is Postgres-backed through TigerFS, so it is genuinely
shared: any number of processes can read and write it. That makes it easy to
assume the application is stateless and can be scaled horizontally like an
ordinary web service. It cannot, and the reason is not capacity.

Three things live on the Fly volume mounted at `/work`, and a Fly volume
attaches to exactly one machine:

- `$WORK_DIR/{user_slug}/.beads` — the work ledger (ADR 0006). Deliberately not
  on the mount: Dolt is a binary database and running one over FUSE→SQL invites
  corruption.
- `$WORK_DIR/kb.git` — the savepoint history the revert button depends on
  (ADR 0003), in `app/kb.py`.
- Per-user scratch: the agent's cwd, and the SDK's local session files.

A second machine therefore gets its own ledger, its own savepoint history and
its own scratch, while sharing one knowledge base with the first.

**Each split fails silently, and one of them is dangerous.** `bd ready`
diverges, so "a past session's findings reach this one" — the entire argument
for ADR 0006 — holds for only about half of turns. `memory/backlog.md` is
regenerated from the graph into the *shared* KB after every turn, so two
ledgers fight over one projection and each overwrite hides the other's work;
a human reading `/kb` sees a backlog that flips with no indication why.

The dangerous one is savepoints. A turn served by machine A writes its
savepoint into A's `kb.git`. A revert routed to B finds no such savepoint —
while the content it refers to is shared, present, and very much still there.
Revert is the mechanism that makes writing to the wiki reviewable (0003) and
bounded self-modification defensible at all (0008, "one click from gone").
Splitting it removes that guarantee while leaving the button in place.

## Decision

Run exactly one active machine. `fly.toml` keeps `min_machines_running = 0`
with `auto_start_machines`, which is one machine that suspends, not zero
machines. Do not `fly scale count`.

This is currently enforced by accident rather than intent: there is one volume,
and Fly will not attach it to a second machine, so scaling out requires
deliberately creating a second volume. That is a real guarantee, but it was
nowhere in the documentation, which is what this ADR fixes.

## Consequences

**The real ceiling is below the hardware ceiling, and it is shared state.**
`create_savepoint` is `git add -A` and a commit over the single shared
workspace at `$KB_MOUNT/memory`. The savepoint *name* is per turn; the content
is global. `POST /api/turns` has no concurrency guard — reflection has one, and
ordinary turns do not. So two turns in flight at once, from two browser tabs on
one machine, already interleave: one turn's half-written files are swept into
the other's savepoint, and reverting either rolls back both. That is a
correctness bug today, at one machine, and it is filed rather than fixed here.

It also settles the scaling question. Throughput cannot be the thing that
drives a second machine, because this is hit first and a second machine makes
it worse rather than better.

**Scaling up is capped too, by a different constraint.** 2GB is the Fly suspend
ceiling, and suspend is what gives sub-second resume (ADR 0002). The usual
"go vertical before horizontal" escape valve is closed by design.

**Geography is an anti-driver.** TigerFS turns every `ls`, `cat` and `grep`
into SQL, which is why `fly.toml` says to colocate with Postgres. A machine in
a second region multiplies every file read by the cross-region round trip
unless Postgres is replicated to match.

**What would genuinely drive a second machine**, in rough order of likelihood:

1. *Availability.* With a volume, Fly updates in place, so every deploy is a
   short outage. If other people come to depend on the wiki during working
   hours, this is the first real pressure — though a maintenance window, or a
   read-only replica of `/kb`, answers it more cheaply than a second writer.
2. *Background work starved by interactive use.* Reflection is skipped whenever
   any turn is running. A nightly bulk ingest, or reflection that never gets a
   gap, would argue for a dedicated worker — but it would have to own the
   ledger and the savepoints, so it is an asymmetric worker with its own volume
   and a routing rule, not a symmetric replica. The early warning is already a
   log line: "a turn is running; skipping reflection".
3. *Real multi-tenancy.* Beads and scratch are already per-user, so per-tenant
   machines with per-tenant volumes is the natural shape — this is the one case
   where the split stops being a bug and becomes the design. The KB is a single
   shared wiki, so that would have to be split as well.

**If the constraint has to go, the order is fixed by the above.** Fix savepoint
isolation first — scope savepoints per user, or serialize turns per workspace —
because until then the ceiling is one turn at a time regardless of hardware.
Then route each user's turns to the machine holding their ledger and savepoints
(`fly-replay` is the cheap version, and keeps volumes per-machine). Sharing the
bead graph itself is the last step, not the first: `bd` ships `federation` and
`sync` for exactly this, and `_BEADS_OVERRIDES` currently forbids the Dolt
remote sync it would need. Doing that step alone would fix the ledger and leave
`kb.git` split, which would make revert *look* safer than it is.

## Note on verification

The three-way split is read off the code rather than observed: `GIT_DIR` in
`app/kb.py`, the beads path in `app/kb.py`, and the `[mounts]` block in
`fly.toml`. Nobody has run two machines against one knowledge base, and nobody
should, so the failure modes above are reasoned rather than measured — which is
exactly why the decision is to not find out.
