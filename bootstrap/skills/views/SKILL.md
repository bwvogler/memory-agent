---
name: views
description: >
  Give a knowledge-base directory a rendering — survey what frontmatter its
  pages already carry, agree the fields with the human, backfill the pages
  that lack them, and write that directory's VIEW.md and GUIDE.md so the index
  keeps working. Use when asked to change how a folder looks, to show a
  directory as a table or a grouped list, to sort or group pages by a field,
  or when an index reads wrongly. Triggers include "show these as a table",
  "group these by course", "sort this folder by date", "make this folder look
  better", "add a column for", and "why does the index say Unfiled".
---

# Giving a directory a rendering

A directory can hold a `VIEW.md` whose frontmatter decides how the wiki renders
that folder as an index, and what appears above each page inside it. Editing
that file is the small part of this job.

The large part is that **a view can only show fields the pages actually have**.
A spec naming `cuisine` over a directory of forty prose notes produces forty
rows saying "Unfiled" — which reads as a fact about the recipes and is really a
fact about the wiki. So this is a pass over the directory, not an edit to one
file, and it usually spans more than one turn.

Three things this skill does not restate, each read at the step that needs it:

- **The spec's keys** — every key a `VIEW.md` may contain, and what it does.
  These live in `references/directory-views.md` belonging to the `kb-curator`
  skill, which ships inside the application image rather than in the knowledge
  base, so look for it beside that skill and not beside this one. It is the
  specification; this file is the procedure. Read it before writing or changing
  any `VIEW.md`.
- `references/choosing-a-layout.md`, beside this file — worked examples, step 3.
- `references/backfilling.md`, beside this file — the long half, step 4.

## The pass, and how much of it fits in a turn

```
- [ ] 1  Survey what the directory already has
- [ ] 2  Agree the fields with the human
- [ ] 3  Choose the layout            -> references/choosing-a-layout.md
- [ ] 4  Backfill the pages           -> references/backfilling.md
- [ ] 5  Write GUIDE.md, then VIEW.md
- [ ] 6  Check it
- [ ] 7  File what is left
```

Track this with `TodoWrite` **inside** a turn. Never as the record *between*
turns: todos are in-turn progress and are not persisted anywhere, so work that
survives the turn has to be a bead or it is simply lost.

A first pass over a small directory reaches step 7 in one turn. Over a large
one it will not, and that is the normal case rather than a failure — step 4
says how to stop cleanly and how a later turn picks the work up.

## Survey before you propose

Read the directory's `GUIDE.md` and its `VIEW.md` if either exists, then read
the pages and count what frontmatter keys are actually present and on how many.
Report that count before proposing anything: a key on three pages out of forty
is a backfill, not a column, and the human should hear the number before
agreeing to it.

**The survey is also how a later turn resumes.** It is written to be re-run
because the pages themselves are the progress record — a page either carries
the field or it does not. Re-deriving from them is idempotent, cannot go stale,
and cannot disagree with what is there. Do not keep a separate list of what is
done; there is nothing to keep it honest.

## Agree the fields with the human

Ask, with `mcp__ask__ask_user`. What a directory is *about* — which handful of
its facts are worth being a column — is the human's call, and the cost of
guessing is not one file: it is a backfill in the wrong direction across every
page, and then a second one to undo it.

Propose a concrete spec, name the page count it implies, and offer the smaller
version too. Two fields over forty pages usually beats five.

## Choose a layout

`list` is the default and suits pages with long titles or where an excerpt
helps. `table` wants a short value in every column for most pages. If the
directory does not obviously match one of those — or the human wants grouping —
read `references/choosing-a-layout.md`.

## Backfill

Pages gain the agreed fields one at a time. **Read the page and write it back
whole.** The store replaces a file's frontmatter wholesale on every write, so
there is no such thing as adding a key to a page: there is only rewriting the
page with the key present, or destroying what was there.

Everything else about this step — what to do with a value you do not know, how
big a batch should be, and what a bead has to say for a later turn to continue
— is in `references/backfilling.md`. Read it before you start writing pages.

## Write the GUIDE.md before the VIEW.md

This order matters more than it looks. A view whose directory `GUIDE.md` does
not say which fields a new page carries starts decaying with the very next page
written there, and nothing detects it. So: update `GUIDE.md` to name the fields
and what each means, then write `VIEW.md`, whole, like any other file.

Only put a field in `GUIDE.md` because the view reads it. Frontmatter when code
will read it, prose when only people will — a directory of free prose should
not grow frontmatter because it could.

## Check it

You cannot see the rendered page, so check what you can:

- Read the stored `VIEW.md` back. The store re-serialises what you wrote, so
  what is on disk is not byte-for-byte what you sent, and a spec composed from
  memory produces a diff you did not intend.
- Every name in `fields`, `sort`, `group_by` and `page.header` should appear in
  at least one page — a name that appears in none is almost always a typo, and
  it renders as an empty column rather than as an error.
- Anything in `page.header` missing from most pages will show the same label on
  every page. Either backfill it or take it out of the header.

Then ask the human to look. A bad key renders as a warning box above the
entries, which is information only they can see.

## File what you did not finish

One bead per unfinished thing, described so a cold reader can act on it:

```bash
bd create --title="Backfill cuisine and time on the remaining wiki/recipes pages" \
  --description="wiki/recipes/VIEW.md now groups by cuisine and shows time. 12 of 40 pages carry both; the rest show the empty labels. Backfill every page under wiki/recipes/ missing either key, reading each page and writing it back whole." \
  --type=task --priority=2
```

The other cases worth a bead: a field the human wants but could not decide on,
and a directory that wants something the vocabulary does not have — a layout,
or a way of grouping, that no key in the spec expresses. That last one is about
the application rather than the wiki and cannot be fixed from here, so it is
filed differently. The `kb-curator` skill's *"When the fix belongs in the app,
not the wiki"* section has the exact commands; follow them rather than
improvising the flags, and say what the directory needed and what you tried.

## After a pass

Append one line to `log.md`:

```
## [YYYY-MM-DD] views | wiki/recipes | 12 pages backfilled (1 bead)
```
