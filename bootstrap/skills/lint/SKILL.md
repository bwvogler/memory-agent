---
name: lint
description: >
  Health-check the wiki for quality issues. Use when asked to review, audit,
  or clean up the knowledge base.
---

# Linting the wiki

Lint is a periodic health check. Run it when the wiki feels stale, after a
burst of ingest activity, or whenever the human asks for a review.

## What to check

**Contradictions.** Find pages that make conflicting claims about the same
fact. Report them; do not silently pick a winner. Ask the human which is
correct, then update both pages so the current version is consistent.

**Orphan pages.** Find pages with no inbound links from other wiki pages.
Orphans are hard to discover and often forgotten. Either link them from a
relevant page, add them to `index.md`, or ask if they should be deleted.

**Stale claims.** Look for time-sensitive assertions that have likely expired
(predictions, "current" figures, in-progress statuses). Flag them for review;
do not update them without a source.

**Missing concept pages.** Find concepts mentioned repeatedly across multiple
pages that do not have their own dedicated page. Create stubs for the most
important ones, or note them for the human to prioritize.

**Missing cross-references.** Find pages that reference an entity or concept
that has its own page, but do not link to it. Add the links.

**`index.md` drift.** Check that every page in the wiki is listed in
`index.md` and that every entry in `index.md` points to a real file.

## Output format

Report findings as a short list grouped by category. For each finding, say
what the problem is and what you propose to do. Wait for the human to confirm
before making any changes — lint findings are observations, not auto-fixes.

## After a lint pass

Append to `log.md`: `## [YYYY-MM-DD] lint | <N> findings`. One sentence
summarizing what was found.
