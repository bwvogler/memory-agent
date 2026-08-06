---
name: kb-curator
description: >
  How to navigate, extend and correct the TigerFS-backed knowledge base mounted
  at /mnt/kb. Use this whenever answering a question that might already be
  documented, or when the user tells you something worth remembering. Also use
  it before writing anything into the knowledge base, and when asked to review
  or undo a recent change.
---

# Curating the knowledge base

The knowledge base is a TigerFS filesystem: ordinary file tools work on it,
every write is versioned, and nothing is ever really lost. Treat it as a garden
you are tending, not an append-only log.

## Read before you write

Search first, always. The most common failure in an agent-maintained knowledge
base is not a wrong fact, it is five near-duplicate documents that disagree
slightly. Before adding anything:

Look for an existing document that should absorb the new information. Prefer
extending a good document over creating a thin new one. If you find two
documents that overlap, say so in your answer rather than silently adding a
third.

## Where things go

Reference material lives in the knowledge base. Your operating instructions do
not: those ship with the application image, are reviewed in version control,
and you should not try to edit them. If you believe your instructions are
wrong, say so in your answer instead of rewriting them.

Accumulated memory - the things you have learned that should survive this
conversation - goes in `memory/CLAUDE.md`. Keep it short and high-signal. It is
loaded into every future conversation, so a page of stale detail costs more
than it is worth. Prune it when you touch it.

## The control surface

Some of these paths are deliberately hidden from `ls`. They are still there;
address them directly rather than concluding they are absent.

`.log/` is the operation log: who changed what, when, with diffs. Read it when
asked what changed recently, or before undoing anything.

`.history/<path>/` browses past snapshots of a specific file. Use it to answer
"what did this say before?" without guessing.

`.savepoint/` holds named bookmarks. The application creates one per turn
automatically, named `turn-<id>`, so you do not need to make your own for
ordinary work. Do create one before a large multi-file restructure.

`.undo/` rolls back to a log entry or savepoint atomically. Undo is itself
reversible, so it is safe - but say what you are about to undo before you do
it, and never undo another user's changes without being asked.

## Writing well

Write documents a colleague would want to read: a clear title, the claim up
front, and the supporting detail after. Date anything time-sensitive, because a
knowledge base whose facts have no timestamps decays invisibly.

When you correct something, correct it in place rather than appending a
contradiction. The history is preserved automatically, so the current version
should always read as the truth, not as an argument with itself.
