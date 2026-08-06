# 0003 — Wrap every turn in a TigerFS savepoint

**Status:** accepted

## Context

We want the agent to *write* to the knowledge base, not just read it — adding
what it learns, correcting what is wrong, reorganising as the corpus grows. That
is the whole point; a read-only knowledge base could have been a vector store.

But an agent with write access to your institutional memory is a scary thing.
The usual mitigations are bad: approve every write (defeats the automation),
or write to a staging area and merge by hand (defeats it more slowly).

## Decision

Before each turn, create a TigerFS savepoint named `turn-<id>`. Expose a
per-turn "revert" action in the UI and via `POST /api/turns/{id}/revert`.

## Consequences

A bad turn is one atomic rollback away, at any granularity the user cares about,
including hours later. TigerFS undo is itself reversible, so the revert button is
safe to hand to a non-expert — the worst case of clicking it by mistake is
clicking it again.

This reframes the whole risk conversation. "Let the agent write to the knowledge
base" stops being a leap of faith and becomes a reviewable, reversible
operation with an audit trail. In a shared knowledge base the operation log
carries per-user attribution, so you can also answer "who told the agent to
change this?" — provided you thread the authenticated identity through to the
mount rather than letting every write show up as "the agent".

Cost: none meaningful. A savepoint is a bookmark, not a copy.

Failure handling: if the savepoint cannot be created, log a warning and run the
turn anyway. A turn that cannot be checkpointed should still answer the user; the
alternative — refusing to work because bookkeeping failed — is worse.

## Note on verification

The dot-directory control surface is documented. The exact *write gesture* for
creating a savepoint is inferred in this implementation and confined to
`app/kb.py`. Run `scripts/spike-fuse.sh` to confirm it against a live mount
before relying on the revert button in anger.
