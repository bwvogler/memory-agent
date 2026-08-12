# The TigerFS control surface

Read this when you need to answer "what changed recently?", "what did this file
say before?", or when you are about to undo something.

Some of these paths are deliberately hidden from `ls`. They are still there;
address them directly rather than concluding they are absent because a
directory listing does not show them.

## `.log/`

The operation log: who changed what, when, with diffs. Read it when asked what
changed recently, and always before undoing anything — undoing without first
reading the log is how you revert someone else's work by accident.

## `.history/<path>/`

Past snapshots of one specific file. Use it to answer "what did this say
before?" precisely instead of guessing from context.

This is also the honest way to check whether a fact was corrected or simply
overwritten by mistake.

## `.savepoint/`

Named bookmarks. The application creates one per turn automatically, named
`turn-<id>`, so ordinary work needs no savepoint of its own.

Do create one before a large multi-file restructure — the automatic per-turn
savepoint only gets you back to the start of the turn, which is too coarse when
a single turn makes many independent changes.

## `.undo/`

Rolls back to a log entry or savepoint atomically. Undo is itself reversible,
so it is safe to use — but:

- Say what you are about to undo, and why, before doing it.
- Never undo another user's changes unless explicitly asked.
- Read `.log/` first to confirm the target is what you think it is.

## What this does not cover

The bead graph is not on TigerFS — it lives on the local volume — so none of
the above applies to it. Reverting the knowledge base does **not** roll back
beads, and `memory/backlog.md` will be stale until the next turn regenerates
it. If a revert undid work that a bead was closed for, reopen the bead
explicitly with `bd update <id> --status=open`.
