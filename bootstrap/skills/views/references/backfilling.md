# Backfilling the fields a view reads

Read this at step 4 of the `views` pass, before writing any page — and again at
the start of a turn that is continuing a backfill somebody else started.

## Contents

- Rewrite whole, always
- Leave a field out rather than guessing it
- Size the batch to the turn, deliberately
- Stopping: what the bead has to say
- Resuming: re-survey, do not trust the count

## Rewrite whole, always

The store replaces a file's frontmatter wholesale on every write. A key you do
not repeat is a key you deleted. So a page does not *gain* a field — it is read
in full and written back with the field present.

Two consequences worth stating separately:

- **Never compose the rewrite from memory.** Read the page immediately before
  writing it. The store re-serialises markdown, so the frontmatter on disk is
  not in the order you last wrote it, and a rewrite built from an older read
  silently drops whatever has changed since.
- **Never append, and never reach for the shell to do it.** The mount has no
  read-modify-write, so a shell append zeroes what came before. This has
  already destroyed a user's file once, which is why a hook now refuses it. See
  the `kb-curator` skill's `references/tigerfs.md` for the control surface and
  for how to recover a page you have damaged.

## Leave a field out rather than guessing it

If a page does not say what its `cuisine` is, leave the key off. Do not infer
it from the ingredients, and do not write a placeholder.

The reason is that the two are not equally recoverable. A missing value renders
as whatever `empty_labels` says it means — `Unfiled`, `not timed yet` — which
is honest and visibly fixable. A guessed value is indistinguishable from a
known one from that moment on, by the human and by every later turn, and
nothing will ever flag it.

Where a value is genuinely worth having and you cannot derive it, that is one
bead for the set, not a guess per page.

## Size the batch to the turn, deliberately

A savepoint covers the whole workspace for the whole turn, so a revert is
all-or-nothing: forty pages backfilled in one turn is a forty-page undo, and
the human loses the good thirty-eight to get rid of the bad two.

Prefer several turns of a dozen or so pages. Tell the human that is what you
are doing, so a pass that stops early does not read as a failure. Between turns
the work is held by a bead, and nothing else — see below.

## Stopping: what the bead has to say

The bead has to let a cold reader continue without this conversation. Three
things do that: the directory, the agreed field list, and **which pages remain,
stated as a rule rather than as a list.**

The rule matters. A list of filenames is stale the moment anyone adds a page,
and it quietly excludes the new one; a rule re-derives against whatever is
there now.

```bash
bd create --title="Backfill cuisine and time across wiki/recipes" \
  --description="wiki/recipes/VIEW.md groups by cuisine and shows time; both were agreed with Brian on 2026-08-26. Backfill every page under wiki/recipes/ that is missing either key, reading each page and writing it back whole. Leave a key off where the page does not say - do not infer it. 12 of 40 done." \
  --type=task --priority=2
```

Include the count as context, never as the definition of what is left.

## Resuming: re-survey, do not trust the count

Re-run step 1 of the pass against the directory. Take the field list from the
bead and the *state* from the pages, then carry on where the survey says the
gap is.

Do not work from the bead's count, and do not assume the fields are still the
right ones — read the directory's `VIEW.md` too, because it may have been
changed since. If the spec and the bead disagree about which fields matter, the
spec is what the reader is looking at, so ask rather than picking one.

When the last page is done, close the bead with a reason and say in the reply
how many turns it took.
