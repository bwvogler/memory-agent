---
name: lint
description: >
  Health-check the wiki for contradictions, orphan pages, stale claims, missing
  concept pages, broken cross-references and index drift, and file each finding
  as a bead. Use when asked to review, audit, clean up, tidy, or check the
  health of the knowledge base, when the wiki feels stale, or after a burst of
  ingest activity. Triggers include "review the wiki", "audit the KB", "clean
  this up", "is anything out of date", and "what needs fixing".
---

# Linting the wiki

Lint is a periodic health check. Run it when the wiki feels stale, after a
burst of ingest activity, or whenever the human asks for a review.

A lint pass finds more than one session can fix. That is expected and is why
findings become beads rather than a list in chat: the pass is only worth doing
if what it finds survives the conversation.

> Not to be confused with `bd lint`, which checks that *beads* have adequate
> descriptions. This skill checks the *wiki*.

## Check for dupes first

Load what is already filed, so a weekly lint does not refile the same six
findings every week:

```bash
bd list --status=open --json
```

If a finding is already there, leave it alone. If it is already there and now
worse or more urgent, raise the priority and add a note rather than filing a
second bead:

```bash
bd update <id> --priority=1 --notes="Still present as of <date>; now also on X."
```

## What to check

**Contradictions.** Pages making conflicting claims about the same fact. Do not
silently pick a winner — file it, `--type=bug`, and include both page paths and
both claims in the description.

**Orphan pages.** Pages with no inbound links from other wiki pages. Orphans are
hard to discover and quietly forgotten.

**Stale claims.** Time-sensitive assertions that have likely expired:
predictions, "current" figures, in-progress statuses. File them; do not update
them without a source.

**Missing concept pages.** Concepts mentioned repeatedly across pages with no
page of their own.

**Missing cross-references.** Pages referencing an entity or concept that has
its own page without linking to it.

**`index.md` drift.** Every page listed in `index.md`, and every entry in
`index.md` pointing at a real file.

## Filing findings

One bead per finding, not one per category. The description must stand alone
for a reader who has never seen this wiki:

```bash
bd create --title="wiki/notes/tea.md contradicts wiki/recipes/chai.md on steep time" \
  --description="tea.md says 3 minutes, chai.md says 5. Both cite no source. Need to decide which is right and correct the other in place." \
  --type=bug --priority=2
```

Priorities: `1` for contradictions and broken links a reader would hit today,
`2` for orphans and missing cross-references, `3` for stale claims needing a
source, `4` for nice-to-have concept stubs.

Where one fix must precede another — a concept page must exist before other
pages can link to it — record it:

```bash
bd dep add <blocked-id> <blocker-id>
```

## Fixing

Filing is safe and needs no permission. *Changing the wiki* is a different
matter: for anything beyond an obviously correct fix (a broken link, a missing
index entry), confirm with the human first. Contradictions in particular are
for the human to resolve — you usually cannot tell which page is right.

If you do fix something in the same pass, close its bead with a reason.

## After a lint pass

Report a short summary grouped by category, with the bead IDs, and say how many
were new versus already known. Then append to `log.md`:
`## [YYYY-MM-DD] lint | <N> findings (<M> new)`.
