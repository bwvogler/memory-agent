---
name: ship
description: >
  Close a bead after its code has shipped: verify the id, append the
  docs/shipped-beads.jsonl manifest line for a kb- bead, and re-export the
  ledger before committing. Use when asked to ship, close out, or finish a
  bead, or right after merging a change that resolves one.
argument-hint: "[bead-id]"
arguments: [id]
disable-model-invocation: true
---

# Ship a Bead

Closing a `kb-` bead here does not close it on the volume — that needs a line
in `docs/shipped-beads.jsonl`, applied by `reconcile_shipped` at the next
deploy's startup. This ritual has already failed once: `kb-068` depended on
`kb-b82`, was listed first in the manifest, had its close refused for an open
blocker, got recorded as applied anyway, and was never retried. The only trace
was one `log.info` nobody read. Verify before you write anything.

## Non-Negotiable Rules

1. **Verify `$id` against `bd list --all` (or `bd show $id`) first.** A wrong
   id is not caught downstream — `reconcile_shipped` records whatever id you
   give it as applied and never retries, silently.
2. **Know which kind of bead this is before doing anything else:**
   - `img-` prefix: created here, has exactly one copy. `bd close $id` is the
     entire ritual — **no manifest line, ever.**
   - `kb-` prefix: exists in two ledgers (here and the volume). Closing it
     here only closes the local copy.
3. **If `bd close` refuses with `cannot close blocked issue`**, do not reach
   for `--force` and do not reorder the manifest around it.
   `reconcile_shipped` makes repeated passes at startup, so closing the
   blocker (in dependency order) is what actually fixes this. `no issue found`
   is the one refusal that's permanent and recorded; every other refusal
   retries on the next boot.
4. **Before committing, run `bd export -o .beads/issues.jsonl`.** bd's own
   pre-commit hook explicitly refuses to auto-export when invoked as a git
   hook (`auto-export: skipping — running as git hook`), so a commit made
   right after `bd close` can carry a stale ledger while the hook still
   reports success.

## Steps

1. `bd show $id` — confirm the bead exists, note its prefix and any open
   blockers.
2. **`img-` bead:** `bd close $id`. Done — skip to step 5.
3. **`kb-` bead with an open blocker:** ship the blocker first (recurse into
   this same skill for it), then continue.
4. **`kb-` bead, no blocker:**
   - `bd close $id` (closes the local copy).
   - Append one line to `docs/shipped-beads.jsonl`:
     ```json
     {"id": "kb-xxx", "summary": "<what shipped, present tense>. See docs/decisions/NNNN.", "commit": "<short sha>"}
     ```
     Match the shape of the two existing entries — the summary ends with an
     ADR pointer when the change has one.
5. `bd export -o .beads/issues.jsonl`, then stage `.beads/issues.jsonl` and
   `docs/shipped-beads.jsonl` for the commit.

## Rescuing a bead that already fell into the permanent-failure state

If a `kb-` bead's id is already recorded in the manifest as applied (it hit
the refused-close trap, or someone edited the manifest by hand),
`reconcile_shipped` will never retry it. The rescue is two commands against
prod, and **the second one is the one that matters** — a hand `bd close`
writes no note, so without it there is no audit trail at all:

```sh
scripts/fly.sh --write bd close <id> --reason "shipped by hand"
scripts/fly.sh --write bd note <id> "<manifest summary> (commit <sha>)"
```

## Notes

- `docs/shipped-beads.jsonl` is append-only by convention — never edit an
  existing line.
- The manifest closes only the **prod** copy. It has no effect on this repo's
  local ledger; close that yourself, in the same commit.
- Full reasoning: CLAUDE.md's "The work ledger" section ("Closing a bead after
  shipping" and "Absence is the only failure treated as permanent"), and
  `docs/decisions/0010` for why `img-` and `kb-` beads travel differently at
  all. The `prod-ops` skill covers touching the volume directly for anything
  beyond this.
