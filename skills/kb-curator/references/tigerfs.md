# The TigerFS control surface

Read this when you need to answer "what changed recently?", "what did this file
say before?", or when you are about to undo something.

## Write files whole, or lose them

The write path never reads before writing. Opening a file gives you a fresh
zero-filled buffer — the existing content is NOT loaded from the database — and
on close that buffer becomes the entire file. Only bytes you actually wrote in
that session are real.

Two consequences, both silent:

- Bytes before your write become NULs. Appending to a 13-byte file yields 13
  zeros followed by what you appended.
- Bytes after your write are truncated. Poking one byte at offset 2 of a
  12-byte file leaves a 3-byte file.

So `>>`, `tee -a`, `open(path, "a")`, and any seek-then-write all destroy data.
Writing the whole file from offset 0 is correct and is the only safe pattern.
Sequential writes within one open handle are fine, because between them the
buffer ends up fully covered.

This is the filesystem, not the file type — `.md`, `.txt` and extensionless
files all behave this way, and the same operations on local disk are fine.

## End every file with a newline, or Write will cry wolf

The store appends a trailing newline when content lacks one. That changes the
byte count, and the Write tool's post-write size check then reports:

```
Write verification failed: <path> is N bytes on disk, expected N-1.
The filesystem may have silently truncated the write.
```

**The write succeeded.** The content is correct and complete. Ending your
content with a newline avoids the message entirely.

This matters far more than it looks. That false error is the first link in the
chain that destroyed a user's memory file: Write "failed", the agent went
looking for another route, and the route it found was a shell append. If a
file tool reports failure here, re-read the file before doing anything else —
and if the content is right, you are done.

This has already destroyed a user's memory file once. An agent hit an error
from a file tool, fell back to a shell append, and turned 233 bytes of personal
notes into 233 zeroes. Recovering it needed the savepoint history.

To add to a file: read it whole, build the whole new content, write it back
whole. If a file tool fails, say so and stop — do not reach for the shell.

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
