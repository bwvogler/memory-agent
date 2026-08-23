# 0010 — Ideas about the image originate on prod, and the image closes its own beads

**Status:** accepted

## Context

ADR 0006 gave the agent a ledger for work on the wiki, and it works. What it
did not anticipate is the other thing the agent files.

The agent is the only user of this product who sees it from the inside. It
knows that opening a skill directory in `/kb` shows a file list where a rendered
`SKILL.md` would be better, that chat history does not survive a reload, that
there is nowhere to configure an MCP. Those are good ideas, and it filed them —
five of the six beads in the deployed ledger were about the image, not the wiki.

**It cannot act on a single one of them.** It has no repo, `_BEADS_OVERRIDES`
forbids it git, and the image it is running is read-only. So they sat in
`bd ready` and were offered as claimable work on every turn, forever. That is
precisely the failure the signal-bead amendment to 0006 was written to prevent —
"filling the frontier with things nobody intends to do" — arriving through a
different door.

Meanwhile the repo had no ledger at all. `CLAUDE.md`, ADR 0008 and
`app/guards.py` cited five bead ids that existed nowhere: they had lived in a
local Docker volume, and `docker compose down -v` destroyed it. Shipped
documentation pointed at work nobody could read.

So the two halves failed symmetrically: prod held work it could not do, and the
repo did the work with nothing to track it in.

## Decision

**Two ledgers, joined by bead id.**

The deployed ledger keeps wiki work, which the agent can actually do, plus an
inbox of ideas about the image, labelled `image` and created `deferred`. The
`deferred` status is doing real work: it keeps them out of `bd ready` by
construction, exactly as it does for signal beads.

The repo gets a ledger of its own, `bd init --prefix img`, with
`.beads/issues.jsonl` committed. It is the home for work on the image, and it
is in git — which is what a ledger holding beads that documentation cites needs
to be, since the last one was destroyed by a routine volume teardown.

**Only `image` beads travel.** Nothing about the *content* of the knowledge base
is ever committed to this repo. Wiki beads stay on the volume, where the agent
that filed them can also work them, and reach Postgres through the existing
`memory/backlog.md` projection.

**Prod → repo is `scripts/beads-pull.sh`**: export, filter to `label:image`,
import. Ids are preserved, so `kb-1m7` on prod is `kb-1m7` here and no mapping
table exists. Status is translated on first import only — `deferred` means "do
not claim this" on prod and "this is the work" here — and dropped on re-import,
because prod goes on saying `deferred` until the fix deploys and honouring it
would reopen work already closed in the repo.

**Repo → prod is the image itself.** `docs/shipped-beads.jsonl` lists what an
image resolves, one hand-appended line per bead. At startup,
`kb.reconcile_shipped_all()` closes those beads in every per-user ledger on the
volume, with a note carrying the summary, the commit and `FLY_IMAGE_REF`.

Startup is the hook because a deploy *is* the event: it is the only moment at
which "what this image resolves" changes. It needs no ssh, no credentials and no
human step at deploy time, and it reaches every user's ledger on whichever
machine holds it — which matters, because ADR 0009's whole subject is that the
ledger is machine-local while the knowledge base is not.

## Consequences

**The manifest is hand-maintained and can lie.** Nothing verifies that a line
claiming to close `kb-1m7` corresponds to code that shipped. This is why the
bead note carries the commit sha rather than just a summary: the claim is
checkable by whoever reads the closed bead, which is the same bet ADR 0008 makes
with `memory/evolution.md` — the failure mode to design against is not a wrong
entry but an unnoticed one.

**Applied entries are recorded per user, and status is deliberately not
consulted.** `$WORK_DIR/{user_slug}/.shipped-beads.json` mirrors
`.bootstrap-state.json`. Reopening a bead is how a human says "that did not
actually ship"; a reconciler keyed on current status would close it again on the
next boot and overrule exactly the person it exists to inform.

A bead the ledger never had is recorded as applied anyway. Every user's ledger
sees the same manifest, so most ids are absent from most ledgers — that is the
common case, not a fault, and retrying it would log the same failure on every
boot for the life of the deployment.

**The reconciler fails open**, like every other `bd` call in `app/kb.py`. A
ledger that cannot be reached must not take down the boot it was only reporting
to. Which means it fails the way everything here fails — silently — so the
container tier asserts both that the manifest is actually in the image and that
a real bead gets closed exactly once in a real ledger.

**The two ledgers can disagree, and one direction is unprotected.** Closing a
bead in the repo does nothing on prod until a deploy carries a manifest line for
it. Between those two moments prod still lists the work as outstanding. That is
the honest state — it has not shipped — but it means `memory/backlog.md` is not
a statement about what is done, only about what has reached users.

**`kb-` in a git diff does not mean "about the knowledge base".** It is the
deployed ledger's prefix (`BEADS_PREFIX`), so it means "originated on prod".
Work discovered in the repo is `img-`. This is a genuine readability cost,
accepted because the shared id is what lets the manifest close a bead by name.

**bd init is not a quiet command.** Run in this repo it wrote `AGENTS.md`, a
`.claude/settings.json` hook, `.cursor/` and `.codex/` config, appended 56 lines
to `CLAUDE.md` — restating both rules `_BEADS_OVERRIDES` exists to override —
and committed all of it to git unprompted. Only `.beads/` and the agent skill
were kept. ADR 0006 already says to re-run `bd init` in a clean directory and
read what appears before bumping the pin; this is the second half of that
warning, for a repo rather than a scratch dir.

## Amendment — a refused close is not a missing bead (`kb-068`)

The reconciler treated every `bd close` failure identically: log it, record the
id as applied, never try again. The reasoning was sound for the case it was
written for — most ids are absent from most ledgers, and no later boot can fix
an id that does not exist — and wrong for the case nobody had hit yet.

`bd close` also refuses a bead whose blocker is still open, and manifest lines
are appended in the order work merged, which is not dependency order. `kb-068`
depends on `kb-b82` and its line came first, so on the boot that carried both:
the close of `kb-068` was refused, filed as applied, and never attempted again,
while `kb-b82` closed a moment later and made it closable. The image shipped,
prod kept listing resolved work as outstanding, and the evidence was one
`log.info` on a machine that suspends when idle. It is exactly the rot this ADR
exists to prevent, arriving through the mechanism built to prevent it.

Two changes, both narrow. `reconcile_shipped` makes passes until a pass closes
nothing new, so a blocker further down the manifest unblocks the line above it
within the same run. And absence is now distinguished from refusal by bd's own
wording — `no issue found` is recorded as applied, everything else is left
pending and warned about on every boot until it resolves.

The second half is the part worth defending, because it accepts a repeating
warning that the original design was written to avoid. A bead whose blocker
never ships now complains forever. That is correct: unlike an id this ledger
never had, shipped work that is still open is a state a person has to resolve,
and the alternative is the silence that produced this amendment. `--force` was
rejected for the same reason — it would close the bead and destroy the signal.

`kb-068` itself is not recoverable by deploying this, since its id is already in
the applied list; it needs one `scripts/fly.sh --write bd close` by hand. Both
tests pinning this fail against the single-pass implementation, which is the
only reason to believe they test anything: every other test in the file passes
against it.

## Note on verification

The five reconstructed beads (`kb-3sv`, `kb-56n`, `kb-5uu`, `kb-wk2`, `kb-3cl`)
are rebuilt from what the documentation records, not recovered — the originals
are gone. `kb-3cl` is a marker only: `CLAUDE.md` cites it as *the work that was
lost*, and its content is genuinely unrecoverable, so the bead says so rather
than inventing a description that would read as recovered.
