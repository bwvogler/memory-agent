---
name: prod-ops
description: >
  Reach the deployed Fly machine or its volume safely: scripts/fly.sh's
  --write guard and verb allowlist, scripts/mount-kb.sh's read-only-vs-
  writable production mount, scripts/beads-pull.sh's upsert semantics, and
  the Google OAuth 7-day re-auth clock. Use when asked to touch production,
  mount the prod knowledge base, pull beads from prod, or debug a Google
  integration that went quiet.
disable-model-invocation: true
---

# Touch Production Safely

Nothing here is undoable through a savepoint — the volume that holds the bead
ledger, `kb.git`, and per-user scratch is unreplicated (ADR 0009). Every rule
below exists because a version of it already went wrong once.

## Non-Negotiable Rules

1. **`scripts/fly.sh` is read-only by default.** Mutating `bd` verbs need
   `--write`, and past the flag there is no confirmation and no undo.
2. **The `--write` guard is a verb allowlist, not a semantic check** — `run`
   and `shell` bypass it entirely, deliberately (they're the documented
   escape hatch, and announce themselves the way `bd sql` does not). Don't
   assume an unlisted verb is safe just because the guard didn't refuse it;
   the guard is only as good as its list.
3. **`scripts/mount-kb.sh --prod` (without `--writable`) is read-only — and on
   macOS, a "successful" write under it is a silent no-op.** The NFS client
   caches the write and reports success; nothing reaches Postgres. `ls`/`cat`
   right after such a write proves nothing.
4. **`--prod --writable` is the one combination that can destroy production
   from a laptop.** It prompts for the database host as confirmation — read
   that prompt, don't reflexively type it.
5. **Never `fly scale count`.** The volume, bead ledger, and savepoints are
   single-machine by design (ADR 0009) — a second machine gets its own copies
   of all three while sharing one knowledge base with the first. `fly deploy`
   updates in place, so every deploy is a short outage; that's expected, not
   a bug to chase.

## Task Recipes

### Pull image-labelled beads from prod

```sh
scripts/beads-pull.sh [user_slug]
```

Safe to re-run — an upsert. It ends with `bd export`, so **check `git status`
/ `git diff .beads/issues.jsonl`, not the "Imported N issues" line** (bd
prints that count even for rows re-applied unchanged; a clean diff means
nothing new arrived regardless of what the number says). Read the
changed-field list it prints. Never reach for `--allow-stale` — a locally
edited bead is by definition newer than its prod twin, and a pull cannot
revert deliberate local work; that's measured, not assumed.

### Look at prod without pulling

```sh
scripts/fly.sh bd list --all      # any read verb — no --write needed
```

Touches no local state. Use this to spot a bead that *should* carry the
`image` label and doesn't — that's the only filter `beads-pull.sh` applies,
so an idea filed without it stays stranded on the volume forever.

### Rescue a stranded bead

```sh
scripts/fly.sh --write bd label <id> image
```
then pull.

### Undo an accidental `--write bd close`

`bd reopen` restores a bead to `open`, not to whatever status it had before —
if it needs to go back to `deferred` (image beads only), say so explicitly in
a second command:

```sh
scripts/fly.sh --write bd reopen <id>
scripts/fly.sh --write bd update <id> --status deferred   # image beads only
```

Miss the second command and the bead starts showing up in the prod agent's
`bd ready`, which `deferred` exists to prevent.

### Mount the KB from a laptop

```sh
bash scripts/mount-kb.sh --dev              # local docker-compose Postgres
bash scripts/mount-kb.sh --prod             # read-only, whatever .env points at
bash scripts/mount-kb.sh --prod --writable  # the dangerous one — see Rule 4
bash scripts/mount-kb.sh --kill             # unmount AND stop leftover processes
```

Use `--kill`, not a plain `umount` — a bare unmount can leave the process
still holding a database connection (observed running 12 and 21 hours).

### Cross-prefix `bd dep` edges don't block

`bd dep add kb-x img-y` is accepted and `bd dep tree` renders it as
`[BLOCKED]`, but `bd ready` offers the "blocked" bead anyway (upstream bug,
gastownhall/beads#4647 — not fixed by any available pin). State the ordering
in the bead's description too; the edge visible in `dep tree` enforces
nothing on its own.

### Re-authenticate a Google integration

The clock: a consent screen left in "Testing" mode expires refresh tokens
every 7 days, and nothing schedules the renewal. Run
`scripts/google-auth.sh <path-to-client.json>` on a laptop (needs a browser),
apply the printed `fly secrets set` line, then check `/healthz`'s
`mcp_catalog` block for `"state": "ready"` and a valid `refresh` — `missing`
means the config isn't there at all, which is a different problem from an
expired token.

## Error Handling

- A mutating `bd` verb refuses without `--write`: that's the guard working —
  confirm you actually mean it before adding the flag, don't reflexively
  retry with it.
- A `--prod` write "succeeds" but the next mount doesn't show it: check
  whether `--writable` was actually passed (Rule 3).
- `fly.sh` seems to hang: it wakes a suspended machine itself before running
  anything — give the wake a few seconds before assuming it's stuck.

## Notes

- Full reasoning: CLAUDE.md's "The work ledger" section, and
  `docs/decisions/0009` for why exactly one machine.
- The companion skill for the other half of this ritual — closing a bead
  *after* code has shipped — is `/ship`.
